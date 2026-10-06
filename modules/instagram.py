"""Instagram actions backed by the configured instagrapi client."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from config import Settings
from core.interaction import Interaction
from core.service import ServiceModule, ToolError
from core.tool_registry import Risk, tool
from core.util import describe_exception, open_url_in_browser, truncate

log = logging.getLogger("jarvis.instagram")

_MAX_MEDIA = 20
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _username(value: str) -> str:
    result = value.strip().lstrip("@")
    if not result or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", result):
        raise ToolError("Provide a valid Instagram username.")
    return result


class InstagramService(ServiceModule):
    key = "instagram"
    title = "Instagram"

    def __init__(self, settings: Settings, interaction: Interaction) -> None:
        super().__init__(settings, interaction)
        self._client: Any = None
        self._client_lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return self.settings.instagram.configured

    def unconfigured_reason(self) -> str:
        return "set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD in .env"

    async def _start(self) -> str | None:
        return await asyncio.to_thread(self._login)

    def _login(self) -> str:
        from instagrapi import Client
        from instagrapi.exceptions import BadPassword, ChallengeRequired, TwoFactorRequired

        client = Client(override_app_version=True)
        client.delay_range = [1, 2]
        if self.settings.instagram.proxy:
            client.set_proxy(self.settings.instagram.proxy)
        if self.settings.instagram.session_file.is_file():
            try:
                client.load_settings(self.settings.instagram.session_file, override_app_version=True)
            except Exception as exc:
                log.warning("Could not load Instagram session; logging in again: %s", describe_exception(exc))

        def request_challenge_code(username: str, choice: Any = None) -> str:
            channel = f" ({choice})" if choice else ""
            code = self.interaction.ask_text(
                "Instagram verification",
                f"Enter the Instagram security code sent to {username}{channel}",
                timeout=300.0,
            )
            if not code:
                raise ToolError("Instagram verification was cancelled.")
            return code

        client.challenge_code_handler = request_challenge_code
        verification_code = ""
        if self.settings.instagram.totp_seed:
            import pyotp

            verification_code = pyotp.TOTP(self.settings.instagram.totp_seed).now()

        try:
            try:
                logged_in = client.login(
                    self.settings.instagram.username,
                    self.settings.instagram.password,
                    verification_code=verification_code,
                )
            except TwoFactorRequired:
                code = request_challenge_code(
                    self.settings.instagram.username,
                    "two-factor authentication (or a current authenticator code)",
                )
                logged_in = client.login(
                    self.settings.instagram.username,
                    self.settings.instagram.password,
                    verification_code=code,
                )
        except BadPassword:
            raise ToolError("Instagram rejected the password. Check INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD.") from None
        except ChallengeRequired as exc:
            detail = str(exc).strip()
            raise ToolError(
                "Instagram requires a security checkpoint that could not be completed in-app. "
                "Verify the account in the official Instagram app or website, then reconnect."
                + (f" Details: {truncate(detail, 180)}" if detail else "")
            ) from None
        except TwoFactorRequired:
            raise ToolError("Instagram two-factor verification failed. Check the code and try again.") from None
        if not logged_in:
            raise ToolError("Instagram login failed. Check the credentials or verification requirements.")
        session_file = self.settings.instagram.session_file
        session_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = session_file.with_name(f"{session_file.name}.tmp")
        try:
            client.dump_settings(temporary)
            os.replace(temporary, session_file)
        except Exception as exc:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                log.exception("Could not remove temporary Instagram session file")
            raise ToolError(f"Instagram connected, but its session could not be saved: {describe_exception(exc)}") from exc
        with self._client_lock:
            self._client = client
        return f"@{self.settings.instagram.username}"

    async def _stop(self) -> None:
        with self._client_lock:
            self._client = None

    def _api(self):
        self.require_ready()
        if self._client is None:
            raise ToolError("Instagram is not connected. Switch the Instagram capability off and on.")
        return self._client

    def _client_call(self, operation: Callable[[Any], Any]) -> Any:
        from instagrapi.exceptions import BadPassword, ChallengeRequired, LoginRequired, TwoFactorRequired

        client = self._api()
        try:
            with self._client_lock:
                return operation(client)
        except LoginRequired:
            with self._client_lock:
                self._client = None
                self._ready = False
            self.set_status("error", "Instagram session expired; reconnect the Instagram capability.")
            raise ToolError("Instagram session expired. Reconnect the Instagram capability and try again.") from None
        except BadPassword:
            raise ToolError("Instagram rejected the credentials. Check the account login details.") from None
        except TwoFactorRequired:
            self.set_status("error", "Instagram requires two-factor verification; reconnect to verify.")
            raise ToolError("Instagram requires two-factor verification. Reconnect the Instagram capability.") from None
        except ChallengeRequired:
            self.set_status("error", "Instagram security checkpoint required; verify in the official app.")
            raise ToolError(
                "Instagram requires a security checkpoint. Verify in the official Instagram app, then reconnect."
            ) from None
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Instagram request failed: {describe_exception(exc)}") from exc

    @staticmethod
    def _media_id(client: Any, media_id_or_url: str) -> str:
        value = media_id_or_url.strip()
        if value.isdigit():
            return value
        return InstagramService._post_id(client, value)

    @staticmethod
    def _media_kind(media: Any) -> str:
        product = str(_value(media, "product_type", "") or "").lower()
        media_type = _value(media, "media_type")
        if product in {"clips", "reel"}:
            return "reel"
        if media_type == 1:
            return "photo"
        if media_type == 2:
            return "video"
        if media_type == 8:
            return "album"
        return product or "unknown"

    def _first_explore_reel(self, client: Any) -> tuple[str, str]:
        reels = client.explore_reels(amount=1)
        if not reels:
            raise ToolError("Instagram did not return any Reels.")
        reel = reels[0]
        if self._media_kind(reel) != "reel":
            raise ToolError("Instagram's Reels feed returned an item that is not a Reel.")
        media_id = str(_value(reel, "pk", "")).strip()
        if not media_id:
            raise ToolError("Instagram returned a Reel without a media ID.")
        shortcode = str(_value(reel, "code", "")).strip()
        if not shortcode:
            raise ToolError("Instagram returned a Reel without a browser URL.")
        return media_id, shortcode

    @staticmethod
    def _open_reel(shortcode: str, browser: str | None = None) -> str:
        reel_url = f"https://www.instagram.com/reel/{shortcode}/"
        try:
            return open_url_in_browser(reel_url, browser)
        except (OSError, ValueError) as exc:
            raise ToolError(f"Could not open the selected Instagram Reel: {exc}") from exc

    @classmethod
    def _media_summary(cls, media: Any) -> dict[str, Any]:
        user = _value(media, "user")
        caption = _value(media, "caption_text") or _value(_value(media, "caption"), "text", "")
        taken_at = _value(media, "taken_at")
        code = _value(media, "code", "")
        kind = cls._media_kind(media)
        url = f"https://www.instagram.com/p/{code}/" if code else ""
        if code and kind == "reel":
            url = f"https://www.instagram.com/reel/{code}/"
        return {
            "id": str(_value(media, "pk", "")),
            "url": url,
            "username": str(_value(user, "username", "")),
            "caption": truncate(str(caption or ""), 400),
            "taken_at": taken_at.isoformat() if hasattr(taken_at, "isoformat") else str(taken_at or ""),
            "likes": _value(media, "like_count"),
            "comments": _value(media, "comment_count"),
            "media_type": kind,
        }

    @tool(
        group="instagram",
        capability="instagram",
        activity="Finding @{username}'s latest {media_type}...",
    )
    def get_latest_media(self, username: str, media_type: Literal["reel", "photo", "video"] = "reel") -> dict:
        """Find a user's latest Reel, photo, or video and return its URL and details.

        Args:
            username: Instagram username, with or without @.
            media_type: Which media to find: reel, photo, or video.
        """
        username = _username(username)
        kind = media_type.strip().lower()
        if kind not in {"reel", "photo", "video"}:
            raise ToolError("media_type must be reel, photo, or video.")

        def find(client):
            user_id = client.user_id_from_username(username)
            media_items = client.user_medias(user_id, amount=50)
            for item in media_items:
                if self._media_kind(item) == kind:
                    return item
            return None

        media = self._client_call(find)
        if media is None:
            raise ToolError(f"No recent {kind} was found for @{username}.")
        result = self._media_summary(media)
        result["username"] = username
        return result

    @staticmethod
    def _post_id(client: Any, url: str) -> str:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in _INSTAGRAM_HOSTS:
            raise ToolError("Provide a valid instagram.com post or Reel link.")
        try:
            return str(client.media_pk_from_url(url.strip()))
        except Exception as exc:
            raise ToolError(f"Could not read that Instagram post link: {describe_exception(exc)}") from exc

    @staticmethod
    def _message_summary(message: Any) -> dict[str, Any]:
        timestamp = _value(message, "timestamp")
        reactions = _value(message, "reactions")
        reaction_list = _value(reactions, "emojis", []) or []
        return {
            "id": str(_value(message, "id", "")),
            "sender_id": str(_value(message, "user_id", "")),
            "text": truncate(str(_value(message, "text", "") or ""), 1000),
            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp or ""),
            "sent_by_me": bool(_value(message, "is_sent_by_viewer", False)),
            "type": str(_value(message, "item_type", "text") or "text"),
            "reactions": [
                {
                    "emoji": str(_value(reaction, "emoji", "")),
                    "sender_id": str(_value(reaction, "sender_id", "")),
                }
                for reaction in reaction_list[:10]
            ],
        }

    @classmethod
    def _thread_summary(cls, thread: Any) -> dict[str, Any]:
        users = _value(thread, "users", []) or []
        messages = _value(thread, "messages", []) or []
        latest = messages[0] if messages else None
        return {
            "thread_id": str(_value(thread, "id", "") or _value(thread, "pk", "")),
            "title": str(_value(thread, "thread_title", "") or ""),
            "participants": [
                {
                    "username": str(_value(user, "username", "")),
                    "full_name": str(_value(user, "full_name", "")),
                    "user_id": str(_value(user, "pk", "")),
                }
                for user in users[:20]
            ],
            "is_group": bool(_value(thread, "is_group", False)),
            "pending": bool(_value(thread, "pending", False)),
            "latest_message": cls._message_summary(latest) if latest is not None else None,
        }

    @tool(group="instagram", capability="instagram", activity="Reading Instagram profile @{username}...")
    def get_user_profile(self, username: str) -> dict:
        """Get Instagram profile details; accepts a username or a searchable display name.

        Args:
            username: Instagram username, @handle, or person's display name.
        """
        try:
            username = _username(username)
        except ToolError:
            return self.find_instagram_profile(username)

        def fetch(client):
            user_id = client.user_id_from_username(username)
            info = client.user_info(user_id)
            recent = client.user_medias(user_id, amount=5)
            return info, recent

        info, recent = self._client_call(fetch)
        return {
            "username": str(_value(info, "username", username)),
            "full_name": str(_value(info, "full_name", "")),
            "biography": truncate(str(_value(info, "biography", "")), 500),
            "followers": _value(info, "follower_count"),
            "following": _value(info, "following_count"),
            "posts": _value(info, "media_count"),
            "is_private": bool(_value(info, "is_private", False)),
            "recent_posts": [self._media_summary(item) for item in recent],
        }

    @tool(
        group="instagram",
        capability="instagram",
        activity="Finding Instagram profile '{query}'...",
        description=(
            "Find an Instagram profile using a display name or username, such as 'Teacher Azam'. "
            "Use this instead of get_user_profile when the user gives a person's name or an @handle. "
            "Search Instagram automatically; never ask for a syntactically valid username first. "
            "Open the profile when there is one clear exact match. If several plausible matches exist, "
            "return their names and usernames and ask the user which one."
        ),
    )
    def find_instagram_profile(self, query: str, browser: str | None = None) -> dict:
        """Search Instagram by a person's display name or username and open an exact profile match.

        Args:
            query: Person's display name or Instagram username.
            browser: Optional browser name; "auto" or "chrome" uses the system default.
        """
        query = query.strip().lstrip("@").strip()
        if len(query) < 2:
            raise ToolError("Instagram profile search needs at least two characters.")

        def find(client: Any) -> dict[str, Any]:
            users = list(client.search_users(query) or [])[:20]
            normalized_query = " ".join(query.casefold().split())

            def identity(user: Any) -> tuple[str, str]:
                username = str(_value(user, "username", "")).strip()
                full_name = str(_value(user, "full_name", "")).strip()
                return username, full_name

            exact = [
                user for user in users
                if any(
                    " ".join(value.casefold().split()) == normalized_query
                    for value in identity(user)
                    if value
                )
            ]
            if len(exact) != 1:
                candidates = exact if exact else users[:5]
                return {
                    "found": False,
                    "query": query,
                    "matches": [
                        {
                            "username": identity(user)[0],
                            "full_name": identity(user)[1],
                            "is_private": bool(_value(user, "is_private", False)),
                            "is_verified": bool(_value(user, "is_verified", False)),
                        }
                        for user in candidates
                    ],
                }

            user = exact[0]
            username = str(_value(user, "username", "")).strip()
            if not username:
                raise ToolError("Instagram returned a matching profile without a username.")
            user_id = _value(user, "pk")
            if not user_id:
                user_id = client.user_id_from_username(username)
            info = client.user_info(user_id)
            profile_url = f"https://www.instagram.com/{username}/"
            try:
                opened_in = open_url_in_browser(profile_url, browser)
            except (OSError, ValueError) as exc:
                raise ToolError(f"Could not open the Instagram profile in a browser: {exc}") from exc
            return {
                "found": True,
                "username": str(_value(info, "username", username)),
                "full_name": str(_value(info, "full_name", _value(user, "full_name", ""))),
                "biography": truncate(str(_value(info, "biography", "")), 500),
                "followers": _value(info, "follower_count"),
                "following": _value(info, "following_count"),
                "posts": _value(info, "media_count"),
                "is_private": bool(_value(info, "is_private", _value(user, "is_private", False))),
                "is_verified": bool(_value(info, "is_verified", _value(user, "is_verified", False))),
                "url": profile_url,
                "browser": opened_in,
            }

        return self._client_call(find)

    @tool(group="instagram", capability="instagram", activity="Reading Instagram timeline...")
    def instagram_feed(self, limit: int = 10) -> dict:
        """Read recent posts from the logged-in Instagram timeline.

        Args:
            limit: Maximum posts to return, from 1 to 20.
        """
        limit = max(1, min(int(limit), _MAX_MEDIA))
        response = self._client_call(lambda client: client.get_timeline_feed())
        items = _value(response, "items", []) or []
        return {"posts": [self._media_summary(item) for item in items[:limit]]}

    @tool(group="instagram", capability="instagram", activity="Reading recent posts from @{username}...")
    def instagram_user_posts(self, username: str, limit: int = 10) -> dict:
        """Read recent posts from an Instagram profile.

        Args:
            username: Instagram username, with or without @.
            limit: Maximum posts to return, from 1 to 20.
        """
        username = _username(username)
        limit = max(1, min(int(limit), _MAX_MEDIA))
        def fetch(client):
            user_id = client.user_id_from_username(username)
            return client.user_medias(user_id, amount=limit)

        media = self._client_call(fetch)
        return {"username": username, "posts": [self._media_summary(item) for item in media[:limit]]}

    @tool(group="instagram", capability="instagram", activity="Reading Instagram direct-message inbox...")
    def instagram_list_chats(self, limit: int = 10) -> dict:
        """List recent Instagram direct-message chats and their latest messages.

        Args:
            limit: Maximum number of chats to return, from 1 to 20.
        """
        limit = max(1, min(int(limit), _MAX_MEDIA))
        threads = self._client_call(lambda client: client.direct_threads(amount=limit, thread_message_limit=1))
        return {"chats": [self._thread_summary(thread) for thread in threads[:limit]]}

    @tool(group="instagram", capability="instagram", activity="Reading Instagram chat...")
    def instagram_read_chat(self, thread_id: str, limit: int = 20) -> dict:
        """Read recent messages from an Instagram direct-message chat.

        Args:
            thread_id: Chat ID from instagram_list_chats.
            limit: Maximum number of messages to return, from 1 to 50.
        """
        thread_id = thread_id.strip()
        if not thread_id.isdigit():
            raise ToolError("Choose a valid thread_id from the Instagram chat list.")
        limit = max(1, min(int(limit), 50))
        thread = self._client_call(lambda client: client.direct_thread(int(thread_id), amount=limit))
        messages = _value(thread, "messages", []) or []
        return {
            "chat": self._thread_summary(thread),
            "messages": [self._message_summary(message) for message in messages[:limit]],
        }

    @tool(group="instagram", capability="instagram", activity="Reading unread Instagram messages...")
    def get_unread_messages(self, limit: int = 5) -> dict:
        """Fetch unread Instagram direct messages with sender and chat information.

        Args:
            limit: Maximum unread chats to return, from 1 to 20.
        """
        limit = max(1, min(int(limit), _MAX_MEDIA))
        threads = self._client_call(
            lambda client: client.direct_threads(
                amount=limit,
                selected_filter="unread",
                thread_message_limit=10,
            )
        )
        unread = []
        for thread in threads[:limit]:
            users = _value(thread, "users", []) or []
            user_by_id = {str(_value(user, "pk", "")): user for user in users}
            messages = _value(thread, "messages", []) or []
            unread.append({
                **self._thread_summary(thread),
                "messages": [
                    {
                        **self._message_summary(message),
                        "sender": {
                            "username": str(_value(user_by_id.get(str(_value(message, "user_id", ""))), "username", "")),
                            "full_name": str(_value(user_by_id.get(str(_value(message, "user_id", ""))), "full_name", "")),
                            "user_id": str(_value(message, "user_id", "")),
                        },
                    }
                    for message in messages[:10]
                ],
            })
        return {"unread_chats": unread, "count": len(unread)}

    @tool(
        group="instagram",
        capability="instagram",
        risk=Risk.CONFIRM,
        summary="Send Instagram DM to @{username}: {message}",
        activity="Sending Instagram direct message to @{username}...",
    )
    def send_direct_message(self, username: str, text: str) -> dict:
        """Send an Instagram direct message after user confirmation.

        Args:
            username: Exact Instagram username of the recipient, with or without @.
            text: Exact message text to send.
        """
        username = _username(username)
        text = text.strip()
        if not text:
            raise ToolError("Provide the exact message to send.")
        if len(text) > 1000:
            raise ToolError("Instagram direct messages must be 1,000 characters or fewer.")
        def send(client):
            user_id = int(client.user_id_from_username(username))
            return client.direct_send(text, user_ids=[user_id])

        sent = self._client_call(send)
        return {
            "sent": True,
            "recipient": username,
            "message": self._message_summary(sent),
        }

    @tool(
        group="instagram",
        capability="instagram",
        risk=Risk.CONFIRM,
        summary="React {emoji} to Instagram DM {message_id} in chat {thread_id}",
        activity="Reacting to Instagram direct message...",
    )
    def instagram_react_to_message(self, thread_id: str, message_id: str, emoji: str = "❤️") -> dict:
        """Send an emoji reaction to an Instagram direct message after confirmation.

        Args:
            thread_id: Chat ID from instagram_list_chats.
            message_id: Message ID from instagram_read_chat.
            emoji: Emoji reaction, such as ❤️, 😂, or 👍.
        """
        thread_id = thread_id.strip()
        message_id = message_id.strip()
        emoji = emoji.strip()
        if not thread_id.isdigit() or not message_id.isdigit():
            raise ToolError("Choose a valid thread_id and message_id from the Instagram chat.")
        if not emoji or len(emoji) > 8:
            raise ToolError("Provide one emoji to react with.")
        reacted = self._client_call(
            lambda client: client.direct_send_reaction(int(thread_id), int(message_id), emoji=emoji)
        )
        if not reacted:
            raise ToolError("Instagram did not confirm the message reaction.")
        return {"reacted": True, "emoji": emoji, "thread_id": thread_id, "message_id": message_id}

    @tool(group="instagram", capability="instagram", risk=Risk.CONFIRM,
          summary="Like Instagram media {media_id_or_url}", activity="Liking Instagram media...")
    def like_media(self, media_id_or_url: str) -> dict:
        """Like an Instagram post or Reel by media ID or URL after confirmation.

        Args:
            media_id_or_url: Instagram media ID or post/Reel URL.
        """
        def like(client):
            media_id = self._media_id(client, media_id_or_url)
            return media_id, client.media_like(media_id)

        media_id, liked = self._client_call(like)
        if not liked:
            raise ToolError("Instagram did not confirm the like; the media may already be liked.")
        return {"liked": True, "media_id": media_id}

    @tool(
        group="instagram",
        capability="instagram",
        risk=Risk.CONFIRM,
        summary="Like the first Reel in the Instagram Reels feed",
        activity="Finding the first Instagram Reel...",
        description=(
            "Only use this tool when the user explicitly asks to like a Reel, including a request to open Instagram, "
            "go to Reels, and like the first video. It fetches the first recommended Reel, opens that exact Reel in "
            "the requested browser, and likes the same media. If the user asks to both like and comment on the first "
            "Reel, use like_and_comment_on_first_reel instead so both actions target the same media. Do NOT use this "
            "tool for requests only to open, show, or play Reels; those requests must not cause a like."
        ),
    )
    def like_first_reel(self, browser: str | None = None) -> dict:
        """Open the first recommended Reel in the browser and like that Reel."""
        media_id, shortcode = self._client_call(
            self._first_explore_reel
        )
        opened_in = self._open_reel(shortcode, browser)
        liked = self._client_call(lambda client: client.media_like(media_id))
        if not liked:
            raise ToolError("Instagram did not confirm the like; the Reel may already be liked.")
        return {"liked": True, "media_id": media_id, "media_type": "reel", "browser": opened_in}

    @tool(
        group="instagram",
        capability="instagram",
        risk=Risk.CONFIRM,
        summary="Comment on the first Instagram Reel: {text}",
        activity="Commenting on the first Instagram Reel...",
        description=(
            "Use only when the user explicitly asks to comment on the first Reel in the Instagram Reels feed. "
            "Find the first Reel automatically; never ask for a username, URL, or media ID. Ask only for the exact "
            "comment text if it is missing, and never invent or rewrite the user's comment. Open the selected Reel "
            "in the requested browser, then post the exact text after user confirmation. Do not use for playback-only "
            "or like-only requests. If the user asks to both like and comment on the first Reel, use "
            "like_and_comment_on_first_reel instead so both actions target the same media."
        ),
    )
    def comment_on_first_reel(self, text: str, browser: str | None = None) -> dict:
        """Open the first recommended Reel and post the user's exact comment after confirmation.

        Args:
            text: Exact comment text supplied by the user.
        """
        text = text.strip()
        if not text:
            raise ToolError("Provide the exact comment text to post.")
        if len(text) > 2200:
            raise ToolError("Instagram comments must be 2,200 characters or fewer.")
        media_id, shortcode = self._client_call(
            self._first_explore_reel
        )
        opened_in = self._open_reel(shortcode, browser)
        comment = self._client_call(lambda client: client.media_comment(media_id, text))
        if not comment:
            raise ToolError("Instagram did not confirm posting the comment.")
        return {
            "commented": True,
            "media_id": media_id,
            "comment_id": str(_value(comment, "pk", "")),
            "text": text,
            "browser": opened_in,
        }

    @tool(
        group="instagram",
        capability="instagram",
        risk=Risk.CONFIRM,
        summary="Like the first Instagram Reel and comment: {text}",
        activity="Liking and commenting on the same Instagram Reel...",
        description=(
            "Use this single tool when the user asks to both like and comment on the first/current Reel, including "
            "requests such as 'like this video and write this comment'. It selects one Reel once, opens that Reel "
            "once, then likes and comments on the same media ID. Both actions require the user's confirmation. "
            "Use the exact comment text; ask for it first if missing. Return each action's actual result and clearly "
            "report partial failures."
        ),
    )
    def like_and_comment_on_first_reel(self, text: str, browser: str | None = None) -> dict:
        """Like and comment on the same first recommended Reel after user confirmation.

        Args:
            text: Exact comment text supplied by the user.
            browser: Optional browser name; "auto" or "chrome" uses the system default.
        """
        text = text.strip()
        if not text:
            raise ToolError("Provide the exact comment text to post.")
        if len(text) > 2200:
            raise ToolError("Instagram comments must be 2,200 characters or fewer.")

        media_id, shortcode = self._client_call(self._first_explore_reel)
        opened_in = self._open_reel(shortcode, browser)
        errors: list[str] = []
        liked = False
        commented = False
        comment: Any = None

        try:
            liked = bool(self._client_call(lambda client: client.media_like(media_id)))
            if not liked:
                errors.append("Instagram did not confirm the like; the Reel may already be liked.")
        except ToolError as exc:
            errors.append(f"Like failed: {exc}")

        try:
            comment = self._client_call(lambda client: client.media_comment(media_id, text))
            commented = bool(comment)
            if not commented:
                errors.append("Instagram did not confirm posting the comment.")
        except ToolError as exc:
            errors.append(f"Comment failed: {exc}")

        return {
            "ok": not errors,
            "liked": liked,
            "commented": commented,
            "media_id": media_id,
            "comment_id": str(_value(comment, "pk", "")) if comment else "",
            "text": text,
            "browser": opened_in,
            "error": "; ".join(errors),
        }

    @tool(group="instagram", capability="instagram", risk=Risk.CONFIRM,
          summary="Comment on Instagram media {media_id_or_url}: {text}", activity="Commenting on Instagram media...")
    def comment_on_media(self, media_id_or_url: str, text: str) -> dict:
        """Comment on an Instagram post or Reel by ID or URL after confirmation.

        Args:
            media_id_or_url: Instagram media ID or post/Reel URL.
            text: Exact comment text to post.
        """
        text = text.strip()
        if not text:
            raise ToolError("Provide the exact comment text.")
        if len(text) > 2200:
            raise ToolError("Instagram comments must be 2,200 characters or fewer.")
        def comment(client):
            media_id = self._media_id(client, media_id_or_url)
            return media_id, client.media_comment(media_id, text)

        media_id, result = self._client_call(comment)
        return {"commented": True, "media_id": media_id, "comment_id": str(_value(result, "pk", "")), "text": text}

    @tool(group="instagram", capability="instagram", risk=Risk.CONFIRM,
          summary="Publish Instagram photo {image_path} with caption: {caption}",
          activity="Publishing Instagram photo...", timeout=180)
    def upload_photo(self, image_path: str, caption: str = "") -> dict:
        """Publish a local image to Instagram after user confirmation.

        Args:
            image_path: Path to an image file on this computer.
            caption: Caption to publish with the image.
        """
        path = Path(image_path).expanduser().resolve()
        if not path.is_file():
            raise ToolError("The image file does not exist.")
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise ToolError("Choose a JPG or PNG image.")
        media = self._client_call(lambda client: client.photo_upload(path, caption))
        return {"published": True, **self._media_summary(media)}

    @tool(group="instagram", capability="instagram", risk=Risk.CONFIRM,
          summary="Publish Instagram Reel {video_path} with caption: {caption}",
          activity="Publishing Instagram Reel...", timeout=240)
    def upload_reel(self, video_path: str, caption: str) -> dict:
        """Publish a local video as an Instagram Reel after user confirmation.

        Args:
            video_path: Path to a video file on this computer.
            caption: Caption to publish with the video.
        """
        path = Path(video_path).expanduser().resolve()
        if not path.is_file():
            raise ToolError("The video file does not exist.")
        if path.suffix.lower() not in {".mp4", ".mov"}:
            raise ToolError("Choose an MP4 or MOV video.")
        media = self._client_call(lambda client: client.clip_upload(path, caption))
        return {"published": True, **self._media_summary(media)}

    @tool(group="instagram", capability="instagram", activity="Downloading Instagram media...")
    def download_media(self, media_id_or_url: str, save_path: str = "data/downloads") -> dict:
        """Download an Instagram photo, album, video, or Reel to a local folder.

        Args:
            media_id_or_url: Instagram media ID or post/Reel URL.
            save_path: Destination folder; defaults to data/downloads.
        """
        folder = Path(save_path).expanduser().resolve()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"Could not create the download folder: {describe_exception(exc)}") from exc

        def download(client):
            media_id = self._media_id(client, media_id_or_url)
            media = client.media_info(media_id)
            kind = self._media_kind(media)
            if kind == "album":
                paths = client.album_download(int(media_id), folder=folder, overwrite=False)
            elif kind == "photo":
                paths = [client.photo_download(media_id, folder=folder, overwrite=False)]
            elif kind in {"video", "reel"}:
                paths = [client.video_download(int(media_id), folder=folder, overwrite=False)]
            else:
                raise ToolError(f"Unsupported Instagram media type: {kind}.")
            return media_id, kind, paths

        media_id, kind, paths = self._client_call(download)
        return {
            "downloaded": True,
            "media_id": media_id,
            "media_type": kind,
            "files": [str(path) for path in paths],
        }

    @tool(group="instagram", capability="instagram", risk=Risk.CONFIRM,
          summary="Follow Instagram user @{username}", activity="Following Instagram user @{username}...")
    def follow_user(self, username: str) -> dict:
        """Follow an Instagram user after user confirmation.

        Args:
            username: Instagram username, with or without @.
        """
        username = _username(username)
        def follow(client):
            user_id = client.user_id_from_username(username)
            return client.user_follow(user_id)

        followed = self._client_call(follow)
        if not followed:
            raise ToolError(f"Instagram did not confirm following @{username}.")
        return {"followed": True, "username": username}

    @tool(group="instagram", capability="instagram", risk=Risk.CONFIRM,
          summary="Unfollow Instagram user @{username}", activity="Unfollowing Instagram user @{username}...")
    def unfollow_user(self, username: str) -> dict:
        """Unfollow an Instagram user after user confirmation.

        Args:
            username: Instagram username, with or without @.
        """
        username = _username(username)
        def unfollow(client):
            user_id = client.user_id_from_username(username)
            return client.user_unfollow(user_id)

        unfollowed = self._client_call(unfollow)
        if not unfollowed:
            raise ToolError(f"Instagram did not confirm unfollowing @{username}.")
        return {"unfollowed": True, "username": username}

    @tool(group="instagram", capability="instagram", activity="Searching Instagram users...")
    def search_users(self, query: str) -> dict:
        """Search Instagram for users by name or username.

        Args:
            query: Name or username to search for.
        """
        query = query.strip()
        if len(query) < 2:
            raise ToolError("Enter at least two characters to search Instagram users.")
        users = self._client_call(lambda client: client.search_users(query)[:20])
        return {
            "users": [
                {
                    "username": str(_value(user, "username", "")),
                    "full_name": str(_value(user, "full_name", "")),
                    "user_id": str(_value(user, "pk", "")),
                    "is_private": bool(_value(user, "is_private", False)),
                    "is_verified": bool(_value(user, "is_verified", False)),
                }
                for user in users
            ]
        }

    @tool(
        group="instagram",
        capability="instagram",
        risk=Risk.CONFIRM,
        summary="Unlike Instagram media {media_id_or_url}",
        activity="Removing Instagram like...",
    )
    def unlike_media(self, media_id_or_url: str) -> dict:
        """Remove your like from an Instagram post or Reel by ID or URL after confirmation.

        Args:
            media_id_or_url: Instagram media ID or post/Reel URL.
        """
        def unlike(client):
            media_id = self._media_id(client, media_id_or_url)
            return media_id, client.media_unlike(media_id)

        media_id, unliked = self._client_call(unlike)
        if not unliked:
            raise ToolError("Instagram did not confirm removing the like.")
        return {"unliked": True, "media_id": media_id}
