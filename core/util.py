"""Small helpers shared across modules."""
from __future__ import annotations

import re
import os
import shutil
import subprocess
import webbrowser
from datetime import datetime, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def truncate(text: object, limit: int, marker: str = "…") -> str:
    """Cut ``text`` to ``limit`` characters, ending with ``marker`` when shortened."""
    text = str(text)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))].rstrip() + marker


def clean_text(text: object) -> str:
    """Strip control characters and surrounding whitespace."""
    return _CONTROL_CHARS.sub("", str(text)).strip()


def human_bytes(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024.0:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def describe_exception(exc: BaseException, limit: int = 220) -> str:
    """One-line ``Type: message`` description for logs and tool errors."""
    message = clean_text(exc) or "no details"
    return truncate(f"{type(exc).__name__}: {message}", limit)


def open_url_in_browser(url: str, browser: str = "") -> str:
    """Open a URL in a named browser, falling back to the system default."""
    browser = str(browser or "").strip().casefold()
    auto_detect = browser in {"", "auto", "chrome", "google-chrome", "chromium"}
    if auto_detect:
        try:
            hostname = urlparse(url).hostname or ""
        except ValueError:
            hostname = ""
        if hostname.casefold() in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
            from core.events import activity

            activity("INSTAGRAM", "Opening with auto-detected browser...", "info")
        if not webbrowser.open(url):
            raise OSError("Windows could not open the default browser.")
        return "default"
    if browser not in {"edge", "microsoft edge"}:
        if not webbrowser.open(url):
            raise OSError(f"Could not open URL with the default browser (requested '{browser}').")
        return "default"

    candidates = [
        shutil.which("msedge.exe"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"),
        str(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe"),
    ]
    executable = next((Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()), None)
    if executable is None:
        raise FileNotFoundError("Microsoft Edge was not found.")
    subprocess.Popen([str(executable), url], close_fds=True)
    return "edge"


@lru_cache(maxsize=1)
def local_tz() -> tzinfo:
    """The machine's IANA time zone (falls back to a fixed UTC offset)."""
    try:
        from tzlocal import get_localzone

        return get_localzone()
    except Exception:
        return datetime.now().astimezone().tzinfo or timezone.utc


def local_tz_name() -> str:
    tz = local_tz()
    return getattr(tz, "key", None) or str(tz)


def now_context() -> str:
    """Human readable 'now' for the LLM, e.g. ``Sunday 2026-10-04 18:40 (+0300 Europe/Istanbul)``."""
    now = datetime.now(local_tz())
    return f"{now:%A %Y-%m-%d %H:%M} ({now:%z} {local_tz_name()})"


_FALLBACK_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%d.%m.%Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d",
)


def parse_datetime(value: str | datetime, default_tz: tzinfo | None = None) -> datetime:
    """Parse an ISO-8601-style timestamp into an *aware* datetime.

    Naive values are interpreted in ``default_tz`` (the machine's zone by default).
    Raises ``ValueError`` with a message suitable for the LLM when unparseable.
    """
    tz = default_tz or local_tz()
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("empty date/time")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in _FALLBACK_FORMATS:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"cannot parse {text!r}; use ISO 8601 like 2026-10-04T18:30:00") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed
