"""Thread-safe event bus, activity feed and assistant state tracking.

Everything below the GUI layer (brain, speech, modules) is Qt-free. It talks to
the interface purely through this bus; ``main.py`` bridges bus events onto Qt
signals, so any thread may emit safely.

Event names (keyword payloads):

    ACTIVITY      source:str, message:str, level:str      -> right-hand log feed
    FLAG          name:str, value:bool, owner:str, detail:str  -> raw state inputs
    STATE         state:str, detail:str                   -> derived assistant state
    TRANSCRIPT    role:str ("user"|"jarvis"|"system"), text:str
    REPLY         text:str (accumulated so far), final:bool
    AUDIO_LEVELS  levels:list[float]                      -> 0..1 spectrum bands
    CAPABILITY    key:str, status:str, detail:str         -> left-hand toggles
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from enum import Enum
from typing import Any, Callable, Iterator

from config import settings

log = logging.getLogger("jarvis.events")

ACTIVITY = "activity"
FLAG = "flag"
STATE = "state"
TRANSCRIPT = "transcript"
REPLY = "reply"
AUDIO_LEVELS = "audio_levels"
CAPABILITY = "capability"

LEVELS = ("info", "ok", "warn", "error", "action", "dim")
_LOG_LEVEL = {
    "info": logging.INFO,
    "ok": logging.INFO,
    "action": logging.INFO,
    "dim": logging.DEBUG,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


class EventBus:
    """Minimal synchronous publish/subscribe; handlers run in the emitting thread."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., None]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event: str, callback: Callable[..., None]) -> Callable[[], None]:
        """Register ``callback(**payload)``; returns an unsubscribe function."""
        with self._lock:
            self._subscribers[event].append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers[event]:
                    self._subscribers[event].remove(callback)

        return unsubscribe

    def emit(self, event: str, **payload: Any) -> None:
        with self._lock:
            callbacks = tuple(self._subscribers.get(event, ()))
        for callback in callbacks:
            try:
                callback(**payload)
            except Exception:  # a broken listener must never take the emitter down
                log.exception("Handler for event %r failed", event)


bus = EventBus()


def activity(source: str, message: str, level: str = "info") -> None:
    """Publish a line to the activity feed (and the log file)."""
    level = level if level in LEVELS else "info"
    message = settings.redact(str(message))
    logging.getLogger("jarvis.activity").log(_LOG_LEVEL[level], "%s: %s", source, message)
    bus.emit(ACTIVITY, source=source.upper(), message=message, level=level)


def transcript(role: str, text: str) -> None:
    bus.emit(TRANSCRIPT, role=role, text=text)


# --------------------------------------------------------------------------- state
class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    EXECUTING = "EXECUTING"


# Highest priority first: a tool running outranks speech, which outranks thinking...
_PRIORITY: tuple[tuple[str, State], ...] = (
    ("executing", State.EXECUTING),
    ("speaking", State.SPEAKING),
    ("thinking", State.THINKING),
    ("listening", State.LISTENING),
)


def set_flag(name: str, value: bool, owner: str = "main", detail: str = "") -> None:
    """Raise or lower a state input.

    ``name`` is one of listening/thinking/executing/speaking. A flag stays raised
    while *any* owner holds it, so independent components can't clear each other.
    """
    bus.emit(FLAG, name=name, value=value, owner=owner, detail=detail)


@contextmanager
def flag(name: str, owner: str | None = None, detail: str = "") -> Iterator[None]:
    owner = owner or f"{name}-{threading.get_ident()}"
    set_flag(name, True, owner, detail)
    try:
        yield
    finally:
        set_flag(name, False, owner)


class StateTracker:
    """Folds FLAG events into one assistant state and re-emits it as STATE."""

    def __init__(self, event_bus: EventBus = bus) -> None:
        self._bus = event_bus
        self._lock = threading.Lock()
        self._held: dict[str, dict[str, str]] = {name: {} for name, _ in _PRIORITY}
        self._current = (State.IDLE, "")
        event_bus.subscribe(FLAG, self._on_flag)

    @property
    def state(self) -> State:
        return self._current[0]

    def _on_flag(self, name: str, value: bool, owner: str = "main", detail: str = "") -> None:
        if name not in self._held:
            return
        with self._lock:
            if value:
                self._held[name][owner] = detail
            else:
                self._held[name].pop(owner, None)
            new = self._derive()
            changed = new != self._current
            self._current = new
        if changed:
            self._bus.emit(STATE, state=new[0].value, detail=new[1])

    def _derive(self) -> tuple[State, str]:
        for name, state in _PRIORITY:
            owners = self._held[name]
            if owners:
                detail = next((d for d in reversed(list(owners.values())) if d), "")
                return state, detail
        return State.IDLE, ""

    def clear(self, owner: str) -> None:
        """Drop every flag held by ``owner`` (used when a task is cancelled)."""
        for name, _ in _PRIORITY:
            self._on_flag(name, False, owner)
