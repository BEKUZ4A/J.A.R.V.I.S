"""Configuration loader for J.A.R.V.I.S.

``.env`` is read once, at import time. Every setting is exposed through frozen
dataclasses so the rest of the code base never touches ``os.environ`` directly.
Invalid or missing values fall back to safe defaults and are recorded in
``Settings.warnings`` instead of crashing start-up.

    from config import settings
    settings.gemini.model
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "J.A.R.V.I.S"
APP_VERSION = "1.0.0"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


def clean_gemini_model(value: str | None) -> str:
    """Remove surrounding quotes/whitespace and repeated SDK resource prefixes."""
    model = (value or "").strip().strip("\"'").strip()
    while model.casefold().startswith("models/"):
        model = model[len("models/"):].strip().strip("\"'").strip()
    return model or DEFAULT_GEMINI_MODEL


BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ENV_FILE = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
MODEL_DIR = DATA_DIR / "models"
UI_DIR = RESOURCE_DIR / "ui"
STYLESHEET = UI_DIR / "styles.qss"

# Workspace and YouTube use separate OAuth grants/tokens. gmail.send is included
# because gmail.modify alone does not authorize sending messages.
WORKSPACE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file",
)
YOUTUBE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/youtube.force-ssl",
)

_LANGUAGE_NAMES = {
    "en": "English", "tr": "Turkish", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "ru": "Russian",
    "uk": "Ukrainian", "ar": "Arabic", "fa": "Persian", "hi": "Hindi", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "sv": "Swedish", "az": "Azerbaijani", "ur": "Urdu",
    "uz": "Uzbek",
}

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


class _Env:
    """Typed accessors over ``os.environ`` that record problems instead of raising."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def text(self, name: str, default: str = "") -> str:
        value = os.environ.get(name)
        if value is None:
            return default
        value = value.strip()
        return value or default

    def secret(self, name: str) -> str:
        """Secrets are returned verbatim (a password may legitimately end in a space)."""
        return os.environ.get(name, "")

    def boolean(self, name: str, default: bool) -> bool:
        raw = self.text(name).lower()
        if not raw:
            return default
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
        self.warnings.append(f"{name}={raw!r} is not a boolean; using {default}.")
        return default

    def integer(self, name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.warnings.append(f"{name}={raw!r} is not an integer; using {default}.")
            return default
        return self._clamp(name, value, default, lo, hi)

    def number(self, name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.warnings.append(f"{name}={raw!r} is not a number; using {default}.")
            return default
        return self._clamp(name, value, default, lo, hi)

    def items(self, name: str) -> tuple[str, ...]:
        raw = self.text(name)
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    def path(self, name: str, default: str) -> Path:
        raw = self.text(name, default)
        candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
        return candidate if candidate.is_absolute() else BASE_DIR / candidate

    def choice(self, name: str, default: str, allowed: tuple[str, ...]) -> str:
        raw = self.text(name, default).lower()
        if raw in allowed:
            return raw
        self.warnings.append(f"{name}={raw!r} must be one of {', '.join(allowed)}; using {default!r}.")
        return default

    def pattern(self, name: str, default: str, regex: str) -> str:
        raw = self.text(name, default)
        if re.fullmatch(regex, raw):
            return raw
        self.warnings.append(f"{name}={raw!r} has an invalid format; using {default!r}.")
        return default

    def _clamp(self, name, value, default, lo, hi):
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            self.warnings.append(f"{name}={value} is outside [{lo}, {hi}]; using {default}.")
            return default
        return value


@dataclass(frozen=True)
class GeminiSettings:
    api_key: str = field(repr=False)
    model: str
    temperature: float
    timeout: float
    tool_selection: str  # "auto" | "all"
    max_tool_rounds: int
    history_turns: int


@dataclass(frozen=True)
class TelegramSettings:
    mode: str  # requested: auto | userbot | bot | both | off
    api_id: int | None = field(repr=False)
    api_hash: str = field(repr=False)
    phone: str = field(repr=False)
    bot_token: str = field(repr=False)
    admin_id: int | None
    default_channel: str
    notify_incoming: bool
    auto_reply_users: tuple[str, ...]
    user_session: Path
    bot_session: Path
    clients: tuple[str, ...]  # resolved: any of "user", "bot"

    @property
    def configured(self) -> bool:
        return bool(self.clients)


@dataclass(frozen=True)
class InstagramSettings:
    username: str
    password: str = field(repr=False)
    totp_seed: str = field(repr=False)
    proxy: str = field(repr=False)
    session_file: Path

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)


@dataclass(frozen=True)
class GoogleSettings:
    credentials_file: Path
    workspace_token_file: Path
    youtube_token_file: Path
    workspace_scopes: tuple[str, ...]
    youtube_scopes: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return (
            self.credentials_file.is_file()
            or self.workspace_token_file.is_file()
            or self.youtube_token_file.is_file()
        )


@dataclass(frozen=True)
class VoiceSettings:
    enabled: bool
    wake_word: str
    wake_aliases: tuple[str, ...]
    language: str  # Whisper code or "auto"
    whisper_model: str
    wake_model: str
    device: str
    compute_type: str
    model_dir: Path
    tts_voice: str
    tts_rate: str
    tts_volume: str
    tts_pitch: str
    speak_replies: bool
    wake_chime: bool
    followup_seconds: float
    input_device: str | int | None
    output_device: str | int | None
    vad_silence_ms: int
    vad_min_rms: float
    min_speech_ms: int
    max_command_seconds: float
    command_wait_seconds: float


@dataclass(frozen=True)
class SafetySettings:
    confirm_risky: bool
    confirm_timeout: float
    max_tool_result_chars: int


@dataclass(frozen=True)
class PersonaSettings:
    user_name: str
    user_title: str
    reply_language: str


@dataclass(frozen=True)
class UiSettings:
    minimize_to_tray: bool
    fps: int


@dataclass(frozen=True)
class Settings:
    gemini: GeminiSettings
    telegram: TelegramSettings
    instagram: InstagramSettings
    google: GoogleSettings
    voice: VoiceSettings
    safety: SafetySettings
    persona: PersonaSettings
    ui: UiSettings
    log_level: str
    warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------------ secrets
    def secrets(self) -> tuple[str, ...]:
        """Every secret value, used to scrub log lines."""
        candidates = [
            self.telegram.api_hash,
            self.telegram.bot_token,
            self.gemini.api_key,
            self.instagram.password,
            self.instagram.totp_seed,
            self.instagram.proxy,
        ]
        return tuple(c for c in candidates if c and len(c) >= 4)

    def redact(self, text: str) -> str:
        for secret in self.secrets():
            text = text.replace(secret, "***")
        return text

    # ------------------------------------------------------------- diagnostics
    def service_status(self) -> dict[str, tuple[bool, str]]:
        """capability key -> (configured, human readable detail). Never includes secrets."""
        tg = self.telegram
        if tg.configured:
            telegram = (True, "+".join(tg.clients))
        elif tg.mode == "off":
            telegram = (False, "disabled (TELEGRAM_MODE=off)")
        else:
            missing = [
                name
                for name, value in (
                    ("TELEGRAM_API_ID", tg.api_id),
                    ("TELEGRAM_API_HASH", tg.api_hash),
                )
                if not value
            ]
            if not (tg.phone or tg.bot_token):
                missing.append("TELEGRAM_PHONE_NUMBER or TELEGRAM_BOT_TOKEN")
            telegram = (False, "missing " + ", ".join(missing))

        ig = self.instagram
        if ig.configured:
            instagram = (True, f"@{ig.username}")
        else:
            missing = [n for n, v in (("INSTAGRAM_USERNAME", ig.username), ("INSTAGRAM_PASSWORD", ig.password)) if not v]
            instagram = (False, "missing " + ", ".join(missing))

        gg = self.google
        if gg.configured:
            token_names = ", ".join(
                token.name for token in (gg.workspace_token_file, gg.youtube_token_file) if token.is_file()
            )
            google = (
                True,
                gg.credentials_file.name if gg.credentials_file.is_file() else token_names,
            )
        else:
            google = (False, f"place OAuth client file at {gg.credentials_file}")

        return {
            "gemini": (bool(self.gemini.api_key), self.gemini.model if self.gemini.api_key else "missing GEMINI_API_KEY"),
            "system": (True, "Windows control"),
            "telegram": telegram,
            "instagram": instagram,
            "google": google,
            "voice": (self.voice.enabled, f"wake word '{self.voice.wake_word}', STT {self.voice.whisper_model}, TTS {self.voice.tts_voice}"
                      if self.voice.enabled else "disabled (VOICE_ENABLED=false)"),
        }


def _telegram_clients(mode: str, api_ok: bool, phone: str, token: str, warn: list[str]) -> tuple[str, ...]:
    user_ready = api_ok and bool(phone)
    bot_ready = api_ok and bool(token)
    if mode == "off":
        return ()
    wanted = {
        "auto": ("user", "bot"),
        "userbot": ("user",),
        "bot": ("bot",),
        "both": ("user", "bot"),
    }[mode]
    clients = tuple(c for c in wanted if (c == "user" and user_ready) or (c == "bot" and bot_ready))
    if mode in {"userbot", "bot", "both"} and len(clients) < len(wanted):
        warn.append(f"TELEGRAM_MODE={mode} but the required credentials are incomplete.")
    return clients


def _normalise_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits and not digits.startswith("+"):
        digits = "+" + digits
    return digits


def _audio_device(raw: str) -> str | int | None:
    if not raw:
        return None
    return int(raw) if raw.isdigit() else raw


def load_settings(env_file: Path | None = ENV_FILE, override: bool = False) -> Settings:
    """Read ``.env`` (when present) and build an immutable :class:`Settings`."""
    if env_file and env_file.is_file():
        load_dotenv(env_file, override=override)
    env = _Env()

    # ---- Gemini ----------------------------------------------------------------
    gemini = GeminiSettings(
        api_key=env.secret("GEMINI_API_KEY").strip(),
        model=clean_gemini_model(env.text("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)),
        temperature=env.number("GEMINI_TEMPERATURE", 0.2, 0.0, 2.0),
        timeout=env.number("GEMINI_TIMEOUT", 180.0, 5.0, 3600.0),
        tool_selection=env.choice("TOOL_SELECTION", "auto", ("auto", "all")),
        max_tool_rounds=env.integer("GEMINI_MAX_TOOL_ROUNDS", 6, 1, 20),
        history_turns=env.integer("HISTORY_TURNS", 50, 1, 50),
    )

    # ---- Telegram --------------------------------------------------------------
    raw_api_id = env.text("TELEGRAM_API_ID")
    api_id: int | None = None
    if raw_api_id:
        if raw_api_id.isdigit():
            api_id = int(raw_api_id)
        else:
            env.warnings.append("TELEGRAM_API_ID must be a number (see my.telegram.org).")
    api_hash = env.text("TELEGRAM_API_HASH")
    phone = _normalise_phone(env.text("TELEGRAM_PHONE_NUMBER"))
    bot_token = env.text("TELEGRAM_BOT_TOKEN")
    raw_admin_id = env.text("TELEGRAM_ADMIN_ID")
    parsed_admin_id = env.integer("TELEGRAM_ADMIN_ID", 0, 1, 9_223_372_036_854_775_807) if raw_admin_id else 0
    admin_id = parsed_admin_id or None
    tg_mode = env.choice("TELEGRAM_MODE", "auto", ("auto", "userbot", "bot", "both", "off"))
    auto_reply_users = tuple(
        user.strip().lstrip("@").casefold()
        for user in env.text("TELEGRAM_AUTO_REPLY_USERS", "Euro_Elektromontaj").split(",")
        if user.strip()
    )
    telegram = TelegramSettings(
        mode=tg_mode,
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        bot_token=bot_token,
        admin_id=admin_id,
        default_channel=env.text("TELEGRAM_DEFAULT_CHANNEL"),
        notify_incoming=env.boolean("TELEGRAM_NOTIFY_INCOMING", True),
        auto_reply_users=auto_reply_users,
        # Telethon appends ".session" itself, so these are extension-less.
        user_session=DATA_DIR / "telegram_user",
        bot_session=DATA_DIR / "telegram_bot",
        clients=_telegram_clients(tg_mode, api_id is not None and bool(api_hash), phone, bot_token, env.warnings),
    )

    # ---- Instagram -------------------------------------------------------------
    instagram = InstagramSettings(
        username=env.text("INSTAGRAM_USERNAME").lstrip("@"),
        password=env.secret("INSTAGRAM_PASSWORD"),
        totp_seed=re.sub(r"\s+", "", env.secret("INSTAGRAM_2FA_SEED")),
        proxy=env.text("INSTAGRAM_PROXY"),
        session_file=env.path("INSTAGRAM_SESSION_FILE", "data/insta_session.json"),
    )

    # ---- Google ----------------------------------------------------------------
    google = GoogleSettings(
        credentials_file=env.path("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        workspace_token_file=env.path("GOOGLE_WORKSPACE_TOKEN_FILE", "data/workspace_token.json"),
        youtube_token_file=env.path("GOOGLE_YOUTUBE_TOKEN_FILE", "data/youtube_token.json"),
        workspace_scopes=WORKSPACE_SCOPES,
        youtube_scopes=YOUTUBE_SCOPES,
    )

    # ---- Voice -----------------------------------------------------------------
    language = env.text("SPEECH_LANGUAGE", "en").lower()
    device = env.choice("WHISPER_DEVICE", "cpu", ("cpu", "cuda", "auto"))
    if device == "cuda":
        env.warnings.append("WHISPER_DEVICE=cuda may reduce available VRAM for speech recognition.")
    voice = VoiceSettings(
        enabled=env.boolean("VOICE_ENABLED", True),
        wake_word=env.text("WAKE_WORD", "jarvis").lower(),
        wake_aliases=tuple(a.lower() for a in env.items("WAKE_WORD_ALIASES")),
        language=language,
        whisper_model=env.text("WHISPER_MODEL", "small"),
        wake_model=env.text("WHISPER_WAKE_MODEL", "base"),
        device=device,
        compute_type=env.text("WHISPER_COMPUTE_TYPE", "int8"),
        model_dir=MODEL_DIR,
        tts_voice=env.text("TTS_VOICE", "en-GB-RyanNeural"),
        tts_rate=env.pattern("TTS_RATE", "+0%", r"[+-]\d{1,3}%"),
        tts_volume=env.pattern("TTS_VOLUME", "+0%", r"[+-]\d{1,3}%"),
        tts_pitch=env.pattern("TTS_PITCH", "+0Hz", r"[+-]\d{1,3}Hz"),
        speak_replies=env.boolean("SPEAK_REPLIES", True),
        wake_chime=env.boolean("WAKE_CHIME", True),
        followup_seconds=env.number("FOLLOWUP_SECONDS", 8.0, 0.0, 60.0),
        input_device=_audio_device(env.text("AUDIO_INPUT_DEVICE")),
        output_device=_audio_device(env.text("AUDIO_OUTPUT_DEVICE")),
        vad_silence_ms=env.integer("VAD_SILENCE_MS", 700, 200, 3000),
        vad_min_rms=env.number("VAD_MIN_RMS", 0.004, 0.0005, 0.2),
        min_speech_ms=env.integer("VAD_MIN_SPEECH_MS", 250, 100, 2000),
        max_command_seconds=env.number("MAX_COMMAND_SECONDS", 20.0, 3.0, 120.0),
        command_wait_seconds=env.number("COMMAND_WAIT_SECONDS", 8.0, 2.0, 60.0),
    )

    # ---- Safety / persona / UI -------------------------------------------------
    safety = SafetySettings(
        confirm_risky=env.boolean("CONFIRM_RISKY_ACTIONS", True),
        confirm_timeout=env.number("CONFIRM_TIMEOUT_SECONDS", 60.0, 5.0, 600.0),
        max_tool_result_chars=env.integer("MAX_TOOL_RESULT_CHARS", 3500, 500, 20000),
    )
    persona = PersonaSettings(
        user_name=env.text("USER_NAME"),
        user_title=env.text("USER_TITLE"),
        reply_language="the user's language" if language == "auto" else _LANGUAGE_NAMES.get(language, language),
    )
    ui = UiSettings(
        minimize_to_tray=env.boolean("MINIMIZE_TO_TRAY", True),
        fps=env.integer("UI_FPS", 30, 10, 144),
    )

    return Settings(
        gemini=gemini,
        telegram=telegram,
        instagram=instagram,
        google=google,
        voice=voice,
        safety=safety,
        persona=persona,
        ui=ui,
        log_level=env.choice("LOG_LEVEL", "info", ("debug", "info", "warning", "error")).upper(),
        warnings=tuple(env.warnings),
    )


def ensure_directories() -> None:
    """Create the runtime folders (idempotent)."""
    for directory in (DATA_DIR, LOG_DIR, SCREENSHOT_DIR, MODEL_DIR):
        directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


settings: Settings = get_settings()
