"""Tool registry: turns decorated methods into LLM tool definitions and runs them safely.

A capability module declares tools with the :func:`tool` decorator. The JSON
schema shown to the model is generated from the method signature (type hints)
and the Google-style docstring (first paragraph = description, ``Args:`` =
per-parameter help), so schema and code can't drift apart::

    class Files(ServiceModule):
        key = "system"

        @tool(group="core", capability="system", risk=Risk.CONFIRM,
              summary=lambda a: f"Delete {a['path']}",
              activity="Deleting {path}")
        def delete_file(self, path: str) -> dict:
            \"\"\"Delete a file from disk.

            Args:
                path: Absolute path of the file.
            \"\"\"

``ToolRegistry.invoke`` is the single entry point used by the brain. It coerces
the model's (often sloppy) arguments, enforces the confirmation policy, runs
sync tools on worker threads, applies a timeout and always returns a structured
:class:`ToolOutcome` - it never raises (except for task cancellation).
"""
from __future__ import annotations

import asyncio
import collections.abc
import difflib
import inspect
import json
import logging
import re
import time
import types
import typing
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Collection, Literal, get_args, get_origin, get_type_hints

from config import Settings
from core.events import activity, flag
from core.interaction import Interaction
from core.service import ToolError
from core.util import describe_exception, truncate

log = logging.getLogger("jarvis.tools")


class Risk(str, Enum):
    SAFE = "safe"  # runs immediately
    CONFIRM = "confirm"  # irreversible / outward-facing: needs approval when CONFIRM_RISKY_ACTIONS=true


RiskSpec = Risk | Callable[[dict[str, Any]], Risk]
TextSpec = str | Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class GroupInfo:
    """A family of tools. ``pattern`` decides when the family is offered to the model."""

    name: str
    capability: str
    always: bool
    pattern: re.Pattern[str] | None = None


