"""Base class shared by every capability module (system, telegram, instagram, google)."""
from __future__ import annotations

import asyncio
import logging

from config import Settings
from core.events import CAPABILITY, activity, bus
from core.interaction import Interaction
from core.util import describe_exception


class ToolError(Exception):
    """An expected, user-facing failure.

    Raise it from any ``@tool`` method; the message is returned to the LLM as the
    tool's error text (no stack trace is logged). Keep it short and actionable.
    """


class ServiceModule:
    """Lifecycle + status plumbing for a capability.

    Subclasses set ``key``/``title`` and override :meth:`configured`,
    :meth:`_start` and :meth:`_stop`. ``start`` never raises (except for task
    cancellation): failures become an ``error`` status shown in the GUI.
    """

    key: str = ""  # capability key used by the GUI toggles
    title: str = ""  # human name used in messages, e.g. "Telegram"

    def __init__(self, settings: Settings, interaction: Interaction) -> None:
        self.settings = settings
        self.interaction = interaction
        self.log = logging.getLogger(f"jarvis.{self.key or type(self).__name__.lower()}")
        self._ready = False
        self._status = "offline"
        self._detail = ""

    # ----------------------------------------------------------- subclass hooks
    @property
    def configured(self) -> bool:
        """True when the credentials needed to connect are present."""
        return True

    def unconfigured_reason(self) -> str:
        return "not configured (see .env)"

    async def _start(self) -> str | None:
        """Connect/log in. Return an optional detail string for the 'online' badge."""
        return None

    async def _stop(self) -> None:
        """Disconnect and release resources."""

    # --------------------------------------------------------------- lifecycle
    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def status(self) -> str:
        return self._status

    @property
    def detail(self) -> str:
        return self._detail

    def set_status(self, status: str, detail: str = "") -> None:
        """status: online | offline | disabled | connecting | error"""
        self._status, self._detail = status, detail
        bus.emit(CAPABILITY, key=self.key, status=status, detail=detail)

    async def start(self) -> bool:
        if self._ready:
            return True
        if not self.configured:
            self.set_status("offline", self.unconfigured_reason())
            return False
        self.set_status("connecting", "")
        try:
            detail = await self._start()
        except asyncio.CancelledError:
            self.set_status("offline", "start cancelled")
            raise
        except ToolError as exc:
            self.set_status("error", str(exc))
            activity(self.title.upper(), f"Could not start: {exc}", "error")
            return False
        except Exception as exc:
            self.log.exception("%s failed to start", self.title)
            self.set_status("error", describe_exception(exc))
            activity(self.title.upper(), f"Could not start: {describe_exception(exc)}", "error")
            return False
        self._ready = True
        self.set_status("online", detail or "")
        return True

    async def stop(self) -> None:
        was_ready = self._ready
        self._ready = False
        try:
            if was_ready:
                await self._stop()
        except Exception:
            self.log.exception("%s failed to stop cleanly", self.title)
        self.set_status("disabled", "")

    def require_ready(self) -> None:
        """Call at the top of every ``@tool`` method that needs a live connection."""
        if not self._ready:
            reason = self._detail or self._status
            raise ToolError(f"{self.title} is not available right now ({reason}).")
