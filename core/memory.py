"""Persistent, bounded short-term conversation history."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from config import DATA_DIR

log = logging.getLogger("jarvis.memory")


class MemoryPersistenceError(OSError):
    """Conversation history could not be written to disk."""


class ConversationMemory:
    """Store recent user/assistant messages in a small, restart-safe JSON buffer."""

    def __init__(self, path: Path | None = None, max_messages: int = 16) -> None:
        self.path = path or DATA_DIR / "chat_history.json"
        self.max_messages = max(2, max_messages - (max_messages % 2))
        self._lock = threading.RLock()
        self._messages = self._load()

    @property
    def messages(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(message) for message in self._messages]

    def append_turn(self, user_text: str, assistant_text: str) -> None:
        """Append one completed exchange and persist the newest whole exchanges."""
        if not user_text.strip() or not assistant_text.strip():
            raise ValueError("Conversation messages cannot be empty.")
        with self._lock:
            self._messages.extend((
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ))
            self._messages = self._messages[-self.max_messages:]
            self._save()

    def clear_memory(self) -> None:
        """Forget all conversation messages and persist an empty history."""
        with self._lock:
            self._messages.clear()
            self._save()

    def _load(self) -> list[dict[str, str]]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                data: Any = json.load(stream)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not load conversation history from %s: %s", self.path, exc)
            return []

        raw_messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(raw_messages, list):
            log.warning("Conversation history at %s has an invalid format; starting with empty history.", self.path)
            return []

        messages: list[dict[str, str]] = []
        for item in raw_messages:
            if (
                isinstance(item, dict)
                and item.get("role") in {"user", "assistant"}
                and isinstance(item.get("content"), str)
                and item["content"].strip()
            ):
                messages.append({"role": item["role"], "content": item["content"]})
        if len(messages) != len(raw_messages):
            log.warning("Discarded invalid messages from conversation history at %s.", self.path)
        if len(messages) % 2:
            messages = messages[:-1]
        if messages and messages[0]["role"] != "user":
            messages = messages[1:]
        if any(
            messages[index]["role"] == messages[index - 1]["role"]
            for index in range(1, len(messages))
        ):
            log.warning("Conversation history at %s is not alternating; starting with empty history.", self.path)
            return []
        return messages[-self.max_messages:]

    def _save(self) -> None:
        temporary_path: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = stream.name
                json.dump({"messages": self._messages}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    log.warning("Could not remove temporary conversation history file %s.", temporary_path)
            raise MemoryPersistenceError(f"Could not save conversation history to {self.path}: {exc}") from exc
