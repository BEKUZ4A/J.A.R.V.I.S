"""Speech: Whisper speech-to-text, wake-word spotting and Edge-TTS playback.

Input path  : sounddevice mic -> energy VAD segmenter -> faster-whisper (CPU, int8) -> wake word / command.
Output path : text -> sentence chunks -> Edge-TTS (MP3) -> miniaudio decode -> sounddevice playback,
              with Windows SAPI as an offline fallback. While Jarvis speaks the mic is gated so it never
              hears itself.

Nothing here imports Qt: threads are plain ``threading`` objects, wrapped in QThreads by ``core.runtime``.
"""
from __future__ import annotations

import asyncio
import collections
import difflib
import logging
import math
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from config import VoiceSettings
from core.events import AUDIO_LEVELS, activity, bus, set_flag
from core.util import describe_exception, truncate

log = logging.getLogger("jarvis.speech")

MIC_RATE = 16_000
TTS_RATE = 24_000
FRAME_MS = 30
FRAME_LEN = MIC_RATE * FRAME_MS // 1000  # 480 samples
POST_SPEECH_GUARD = 0.45  # seconds the mic stays muted after Jarvis stops talking


# ------------------------------------------------------------------- helpers
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️‍]")


def speech_text(text: str) -> str:
    """Strip markdown, URLs and emoji so the voice doesn't read symbols aloud."""
    text = re.sub(r"```.*?```", " code block ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"^\s*[-•*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_#>~|]+", " ", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


_HANN: dict[int, np.ndarray] = {}


def spectrum_levels(samples: np.ndarray, rate: int, bands: int = 48) -> list[float]:
    """Log-spaced spectrum bands (0..1) of a short audio block, for the HUD visualiser."""
    n = len(samples)
    if n < 64:
        return [0.0] * bands
    data = samples.astype(np.float32)
    if samples.dtype == np.int16:
        data /= 32768.0
    window = _HANN.get(n)
    if window is None:
        window = _HANN.setdefault(n, np.hanning(n).astype(np.float32))
    spectrum = np.abs(np.fft.rfft(data * window)) / (n / 2.0)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    edges = np.geomspace(80.0, min(7500.0, rate / 2.0 - 100.0), bands + 1)
    index = np.searchsorted(freqs, edges)
    levels: list[float] = []
    for lo, hi in zip(index[:-1], index[1:]):
        hi = max(int(hi), int(lo) + 1)
        magnitude = float(spectrum[lo:hi].mean()) if lo < len(spectrum) else 0.0
        decibels = 20.0 * math.log10(magnitude + 1e-9)
        levels.append(min(1.0, max(0.0, (decibels + 72.0) / 48.0)))
    return levels


class SentenceChunker:
    """Turns a token stream into speakable sentences (the first one is released as early as possible)."""

    _BOUNDARY = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")

    def __init__(self, min_len: int = 24) -> None:
        self._buf = ""
        self._first = True
        self._min_len = min_len

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while True:
            match = self._BOUNDARY.search(self._buf)
            if not match:
                if len(self._buf) > 220:  # a long run-on: split at the last comma / space
                    cut = max(self._buf.rfind(", "), self._buf.rfind(" "))
                    if cut > 60:
                        out.append(self._buf[:cut].strip())
                        self._buf = self._buf[cut + 1:]
                        self._first = False
                        continue
                break
            end = match.end()
            chunk = self._buf[:end].strip()
            if len(chunk) < self._min_len and not self._first:  # "Dr." / "e.g." style fragments: merge forward
                following = self._BOUNDARY.search(self._buf, end)
                if not following:
                    break
                end = following.end()
                chunk = self._buf[:end].strip()
            self._buf = self._buf[end:]
            if chunk:
                out.append(chunk)
                self._first = False
        return out

    def flush(self) -> str:
        tail, self._buf = self._buf.strip(), ""
        self._first = True
        return tail


# -------------------------------------------------------------- wake word
def _compact(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower(), flags=re.UNICODE)


_BUILTIN_ALIASES = {"jarvis": ("jarvas", "jervis", "jarvice", "jarvus", "jarves", "jarviz", "gervis", "jarvish", "jarvi")}


class WakeWordMatcher:
    """Finds the wake word in the first few words of a transcript and returns what follows it."""

    def __init__(self, word: str, aliases: tuple[str, ...] = ()) -> None:
        self.word = _compact(word) or "jarvis"
        self.words_in_phrase = max(1, len(word.split()))
        extra = _BUILTIN_ALIASES.get(self.word, ())
        self.variants = {self.word, *(_compact(a) for a in aliases if a), *extra}

    def _is_wake(self, candidate: str) -> bool:
        if candidate in self.variants:
            return True
        return (
            len(candidate) >= 4
            and candidate[0] == self.word[0]
            and difflib.SequenceMatcher(None, candidate, self.word).ratio() >= 0.82
        )

    def match(self, text: str) -> tuple[bool, str]:
        tokens = list(re.finditer(r"[\w']+", text, flags=re.UNICODE))
        for start in range(min(3, len(tokens))):  # "Hey Jarvis ..." - but not "I was talking to Jarvis ..."
            for size in (self.words_in_phrase, self.words_in_phrase + 1):
                window = tokens[start : start + size]
                if len(window) < size:
                    continue
                if self._is_wake("".join(_compact(t.group()) for t in window)):
                    rest = text[window[-1].end():].lstrip(" ,.:;!?-–—\t\r\n").strip()
                    return True, rest
        return False, ""


# ----------------------------------------------------------- speech to text
@dataclass
class Transcript:
    text: str = ""
    language: str = ""
    confidence: float = 0.0
    no_speech: float = 0.0
    duration: float = 0.0

    @property
    def is_noise(self) -> bool:
        if not self.text:
            return True
        if self.no_speech > 0.6 and self.confidence < 0.5:
            return True
        phrase = re.sub(r"[^\w ]", "", self.text.lower()).strip()
        return self.confidence < 0.7 and phrase in _HALLUCINATIONS


_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "you", "bye", "thanks", "the end",
    "subtitles by the amaraorg community", "please subscribe",
}
COMMAND_PROMPT = "Voice commands for a computer assistant: open, close, volume, brightness, Telegram, Instagram, Gmail, calendar, Drive."


class SpeechToText:
    """faster-whisper wrapper with lazily loaded, shared model instances."""

    def __init__(self, cfg: VoiceSettings) -> None:
        self.cfg = cfg
        self._models: dict[str, object] = {}
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()  # one transcription at a time keeps CPU usage predictable

    def model(self, name: str):
        with self._load_lock:
            if name not in self._models:
                os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
                from faster_whisper import WhisperModel

                cached = any(self.cfg.model_dir.glob(f"*{name.replace('/', '--')}*")) if self.cfg.model_dir.exists() else False
                activity("VOICE", f"Loading Whisper '{name}' ({self.cfg.device}/{self.cfg.compute_type})"
                         + ("" if cached else " - first run downloads the model, please wait"), "info")
                started = time.perf_counter()
                self.cfg.model_dir.mkdir(parents=True, exist_ok=True)
                self._models[name] = WhisperModel(
                    name, device=self.cfg.device, compute_type=self.cfg.compute_type,
                    download_root=str(self.cfg.model_dir), cpu_threads=4,
                )
                activity("VOICE", f"Whisper '{name}' ready in {time.perf_counter() - started:.1f}s", "ok")
            return self._models[name]

    def preload(self) -> None:
        for name in dict.fromkeys((self.cfg.wake_model, self.cfg.whisper_model)):
            self.model(name)

    def transcribe(self, audio: np.ndarray, *, wake: bool = False, prompt: str | None = None) -> Transcript:
        model = self.model(self.cfg.wake_model if wake else self.cfg.whisper_model)
        language = None if self.cfg.language == "auto" else self.cfg.language
        started = time.perf_counter()
        with self._run_lock:
            segments, info = model.transcribe(  # type: ignore[attr-defined]
                audio.astype(np.float32), language=language, beam_size=1 if wake else 3, best_of=1, temperature=0.0,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 150},
                condition_on_previous_text=False, initial_prompt=prompt, without_timestamps=True,
                no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
            )
            parts, logprobs, silences = [], [], []
            for segment in segments:  # generator: must be consumed while holding the lock
                parts.append(segment.text.strip())
                logprobs.append(segment.avg_logprob)
                silences.append(segment.no_speech_prob)
        text = " ".join(p for p in parts if p).strip()
        confidence = math.exp(sum(logprobs) / len(logprobs)) if logprobs else 0.0
        result = Transcript(text, getattr(info, "language", language or ""), confidence, max(silences, default=1.0),
                            len(audio) / MIC_RATE)
        log.debug("stt(%s) %.1fs audio in %.2fs -> %r (conf %.2f)", "wake" if wake else "cmd", result.duration,
                  time.perf_counter() - started, truncate(text, 80), confidence)
        return result

    def transcribe_file(self, audio_path: str | os.PathLike[str], *, prompt: str | None = None) -> Transcript:
        """Transcribe an audio file supported by faster-whisper/FFmpeg."""
        model = self.model(self.cfg.whisper_model)
        language = None if self.cfg.language == "auto" else self.cfg.language
        started = time.perf_counter()
        with self._run_lock:
            segments, info = model.transcribe(  # type: ignore[attr-defined]
                str(audio_path),
                language=language,
                beam_size=3,
                best_of=1,
                temperature=0.0,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 150},
                condition_on_previous_text=False,
                initial_prompt=prompt,
                without_timestamps=True,
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
            )
            parts, logprobs, silences = [], [], []
            for segment in segments:
                parts.append(segment.text.strip())
                logprobs.append(segment.avg_logprob)
                silences.append(segment.no_speech_prob)
        text = " ".join(part for part in parts if part).strip()
        confidence = math.exp(sum(logprobs) / len(logprobs)) if logprobs else 0.0
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        result = Transcript(
            text,
            getattr(info, "language", language or ""),
            confidence,
            max(silences, default=1.0),
            duration,
        )
        log.debug(
            "stt(file) %.1fs audio in %.2fs -> %r (conf %.2f)",
            duration,
            time.perf_counter() - started,
            truncate(text, 80),
            confidence,
        )
        return result


