"""Blocking user-interaction hooks shared by every module.

Modules run on worker threads and sometimes need a human decision (a Telegram
login code, an Instagram 2FA code, "really post this?"). They call an
``Interaction`` object; the Qt application provides an implementation that shows
a dialog on the GUI thread and blocks the *calling* thread until answered.
Calls are synchronous on purpose - from async code use ``await asyncio.to_thread``.
"""
from __future__ import annotations

import getpass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Interaction(Protocol):
    def ask_text(
        self,
        title: str,
        prompt: str,
        *,
        secret: bool = False,
        timeout: float | None = 300.0,
    ) -> str | None:
        """Ask for a line of text. Returns ``None`` if cancelled or timed out."""

    def confirm(self, title: str, details: str, *, timeout: float | None = None) -> bool:
        """Ask for approval. Returns ``False`` if declined or timed out."""


class ConsoleInteraction:
    """Fallback used when no GUI is attached (``--check``, scripts, tests)."""

    def ask_text(self, title: str, prompt: str, *, secret: bool = False, timeout: float | None = 300.0) -> str | None:
        try:
            label = f"[{title}] {prompt}: "
            value = getpass.getpass(label) if secret else input(label)
        except (EOFError, KeyboardInterrupt):
            return None
        return value.strip() or None

    def confirm(self, title: str, details: str, *, timeout: float | None = None) -> bool:
        try:
            answer = input(f"[{title}] {details}\nApprove? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}


class DenyAllInteraction:
    """Non-interactive stand-in: nothing is ever approved, nothing is ever typed."""

    def ask_text(self, title: str, prompt: str, *, secret: bool = False, timeout: float | None = 300.0) -> str | None:
        return None

    def confirm(self, title: str, details: str, *, timeout: float | None = None) -> bool:
        return False


class ApproveAllInteraction:
    """Test helper: approves every confirmation, answers every prompt with ''."""

    def ask_text(self, title: str, prompt: str, *, secret: bool = False, timeout: float | None = 300.0) -> str | None:
        return None

    def confirm(self, title: str, details: str, *, timeout: float | None = None) -> bool:
        return True
