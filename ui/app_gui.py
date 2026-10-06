"""Main window of the J.A.R.V.I.S HUD.

Layout: header / [capabilities + resources | radar core + conversation | activity log] / footer.
The backend drives it purely through the signals and slots below (see ``core.runtime``);
all slots are safe to invoke from other threads through queued connections.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from config import APP_NAME, APP_VERSION, STYLESHEET
from .widgets import CapabilityRow, ConfirmDialog, CoreOrb, HudPanel, LogConsole, NeonBar, TextPromptDialog, Tray, make_icon
from .widgets.theme import (
    BG, CYAN, STATE_COLORS, STATE_TEXT, TEXT, TEXT_DIM, mono_font, ui_font, with_alpha,
)

CAPABILITIES = (
    ("gemini", "Google Gemini AI"),
    ("system", "Windows Full System Control"),
    ("telegram", "Telegram Userbot Engine"),
    ("instagram", "Instagram instagrapi Core"),
    ("google", "Google Workspace (Gmail / Calendar / Drive / YouTube)"),
    ("voice", "Voice Processing (Whisper + Edge-TTS)"),
)

STATE_SUBTITLE = {
    "LISTENING": "Recording audio from the microphone…",
    "THINKING": "Gemini is processing your request…",
    "SPEAKING": "Voice playback in progress…",
    "EXECUTING": "Running system / API commands…",
}
FOOTER_STATUS = {
    "IDLE": "ALL SYSTEMS NOMINAL",
    "LISTENING": "AUDIO CAPTURE ACTIVE",
    "THINKING": "NEURAL CORE ENGAGED",
    "SPEAKING": "VOICE SYNTHESIS ACTIVE",
    "EXECUTING": "ACTION IN PROGRESS",
}

FALLBACK_QSS = f"""
QWidget {{ color: {TEXT}; background: transparent; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QLineEdit, QPlainTextEdit {{ background: #07121c; border: 1px solid #0b4a57; color: {TEXT}; }}
QPushButton {{ border: 1px solid {CYAN}; padding: 6px 14px; }}
"""


class HudBackground(QWidget):
    """Central widget: deep navy field with a faint grid and a soft vignette."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("root")

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(BG))
        glow = QRadialGradient(w * 0.5, h * 0.46, max(w, h) * 0.62)
        glow.setColorAt(0.0, QColor(0, 70, 95, 70))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), glow)
        painter.setPen(QColor(0, 229, 255, 9))
        for x in range(0, w, 44):
            painter.drawLine(x, 0, x, h)
        for y in range(0, h, 44):
            painter.drawLine(0, y, w, y)


class CommandLine(QLineEdit):
    """QLineEdit with Up/Down command history."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history: list[str] = []
        self._cursor = 0

    def remember(self, text: str) -> None:
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
            del self._history[:-100]
        self._cursor = len(self._history)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Up and self._history:
            self._cursor = max(0, self._cursor - 1)
            self.setText(self._history[self._cursor])
            return
        if event.key() == Qt.Key.Key_Down and self._history:
            self._cursor = min(len(self._history), self._cursor + 1)
            self.setText(self._history[self._cursor] if self._cursor < len(self._history) else "")
            return
        super().keyPressEvent(event)


class JarvisWindow(QMainWindow):
    # ---- signals emitted BY the window --------------------------------------
    command_submitted = Signal(str)
    mic_requested = Signal()
    stop_requested = Signal()
    capability_toggled = Signal(str, bool)
    confirm_answered = Signal(str, bool)
    text_answered = Signal(str, object)
    voice_mute_toggled = Signal(bool)
    quit_requested = Signal()

    def __init__(self, settings) -> None:
        super().__init__()
        self._settings = settings
        self._started = time.monotonic()
        self._state = "IDLE"
        self._idle_hint = f'Say "{settings.voice.wake_word.title()}" or type a command'
        self._dialogs: dict[str, QWidget] = {}
        self._tray_hint_shown = False
        self._force_quit = False
        self._styled_titlebar = False

        self.setWindowTitle(f"{APP_NAME} — Personal AI Operating System")
        self.setWindowIcon(make_icon())
        self.resize(1480, 900)
        self.setMinimumSize(1180, 720)

        root = HudBackground(self)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 12, 16, 10)
        outer.setSpacing(10)
        outer.addWidget(self._build_header())
        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_left())
        body.addWidget(self._build_center(), 1)
        body.addWidget(self._build_right())
        outer.addLayout(body, 1)
        outer.addWidget(self._build_footer())

        self.apply_stylesheet()
        self._tray = Tray(self, APP_NAME)
        self._tray.toggle_window.connect(self._toggle_visibility)
        self._tray.mute_toggled.connect(self.voice_mute_toggled)
        self._tray.quit_requested.connect(self.quit_requested)
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_clock)
        self._clock.start()
        self._update_clock()
        self.set_state("IDLE", "")

    # ------------------------------------------------------------------- build
    def _build_header(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("header")
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 0, 6, 4)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel(APP_NAME, bar)
        title.setObjectName("brandTitle")
        title.setFont(ui_font(24, QFont.Weight.Bold, 7.0))
        glow = QGraphicsDropShadowEffect(title)
        glow.setBlurRadius(22)
        glow.setOffset(0, 0)
        glow.setColor(QColor(CYAN))
        title.setGraphicsEffect(glow)
        subtitle = QLabel("JUST A RATHER VERY INTELLIGENT SYSTEM", bar)
        subtitle.setObjectName("brandSubtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        row.addLayout(brand)
        row.addStretch(1)

        self._model_label = QLabel("", bar)
        self._model_label.setObjectName("modelLabel")
        row.addWidget(self._model_label, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(26)
        clock = QVBoxLayout()
        clock.setSpacing(0)
        self._time_label = QLabel("00:00:00", bar)
        self._time_label.setObjectName("clockTime")
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._date_label = QLabel("", bar)
        self._date_label.setObjectName("clockDate")
        self._date_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock.addWidget(self._time_label)
        clock.addWidget(self._date_label)
        row.addLayout(clock)
        return bar

    def _build_left(self) -> QWidget:
        column = QWidget(self)
        column.setFixedWidth(310)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        resources = HudPanel("System Resources", column)
        self._bar_cpu = NeonBar("CPU", resources)
        self._bar_ram = NeonBar("RAM", resources)
        self._bar_vram = NeonBar("VRAM", resources)
        self._bar_disk = NeonBar("Storage", resources)
        for bar in (self._bar_cpu, self._bar_ram, self._bar_vram, self._bar_disk):
            resources.body().addWidget(bar)
        layout.addWidget(resources)

        caps = HudPanel("Active Capabilities", column)
        caps.body().setSpacing(4)
        self._rows: dict[str, CapabilityRow] = {}
        for key, text in CAPABILITIES:
            row = CapabilityRow(key, text, caps)
            row.toggled.connect(self.capability_toggled)
            self._rows[key] = row
            caps.body().addWidget(row)
        caps.body().addStretch(1)
        layout.addWidget(caps, 1)
        return column

    def _build_center(self) -> QWidget:
        column = QWidget(self)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._orb = CoreOrb(self._settings.ui.fps, column)
        layout.addWidget(self._orb, 1)

        self._state_label = QLabel("[ IDLE ]", column)
        self._state_label.setObjectName("stateLabel")
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state_label.setFont(ui_font(22, QFont.Weight.Bold, 6.0))
        self._state_glow = QGraphicsDropShadowEffect(self._state_label)
        self._state_glow.setBlurRadius(26)
        self._state_glow.setOffset(0, 0)
        self._state_label.setGraphicsEffect(self._state_glow)
        layout.addWidget(self._state_label)

        self._detail_label = QLabel("", column)
        self._detail_label.setObjectName("stateDetail")
        self._detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._detail_label)

        strip = QFrame(column)
        strip.setObjectName("conversation")
        strip_layout = QVBoxLayout(strip)
        strip_layout.setContentsMargins(16, 10, 16, 10)
        strip_layout.setSpacing(4)
        self._user_label = QLabel("", strip)
        self._user_label.setObjectName("userLine")
        self._user_label.setWordWrap(True)
        self._reply_label = QLabel("", strip)
        self._reply_label.setObjectName("replyLine")
        self._reply_label.setWordWrap(True)
        self._reply_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        strip_layout.addWidget(self._user_label)
        strip_layout.addWidget(self._reply_label)
        strip.setMinimumHeight(104)
        strip.setMaximumHeight(150)
        layout.addWidget(strip)

        command = QFrame(column)
        command.setObjectName("commandBar")
        command_row = QHBoxLayout(command)
        command_row.setContentsMargins(0, 0, 0, 0)
        command_row.setSpacing(8)
        self._input = CommandLine(command)
        self._input.setObjectName("commandLine")
        self._input.setPlaceholderText("TYPE A COMMAND OR SAY 'JARVIS'…")
        self._input.returnPressed.connect(self._submit)
        self._mic = QPushButton("● MIC", command)
        self._mic.setObjectName("micButton")
        self._mic.setToolTip("Push to talk: the next thing you say is a command (no wake word needed)")
        self._mic.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mic.clicked.connect(self.mic_requested)
        self._stop = QPushButton("■ STOP", command)
        self._stop.setObjectName("stopButton")
        self._stop.setToolTip("Cancel the current task and stop speaking (Esc)")
        self._stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop.clicked.connect(self.stop_requested)
        command_row.addWidget(self._input, 1)
        command_row.addWidget(self._mic)
        command_row.addWidget(self._stop)
        layout.addWidget(command)
        return column

    def _build_right(self) -> QWidget:
        panel = HudPanel("System Activity Log", self)
        panel.setFixedWidth(400)
        panel.body().setContentsMargins(10, 40, 10, 12)
        self._log = LogConsole(panel)
        panel.body().addWidget(self._log, 1)
        return panel

    def _build_footer(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("footer")
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 2, 6, 0)
        self._uptime_label = QLabel("UPTIME 00:00:00", bar)
        self._version_label = QLabel(f"{APP_NAME} v{APP_VERSION}", bar)
        self._status_label = QLabel(FOOTER_STATUS["IDLE"], bar)
        for label in (self._uptime_label, self._version_label, self._status_label):
            label.setObjectName("footerText")
        row.addWidget(self._uptime_label)
        row.addStretch(1)
        row.addWidget(self._version_label)
        row.addStretch(1)
        row.addWidget(self._status_label)
        return bar

    # ------------------------------------------------------------------ styling
    def apply_stylesheet(self) -> None:
        try:
            self.setStyleSheet(STYLESHEET.read_text(encoding="utf-8"))
        except OSError:
            self.setStyleSheet(FALLBACK_QSS)

    def _style_title_bar(self) -> None:
        """Windows 11: dark caption in the HUD colours so the native frame blends in."""
        if sys.platform != "win32" or self._styled_titlebar:
            return
        self._styled_titlebar = True
        try:
            from ctypes import wintypes

            hwnd = wintypes.HWND(int(self.winId()))
            dwm = ctypes.windll.dwmapi

            def put(attribute: int, value: int) -> None:
                data = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))

            def colorref(color: str) -> int:
                c = QColor(color)
                return c.red() | (c.green() << 8) | (c.blue() << 16)

            put(20, 1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            put(35, colorref(BG))  # DWMWA_CAPTION_COLOR
            put(36, colorref(CYAN))  # DWMWA_TEXT_COLOR
            put(34, colorref("#0B4A57"))  # DWMWA_BORDER_COLOR
        except Exception:
            pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._style_title_bar()
        self._input.setFocus()

    # --------------------------------------------------------------- internals
    def _update_clock(self) -> None:
        now = datetime.now()
        self._time_label.setText(now.strftime("%H:%M:%S"))
        self._date_label.setText(now.strftime("%A · %d %B %Y").upper())
        elapsed = int(time.monotonic() - self._started)
        self._uptime_label.setText(f"UPTIME {elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}")

    def _submit(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.remember(text)
        self._input.clear()
        self.command_submitted.emit(text)

    def _toggle_visibility(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    @staticmethod
    def _tail(text: str, limit: int) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else "…" + text[-(limit - 1):]

    # --------------------------------------------------------------- key / close
    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.stop_requested.emit()
            return
        if event.key() == Qt.Key.Key_L and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._input.setFocus()
            self._input.selectAll()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if not self._force_quit and self._settings.ui.minimize_to_tray and self._tray.available:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.notify(APP_NAME, "Still running in the tray. Right-click the icon to quit.")
            return
        self._clock.stop()
        self._orb.hide()
        for dialog in list(self._dialogs.values()):
            dialog.close()
        self._tray.hide()
        event.accept()
        if not self._force_quit:
            self.quit_requested.emit()

    @Slot()
    def force_quit(self) -> None:
        self._force_quit = True
        self.close()

    # ======================================================== backend-facing slots
    @Slot(str, str)
    def set_state(self, state: str, detail: str = "") -> None:
        if state not in STATE_COLORS:
            return
        self._state = state
        color = QColor(STATE_COLORS[state])
        self._orb.set_state(state)
        self._state_label.setText(STATE_TEXT[state])
        self._state_label.setStyleSheet(f"color: {color.name()};")
        self._state_glow.setColor(with_alpha(color, 0.9))
        if detail:
            self._detail_label.setText(detail)
        else:
            self._detail_label.setText(self._idle_hint if state == "IDLE" else STATE_SUBTITLE.get(state, ""))
        self._status_label.setText(FOOTER_STATUS[state])
        self._status_label.setStyleSheet(f"color: {color.name()};")

    @Slot(str)
    def set_listening_hint(self, text: str) -> None:
        self._idle_hint = text
        if self._state == "IDLE":
            self._detail_label.setText(text)

    @Slot(str, str, str)
    def append_log(self, source: str, message: str, level: str = "info") -> None:
        self._log.add_line(source, message, level)

    @Slot(str, str)
    def add_transcript(self, role: str, text: str) -> None:
        text = self._tail(text, 420)
        if role == "user":
            self._user_label.setText(f"›  {text}")
            self._reply_label.setText("")
        elif role == "jarvis":
            self._reply_label.setProperty("role", "jarvis")
            self._reply_label.setText(text)
        else:
            self._reply_label.setProperty("role", "system")
            self._reply_label.setText(text)
        self._reply_label.style().unpolish(self._reply_label)
        self._reply_label.style().polish(self._reply_label)

    @Slot(str)
    def set_reply_text(self, text: str) -> None:
        self._reply_label.setProperty("role", "jarvis")
        self._reply_label.setText(self._tail(text, 420))
        self._reply_label.style().unpolish(self._reply_label)
        self._reply_label.style().polish(self._reply_label)

    @Slot(dict)
    def update_resources(self, stats: dict) -> None:
        cpu = stats.get("cpu")
        self._bar_cpu.set_value(cpu, f"{os.cpu_count() or '?'} threads")

        ram = stats.get("ram")
        self._bar_ram.set_value(ram, f"{stats.get('ram_used_gb', 0):.1f} / {stats.get('ram_total_gb', 0):.1f} GB" if ram is not None else "")

        gpu = (stats.get("gpu_name") or "").strip()
        self._bar_vram.set_label(f"VRAM · {gpu}" if gpu else "VRAM")
        vram = stats.get("vram")
        if vram is None:
            self._bar_vram.set_value(None, "no NVIDIA GPU detected")
        else:
            extra = []
            if stats.get("gpu_util") is not None:
                extra.append(f"GPU {stats['gpu_util']:.0f}%")
            if stats.get("gpu_temp") is not None:
                extra.append(f"{stats['gpu_temp']:.0f}°C")
            detail = f"{stats.get('vram_used_gb', 0):.1f} / {stats.get('vram_total_gb', 0):.1f} GB"
            self._bar_vram.set_value(vram, detail + ("  ·  " + "  ".join(extra) if extra else ""))

        disk = stats.get("disk")
        name = stats.get("disk_name") or ""
        self._bar_disk.set_label(f"Storage · {name}" if name else "Storage")
        self._bar_disk.set_value(disk, f"{stats.get('disk_used_gb', 0):.0f} / {stats.get('disk_total_gb', 0):.0f} GB" if disk is not None else "")

    @Slot(str, str, str)
    def set_capability(self, key: str, status: str, detail: str = "") -> None:
        row = self._rows.get(key)
        if row is not None:
            row.set_status(status, detail)

    @Slot(list)
    def set_audio_levels(self, levels: list) -> None:
        self._orb.set_levels(levels)

    @Slot(str)
    def set_model_info(self, text: str) -> None:
        self._model_label.setText(text.upper())

    @Slot(str, str)
    def show_notification(self, title: str, message: str) -> None:
        if not self._tray.notify(title, message):
            self.append_log("SYSTEM", f"{title}: {message}", "info")

    # ------------------------------------------------------------------ dialogs
    @Slot(str, str, str, float)
    def request_confirmation(self, request_id: str, title: str, details: str, timeout: float) -> None:
        dialog = ConfirmDialog(title, details, timeout)
        dialog.answered.connect(lambda ok, rid=request_id: self._dialog_done(rid, ok, is_text=False))
        self._dialogs[request_id] = dialog
        dialog.present()

    @Slot(str, str, str, bool, float)
    def request_text(self, request_id: str, title: str, prompt: str, secret: bool, timeout: float) -> None:
        dialog = TextPromptDialog(title, prompt, secret, timeout)
        dialog.answered.connect(lambda value, rid=request_id: self._dialog_done(rid, value, is_text=True))
        self._dialogs[request_id] = dialog
        dialog.present()

    @Slot(str)
    def dismiss_request(self, request_id: str) -> None:
        dialog = self._dialogs.pop(request_id, None)
        if dialog is not None:
            dialog.dismiss()

    def _dialog_done(self, request_id: str, value, *, is_text: bool) -> None:
        if self._dialogs.pop(request_id, None) is None:
            return  # already dismissed elsewhere
        if is_text:
            self.text_answered.emit(request_id, value)
        else:
            self.confirm_answered.emit(request_id, bool(value))
