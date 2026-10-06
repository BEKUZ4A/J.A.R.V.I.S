"""Capability rows: a glowing toggle switch, a label and a live status line."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .theme import STATUS_COLORS, STATUS_TEXT, TEXT, ui_font, with_alpha


class ToggleSwitch(QAbstractButton):
    """Small sci-fi switch; ``set_glow`` tints the knob with the status colour."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 20)
        self._pos = 0.0
        self._glow = QColor(STATUS_COLORS["disabled"])
        self._pulse = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(40, 20)

    def set_checked_silently(self, checked: bool) -> None:
        blocked = self.blockSignals(True)
        self.setChecked(checked)
        self.blockSignals(blocked)
        self._anim.stop()
        self._pos = 1.0 if checked else 0.0
        self.update()

    def set_glow(self, color: QColor, pulse: float = 0.0) -> None:
        self._glow, self._pulse = color, pulse
        self.update()

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(float(self._pos))
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._pos = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QRectF(1.5, 3.5, self.width() - 3.0, self.height() - 7.0)
        on = self.isChecked()
        edge = with_alpha(self._glow, 0.9 if on else 0.35)
        painter.setPen(edge)
        painter.setBrush(with_alpha(self._glow, 0.22 + 0.15 * self._pulse) if on else QColor(10, 24, 34, 220))
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        radius = self.height() / 2 - 3.0
        cx = track.left() + radius + 1.5 + (track.width() - 2 * radius - 3.0) * self._pos
        painter.setPen(Qt.PenStyle.NoPen)
        if on:
            painter.setBrush(with_alpha(self._glow, 0.25 + 0.25 * self._pulse))
            painter.drawEllipse(QRectF(cx - radius - 3, self.height() / 2 - radius - 3, 2 * radius + 6, 2 * radius + 6))
        painter.setBrush(self._glow if on else QColor("#4A5E68"))
        painter.drawEllipse(QRectF(cx - radius, self.height() / 2 - radius, 2 * radius, 2 * radius))


class CapabilityRow(QWidget):
    """``[switch] Label`` with a colour-coded status line underneath."""

    toggled = Signal(str, bool)

    def __init__(self, key: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self._status = "offline"
        self._detail = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(46)

        self._switch = ToggleSwitch(self)
        self._label = QLabel(text, self)
        self._label.setObjectName("capLabel")
        self._label.setWordWrap(True)
        self._status_label = QLabel("OFFLINE", self)
        self._status_label.setObjectName("capStatus")
        self._status_label.setFont(ui_font(8.0, QFont.Weight.DemiBold, 1.4))

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        column.addWidget(self._label)
        column.addWidget(self._status_label)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 3, 0, 3)
        row.setSpacing(12)
        row.addWidget(self._switch, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(column, 1)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(60)
        self._pulse_timer.timeout.connect(self._pulse)
        self._switch.clicked.connect(self._on_clicked)
        self.set_status("offline", "")

    def _on_clicked(self, checked: bool) -> None:
        # Optimistic: the backend confirms (or corrects) through set_status.
        self.set_status("connecting" if checked else "disabled", "")
        self.toggled.emit(self.key, checked)

    def _pulse(self) -> None:
        wave = 0.5 + 0.5 * math.sin(time.perf_counter() * 5.0)
        self._switch.set_glow(QColor(STATUS_COLORS[self._status]), wave)

    def set_status(self, status: str, detail: str = "") -> None:
        if status not in STATUS_COLORS:
            return
        self._status, self._detail = status, detail
        color = QColor(STATUS_COLORS[status])
        # "offline" (not configured / not running) and "disabled" both show the switch off.
        self._switch.set_checked_silently(status in {"online", "connecting", "error"})
        self._switch.set_glow(color, 0.0)
        if status == "connecting":
            self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
        self._status_label.setStyleSheet(f"color: {color.name()};")
        text = STATUS_TEXT[status] + (f"  ·  {detail}" if detail else "")
        metrics = QFontMetrics(self._status_label.font())
        self._status_label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(60, self.width() - 70)))
        self.setToolTip(f"{self._label.text()}\n{STATUS_TEXT[status]}" + (f"\n{detail}" if detail else ""))
        self._label.setProperty("dim", status in {"disabled", "offline"})
        self._label.style().unpolish(self._label)
        self._label.style().polish(self._label)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_status(self._status, self._detail)  # re-elide for the new width
