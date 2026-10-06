"""Developer preview: drive the HUD with simulated data.

    python -m ui.preview                 interactive demo (cycles through every state)
    python -m ui.preview --shot DIR      render one PNG per state into DIR (offscreen) and exit
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from pathlib import Path

if "--shot" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))  # offscreen has no font db

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from config import settings
from ui.app_gui import JarvisWindow

SAMPLE_LOG = [
    ("SYSTEM INTEGRITY", "OPTIMAL", "ok"),
    ("GEMINI", "Connected - gemini-2.5-flash-lite (native tool calling)", "ok"),
    ("VOICE", "Whisper 'small' loaded - listening for 'jarvis'", "info"),
    ("EXECUTING", "Posting to Instagram via instagrapi...", "action"),
    ("GOOGLE WORKSPACE", "Reading unread emails...", "info"),
    ("TELEGRAM", "Sending post to target channel...", "info"),
    ("SYSTEM", "Awaiting approval: Run in PowerShell: Get-Process", "warn"),
    ("INSTAGRAM", "Session expired - logging in again", "error"),
    ("GEMINI", "Response completed in 0.8s", "dim"),
]
STATE_DEMO = {
    "IDLE": ("", None),
    "LISTENING": ("", "listen"),
    "THINKING": ("", None),
    "SPEAKING": ("", "speak"),
    "EXECUTING": ("Posting to Instagram via instagrapi...", None),
}


def fake_stats(t: float) -> dict:
    return {
        "cpu": 18 + 12 * math.sin(t / 3), "ram": 78 + 4 * math.sin(t / 5), "ram_used_gb": 6.0, "ram_total_gb": 7.7,
        "vram": 71 + 6 * math.sin(t / 4), "vram_used_gb": 4.3, "vram_total_gb": 6.0, "gpu_name": "RTX 3050",
        "gpu_util": 42, "gpu_temp": 61, "disk": 41.0, "disk_used_gb": 197, "disk_total_gb": 476, "disk_name": "C:",
    }


def fake_levels(kind: str | None, t: float) -> list[float]:
    if kind is None:
        return []
    return [max(0.0, min(1.0, 0.45 + 0.4 * math.sin(t * 6 + i * 0.5) * random.random() + 0.1 * random.random())) for i in range(48)]


def populate(window: JarvisWindow) -> None:
    window.set_model_info(f"{settings.gemini.model} · GOOGLE GEMINI")
    for key, status, detail in (
        ("gemini", "online", "gemini-2.5-flash-lite · Google AI"), ("system", "online", "Windows control ready"),
        ("telegram", "connecting", ""), ("instagram", "offline", "missing INSTAGRAM_USERNAME"),
        ("google", "error", "token expired"), ("voice", "disabled", ""),
    ):
        window.set_capability(key, status, detail)
    for source, message, level in SAMPLE_LOG:
        window.append_log(source, message, level)
    window.add_transcript("user", "Jarvis, post today's photo to Instagram and tell my channel about it")
    window.set_reply_text("Certainly. I'll prepare the post and ask you to confirm before anything goes out.")
    window.update_resources(fake_stats(0))


def render_shots(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    window = JarvisWindow(settings)
    window.show()
    populate(window)
    orb = window._orb
    for size in ((1480, 900), (1180, 720)):
        window.resize(*size)
        for state, (detail, kind) in STATE_DEMO.items():
            window.set_state(state, detail)
            for step in range(60):  # 2 simulated seconds so cross-fades settle
                orb.set_levels(fake_levels(kind, step / 30.0))
                orb.advance(1 / 30)
            window.update_resources(fake_stats(step))
            deadline = time.monotonic() + 0.25  # let queued timers (log batching, bar animations) run
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            path = directory / f"hud_{size[0]}x{size[1]}_{state.lower()}.png"
            window.grab().save(str(path))
            print(path)
    return 0


def run_interactive() -> int:
    app = QApplication(sys.argv)
    window = JarvisWindow(settings)
    window.show()
    populate(window)
    clock = {"t": 0.0}
    order = list(STATE_DEMO)

    def tick() -> None:
        clock["t"] += 0.1
        window.update_resources(fake_stats(clock["t"]))
        kind = STATE_DEMO[window._state][1]
        window.set_audio_levels(fake_levels(kind, clock["t"]))

    def next_state() -> None:
        state = order[(order.index(window._state) + 1) % len(order)]
        window.set_state(state, STATE_DEMO[state][0])
        window.append_log(*random.choice(SAMPLE_LOG))

    window.command_submitted.connect(lambda text: window.add_transcript("user", text))
    window.stop_requested.connect(lambda: window.set_state("IDLE"))
    for interval, fn in ((100, tick), (3500, next_state)):
        timer = QTimer(window)
        timer.timeout.connect(fn)
        timer.start(interval)
    QTimer.singleShot(2500, lambda: window.request_confirmation("demo-1", "Approval required", "Send Telegram message to Alex:\n'I am running late'", 30))
    return app.exec()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shot", metavar="DIR", help="render screenshots of every state into DIR and exit")
    options = parser.parse_args()
    sys.exit(render_shots(Path(options.shot)) if options.shot else run_interactive())