def _words(*alternatives: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b", re.IGNORECASE)


GROUPS: dict[str, GroupInfo] = {
    g.name: g
    for g in (
        GroupInfo("core", "system", always=True),
        GroupInfo(
            "input", "system", False,
            _words("type", "typing", "write", "press", "keys?", "keyboard", "shortcut", "hotkey", "click", "double[- ]?click",
                   "right[- ]?click", "mouse", "cursor", "scroll", "drag", "focus", "switch", "windows?", "minimi[sz]e",
                   "maximi[sz]e", "clipboard", "copy", "paste", "select", "enter", "tab"),
        ),
        GroupInfo(
            "vision", "system", False,
            _words("screen", "screenshot", "display", "monitor", "see", "look", "looking", "read", "ocr", "visible", "button",
                   "icon", "menu", "tab", "ui", "interface", "describe", "what'?s on"),
        ),
        GroupInfo(
            "telegram", "telegram", False,
            _words("telegram\\w*", "tg", "channels?", "chats?", "dms?", "messages?", "msgs?", "texts?", "send", "post", "unread",
                   "groups?", "xabar\\w*", "yoz\\w*", "yubor\\w*", "kanal\\w*", "guruh\\w*",
                   "qidir\\w*", "izla\\w*", "o['’]?q\\w*", "o['’]?chir\\w*", "yuklab\\w*",
                   "video", "videocall", "videokonferens\\w*", "reply", "repl(?:y|ies)", "answer",
                   "javob\\w*", "qaytar\\w*"),
        ),
        GroupInfo(
            "instagram", "instagram", False,
            _words("instagram\\w*", "insta\\w*", "ig", "reels?\\w*", "stor(?:y|ies)", "feed", "post", "photos?", "pictures?", "captions?",
                   "dms?", "direct", "messages?", "inbox", "layk", "like", "yoqtir(?:ish|)", "izoh", "komment(?:ariy)?",
                   "obuna", "rasm", "videolar?", "postla(?:sh|)", "joyla(?:sh|)", "ulash(?:ish|)", "chat",
                   "xabar(?:lar)?", "yoz\\w*", "reaksiya", "reaktsiya", "izla(?:sh|)", "qidiri(?:sh|)",
                   "qo['’]?yib\\s+ber\\w*", "play", "watch"),
        ),
        GroupInfo(
            "google", "google", False,
            _words("google\\w*", "gmail\\w*", "mail\\w*", "e-?mails?", "inbox", "unread", "calendar\\w*", "events?",
                   "meetings?", "appointments?", "schedule\\w*", "agenda", "drive\\w*", "upload", "files?", "documents?",
                   "docs?", "remind(?:er)?s?", "youtube\\w*", "email\\w*", "xat\\w*", "maktub\\w*", "pocht\\w*",
                   "oxirgi.{0,40}(?:xabar\\w*|xat\\w*|email\\w*)",
                   "(?:xabar\\w*|xat\\w*|email\\w*).{0,40}oxirgi"),
        ),
    )
}

SOURCE_LABEL = {
    "system": "SYSTEM",
    "telegram": "TELEGRAM",
    "instagram": "INSTAGRAM",
    "google": "GOOGLE WORKSPACE",
}


def detect_groups(text: str) -> set[str]:
    """Tool families a user request is likely to need (always includes 'always' groups)."""
    return {g.name for g in GROUPS.values() if g.always or (g.pattern is not None and g.pattern.search(text))}


# ------------------------------------------------------------------ declaration
@dataclass(frozen=True)
class ToolMeta:
    group: str
    capability: str
    risk: RiskSpec
    summary: TextSpec | None
    activity: TextSpec | None
    timeout: float
    name: str | None
    description: str | None


def tool(
    *,
    group: str,
    capability: str,
    risk: RiskSpec = Risk.SAFE,
    summary: TextSpec | None = None,
    activity: TextSpec | None = None,
    timeout: float = 60.0,
    name: str | None = None,
    description: str | None = None,
) -> Callable[[Callable], Callable]:
    """Mark a method as an LLM-callable tool.

    group:       key of :data:`GROUPS` (decides when the tool is offered).
    capability:  ``system`` | ``telegram`` | ``instagram`` | ``google`` (GUI toggle that gates it).
    risk:        :class:`Risk` or ``callable(args) -> Risk`` for argument-dependent risk.
    summary:     text shown in the approval dialog (str with ``{arg}`` slots, or ``callable(args)``).
    activity:    text for the activity feed while running (same forms as ``summary``).
    timeout:     seconds before the call is abandoned.
    """

    def decorate(func: Callable) -> Callable:
        func.__jarvis_tool__ = ToolMeta(group, capability, risk, summary, activity, timeout, name, description)  # type: ignore[attr-defined]
        return func

    return decorate


# ----------------------------------------------------------------------- schema
_SCALARS = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _annotation_schema(annotation: Any) -> tuple[dict[str, Any], bool]:
    """Return ``(json_schema, nullable)`` for a type annotation."""
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        members = get_args(annotation)
        concrete = [m for m in members if m is not type(None)]
        nullable = len(concrete) != len(members)
        if len(concrete) == 1:
            return _annotation_schema(concrete[0])[0], nullable
        return {"anyOf": [_annotation_schema(m)[0] for m in concrete]}, nullable
    if origin is Literal:
        values = list(get_args(annotation))
        kind = "integer" if all(isinstance(v, int) and not isinstance(v, bool) for v in values) else "string"
        return {"type": kind, "enum": values}, False
    if origin in (list, tuple, set, frozenset, collections.abc.Sequence):
        args = get_args(annotation)
        item = _annotation_schema(args[0])[0] if args else {}
        return {"type": "array", "items": item}, False
    if origin is dict or annotation is dict:
        return {"type": "object"}, False
    if annotation in _SCALARS:
        return {"type": _SCALARS[annotation]}, False
    return {}, False


_ARGS_HEADER = re.compile(r"^(?:Args|Arguments|Parameters|Params)\s*:\s*$", re.IGNORECASE)
_OTHER_HEADER = re.compile(r"^(?:Returns?|Raises?|Yields?|Notes?|Examples?|See Also)\s*:\s*$", re.IGNORECASE)
_PARAM_LINE = re.compile(r"^\*{0,2}(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$")


def parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Split a docstring into (first paragraph, {param: help})."""
    summary: list[str] = []
    params: dict[str, str] = {}
    section, in_first, current = "summary", True, None
    for raw in doc.strip().splitlines():
        line = raw.strip()
        if _ARGS_HEADER.match(line):
            section, current = "args", None
            continue
        if _OTHER_HEADER.match(line):
            section, current = "other", None
            continue
        if section == "summary":
            if not line:
                in_first = False
            elif in_first:
                summary.append(line)
        elif section == "args" and line:
            match = _PARAM_LINE.match(line)
            if match:
                current = match.group(1)
                params[current] = match.group(2).strip()
            elif current:
                params[current] = f"{params[current]} {line}".strip()
    return " ".join(summary), params


def build_parameters(func: Callable) -> dict[str, Any]:
    """JSON schema (``type: object``) for ``func``'s parameters."""
    signature = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:  # unresolved forward reference: fall back to untyped strings
        hints = {}
    _, docs = parse_docstring(inspect.getdoc(func) or "")
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in signature.parameters.items():
        if pname in {"self", "cls"} or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        schema, nullable = _annotation_schema(hints.get(pname, str))
        schema = dict(schema) or {"type": "string"}
        if pname in docs:
            schema["description"] = docs[pname]
        properties[pname] = schema
        if param.default is inspect.Parameter.empty and not nullable:
            required.append(pname)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "…"


def _render(spec: TextSpec | None, args: dict[str, Any], fallback: str) -> str:
    if spec is None:
        return fallback
    try:
        if callable(spec):
            return str(spec(args))
        safe = _SafeDict({k: truncate(v, 80) if isinstance(v, str) else v for k, v in args.items()})
        return spec.format_map(safe)
    except Exception:
        return fallback


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    group: str
    capability: str
    risk: RiskSpec
    summary: TextSpec | None
    activity: TextSpec | None
    timeout: float
    is_async: bool

    def risk_for(self, args: dict[str, Any]) -> Risk:
        if isinstance(self.risk, Risk):
            return self.risk
        try:
            return Risk(self.risk(args))
        except Exception:
            log.exception("risk callback of %s failed; treating as CONFIRM", self.name)
            return Risk.CONFIRM

    def describe_call(self, args: dict[str, Any]) -> str:
        shown = ", ".join(f"{k}={truncate(repr(v), 80)}" for k, v in args.items())
        return _render(self.summary, args, f"{self.name}({shown})")

    def activity_text(self, args: dict[str, Any]) -> str:
        return _render(self.activity, args, f"Running {self.name.replace('_', ' ')}...")

    def gemini_definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def signature_line(self) -> str:
        """Compact one-line form used by the JSON intent-parsing fallback."""
        required = set(self.parameters.get("required", ()))
        parts = []
        for pname, schema in self.parameters.get("properties", {}).items():
            kind = schema.get("type", "any")
            if schema.get("enum"):
                kind = "|".join(str(v) for v in schema["enum"])
            parts.append(f"{pname}{'' if pname in required else '?'}: {kind}")
        return f"{self.name}({', '.join(parts)}) - {self.description}"


# --------------------------------------------------------------------- coercion
def _coerce_value(value: Any, schema: dict[str, Any], path: str) -> Any:
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                return _coerce_value(value, option, path)
            except ValueError:
                continue
        raise ValueError(f"'{path}' has an invalid value")
    kind = schema.get("type")
    enum = schema.get("enum")

    if kind == "integer":
        if isinstance(value, bool):
            raise ValueError(f"'{path}' must be an integer")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        elif isinstance(value, str) and re.fullmatch(r"\s*-?\d+(?:\.0+)?\s*", value):
            value = int(float(value))
        elif not isinstance(value, int):
            raise ValueError(f"'{path}' must be an integer")
    elif kind == "number":
        if isinstance(value, bool):
            raise ValueError(f"'{path}' must be a number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{path}' must be a number") from None
    elif kind == "boolean":
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "y", "on", "1"}:
                value = True
            elif lowered in {"false", "no", "n", "off", "0"}:
                value = False
            else:
                raise ValueError(f"'{path}' must be true or false")
        elif isinstance(value, (int, float)) and value in (0, 1):
            value = bool(value)
        elif not isinstance(value, bool):
            raise ValueError(f"'{path}' must be true or false")
    elif kind == "array":
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    value = [p.strip() for p in text.strip("[]").split(",") if p.strip()]
            else:
                value = [p.strip() for p in text.split(",") if p.strip()]
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        item_schema = schema.get("items") or {}
        value = [_coerce_value(v, item_schema, f"{path}[]") for v in value]
    elif kind == "object":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError(f"'{path}' must be an object") from None
        if not isinstance(value, dict):
            raise ValueError(f"'{path}' must be an object")
    else:  # string / untyped
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        elif not isinstance(value, str):
            value = str(value)

    if enum:
        for option in enum:
            if str(option).strip().lower() == str(value).strip().lower():
                return option
        raise ValueError(f"'{path}' must be one of: {', '.join(str(o) for o in enum)}")
    return value


