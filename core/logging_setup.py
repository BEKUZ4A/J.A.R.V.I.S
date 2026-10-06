"""Logging configuration: rotating file + console, with secrets scrubbed."""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from config import LOG_DIR, Settings
from core.events import activity

_NOISY = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "asyncio": logging.WARNING,
    "telethon": logging.WARNING,
    "PIL": logging.WARNING,
    "comtypes": logging.WARNING,
    "faster_whisper": logging.WARNING,
    "googleapiclient.discovery_cache": logging.ERROR,
    "google_auth_httplib2": logging.WARNING,
    "instagrapi": logging.WARNING,
    "private_request": logging.WARNING,
    "public_request": logging.WARNING,
    "matplotlib": logging.WARNING,
    "numba": logging.WARNING,
}


class _RedactFilter(logging.Filter):
    """Replaces every configured secret with ``***`` before a record is emitted."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        scrubbed = self._settings.redact(message)
        if scrubbed != message:
            record.msg, record.args = scrubbed, ()
        return True


def setup_logging(settings: Settings, *, console: bool = True) -> logging.Logger:
    """Configure the ``jarvis`` logger tree and hook uncaught-exception reporting."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    scrub = _RedactFilter(settings)

    root = logging.getLogger()
    for handler in list(root.handlers):  # idempotent when called twice
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)

    file_handler = RotatingFileHandler(LOG_DIR / "jarvis.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(scrub)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(scrub)
        root.addHandler(stream)

    logger = logging.getLogger("jarvis")
    logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    for name, level in _NOISY.items():
        logging.getLogger(name).setLevel(level)

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        activity("SYSTEM", f"Unhandled error: {exc_type.__name__}: {exc}", "error")

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        logger.critical(
            "Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        activity("SYSTEM", f"Unhandled error in thread: {args.exc_type.__name__}: {args.exc_value}", "error")

    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    return logger
