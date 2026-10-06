"""Gmail and YouTube actions using the user's Google OAuth account."""
from __future__ import annotations

import base64
import asyncio
import datetime as dt
import html
import logging
import mimetypes
import os
import re
import socket
import webbrowser
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import Settings
from core.interaction import Interaction
from core.service import ServiceModule, ToolError
from core.tool_registry import Risk, tool
from core.util import truncate

log = logging.getLogger("jarvis.google")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_GMAIL_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_GOOGLE_HTTP_TIMEOUT = 20


class GoogleWorkspaceService(ServiceModule):
    key = "google"
    title = "Google Workspace"

    def __init__(self, settings: Settings, interaction: Interaction) -> None:
        super().__init__(settings, interaction)
        self._gmail: Any = None
        self._calendar: Any = None
        self._drive: Any = None
        self._youtube: Any = None
        self._workspace_credentials: Any = None
        self._youtube_credentials: Any = None
        self._workspace_ready = False
        self._youtube_ready = False

    @property
    def configured(self) -> bool:
        return self.settings.google.configured

    def unconfigured_reason(self) -> str:
        return (
            f"place a Desktop OAuth client JSON at {self.settings.google.credentials_file}; "
            "enable Gmail, Calendar, Drive, and YouTube Data APIs"
        )

    async def _start(self) -> str:
        failures: list[str] = []
        workspace = self.settings.google
        try:
            credentials = await asyncio.to_thread(
                self._authorize,
                workspace.workspace_scopes,
                workspace.workspace_token_file,
            )
            self._workspace_credentials = credentials
            self._gmail = self._build_api("gmail", "v1", credentials)
            self._calendar = self._build_api("calendar", "v3", credentials)
            self._drive = self._build_api("drive", "v3", credentials)
            self._workspace_ready = True
        except Exception as exc:
            self._workspace_ready = False
            self._gmail = self._calendar = self._drive = None
            failures.append(f"Workspace OAuth failed: {self._safe_error(exc)}")
            log.exception("Google Workspace authorization failed; attempting YouTube independently")

        try:
            credentials = await asyncio.to_thread(
                self._authorize,
                workspace.youtube_scopes,
                workspace.youtube_token_file,
            )
            self._youtube_credentials = credentials
            self._youtube = self._build_api("youtube", "v3", credentials)
            self._youtube_ready = True
        except Exception as exc:
            self._youtube_ready = False
            self._youtube = None
            failures.append(f"YouTube OAuth failed: {self._safe_error(exc)}")
            log.exception("YouTube authorization failed; Workspace remains available if connected")

        if not self._workspace_ready and not self._youtube_ready:
            raise ToolError("Both Google OAuth flows failed. " + " ".join(failures))
        connected = []
        if self._workspace_ready:
            connected.append("Gmail, Calendar, and Drive")
        if self._youtube_ready:
            connected.append("YouTube")
        detail = " and ".join(connected) + " connected"
        if failures:
            detail += "; " + " ".join(failures)
        return detail

    @staticmethod
    def _build_api(service_name: str, version: str, credentials: Any) -> Any:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build

        http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=_GOOGLE_HTTP_TIMEOUT))
        return build(service_name, version, http=http, cache_discovery=False)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, ToolError):
            return str(exc)
        return f"{type(exc).__name__}; check the Google OAuth configuration and retry."

    def _authorize(self, scopes: tuple[str, ...], token_file: Path) -> Any:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        credentials = None
        if token_file.is_file():
            try:
                import json

                token_data = json.loads(token_file.read_text(encoding="utf-8"))
                granted_scopes = set(token_data.get("scopes") or str(token_data.get("scope") or "").split())
                credentials = Credentials.from_authorized_user_file(str(token_file), list(scopes))
                if credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                if (
                    not credentials.valid
                    or not credentials.has_scopes(scopes)
                    or not set(scopes).issubset(granted_scopes)
                ):
                    log.info("Google OAuth scopes changed for token %s; starting a separate consent flow", token_file.name)
                    credentials = None
            except Exception as exc:
                log.warning("Saved Google token %s could not be reused: %s", token_file.name, type(exc).__name__)
                credentials = None

        if credentials is None:
            credentials_file = self.settings.google.credentials_file
            if not credentials_file.is_file():
                raise ToolError(
                    f"Google OAuth client file is missing: {credentials_file}. "
                    "Create a Desktop app OAuth client in Google Cloud Console."
                )
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_file), list(scopes)
            )
            credentials = flow.run_local_server(port=0, open_browser=True)

        token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = token_file.with_name(token_file.name + ".tmp")
        try:
            temporary.write_text(credentials.to_json(), encoding="utf-8")
            os.replace(temporary, token_file)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

        return credentials

    def _require_workspace(self) -> None:
        if not self._workspace_ready:
            raise ToolError("Google Workspace is not connected. Check its OAuth consent and retry.")

    def _require_youtube(self) -> None:
        if not self._youtube_ready:
            raise ToolError("YouTube is not connected. Check its OAuth consent and retry.")

    def _api(self, request: Any) -> Any:
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except (TimeoutError, socket.timeout) as exc:
            raise ToolError(
                f"Google API request timed out after {_GOOGLE_HTTP_TIMEOUT} seconds. "
                "The action may have completed; check its status before retrying."
            ) from exc
        except OSError as exc:
            raise ToolError(
                f"Google API network request failed ({type(exc).__name__}). Check the internet connection and retry."
            ) from exc
        except HttpError as exc:
            status = int(getattr(exc.resp, "status", 0) or 0)
            reasons, messages = self._http_error_details(exc)
            reason = ", ".join(reasons)
            if status == 401:
                raise ToolError("Google authorization expired. Reconnect the Google capability.") from None
            if status == 403:
                if "commentsDisabled" in reasons:
                    raise ToolError("YouTube has comments disabled for this video.") from None
                if "youtubeSignupRequired" in reasons:
                    raise ToolError("This Google account must activate a YouTube channel before it can comment.") from None
                if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons:
                    raise ToolError("Google API quota is exhausted. Check the project quota in Google Cloud Console.") from None
                detail = f" YouTube/API reason: {reason}." if reason else ""
                if messages:
                    detail += f" {truncate(messages[0], 180)}"
                raise ToolError(
                    "Google denied this action." + detail
                    + " Check account/channel permissions and whether the relevant API is enabled."
                ) from None
            if status == 404:
                raise ToolError("Google could not find that email or YouTube video.") from None
            log.warning("Google API request failed with HTTP %s (reason: %s)", status, reason or "unspecified")
            detail = f" ({reason})" if reason else ""
            raise ToolError(f"Google API request failed (HTTP {status or 'unknown'}{detail}).") from None

    @staticmethod
    def _http_error_details(exc: Any) -> tuple[list[str], list[str]]:
        import json

        try:
            payload = json.loads(exc.content.decode("utf-8", "replace"))
        except (AttributeError, UnicodeError, ValueError):
            return [], []
        error = payload.get("error") or {}
        reasons: list[str] = []
        messages: list[str] = []
        for item in error.get("errors") or []:
            if item.get("reason"):
                reasons.append(str(item["reason"]))
            if item.get("message"):
                messages.append(str(item["message"]))
        if error.get("message"):
            messages.insert(0, str(error["message"]))
        return list(dict.fromkeys(reasons)), list(dict.fromkeys(messages))

    @staticmethod
    def _body_parts(payload: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
        chunks: list[str] = []
        html_chunks: list[str] = []
        attachments: list[dict[str, str]] = []

        def visit(part: dict[str, Any]) -> None:
            body = part.get("body") or {}
            data = body.get("data")
            mime_type = str(part.get("mimeType", ""))
            filename = str(part.get("filename", ""))
            if filename:
                attachments.append({"filename": filename, "mime_type": mime_type, "attachment_id": str(body.get("attachmentId", ""))})
            if data and mime_type in {"text/plain", "text/html"}:
                try:
                    decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
                    if mime_type == "text/plain":
                        chunks.append(decoded)
                    else:
                        html_chunks.append(decoded)
                except (ValueError, UnicodeError):
                    log.warning("Could not decode a Gmail text part")
            for child in part.get("parts") or []:
                visit(child)

        visit(payload)
        body = "\n".join(chunks).strip()
        if not body and html_chunks:
            markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", "\n".join(html_chunks))
            body = html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))
            body = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", body)).strip()
        return body, attachments

    @staticmethod
    def _headers(payload: dict[str, Any]) -> dict[str, str]:
        return {
            str(header.get("name", "")).lower(): str(header.get("value", ""))
            for header in payload.get("headers") or []
        }

    @tool(group="google", capability="google", activity="Searching Gmail...")
    def search_emails(self, query: str = "newer_than:30d", limit: int = 10) -> dict:
        """Search Gmail messages and return their IDs, senders, subjects and snippets.

        Args:
            query: Gmail search query, for example 'from:alice@example.com is:unread'.
            limit: Maximum number of emails to return (1-25).
        """
        self._require_workspace()
        query = query.strip()
        maximum = max(1, min(int(limit), 25))
        response = self._api(
            self._gmail.users().messages().list(userId="me", q=query or "newer_than:30d", maxResults=maximum)
        )
        rows = []
        for item in response.get("messages") or []:
            message = self._api(
                self._gmail.users().messages().get(
                    userId="me", id=item["id"], format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                )
            )
            headers = self._headers(message.get("payload") or {})
            rows.append({
                "id": str(item["id"]),
                "thread_id": str(message.get("threadId", "")),
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "subject": headers.get("subject", "(no subject)"),
                "date": headers.get("date", ""),
                "snippet": str(message.get("snippet", "")),
            })
        return {"count": len(rows), "emails": rows}

    @tool(
        group="google",
        capability="google",
        activity="Reading the latest email...",
        description=(
            "Read the user's most recent Gmail email aloud in Uzbek. Use this tool whenever the user asks "
            "'oxirgi kelgan xabarni o'qib ber', 'pochtamni tekshir', 'yangi xat bormi' or "
            "'oxirgi emailni o'qil'. This tool takes no parameters. Do NOT ask the user for message IDs "
            "or parameters."
        ),
    )
    def read_latest_email(self) -> str:
        """Read the latest Gmail email aloud, without requiring an email or message ID."""
        self._require_workspace()
        response = self._api(self._gmail.users().messages().list(userId="me", maxResults=1))
        messages = response.get("messages") or []
        if not messages:
            return "Pochtada hech qanday yangi xat topilmadi."

        message_id = str(messages[0].get("id", ""))
        if not message_id:
            raise ToolError("Gmail returned a latest email without a message ID.")
        message = self._api(
            self._gmail.users().messages().get(userId="me", id=message_id, format="full")
        )
        payload = message.get("payload") or {}
        headers = self._headers(payload)
        body, _ = self._body_parts(payload)
        if not body:
            snippet = str(message.get("snippet", ""))
            body = snippet
        body = html.unescape(re.sub(r"(?s)<[^>]+>", " ", body))
        body = re.sub(r"\s+", " ", body).strip()
        sender = headers.get("from", "").strip() or "noma'lum yuboruvchi"
        subject = headers.get("subject", "").strip() or "mavzusiz"
        content = body or str(message.get("snippet", "")).strip() or "xat mazmuni mavjud emas"
        return f"Sizga {sender}dan yangi xat kelgan. Mavzu: {subject}. Xat mazmuni: {content}"

    @tool(group="google", capability="google", activity="Reading Gmail message...")
    def read_email(self, message_id: str) -> dict:
        """Read the full text and headers of an email found with search_emails.

        Args:
            message_id: Gmail message ID returned by search_emails.
        """
        self._require_workspace()
        message = self._api(self._gmail.users().messages().get(userId="me", id=message_id, format="full"))
        payload = message.get("payload") or {}
        headers = self._headers(payload)
        body, attachments = self._body_parts(payload)
        return {
            "id": str(message.get("id", message_id)),
            "thread_id": str(message.get("threadId", "")),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "subject": headers.get("subject", "(no subject)"),
            "date": headers.get("date", ""),
            "body": truncate(body or str(message.get("snippet", "")), 12000),
            "attachments": attachments,
            "is_unread": "UNREAD" in (message.get("labelIds") or []),
        }

    @tool(group="google", capability="google", activity="Downloading Gmail attachment...")
    def download_email_attachment(
        self,
        message_id: str,
        attachment_id: str,
        filename: str,
        save_path: str = "data/downloads",
    ) -> dict:
        """Download an attachment listed by read_email.

        Args:
            message_id: Gmail message ID.
            attachment_id: Attachment ID returned by read_email.
            filename: Attachment filename returned by read_email.
            save_path: Destination directory (defaults to data/downloads).
        """
        self._require_workspace()
        response = self._api(self._gmail.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ))
        encoded = str(response.get("data", ""))
        if not encoded:
            raise ToolError("Gmail returned an empty attachment.")
        try:
            content = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as exc:
            raise ToolError("Gmail returned an invalid attachment encoding.") from exc
        directory = Path(save_path).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name.strip() or f"attachment-{attachment_id}"
        destination = directory / safe_name
        temporary = destination.with_name(destination.name + ".part")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, destination)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        return {
            "downloaded": True,
            "message_id": message_id,
            "filename": safe_name,
            "size": len(content),
            "path": str(destination.resolve()),
        }

    @tool(group="google", capability="google", activity="Marking Gmail message as read...")
    def mark_email_read(self, message_id: str = "") -> dict:
        """Mark a Gmail message as read; defaults to the latest unread email.

        Args:
            message_id: Optional Gmail message ID from search_emails. Omit it, pass an
                empty value or "latest" to mark the most recent unread email as read.
        """
        self._require_workspace()
        from googleapiclient.errors import HttpError

        requested_id = str(message_id or "").strip()

        def latest_unread_id() -> str:
            response = self._api(self._gmail.users().messages().list(
                userId="me", q="is:unread", maxResults=1,
            ))
            messages = response.get("messages") or []
            latest = str(messages[0].get("id", "")) if messages else ""
            if not latest:
                raise ToolError("There are no unread Gmail messages to mark as read.")
            return latest

        use_latest = (
            not requested_id
            or requested_id.casefold() == "latest"
            or not _GMAIL_MESSAGE_ID.fullmatch(requested_id)
        )
        resolved_id = latest_unread_id() if use_latest else requested_id

        def mark_read(target_id: str) -> None:
            self._gmail.users().messages().modify(
                userId="me",
                id=target_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()

        def api_error(exc: HttpError) -> ToolError:
            status = int(getattr(exc.resp, "status", 0) or 0)
            reasons, _ = self._http_error_details(exc)
            reason = f" ({', '.join(reasons)})" if reasons else ""
            return ToolError(f"Gmail API request failed (HTTP {status or 'unknown'}{reason}).")

        try:
            mark_read(resolved_id)
        except HttpError as exc:
            status = int(getattr(exc.resp, "status", 0) or 0)
            if not use_latest and status in {400, 404}:
                resolved_id = latest_unread_id()
                try:
                    mark_read(resolved_id)
                except HttpError as retry_exc:
                    raise api_error(retry_exc) from None
            else:
                raise api_error(exc) from None
        return {"marked_read": True, "message_id": resolved_id}

    @tool(
        group="google", capability="google", risk=Risk.CONFIRM,
        summary="Reply to Gmail message {message_id}", activity="Replying to Gmail message...",
    )
    def reply_to_email(self, message_id: str, body: str) -> dict:
        """Reply in the existing Gmail thread for a message.

        Args:
            message_id: Gmail message ID returned by search_emails.
            body: Plain-text reply content.
        """
        self._require_workspace()
        if not body.strip():
            raise ToolError("Provide the reply text.")
        original = self._api(self._gmail.users().messages().get(userId="me", id=message_id, format="full"))
        headers = self._headers(original.get("payload") or {})
        recipient = parseaddr(headers.get("reply-to") or headers.get("from", ""))[1]
        if not recipient or recipient.count("@") != 1:
            raise ToolError("The original email does not contain a valid reply address.")
        message = EmailMessage()
        message["To"] = recipient
        subject = headers.get("subject", "")
        message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        message["In-Reply-To"] = headers.get("message-id", "")
        message["References"] = " ".join(
            part for part in (headers.get("references", ""), headers.get("message-id", "")) if part
        )
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = self._api(self._gmail.users().messages().send(
            userId="me", body={"raw": raw, "threadId": str(original.get("threadId", ""))}
        ))
        return {
            "sent": True,
            "id": str(result.get("id", "")),
            "thread_id": str(result.get("threadId", "")),
            "to": recipient,
            "subject": message["Subject"],
        }

    @tool(
        group="google", capability="google", risk=Risk.CONFIRM,
        summary="Send email to {to}: {subject}", activity="Sending Gmail message...",
    )
    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
    ) -> dict:
        """Send a plain-text email through the authenticated Gmail account.

        Args:
            to: Recipient email address (multiple addresses may be comma-separated).
            subject: Email subject.
            body: Plain-text email body.
            cc: Optional comma-separated CC recipients.
            bcc: Optional comma-separated BCC recipients.
        """
        self._require_workspace()
        if not to.strip() or not subject.strip() or not body.strip():
            raise ToolError("Email recipient, subject, and message body are required.")
        if any("\r" in value or "\n" in value for value in (to, cc, bcc, subject)):
            raise ToolError("Email addresses and subject must not contain line breaks.")
        for address in re.split(r"[,;]", ",".join((to, cc, bcc))):
            if address.strip() and not parseaddr(address.strip())[1].count("@") == 1:
                raise ToolError(f"Invalid email address: {truncate(address.strip(), 80)}")
        message = EmailMessage()
        message["To"] = to
        if cc.strip():
            message["Cc"] = cc
        if bcc.strip():
            message["Bcc"] = bcc
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = self._api(self._gmail.users().messages().send(userId="me", body={"raw": raw}))
        return {
            "sent": True,
            "id": str(result.get("id", "")),
            "thread_id": str(result.get("threadId", "")),
            "to": to,
            "subject": subject,
        }

    @tool(group="google", capability="google", activity="Listing Google Calendar events...")
    def list_calendar_events(self, limit: int = 20) -> dict:
        """List upcoming primary-calendar events for the next seven days.

        Args:
            limit: Maximum events to return (1-50).
        """
        self._require_workspace()
        now = dt.datetime.now(dt.timezone.utc)
        response = self._api(self._calendar.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + dt.timedelta(days=7)).isoformat(),
            maxResults=max(1, min(int(limit), 50)),
            singleEvents=True,
            orderBy="startTime",
        ))
        events = [
            {
                "id": str(item.get("id", "")),
                "summary": str(item.get("summary", "(no title)")),
                "start": (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date", ""),
                "end": (item.get("end") or {}).get("dateTime") or (item.get("end") or {}).get("date", ""),
                "location": str(item.get("location", "")),
                "description": truncate(str(item.get("description", "")), 1000),
                "html_link": str(item.get("htmlLink", "")),
            }
            for item in response.get("items") or []
        ]
        return {"count": len(events), "events": events}

    @tool(
        group="google", capability="google", risk=Risk.CONFIRM,
        summary="Create Google Calendar event: {summary}", activity="Creating Google Calendar event...",
    )
    def create_calendar_event(
        self,
        summary: str,
        start: str,
        end: str,
        timezone: str = "Asia/Tashkent",
        description: str = "",
        location: str = "",
    ) -> dict:
        """Create an event on the primary Google Calendar.

        Args:
            summary: Event title.
            start: Start date/time in ISO-8601 format, for example 2026-10-06T10:00:00.
            end: End date/time in ISO-8601 format.
            timezone: IANA timezone used when start/end have no UTC offset.
            description: Optional event description.
            location: Optional event location.
        """
        self._require_workspace()
        if not summary.strip():
            raise ToolError("Provide a title for the calendar event.")
        try:
            from zoneinfo import ZoneInfo

            start_dt = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
            zone = ZoneInfo(timezone)
        except (ValueError, KeyError) as exc:
            raise ToolError("Use valid ISO date/time values and an IANA timezone, such as Asia/Tashkent.") from exc
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=zone)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=zone)
        if end_dt <= start_dt:
            raise ToolError("The calendar event end must be after its start.")
        event_body = {
            "summary": summary.strip(),
            "description": description.strip(),
            "location": location.strip(),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
        }
        result = self._api(self._calendar.events().insert(calendarId="primary", body=event_body))
        return {
            "created": True,
            "id": str(result.get("id", "")),
            "summary": str(result.get("summary", summary)),
            "start": (result.get("start") or {}).get("dateTime", start_dt.isoformat()),
            "html_link": str(result.get("htmlLink", "")),
        }

    @tool(group="google", capability="google", activity="Searching Google Drive...")
    def search_drive_files(self, query: str = "", limit: int = 20) -> dict:
        """Search accessible Google Drive files by name.

        Args:
            query: Plain-text name fragment; leave empty to list recent files.
            limit: Maximum files to return (1-50).
        """
        self._require_workspace()
        escaped = query.strip().replace("\\", "\\\\").replace("'", "\\'")
        drive_query = "trashed = false"
        if escaped:
            drive_query += f" and name contains '{escaped}'"
        response = self._api(self._drive.files().list(
            q=drive_query,
            pageSize=max(1, min(int(limit), 50)),
            orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime,size,webViewLink)",
        ))
        files = [
            {
                "id": str(item.get("id", "")),
                "name": str(item.get("name", "")),
                "mime_type": str(item.get("mimeType", "")),
                "modified": str(item.get("modifiedTime", "")),
                "size": str(item.get("size", "")),
                "url": str(item.get("webViewLink", "")),
            }
            for item in response.get("files") or []
        ]
        return {"count": len(files), "files": files}

    @tool(
        group="google", capability="google", risk=Risk.CONFIRM,
        summary="Upload {file_path} to Google Drive", activity="Uploading file to Google Drive...",
    )
    def upload_drive_file(self, file_path: str, name: str = "") -> dict:
        """Upload a local file to the authenticated user's Google Drive.

        Args:
            file_path: Path to an existing local file.
            name: Optional Drive filename; defaults to the local filename.
        """
        self._require_workspace()
        from googleapiclient.http import MediaFileUpload

        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ToolError(f"File not found: {truncate(str(path), 160)}")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        upload = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
        response = self._api(self._drive.files().create(
            body={"name": name.strip() or path.name},
            media_body=upload,
            fields="id,name,mimeType,webViewLink",
        ))
        return {
            "uploaded": True,
            "id": str(response.get("id", "")),
            "name": str(response.get("name", path.name)),
            "mime_type": str(response.get("mimeType", mime_type)),
            "url": str(response.get("webViewLink", "")),
        }

    @tool(group="google", capability="google", activity="Downloading from Google Drive...")
    def download_drive_file(self, file_id: str, save_path: str = "data/downloads") -> dict:
        """Download a Google Drive file into a local directory.

        Args:
            file_id: Drive file ID returned by search_drive_files.
            save_path: Destination directory (defaults to data/downloads).
        """
        self._require_workspace()
        from googleapiclient.http import MediaIoBaseDownload

        metadata = self._api(self._drive.files().get(fileId=file_id, fields="id,name,mimeType"))
        name = Path(str(metadata.get("name", "download"))).name
        mime_type = str(metadata.get("mimeType", ""))
        if mime_type.startswith("application/vnd.google-apps."):
            request = self._drive.files().export_media(fileId=file_id, mimeType="application/pdf")
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
        else:
            request = self._drive.files().get_media(fileId=file_id)
        directory = Path(save_path).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / name
        temporary = destination.with_name(destination.name + ".part")
        try:
            with temporary.open("wb") as output:
                downloader = MediaIoBaseDownload(output, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"downloaded": True, "file_id": file_id, "path": str(destination.resolve())}

    @tool(
        group="google", capability="google", risk=Risk.CONFIRM,
        summary="Like YouTube video {video_url_or_id}", activity="Liking YouTube video...",
    )
    def like_youtube_video(self, video_url_or_id: str) -> dict:
        """Like a YouTube video by URL or 11-character video ID.

        Args:
            video_url_or_id: YouTube watch URL, youtu.be URL, or video ID.
        """
        self._require_youtube()
        video_id = self._video_id(video_url_or_id)
        self._api(self._youtube.videos().rate(id=video_id, rating="like"))
        return {"liked": True, "video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"}

    @tool(
        group="google", capability="google", risk=Risk.CONFIRM,
        summary="Comment on YouTube video {video_url_or_id}: {text}",
        activity="Posting YouTube comment...", timeout=45,
    )
    def comment_on_youtube_video(self, video_url_or_id: str, text: str) -> dict:
        """Post a comment on a YouTube video.

        Args:
            video_url_or_id: YouTube watch URL, youtu.be URL, or video ID.
            text: Comment to publish.
        """
        self._require_youtube()
        video_id = self._video_id(video_url_or_id)
        if not text.strip():
            raise ToolError("Provide the comment text to post.")
        response = self._api(self._youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": text.strip()}},
                }
            },
        ))
        comment_id = str(response.get("id", ""))
        return {"commented": True, "video_id": video_id, "comment_id": comment_id, "text": text.strip()}

    @tool(group="google", capability="google", activity="Finding YouTube video...")
    def play_youtube_video(self, query_or_url: str) -> dict:
        """Find a YouTube video by title or open its supplied YouTube URL for playback.

        Args:
            query_or_url: YouTube URL/video ID to play, or search terms for a video.
        """
        self._require_youtube()
        target = query_or_url.strip()
        if not target:
            raise ToolError("Provide a YouTube video URL or search phrase.")
        if self._looks_like_youtube_url(target) or _VIDEO_ID.fullmatch(target):
            video_id = self._video_id(target)
        else:
            response = self._api(self._youtube.search().list(
                part="snippet", q=target, type="video", maxResults=1,
            ))
            items = response.get("items") or []
            if not items:
                raise ToolError(f"No YouTube video found for '{truncate(target, 100)}'.")
            video_id = str((items[0].get("id") or {}).get("videoId", ""))
            if not _VIDEO_ID.fullmatch(video_id):
                raise ToolError("YouTube search did not return a playable video.")
        url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
        if not webbrowser.open(url):
            raise ToolError("Windows could not open the default browser for YouTube playback.")
        return {"opened": True, "playback_requested": True, "video_id": video_id, "url": url}

    @tool(group="google", capability="google", activity="Copying YouTube URL...")
    def copy_youtube_url(self, query_or_url: str) -> dict:
        """Copy a YouTube video's share URL to the Windows clipboard.

        Args:
            query_or_url: YouTube URL/video ID, or search terms to find a video.
        """
        self._require_youtube()
        target = query_or_url.strip()
        if not target:
            raise ToolError("Provide a YouTube video URL or search phrase.")
        if self._looks_like_youtube_url(target) or _VIDEO_ID.fullmatch(target):
            video_id = self._video_id(target)
        else:
            response = self._api(self._youtube.search().list(
                part="snippet", q=target, type="video", maxResults=1,
            ))
            items = response.get("items") or []
            if not items:
                raise ToolError(f"No YouTube video found for '{truncate(target, 100)}'.")
            video_id = str((items[0].get("id") or {}).get("videoId", ""))
            if not _VIDEO_ID.fullmatch(video_id):
                raise ToolError("YouTube search did not return a valid video.")
        url = f"https://www.youtube.com/watch?v={video_id}"
        import pyperclip

        pyperclip.copy(url)
        return {"copied": True, "url": url, "video_id": video_id}

    @staticmethod
    def _looks_like_youtube_url(value: str) -> bool:
        parsed = urlparse(value if "://" in value else "https://" + value)
        return parsed.hostname in _YOUTUBE_HOSTS

    @classmethod
    def _video_id(cls, value: str) -> str:
        raw = value.strip()
        if _VIDEO_ID.fullmatch(raw):
            return raw
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        if parsed.hostname not in _YOUTUBE_HOSTS:
            raise ToolError("Provide a YouTube URL or an 11-character YouTube video ID.")
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            video_id = parsed.path.strip("/").split("/", 1)[0]
        else:
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            if not video_id:
                match = re.match(r"^/(?:shorts|embed)/([A-Za-z0-9_-]{11})(?:/|$)", parsed.path)
                video_id = match.group(1) if match else ""
        if not _VIDEO_ID.fullmatch(video_id):
            raise ToolError("That YouTube URL does not contain a valid video ID.")
        return video_id

    async def _stop(self) -> None:
        self._gmail = None
        self._calendar = None
        self._drive = None
        self._youtube = None
        self._workspace_credentials = None
        self._youtube_credentials = None
        self._workspace_ready = False
        self._youtube_ready = False