def coerce_arguments(spec: ToolSpec, raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate/convert model-supplied arguments; returns ``(args, errors)``."""
    if raw is None:
        raw = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}, ["arguments must be a JSON object"]
    if not isinstance(raw, dict):
        return {}, ["arguments must be a JSON object"]

    properties: dict[str, Any] = spec.parameters.get("properties", {})
    args: dict[str, Any] = {}
    errors: list[str] = []
    for pname, schema in properties.items():
        if raw.get(pname) is None:
            continue
        try:
            args[pname] = _coerce_value(raw[pname], schema, pname)
        except ValueError as exc:
            errors.append(str(exc))
    failed = " ".join(errors)
    for pname in spec.parameters.get("required", ()):
        if pname not in args and f"'{pname}'" not in failed:
            errors.append(f"missing required argument '{pname}'")
    return args, errors


# ----------------------------------------------------------------------- output
def _shrink(obj: Any, str_limit: int, list_limit: int) -> Any:
    if isinstance(obj, str):
        return truncate(obj, str_limit)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _shrink(v, str_limit, list_limit) for k, v in list(obj.items())[:60]}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = list(obj)
        shrunk = [_shrink(v, str_limit, list_limit) for v in items[:list_limit]]
        if len(items) > list_limit:
            shrunk.append(f"... {len(items) - list_limit} more")
        return shrunk
    return truncate(str(obj), str_limit)


def fit_json(obj: Any, limit: int) -> str:
    """Serialise ``obj`` to JSON that fits ``limit`` characters (shrinking strings/lists first)."""
    text = ""
    for str_limit, list_limit in ((limit, 40), (limit // 3, 25), (400, 15), (200, 10), (100, 6)):
        text = json.dumps(_shrink(obj, max(str_limit, 60), list_limit), ensure_ascii=False, default=str)
        if len(text) <= limit:
            return text
    return truncate(text, limit)


@dataclass
class ToolOutcome:
    name: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0
    denied: bool = False

    @property
    def error(self) -> str:
        return str(self.data.get("error", "")) if not self.ok else ""

    def to_message(self, limit: int) -> str:
        """JSON string handed back to the model as the tool result."""
        return fit_json(self.data, limit)

    @classmethod
    def failure(cls, name: str, error: str, elapsed: float = 0.0, **extra: Any) -> "ToolOutcome":
        return cls(name, False, {"ok": False, "error": error, **extra}, elapsed)


def _normalise(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        data = dict(result)
        data.setdefault("ok", True)
        return data
    if result is None:
        return {"ok": True}
    return {"ok": True, "result": result}


# --------------------------------------------------------------------- registry
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {spec.name}")
        if spec.group not in GROUPS:
            raise ValueError(f"tool {spec.name}: unknown group {spec.group!r}")
        self._tools[spec.name] = spec

    def register_instance(self, instance: object) -> list[ToolSpec]:
        """Register every ``@tool``-decorated method of ``instance``."""
        found: list[ToolSpec] = []
        cls = type(instance)
        for attr in dir(cls):
            raw = inspect.getattr_static(cls, attr, None)
            meta: ToolMeta | None = getattr(raw, "__jarvis_tool__", None)
            if meta is None:
                continue
            bound = getattr(instance, attr)
            summary, _ = parse_docstring(inspect.getdoc(raw) or "")
            spec = ToolSpec(
                name=meta.name or attr,
                description=meta.description or summary or attr.replace("_", " "),
                parameters=build_parameters(raw),
                func=bound,
                group=meta.group,
                capability=meta.capability,
                risk=meta.risk,
                summary=meta.summary,
                activity=meta.activity,
                timeout=meta.timeout,
                is_async=inspect.iscoroutinefunction(bound),
            )
            self.register(spec)
            found.append(spec)
        return found

    def select(self, enabled: Collection[str], groups: Collection[str] | None = None) -> list[ToolSpec]:
        """Tools whose capability is enabled and (if ``groups`` is given) whose group is selected."""
        return [
            s
            for s in self._tools.values()
            if s.capability in enabled and (groups is None or s.group in groups)
        ]

    @staticmethod
    def to_gemini(specs: Collection[ToolSpec]) -> list[dict[str, Any]]:
        return [s.gemini_definition() for s in specs]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "group": s.group, "capability": s.capability, "params": list(s.parameters.get("properties", {}))}
            for s in self._tools.values()
        ]

    # ------------------------------------------------------------- execution
    async def invoke(
        self,
        name: str,
        raw_args: Any,
        *,
        interaction: Interaction,
        settings: Settings,
        enabled: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> ToolOutcome:
        """Run one tool call end to end. Never raises (except ``CancelledError``)."""
        spec = self._tools.get(name)
        if spec is None:
            hint = difflib.get_close_matches(name, self._tools, n=1)
            suffix = f" Did you mean '{hint[0]}'?" if hint else ""
            return ToolOutcome.failure(name, f"unknown tool '{name}'.{suffix}")
        if allowed_tools is not None and name not in allowed_tools:
            return ToolOutcome.failure(name, "this tool was not offered for the current request")
        if enabled is not None and spec.capability not in enabled:
            return ToolOutcome.failure(name, f"the {spec.capability} capability is switched off or unavailable")

        args, errors = coerce_arguments(spec, raw_args)
        if errors:
            return ToolOutcome.failure(name, "; ".join(errors), expected=spec.parameters.get("properties", {}))

        source = SOURCE_LABEL.get(spec.capability, spec.capability.upper())
        if spec.risk_for(args) is Risk.CONFIRM and settings.safety.confirm_risky:
            details = spec.describe_call(args)
            activity(source, f"Awaiting approval: {details}", "warn")
            try:
                approved = await asyncio.to_thread(
                    interaction.confirm, "Approval required", details, timeout=settings.safety.confirm_timeout
                )
            except Exception:
                log.exception("confirmation dialog failed; denying %s", name)
                approved = False
            if not approved:
                activity(source, "Action declined by the user.", "warn")
                outcome = ToolOutcome.failure(
                    name,
                    "The user declined this action. Do not retry it; acknowledge and ask what they want instead.",
                )
                outcome.denied = True
                outcome.data["denied"] = True
                return outcome
            activity(source, "Approved.", "ok")

        label = spec.activity_text(args)
        activity("EXECUTING", label, "action")
        log.info("tool %s args=%s", name, truncate(json.dumps(args, ensure_ascii=False, default=str), 300))
        started = time.perf_counter()
        owner = f"tool:{name}:{uuid.uuid4().hex[:6]}"
        try:
            with flag("executing", owner, detail=label):
                result = await self._call(spec, args)
        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - started
            activity(source, f"{name} timed out after {elapsed:.0f}s", "error")
            return ToolOutcome.failure(
                name, f"timed out after {spec.timeout:.0f}s; the action may still finish in the background", elapsed
            )
        except ToolError as exc:
            elapsed = time.perf_counter() - started
            activity(source, f"{name}: {exc}", "warn")
            return ToolOutcome.failure(name, str(exc), elapsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed = time.perf_counter() - started
            log.exception("tool %s crashed", name)
            activity(source, f"{name} failed: {describe_exception(exc)}", "error")
            return ToolOutcome.failure(name, describe_exception(exc), elapsed)

        elapsed = time.perf_counter() - started
        data = _normalise(result)
        ok = bool(data.get("ok", True))
        if ok:
            activity(source, f"{name} completed in {elapsed:.1f}s", "ok")
        else:
            activity(source, f"{name} failed: {truncate(data.get('error', 'unknown error'), 160)}", "warn")
        return ToolOutcome(name, ok, data, elapsed)

    @staticmethod
    async def _call(spec: ToolSpec, args: dict[str, Any]) -> Any:
        if spec.is_async:
            return await asyncio.wait_for(spec.func(**args), timeout=spec.timeout)
        return await asyncio.wait_for(asyncio.to_thread(spec.func, **args), timeout=spec.timeout)
