"""Telegram automation backed by Telethon's asynchronous client."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from config import Settings
from core.events import activity
from core.interaction import Interaction
from core.service import ServiceModule, ToolError
from core.tool_registry import Risk, tool
from core.util import truncate

log = logging.getLogger("jarvis.telegram")
_MAX_LIMIT = 100


class TelegramService(ServiceModule):
    key = "telegram"
    title = "Telegram"

    def __init__(self, settings: Settings, interaction: Interaction) -> None:
        super().__init__(settings, interaction)
        self._user_client: Any = None
        self._bot_client: Any = None
        self._new_message_event: Any = None
        self._remote_event: Any = None
        self._auto_reply_handler: Callable[[str, str], Awaitable[str]] | None = None
        self._incoming_message_handler: Callable[[str, str], None] | None = None
        self._remote_command_handler: Callable[[Any], Awaitable[None]] | None = None
        self._replied_incoming: set[tuple[int, int]] = set()

    def set_auto_reply_handler(self, handler: Callable[[str, str], Awaitable[str]]) -> None:
        self._auto_reply_handler = handler

    def set_incoming_message_handler(self, handler: Callable[[str, str], None]) -> None:
        self._incoming_message_handler = handler

    def set_remote_command_handler(self, handler: Callable[[Any], Awaitable[None]]) -> None:
        self._remote_command_handler = handler

    @property
    def configured(self) -> bool:
        return self.settings.telegram.configured

    def unconfigured_reason(self) -> str:
        return "set TELEGRAM_API_ID, TELEGRAM_API_HASH and user phone or bot token in .env"

    async def _start(self) -> str | None:
        from telethon import TelegramClient, events
        from telethon.errors import SessionPasswordNeededError

        cfg = self.settings.telegram
        if not cfg.api_id or not cfg.api_hash:
            raise ToolError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env.")
        cfg.user_session.parent.mkdir(parents=True, exist_ok=True)
        started: list[Any] = []
        try:
            if "user" in cfg.clients:
                client = TelegramClient(str(cfg.user_session), cfg.api_id, cfg.api_hash)
                started.append(client)
                await client.connect()
                if not await client.is_user_authorized():
                    if not cfg.phone:
                        raise ToolError("Set TELEGRAM_PHONE_NUMBER to sign in to the Telegram user account.")
                    sent_code = await client.send_code_request(cfg.phone)
                    code = await asyncio.to_thread(
                        self.interaction.ask_text,
                        "Telegram sign-in",
                        f"Enter the Telegram verification code sent to {cfg.phone}",
                        secret=True,
                        timeout=300.0,
                    )
                    if not code:
                        raise ToolError("Telegram sign-in was cancelled.")
                    try:
                        await client.sign_in(cfg.phone, code, phone_code_hash=sent_code.phone_code_hash)
                    except SessionPasswordNeededError:
                        password = await asyncio.to_thread(
                            self.interaction.ask_text,
                            "Telegram two-step verification",
                            "Enter your Telegram cloud password",
                            secret=True,
                            timeout=300.0,
                        )
                        if not password:
                            raise ToolError("Telegram two-step verification was cancelled.")
                        await client.sign_in(password=password)
                self._user_client = client
            if "bot" in cfg.clients:
                client = TelegramClient(str(cfg.bot_session), cfg.api_id, cfg.api_hash)
                started.append(client)
                await client.start(bot_token=cfg.bot_token)
                self._bot_client = client
        except Exception as exc:
            await self._disconnect_clients(started)
            self._user_client = None
            self._bot_client = None
            if isinstance(exc, ToolError):
                raise
            raise self._translate_error(exc) from exc

        try:
            clients = [client for client in (self._user_client, self._bot_client) if client is not None]
            if cfg.notify_incoming or cfg.auto_reply_users:
                self._new_message_event = events.NewMessage(incoming=True)
                for client in clients:
                    client.add_event_handler(self._on_new_message, self._new_message_event)
            if self._bot_client is not None:
                if cfg.admin_id is None:
                    activity(
                        "TELEGRAM",
                        "No TELEGRAM_ADMIN_ID is configured; all remote bot messages will be denied.",
                        "warn",
                    )
                self._remote_event = events.NewMessage(incoming=True)
                self._bot_client.add_event_handler(self._on_remote_message, self._remote_event)
            identities = []
            for client in clients:
                me = await client.get_me()
                identities.append(f"@{me.username}" if getattr(me, "username", None) else str(me.id))
        except Exception as exc:
            await self._disconnect_clients(started)
            self._user_client = None
            self._bot_client = None
            self._new_message_event = None
            self._remote_event = None
            raise self._translate_error(exc) from exc
        return ", ".join(identities) or "connected"

    async def _disconnect_clients(self, clients: list[Any]) -> None:
        outcomes = await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                log.warning("Could not disconnect a Telegram client cleanly: %s", self._translate_error(outcome))

    async def _stop(self) -> None:
        clients = [client for client in (self._user_client, self._bot_client) if client is not None]
        for client in clients:
            if self._new_message_event is not None:
                client.remove_event_handler(self._on_new_message, self._new_message_event)
        if self._bot_client is not None and self._remote_event is not None:
            self._bot_client.remove_event_handler(self._on_remote_message, self._remote_event)
        await self._disconnect_clients(clients)
        self._user_client = None
        self._bot_client = None
        self._new_message_event = None
        self._remote_event = None

    async def _on_remote_message(self, event: Any) -> None:
        if self._bot_client is None or event.client is not self._bot_client or not event.is_private:
            return
        admin_id = self.settings.telegram.admin_id
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        if admin_id is None or sender_id != admin_id:
            try:
                await event.respond("Access Denied")
            except Exception:
                log.exception("Could not reject unauthorized Telegram bot command")
            return
        if self._remote_command_handler is None:
            await event.respond("Jarvis remote command handler is unavailable.")
            return
        try:
            await self._remote_command_handler(event)
        except Exception:
            log.exception("Telegram remote command failed")
            try:
                await event.respond("Jarvis could not complete that remote request.")
            except Exception:
                log.exception("Could not send Telegram remote-command error")

    async def _on_new_message(self, event: Any) -> None:
        try:
            sender = await event.get_sender()
            username = str(getattr(sender, "username", "") or "").casefold()
            is_monitored_private = (
                self._user_client is not None
                and event.client is self._user_client
                and bool(getattr(event, "is_private", False))
                and username in self.settings.telegram.auto_reply_users
            )
            if not (event.is_private or event.mentioned):
                return
            name = " ".join(
                part for part in (
                    getattr(sender, "first_name", ""),
                    getattr(sender, "last_name", ""),
                ) if part
            ) or getattr(sender, "username", None) or str(getattr(sender, "id", "unknown"))
            preview = truncate((event.raw_text or "[media message]").replace("\n", " "), 180)
            activity("TELEGRAM", f"Incoming message from {name}: {preview}", "info")
            if not is_monitored_private:
                return
            message_text = str(event.raw_text or "").strip()
            if self._incoming_message_handler is not None:
                spoken_text = message_text or "media xabar yubordi"
                try:
                    self._incoming_message_handler(name, spoken_text)
                except Exception:
                    log.exception("Could not announce monitored Telegram message")
            if not message_text:
                activity("TELEGRAM", f"Auto-reply skipped for @{username}: incoming message has no text.", "warn")
                return
            key = (int(event.chat_id or sender.id), int(event.message.id))
            if key in self._replied_incoming:
                return
            self._replied_incoming.add(key)
            if len(self._replied_incoming) > 200:
                self._replied_incoming.pop()
            if self._auto_reply_handler is None:
                activity("TELEGRAM", "Auto-reply is not available because the Jarvis reply handler is not configured.", "error")
                return
            reply = (await self._auto_reply_handler(username, message_text)).strip()
            if not reply:
                activity("TELEGRAM", f"Jarvis could not compose an auto-reply to @{username}.", "warn")
                return
            await self._request(
                lambda: self._user_client.send_message(
                    sender, truncate(reply, 3500), reply_to=event.message.id
                )
            )
            activity("TELEGRAM", f"Automatically replied to @{username}.", "ok")
        except Exception:
            log.exception("Could not publish incoming Telegram notification")

    def _client(self):
        self.require_ready()
        client = self._user_client or self._bot_client
        if client is None:
            raise ToolError("Telegram is not connected. Check the Telegram capability status.")
        return client

    def _translate_error(self, exc: Exception) -> ToolError:
        name = type(exc).__name__
        if name in {"FloodWaitError", "FloodTestPhoneWaitError"}:
            seconds = int(getattr(exc, "seconds", 0))
            return ToolError(f"Telegram rate limit reached. Wait {seconds} seconds and try again.")
        if name in {"UserDeactivatedError", "UserDeactivatedBanError"}:
            return ToolError("Telegram deactivated this account. Check the account in the official app.")
        if name in {"ChatWriteForbiddenError", "ChannelPrivateError"}:
            return ToolError("Telegram does not allow this account to write in that chat or channel.")
        if name in {"PhoneCodeInvalidError", "PhoneCodeExpiredError"}:
            return ToolError("The Telegram verification code is invalid or expired. Reconnect and request a new code.")
        if name in {"PasswordHashInvalidError"}:
            return ToolError("The Telegram two-step verification password is incorrect.")
        if name in {"UsernameInvalidError", "UsernameNotOccupiedError", "PeerIdInvalidError", "ChatIdInvalidError"}:
            return ToolError("Telegram could not find that user or chat. Check the username or chat ID.")
        if name in {"UserNotParticipantError", "ChatAdminRequiredError", "UserAdminInvalidError"}:
            return ToolError("This Telegram action requires permissions that the account does not have.")
        if name == "FileReferenceExpiredError":
            return ToolError("Telegram media reference expired. Fetch the message again and retry.")
        detail = self.settings.redact(truncate(str(exc), 220))
        return ToolError(f"Telegram request failed ({name}): {detail}")

    async def _request(self, operation):
        try:
            return await operation()
        except ToolError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc

    async def _entity(self, value: str):
        target = value.strip()
        if not target:
            raise ToolError("Provide a Telegram username, phone number, or chat ID.")
        if target.startswith("@"):
            target = target[1:]
        try:
            return await self._client().get_entity(int(target) if target.lstrip("-").isdigit() else target)
        except Exception as exc:
            raise self._translate_error(exc) from exc

    @staticmethod
    def _message_summary(message: Any) -> dict[str, Any]:
        sender = getattr(message, "sender", None)
        sender_name = " ".join(
            part for part in (
                getattr(sender, "first_name", ""),
                getattr(sender, "last_name", ""),
            ) if part
        ) or getattr(sender, "username", None) or str(getattr(message, "sender_id", "") or "")
        sent_at = getattr(message, "date", None)
        return {
            "id": int(message.id),
            "sender": sender_name,
            "text": truncate(str(getattr(message, "message", "") or ""), 1000),
            "date": sent_at.isoformat() if sent_at else "",
            "outgoing": bool(getattr(message, "out", False)),
            "has_media": bool(getattr(message, "media", None)),
        }

    @staticmethod
    def _limit(value: int, maximum: int = _MAX_LIMIT) -> int:
        return max(1, min(int(value), maximum))

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send Telegram message to {target}", activity="Sending Telegram message...")
    async def send_message(
        self, target: str, text: str, reply_to: int | None = None,
        parse_mode: Literal["md", "html", "none"] = "md",
    ) -> dict:
        """Send a formatted message to a Telegram user, group, or channel.

        Args:
            target: Telegram @username, phone number, or chat ID.
            text: Exact message text to send.
            reply_to: Optional message ID to reply to.
            parse_mode: Formatting mode: md, html, or none.
        """
        if not text.strip():
            raise ToolError("Provide the exact message text to send.")
        client = self._client()
        entity = await self._entity(target)
        mode = {"md": "md", "html": "html", "none": None}[parse_mode]
        message = await self._request(lambda: client.send_message(entity, text, reply_to=reply_to, parse_mode=mode))
        return {"sent": True, "chat_id": str(getattr(message, "chat_id", "")), "message": self._message_summary(message)}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send Telegram message to {target}", activity="Sending Telegram message...")
    async def send_telegram_message(self, target: str, text: str) -> dict:
        """Send a Telegram message to a username, phone number, or chat ID.

        Args:
            target: Telegram @username, phone number, or numeric chat ID; usernames are resolved automatically.
            text: Exact message text to send.
        """
        return await self.send_message(target, text)

    @tool(
        group="telegram",
        capability="telegram",
        risk=Risk.CONFIRM,
        summary="Reply to the latest Telegram message from {username_or_chat}",
        activity="Replying to the latest Telegram message...",
        description=(
            "Reply to the latest message in a Telegram chat. Pass the @username, phone number, or numeric chat ID; "
            "the chat and latest message are resolved automatically. Do not ask the user for a raw chat ID or message ID."
        ),
    )
    async def reply_to_latest_message(self, username_or_chat: str, text: str) -> dict:
        """Reply to the most recent Telegram message in a chat without requiring IDs.

        Pass a Telegram @username, phone number, or numeric chat ID. Jarvis resolves
        the entity, fetches its latest message, and replies to that message. Do not
        ask the user for a raw Chat ID or Message ID.

        Args:
            username_or_chat: Telegram @username, phone number, or numeric chat ID; resolved automatically.
            text: Exact reply text to send.
        """
        if not text.strip():
            raise ToolError("Provide the exact reply text to send.")
        entity = await self._entity(username_or_chat)
        client = self._client()
        latest = await self._request(lambda: client.get_messages(entity, limit=1))
        if not latest:
            raise ToolError(f"No messages were found in Telegram chat {username_or_chat}.")
        message = await self._request(
            lambda: client.send_message(entity, text, reply_to=latest[0].id)
        )
        return {
            "sent": True,
            "chat_id": str(getattr(message, "chat_id", getattr(entity, "id", ""))),
            "reply_to_message_id": int(latest[0].id),
            "message": self._message_summary(message),
        }

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send Telegram text to {target}", activity="Sending Telegram text...")
    async def send_telegram_text(self, target: str, text: str) -> dict:
        """Send a plain text message to a Telegram user, group, or channel.

        Args:
            target: Telegram @username, phone number, or chat ID.
            text: Exact text to send.
        """
        return await self.send_message(target, text, parse_mode="none")

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send Telegram media to {target}", activity="Sending Telegram media...")
    async def send_media(self, target: str, file_path: str, caption: str = "") -> dict:
        """Send a photo, video, document, or audio file to a Telegram chat.

        Args:
            target: Telegram @username or chat ID.
            file_path: Path to the local media file.
            caption: Optional caption for the media.
        """
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ToolError(f"Media file does not exist: {path}")
        entity = await self._entity(target)
        sent = await self._request(lambda: self._client().send_file(entity, str(path), caption=caption))
        messages = sent if isinstance(sent, list) else [sent]
        return {"sent": True, "messages": [self._message_summary(item) for item in messages]}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send Telegram post to {target}", activity="Posting to Telegram chat...")
    async def send_telegram_post(self, target: str, text: str, media_path: str | None = None) -> dict:
        """Post text or a media announcement to a Telegram user, group, or channel.

        Args:
            target: Telegram @username or chat ID.
            text: Post text or media caption.
            media_path: Optional local image or video to attach.
        """
        if media_path:
            if not text.strip():
                raise ToolError("Provide post text or a media caption.")
            result = await self.send_media(target, media_path, text)
            result["posted"] = True
            return result
        if not text.strip():
            raise ToolError("Provide the exact post text.")
        result = await self.send_message(target, text)
        result["posted"] = True
        return result

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send Telegram video to {target}", activity="Sending Telegram video...")
    async def send_telegram_video(self, target: str, video_path: str, caption: str = "") -> dict:
        """Send a video file to a Telegram user, group, or channel.

        Args:
            target: Telegram @username or chat ID.
            video_path: Path to the local video file.
            caption: Optional caption.
        """
        path = Path(video_path).expanduser()
        if not path.is_file():
            raise ToolError(f"Video file does not exist: {path}")
        entity = await self._entity(target)
        sent = await self._request(
            lambda: self._client().send_file(entity, str(path), caption=caption, supports_streaming=True)
        )
        messages = sent if isinstance(sent, list) else [sent]
        return {"sent": True, "media_type": "video", "messages": [self._message_summary(item) for item in messages]}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send Telegram file to {target}", activity="Sending Telegram file...")
    async def send_telegram_file(self, target: str, file_path: str, caption: str = "") -> dict:
        """Send a file as a Telegram document to a user, group, or channel.

        Args:
            target: Telegram @username or chat ID.
            file_path: Path to the local file.
            caption: Optional caption.
        """
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ToolError(f"File does not exist: {path}")
        entity = await self._entity(target)
        sent = await self._request(
            lambda: self._client().send_file(entity, str(path), caption=caption, force_document=True)
        )
        messages = sent if isinstance(sent, list) else [sent]
        return {"sent": True, "media_type": "file", "messages": [self._message_summary(item) for item in messages]}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send Telegram audio to {target}", activity="Sending Telegram audio...")
    async def send_telegram_audio(self, target: str, audio_path: str, caption: str = "") -> dict:
        """Send an audio file to a Telegram user, group, or channel.

        Args:
            target: Telegram @username or chat ID.
            audio_path: Path to the local audio file.
            caption: Optional caption.
        """
        path = Path(audio_path).expanduser()
        if not path.is_file():
            raise ToolError(f"Audio file does not exist: {path}")
        entity = await self._entity(target)
        sent = await self._request(
            lambda: self._client().send_file(entity, str(path), caption=caption, force_document=False)
        )
        messages = sent if isinstance(sent, list) else [sent]
        return {"sent": True, "media_type": "audio", "messages": [self._message_summary(item) for item in messages]}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Send voice note to {target}", activity="Sending Telegram voice note...")
    async def send_voice_note(self, target: str, file_path: str) -> dict:
        """Send an audio file as a native Telegram voice note.

        Args:
            target: Telegram @username or chat ID.
            file_path: Path to the audio file.
        """
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ToolError(f"Audio file does not exist: {path}")
        entity = await self._entity(target)
        sent = await self._request(lambda: self._client().send_file(entity, str(path), voice_note=True))
        return {"sent": True, "message": self._message_summary(sent)}

    @tool(group="telegram", capability="telegram")
    async def get_chat_history(self, chat_id: str, limit: int = 10) -> dict:
        """Read recent Telegram messages from a private chat, group, or channel.

        Args:
            chat_id: Telegram @username or chat ID.
            limit: Maximum messages to return, from 1 to 100.
        """
        entity = await self._entity(chat_id)
        client = self._client()
        messages = await self._request(lambda: client.get_messages(entity, limit=self._limit(limit)))
        return {"chat_id": str(getattr(entity, "id", chat_id)), "messages": [self._message_summary(m) for m in messages]}

    @tool(group="telegram", capability="telegram")
    async def get_recent_messages(self, chat_id_or_username: str, limit: int = 5) -> dict:
        """Get recent messages from a Telegram chat.

        Args:
            chat_id_or_username: Telegram @username or chat ID.
            limit: Maximum messages to return.
        """
        return await self.get_chat_history(chat_id_or_username, limit)

    @tool(group="telegram", capability="telegram")
    async def get_dialogs(self, limit: int = 20, unread_only: bool = False) -> dict:
        """List recent Telegram dialogs and their unread message counts.

        Args:
            limit: Maximum dialogs to return, from 1 to 100.
            unread_only: Return only dialogs with unread messages.
        """
        client = self._client()
        dialogs = await self._request(lambda: client.get_dialogs(limit=self._limit(limit)))
        rows = []
        for dialog in dialogs:
            unread = int(getattr(dialog, "unread_count", 0) or 0)
            if unread_only and unread < 1:
                continue
            entity = dialog.entity
            rows.append({
                "chat_id": str(getattr(entity, "id", "")),
                "title": str(getattr(dialog, "name", "") or ""),
                "username": str(getattr(entity, "username", "") or ""),
                "unread_count": unread,
                "is_group": bool(getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False)),
                "last_message": self._message_summary(dialog.message) if dialog.message else None,
            })
        return {"dialogs": rows}

    @tool(group="telegram", capability="telegram")
    async def mark_as_read(self, chat_id: str) -> dict:
        """Mark incoming Telegram messages in a chat as read.

        Args:
            chat_id: Telegram @username or chat ID.
        """
        entity = await self._entity(chat_id)
        await self._request(lambda: self._client().send_read_acknowledge(entity))
        return {"marked_read": True, "chat_id": str(getattr(entity, "id", chat_id))}

    @tool(group="telegram", capability="telegram")
    async def search_messages(self, query: str, chat_id: str | None = None, limit: int = 10) -> dict:
        """Search Telegram messages in one chat or across available dialogs.

        Args:
            query: Keyword or phrase to search for.
            chat_id: Optional Telegram @username or chat ID to limit the search.
            limit: Maximum results to return, from 1 to 100.
        """
        if not query.strip():
            raise ToolError("Provide a keyword or phrase to search for.")
        entity = await self._entity(chat_id) if chat_id else None
        client = self._client()
        messages = await self._request(
            lambda: client.get_messages(entity, limit=self._limit(limit), search=query)
        )
        return {"query": query, "messages": [self._message_summary(m) for m in messages]}

    @tool(group="telegram", capability="telegram")
    async def search_telegram_messages(self, query: str, limit: int = 5) -> dict:
        """Search recent Telegram messages across chats for a keyword.

        Args:
            query: Keyword or phrase to search for.
            limit: Maximum results to return.
        """
        return await self.search_messages(query, None, limit)

    @tool(group="telegram", capability="telegram")
    async def download_telegram_media(
        self, chat_id: str, message_id: int, save_path: str = "data/downloads",
    ) -> dict:
        """Download media attached to a Telegram message.

        Args:
            chat_id: Telegram @username or chat ID containing the message.
            message_id: Message ID that contains the media.
            save_path: Destination directory or file path.
        """
        entity = await self._entity(chat_id)
        client = self._client()
        message = await self._request(lambda: client.get_messages(entity, ids=message_id))
        if not message or not message.media:
            raise ToolError("That Telegram message does not contain downloadable media.")
        destination = Path(save_path).expanduser()
        if destination.suffix:
            destination.parent.mkdir(parents=True, exist_ok=True)
        else:
            destination.mkdir(parents=True, exist_ok=True)
        result = await self._request(lambda: client.download_media(message, file=str(destination)))
        if not result:
            raise ToolError("Telegram did not return a media file.")
        return {"downloaded": True, "path": str(Path(result).resolve()), "message_id": message_id}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Post Telegram channel announcement", activity="Posting to Telegram channel...")
    async def post_to_channel(
        self, text: str, channel_id_or_username: str | None = None, media_path: str | None = None,
    ) -> dict:
        """Post an announcement or media to the configured or specified Telegram channel.

        Args:
            text: Announcement text or media caption.
            channel_id_or_username: Channel username or ID; defaults to TELEGRAM_DEFAULT_CHANNEL.
            media_path: Optional local photo, video, or document path.
        """
        channel = (channel_id_or_username or self.settings.telegram.default_channel).strip()
        if not channel:
            raise ToolError("Set TELEGRAM_DEFAULT_CHANNEL or provide a channel username or ID.")
        if not text.strip() and not media_path:
            raise ToolError("Provide announcement text or a media file.")
        entity = await self._entity(channel)
        client = self._client()
        if media_path:
            path = Path(media_path).expanduser()
            if not path.is_file():
                raise ToolError(f"Media file does not exist: {path}")
            sent = await self._request(lambda: client.send_file(entity, str(path), caption=text))
        else:
            sent = await self._request(lambda: client.send_message(entity, text))
        return {"posted": True, "channel": channel, "message": self._message_summary(sent)}

    @tool(
        group="telegram",
        capability="telegram",
        risk=Risk.CONFIRM,
        summary="Start a Telegram group video chat in {chat_id_or_username}",
        activity="Starting Telegram video chat...",
    )
    async def start_video_chat(self, chat_id_or_username: str, title: str | None = None) -> dict:
        """Start a Telegram group call/video-chat room in a group or channel.

        Args:
            chat_id_or_username: Telegram group or channel username or ID.
            title: Optional title for the voice/video chat room.
        """
        client = self._user_client
        if client is None:
            raise ToolError("Starting a Telegram group video chat requires a connected userbot account.")
        from telethon import types
        from telethon.tl.functions.phone import CreateGroupCallRequest

        entity = await self._entity(chat_id_or_username)
        if not isinstance(entity, (types.Chat, types.Channel)):
            raise ToolError("Telegram video chats can only be started in a group or channel.")
        updates = await self._request(
            lambda: client(CreateGroupCallRequest(peer=entity, title=title.strip() if title else None))
        )
        update_list = getattr(updates, "updates", None) or (
            updates if isinstance(updates, (list, tuple)) else [updates]
        )
        call = next(
            (
                getattr(update, "call", None)
                for update in update_list
                if isinstance(update, types.UpdateGroupCall)
            ),
            None,
        )
        if call is None or isinstance(call, types.GroupCallDiscarded):
            raise ToolError(
                "Telegram accepted the video-chat request but did not return an active call. "
                "Check the group permissions and whether a call is already running."
            )
        return {
            "started": True,
            "chat_id": str(entity.id),
            "title": str(getattr(call, "title", "") or title or ""),
            "call_id": str(call.id),
            "participants": int(getattr(call, "participants_count", 0) or 0),
            "note": "The group call room is open. Join it in Telegram and enable the camera there to publish video.",
        }

    @tool(group="telegram", capability="telegram")
    async def get_channel_info(self, channel_id_or_username: str) -> dict:
        """Get Telegram channel title, description, member count, and recent posts.

        Args:
            channel_id_or_username: Channel username or ID.
        """
        from telethon.tl.functions.channels import GetFullChannelRequest

        entity = await self._entity(channel_id_or_username)
        client = self._client()
        full = await self._request(lambda: client(GetFullChannelRequest(entity)))
        posts = await self._request(lambda: client.get_messages(entity, limit=5))
        return {
            "id": str(getattr(entity, "id", "")),
            "title": str(getattr(entity, "title", "") or ""),
            "username": str(getattr(entity, "username", "") or ""),
            "about": str(getattr(full.full_chat, "about", "") or ""),
            "participants_count": getattr(full.full_chat, "participants_count", None),
            "recent_posts": [self._message_summary(message) for message in posts],
        }

    @tool(group="telegram", capability="telegram")
    async def get_group_members(self, group_id: str, limit: int = 50) -> dict:
        """List members of a Telegram group with their available role information.

        Args:
            group_id: Telegram group username or ID.
            limit: Maximum members to return, from 1 to 100.
        """
        client = self._client()
        entity = await self._entity(group_id)
        members = await self._request(lambda: client.get_participants(entity, limit=self._limit(limit)))
        rows = []
        for user in members:
            participant = getattr(user, "participant", None)
            role = "owner" if type(participant).__name__ == "ChannelParticipantCreator" else (
                "admin" if type(participant).__name__ == "ChannelParticipantAdmin" else "member"
            )
            rows.append({
                "id": str(user.id),
                "username": str(getattr(user, "username", "") or ""),
                "name": " ".join(part for part in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if part),
                "role": role,
            })
        return {"members": rows, "count": len(rows)}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Invite users to Telegram group {group_id}", activity="Inviting members to Telegram group...")
    async def invite_to_group(self, group_id: str, usernames: list[str]) -> dict:
        """Invite specified users to a Telegram group or channel.

        Args:
            group_id: Telegram group or channel username or ID.
            usernames: Usernames or phone numbers to invite.
        """
        if not usernames or len(usernames) > 20:
            raise ToolError("Provide between 1 and 20 users to invite.")
        from telethon.tl.functions.channels import InviteToChannelRequest
        from telethon.tl.functions.messages import AddChatUserRequest
        from telethon.tl.types import Chat

        client = self._client()
        group = await self._entity(group_id)
        users = [await self._entity(value) for value in usernames]
        if isinstance(group, Chat):
            for user in users:
                await self._request(lambda user=user: client(AddChatUserRequest(group.id, user, fwd_limit=0)))
        else:
            await self._request(lambda: client(InviteToChannelRequest(group, users)))
        return {"invited": [str(getattr(user, "username", "") or user.id) for user in users], "group_id": str(group.id)}

    @tool(group="telegram", capability="telegram", name="telegram_get_user_profile")
    async def get_user_profile(self, username_or_phone: str) -> dict:
        """Get public Telegram user details and presence status.

        Args:
            username_or_phone: Telegram username, phone number, or user ID.
        """
        user = await self._entity(username_or_phone)
        client = self._client()
        from telethon.tl.functions.users import GetFullUserRequest
        full = await self._request(lambda: client(GetFullUserRequest(user)))
        photos = await self._request(lambda: client.get_profile_photos(user, limit=10))
        status = getattr(user, "status", None)
        status_type = type(status).__name__ if status else ""
        presence = {
            "UserStatusOnline": "online",
            "UserStatusOffline": "offline",
            "UserStatusRecently": "recently",
            "UserStatusLastWeek": "last_week",
            "UserStatusLastMonth": "last_month",
        }.get(status_type, "unknown")
        status_time = getattr(status, "expires", None) or getattr(status, "was_online", None)
        return {
            "id": str(user.id),
            "username": str(getattr(user, "username", "") or ""),
            "first_name": str(getattr(user, "first_name", "") or ""),
            "last_name": str(getattr(user, "last_name", "") or ""),
            "phone": str(getattr(user, "phone", "") or ""),
            "bio": str(getattr(getattr(full, "full_user", None), "about", "") or ""),
            "has_profile_photo": bool(getattr(user, "photo", None)),
            "profile_photos": [
                {"id": str(photo.id), "date": photo.date.isoformat() if getattr(photo, "date", None) else ""}
                for photo in photos
            ],
            "status": presence,
            "status_time": status_time.isoformat() if status_time else "",
            "bot": bool(getattr(user, "bot", False)),
        }

    @tool(group="telegram", capability="telegram")
    async def search_user(self, query: str) -> dict:
        """Resolve public usernames and search account-visible users by display name.

        Args:
            query: Name, username, or phone fragment to search for.
        """
        if not query.strip():
            raise ToolError("Provide a name or username to search for.")
        client = self._client()
        query_text = query.strip()
        matches: dict[str, dict[str, str]] = {}
        candidate = query_text.lstrip("@")
        if candidate and " " not in candidate:
            try:
                user = await client.get_entity(candidate)
            except Exception as exc:
                if type(exc).__name__ not in {"UsernameInvalidError", "UsernameNotOccupiedError", "ValueError"}:
                    raise self._translate_error(exc) from exc
            else:
                if getattr(user, "id", None) is not None:
                    matches[str(user.id)] = {
                        "id": str(user.id),
                        "username": str(getattr(user, "username", "") or ""),
                        "name": " ".join(
                            part for part in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if part
                        ),
                    }
        from telethon.tl.functions.contacts import SearchRequest
        result = await self._request(lambda: client(SearchRequest(q=query_text, limit=20)))
        contacts = getattr(result, "users", [])
        query_lower = query_text.lstrip("@").casefold()
        for user in contacts:
            name = " ".join(
                part for part in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if part
            )
            if (
                query_lower in str(getattr(user, "username", "") or "").casefold()
                or query_lower in name.casefold()
            ):
                matches[str(user.id)] = {
                    "id": str(user.id),
                    "username": str(getattr(user, "username", "") or ""),
                    "name": name,
                }
        return {"users": list(matches.values())[:20]}

    @tool(group="telegram", capability="telegram")
    async def download_profile_photo(self, username: str, save_path: str = "data/avatars") -> dict:
        """Download the profile photo for a Telegram user.

        Args:
            username: Telegram username or user ID.
            save_path: Destination directory.
        """
        user = await self._entity(username)
        destination = Path(save_path).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        path = await self._request(lambda: self._client().download_profile_photo(user, file=str(destination)))
        if not path:
            raise ToolError("This Telegram user has no downloadable profile photo.")
        return {"downloaded": True, "path": str(Path(path).resolve()), "user_id": str(user.id)}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Schedule Telegram message to {target}", activity="Scheduling Telegram message...")
    async def schedule_message(self, target: str, text: str, schedule_time: str) -> dict:
        """Schedule a Telegram message for a future ISO-8601 date and time.

        Args:
            target: Telegram username, phone number, or chat ID.
            text: Exact message text to send.
            schedule_time: Future ISO-8601 datetime, optionally including timezone.
        """
        try:
            when = datetime.fromisoformat(schedule_time.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ToolError("Provide schedule_time as an ISO-8601 date and time.") from None
        if when.tzinfo is None:
            when = when.astimezone()
        if when <= datetime.now(timezone.utc):
            raise ToolError("The scheduled time must be in the future.")
        if not text.strip():
            raise ToolError("Provide the exact message text to schedule.")
        entity = await self._entity(target)
        sent = await self._request(lambda: self._client().send_message(entity, text, schedule=when))
        return {"scheduled": True, "target": target, "schedule_time": when.isoformat(), "message_id": int(sent.id)}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="React to Telegram message {message_id}", activity="Reacting to Telegram message...")
    async def send_reaction(self, chat_id: str, message_id: int, emoji: str) -> dict:
        """React to a Telegram message with an emoji.

        Args:
            chat_id: Telegram username or chat ID containing the message.
            message_id: Message ID to react to.
            emoji: One supported reaction emoji.
        """
        from telethon import functions, types

        if not emoji.strip() or len(emoji) > 8:
            raise ToolError("Provide one supported reaction emoji.")
        entity = await self._entity(chat_id)
        await self._request(lambda: self._client()(functions.messages.SendReactionRequest(
            peer=entity, msg_id=message_id, reaction=[types.ReactionEmoji(emoticon=emoji.strip())],
        )))
        return {"reacted": True, "chat_id": chat_id, "message_id": message_id, "emoji": emoji.strip()}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Edit Telegram message {message_id}", activity="Editing Telegram message...")
    async def edit_message(self, chat_id: str, message_id: int, new_text: str) -> dict:
        """Edit a Telegram message previously sent by this account.

        Args:
            chat_id: Telegram username or chat ID containing the message.
            message_id: Message ID to edit.
            new_text: Replacement message text.
        """
        entity = await self._entity(chat_id)
        message = await self._request(lambda: self._client().edit_message(entity, message_id, new_text))
        return {"edited": True, "message": self._message_summary(message)}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Delete Telegram messages from {chat_id}", activity="Deleting Telegram messages...")
    async def delete_message(self, chat_id: str, message_ids: list[int]) -> dict:
        """Delete specified Telegram messages from a chat.

        Args:
            chat_id: Telegram username or chat ID containing the messages.
            message_ids: Message IDs to delete, maximum 100.
        """
        if not message_ids or len(message_ids) > 100:
            raise ToolError("Provide between 1 and 100 message IDs.")
        entity = await self._entity(chat_id)
        await self._request(lambda: self._client().delete_messages(entity, message_ids))
        return {"deleted": True, "chat_id": chat_id, "message_ids": message_ids, "count": len(message_ids)}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Pin Telegram message {message_id}", activity="Pinning Telegram message...")
    async def pin_message(self, chat_id: str, message_id: int, notify: bool = False) -> dict:
        """Pin a Telegram message in a group or channel.

        Args:
            chat_id: Telegram username or chat ID.
            message_id: Message ID to pin.
            notify: Whether Telegram should notify chat members.
        """
        entity = await self._entity(chat_id)
        await self._request(lambda: self._client().pin_message(entity, message_id, notify=notify))
        return {"pinned": True, "chat_id": chat_id, "message_id": message_id}

    @tool(group="telegram", capability="telegram", risk=Risk.CONFIRM,
          summary="Forward Telegram messages to {to_chat}", activity="Forwarding Telegram messages...")
    async def forward_messages(self, from_chat: str, to_chat: str, message_ids: list[int]) -> dict:
        """Forward Telegram messages from one chat to another.

        Args:
            from_chat: Source Telegram username or chat ID.
            to_chat: Destination Telegram username or chat ID.
            message_ids: Message IDs to forward, maximum 100.
        """
        if not message_ids or len(message_ids) > 100:
            raise ToolError("Provide between 1 and 100 message IDs.")
        source = await self._entity(from_chat)
        destination = await self._entity(to_chat)
        messages = await self._request(
            lambda: self._client().forward_messages(destination, message_ids, from_peer=source)
        )
        return {"forwarded": True, "count": len(messages) if isinstance(messages, list) else 1, "to_chat": to_chat}