# ---------------------------------------------------------------- microphone
def _resolve_device(spec: str | int | None, *, output: bool) -> int | None:
    if spec is None:
        return None
    import sounddevice as sd

    if isinstance(spec, int):
        return spec
    key = "max_output_channels" if output else "max_input_channels"
    needle = spec.lower()
    for index, device in enumerate(sd.query_devices()):
        if device[key] > 0 and needle in str(device["name"]).lower():
            return index
    activity("VOICE", f"Audio device '{spec}' not found - using the system default", "warn")
    return None


class MicrophoneStream:
    """16 kHz mono float32 frames of FRAME_LEN samples, read from a PortAudio callback queue."""

    def __init__(self, device: str | int | None) -> None:
        self._device = device
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._stream = None
        self._ratio = 1.0

    def _callback(self, indata, frames, time_info, status) -> None:
        block = indata[:, 0]
        if self._ratio != 1.0:  # device could not open at 16 kHz: crude but adequate resample for speech
            target = FRAME_LEN
            block = np.interp(np.linspace(0, len(block) - 1, target), np.arange(len(block)), block)
        try:
            self._queue.put_nowait(np.asarray(block, dtype=np.float32).copy())
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(np.asarray(block, dtype=np.float32).copy())
            except (queue.Empty, queue.Full):
                pass

    def start(self) -> None:
        import sounddevice as sd

        device = _resolve_device(self._device, output=False)
        try:
            self._stream = sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32", blocksize=FRAME_LEN,
                                          device=device, callback=self._callback)
        except sd.PortAudioError:
            info = sd.query_devices(device, "input")
            rate = int(info["default_samplerate"])
            self._ratio = rate / MIC_RATE
            self._stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                                          blocksize=int(round(FRAME_LEN * self._ratio)), device=device,
                                          callback=self._callback)
        self._stream.start()

    def read(self, timeout: float = 0.25) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class UtteranceSegmenter:
    """Energy VAD with an adaptive noise floor, hysteresis and pre-roll."""

    def __init__(self, silence_ms: int, min_speech_ms: int, max_seconds: float, min_rms: float, preroll_ms: int = 300) -> None:
        self._silence_frames = max(1, silence_ms // FRAME_MS)
        self._min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self._max_frames = int(max_seconds * 1000 / FRAME_MS)
        self._preroll_frames = max(1, preroll_ms // FRAME_MS)
        self._min_rms = min_rms
        self._noise = min_rms / 3.0
        self.reset()

    def reset(self) -> None:
        self._pre: collections.deque[np.ndarray] = collections.deque(maxlen=self._preroll_frames)
        self._frames: list[np.ndarray] = []
        self._active = False
        self._voiced_run = 0
        self._speech_frames = 0
        self._silence = 0

    @property
    def in_speech(self) -> bool:
        return self._active

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        """Returns the finished utterance (float32 mono) or ``None``."""
        rms = float(np.sqrt(np.mean(frame * frame))) if len(frame) else 0.0
        threshold = max(self._noise * 3.2, self._min_rms)
        voiced = rms > threshold * (0.65 if self._active else 1.0)
        if not self._active:
            self._pre.append(frame)
            if voiced:
                self._voiced_run += 1
                if self._voiced_run >= 3:
                    self._active = True
                    self._frames = list(self._pre)
                    self._speech_frames = self._voiced_run
                    self._silence = 0
            else:
                self._voiced_run = 0
                self._noise = 0.97 * self._noise + 0.03 * min(rms, 0.05)
            return None
        self._frames.append(frame)
        if voiced:
            self._silence = 0
            self._speech_frames += 1
        else:
            self._silence += 1
        if self._silence >= self._silence_frames or len(self._frames) >= self._max_frames:
            enough = self._speech_frames >= self._min_speech_frames
            audio = np.concatenate(self._frames)
            self.reset()
            return audio if enough else None
        return None


class VoiceListener:
    """Blocking microphone loop: wake word -> command. Run it on its own thread."""

    def __init__(
        self,
        cfg: VoiceSettings,
        stt: SpeechToText,
        is_speaking: Callable[[], bool],
        speaking_ended_at: Callable[[], float],
        on_command: Callable[[str], None],
        on_wake: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.stt = stt
        self._is_speaking = is_speaking
        self._ended_at = speaking_ended_at
        self._on_command = on_command
        self._on_wake = on_wake
        self.matcher = WakeWordMatcher(cfg.wake_word, cfg.wake_aliases)
        self._muted = False
        self._armed_until = 0.0
        self._arm_request: tuple[float, bool] | None = None
        self._quiet_timeout = False
        self._lock = threading.Lock()
        self.running = False

    # ---------------------------------------------------------------- control
    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            set_flag("listening", False, "voice")

    def arm(self, seconds: float | None = None, *, quiet: bool = False) -> None:
        """Treat the next utterance as a command (push-to-talk / follow-up / yes-no answers)."""
        with self._lock:
            self._arm_request = (seconds or self.cfg.command_wait_seconds, quiet)

    def disarm(self) -> None:
        with self._lock:
            self._arm_request = None
            self._armed_until = 0.0
        set_flag("listening", False, "voice")

    @property
    def armed(self) -> bool:
        """True from arm() until the command was heard or the wait ran out."""
        return self._armed_until > 0.0

    # ------------------------------------------------------------------ loop
    def run(self, stop: threading.Event) -> None:
        mic = MicrophoneStream(self.cfg.input_device)
        try:
            mic.start()
        except Exception as exc:
            activity("VOICE", f"Cannot open the microphone: {describe_exception(exc)}", "error")
            raise
        segmenter = UtteranceSegmenter(self.cfg.vad_silence_ms, self.cfg.min_speech_ms, self.cfg.max_command_seconds, self.cfg.vad_min_rms)
        last_levels = 0.0
        self.running = True
        activity("VOICE", f"Listening for '{self.cfg.wake_word}' (say it, or press MIC)", "ok")
        try:
            while not stop.is_set():
                self._apply_arm_request(segmenter)
                frame = mic.read(0.25)
                now = time.monotonic()
                # never cut the user off mid-sentence: only expire while nobody is speaking
                if self._armed_until and now > self._armed_until and not segmenter.in_speech:
                    self._expire_arm()
                if frame is None:
                    continue
                if self._muted or self._is_speaking() or now - self._ended_at() < POST_SPEECH_GUARD:
                    segmenter.reset()
                    continue
                if self.armed and now - last_levels > 0.04:
                    last_levels = now
                    bus.emit(AUDIO_LEVELS, levels=spectrum_levels(frame, MIC_RATE))
                try:
                    utterance = segmenter.feed(frame)
                    if utterance is not None:
                        self._handle(utterance)
                        segmenter.reset()
                except Exception as exc:
                    log.exception("voice pipeline error")
                    activity("VOICE", f"Speech processing error: {describe_exception(exc)}", "error")
                    self.disarm()
        finally:
            self.running = False
            mic.stop()
            set_flag("listening", False, "voice")
            bus.emit(AUDIO_LEVELS, levels=[])

    def _apply_arm_request(self, segmenter: UtteranceSegmenter) -> None:
        with self._lock:
            request, self._arm_request = self._arm_request, None
        if request is None:
            return
        seconds, quiet = request
        segmenter.reset()
        self._armed_until = time.monotonic() + seconds
        self._quiet_timeout = quiet
        set_flag("listening", True, "voice", "Listening for your command…")

    def _expire_arm(self) -> None:
        self._armed_until = 0.0
        set_flag("listening", False, "voice")
        bus.emit(AUDIO_LEVELS, levels=[])
        if not self._quiet_timeout:
            activity("VOICE", "No command heard - back to standby.", "dim")

    def _handle(self, audio: np.ndarray) -> None:
        if self.armed:
            set_flag("thinking", True, "stt", "Transcribing your speech…")
            try:
                transcript = self.stt.transcribe(audio, wake=False, prompt=COMMAND_PROMPT)
            finally:
                set_flag("thinking", False, "stt")
            self.disarm()
            bus.emit(AUDIO_LEVELS, levels=[])
            if transcript.is_noise:
                activity("VOICE", "I didn't catch that.", "dim")
                return
            self._on_command(transcript.text)
            return

        transcript = self.stt.transcribe(audio, wake=True, prompt=f"{self.cfg.wake_word.title()}.")
        if transcript.is_noise:
            return
        found, rest = self.matcher.match(transcript.text)
        if not found:
            log.debug("ignored speech without wake word: %r", truncate(transcript.text, 80))
            return
        activity("VOICE", f"Wake word '{self.cfg.wake_word}' detected", "ok")
        if self._on_wake:
            self._on_wake()
        if len(rest) >= 3:  # "Jarvis, open notepad" in one breath
            if self.cfg.wake_model != self.cfg.whisper_model:  # re-listen with the stronger model
                better = self.stt.transcribe(audio, wake=False, prompt=COMMAND_PROMPT)
                found_again, rest_again = self.matcher.match(better.text)
                rest = rest_again if found_again and rest_again else rest
            self._on_command(rest)
        else:
            self.arm(self.cfg.command_wait_seconds)


# ------------------------------------------------------------- text to speech
def _beep(frequencies: tuple[float, ...] = (880.0, 1320.0), seconds: float = 0.06) -> np.ndarray:
    parts = []
    for freq in frequencies:
        t = np.arange(int(TTS_RATE * seconds)) / TTS_RATE
        envelope = np.minimum(1.0, np.minimum(t / 0.008, (seconds - t) / 0.02))
        parts.append(np.sin(2 * np.pi * freq * t) * envelope * 0.22)
    return (np.concatenate(parts) * 32767).astype(np.int16)


_CHIME = "\x00chime"


class TextToSpeech:
    """Queue-based speaker: synthesis runs one sentence ahead of playback."""

    def __init__(self, cfg: VoiceSettings) -> None:
        self.cfg = cfg
        self._text_q: queue.Queue[tuple[int, str] | None] = queue.Queue()
        self._audio_q: queue.Queue[tuple[int, object] | None] = queue.Queue(maxsize=2)
        self._gen = 0
        self._pending = 0
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._abort = threading.Event()
        self._closing = threading.Event()
        self.last_end = 0.0
        self.edge_ok = True
        self._muted = False
        self._sapi = None
        self._threads = [
            threading.Thread(target=self._synth_loop, name="jarvis-tts-synth", daemon=True),
            threading.Thread(target=self._play_loop, name="jarvis-tts-play", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    # ------------------------------------------------------------------- API
    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            self.stop()

    def speak(self, text: str, *, flush: bool = False) -> None:
        text = speech_text(text)
        if not text or self._muted or self._closing.is_set():
            return
        if flush:
            self.stop()
        self._enqueue(text)

    def chime(self) -> None:
        if not self._muted and not self._closing.is_set():
            self._enqueue(_CHIME)

    def _enqueue(self, item: str) -> None:
        with self._lock:
            self._pending += 1
            self._idle.clear()
            gen = self._gen
        set_flag("speaking", True, "tts", "Speaking…")
        self._text_q.put((gen, item))

    def stop(self) -> None:
        """Silence everything queued and interrupt the sentence being played."""
        with self._lock:
            self._gen += 1
            removed = self._drain(self._text_q) + self._drain(self._audio_q)
            self._pending = max(0, self._pending - removed)
        self._abort.set()
        self._maybe_idle()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def close(self) -> None:
        self._closing.set()
        self.stop()
        self._text_q.put(None)
        try:
            self._audio_q.put_nowait(None)
        except queue.Full:
            pass
        for thread in self._threads:
            thread.join(timeout=2.0)

    # --------------------------------------------------------------- internals
    @staticmethod
    def _drain(q: queue.Queue) -> int:
        removed = 0
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                return removed
            if item is not None:
                removed += 1

    def _item_done(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
        self._maybe_idle()

    def _maybe_idle(self) -> None:
        with self._lock:
            finished = self._pending == 0
        if finished:
            self.last_end = time.monotonic()
            self._idle.set()
            set_flag("speaking", False, "tts")
            bus.emit(AUDIO_LEVELS, levels=[])

    async def _edge_pcm(self, text: str) -> np.ndarray:
        import edge_tts
        import miniaudio

        communicate = edge_tts.Communicate(text, self.cfg.tts_voice, rate=self.cfg.tts_rate,
                                           volume=self.cfg.tts_volume, pitch=self.cfg.tts_pitch)
        data = bytearray()
        async for message in communicate.stream():
            if message.get("type") == "audio":
                data += message["data"]
        if not data:
            raise RuntimeError("Edge-TTS returned no audio")
        decoded = miniaudio.decode(bytes(data), output_format=miniaudio.SampleFormat.SIGNED16,
                                   nchannels=1, sample_rate=TTS_RATE)
        return np.frombuffer(decoded.samples, dtype=np.int16).copy()

    def _synth_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while True:
                item = self._text_q.get()
                if item is None:
                    return
                gen, text = item
                if gen != self._gen:
                    self._item_done()
                    continue
                payload: object
                if text == _CHIME:
                    payload = ("pcm", _beep())
                elif not self.edge_ok:
                    payload = ("sapi", text)
                else:
                    payload = self._synthesize(loop, text)
                while not self._closing.is_set():
                    if gen != self._gen:
                        self._item_done()
                        break
                    try:
                        self._audio_q.put((gen, payload), timeout=0.2)
                        break
                    except queue.Full:
                        continue
        finally:
            loop.close()

    def _synthesize(self, loop: asyncio.AbstractEventLoop, text: str) -> object:
        last: Exception | None = None
        for attempt in range(2):
            try:
                pcm = loop.run_until_complete(asyncio.wait_for(self._edge_pcm(text), timeout=25))
                return ("pcm", pcm)
            except Exception as exc:
                last = exc
                time.sleep(0.4)
        activity("VOICE", f"Edge-TTS unavailable ({describe_exception(last) if last else 'unknown'}) - using Windows voice", "warn")
        self.edge_ok = False
        retry = threading.Timer(120.0, lambda: setattr(self, "edge_ok", True))  # try Edge again later
        retry.daemon = True
        retry.start()
        return ("sapi", text)

    def _play_loop(self) -> None:
        stream = None
        try:
            while True:
                try:
                    item = self._audio_q.get(timeout=0.5)
                except queue.Empty:
                    if stream is not None and self._pending == 0:
                        stream.close()
                        stream = None
                    continue
                if item is None:
                    return
                gen, payload = item
                if gen != self._gen:
                    self._item_done()
                    continue
                self._abort.clear()
                kind, data = payload  # type: ignore[misc]
                try:
                    if kind == "pcm":
                        stream = stream or self._open_stream()
                        self._play_pcm(stream, data, gen)
                    else:
                        self._speak_sapi(str(data), gen)
                except Exception as exc:
                    log.exception("playback failed")
                    activity("VOICE", f"Audio playback failed: {describe_exception(exc)}", "error")
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                        stream = None
                finally:
                    self._item_done()
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def _open_stream(self):
        import sounddevice as sd

        device = _resolve_device(self.cfg.output_device, output=True)
        stream = sd.OutputStream(samplerate=TTS_RATE, channels=1, dtype="int16", device=device)
        stream.start()
        return stream

    def _play_pcm(self, stream, pcm: np.ndarray, gen: int) -> None:
        block = TTS_RATE // 25  # 40 ms: also the pace of the visualiser updates
        for offset in range(0, len(pcm), block):
            if self._abort.is_set() or gen != self._gen:
                break
            chunk = pcm[offset : offset + block]
            stream.write(chunk.reshape(-1, 1))
            bus.emit(AUDIO_LEVELS, levels=spectrum_levels(chunk, TTS_RATE))

    def _speak_sapi(self, text: str, gen: int) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Speak(text, 1)  # SVSFlagsAsync
            started = time.monotonic()
            while not voice.WaitUntilDone(50):
                bus.emit(AUDIO_LEVELS, levels=[0.35 + 0.3 * abs(math.sin((time.monotonic() - started) * 9 + i * 0.4)) for i in range(48)])
                if self._abort.is_set() or gen != self._gen:
                    voice.Speak("", 2)  # SVSFPurgeBeforeSpeak
                    break
        finally:
            pythoncom.CoUninitialize()
