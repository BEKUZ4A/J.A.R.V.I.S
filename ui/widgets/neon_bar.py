"""Segmented neon load bar with an animated fill."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from .theme import TEXT, TEXT_DIM, level_color, mono_font, ui_font, with_alpha


class NeonBar(QWidget):
    """``LABEL ............ 37%`` over a segmented bar and a small detail line."""

    SEGMENT = 7.0
    GAP = 2.5

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label.upper()
        self._shown = 0.0  # animated 0..100
        self._available = False
        self._detail = ""
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(450)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def sizeHint(self) -> QSize:
        return QSize(260, 52)

    def set_label(self, label: str) -> None:
        self._label = label.upper()
        self.update()

    def set_value(self, percent: float | None, detail: str = "") -> None:
        """``None`` shows N/A (e.g. no NVIDIA GPU)."""
        self._detail = detail
        if percent is None:
            self._available = False
            self._anim.stop()
            self._shown = 0.0
            self.update()
            return
        target = max(0.0, min(100.0, float(percent)))
        if not self._available:
            self._available = True
            self._shown = target
            self.update()
            return
        self._anim.stop()
        self._anim.setStartValue(float(self._shown))
        self._anim.setEndValue(target)
        self._anim.start()

    def value(self) -> float:
        return self._shown

    def _on_anim(self, value) -> None:
        self._shown = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = float(self.width())

        painter.setFont(ui_font(8.5, QFont.Weight.DemiBold, 1.8))
        painter.setPen(QColor(TEXT_DIM))
        painter.drawText(QRectF(0, 0, width * 0.7, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        color = level_color(self._shown) if self._available else QColor("#2A3A44")
        painter.setFont(mono_font(10.5))
        painter.setPen(color if self._available else QColor(TEXT_DIM))
        text = f"{self._shown:.0f}%" if self._available else "N/A"
        painter.drawText(QRectF(0, 0, width, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)

        top, height = 20.0, 12.0
        step = self.SEGMENT + self.GAP
        count = max(8, int((width + self.GAP) // step))
        lit = int(round(count * self._shown / 100.0)) if self._available else 0
        for i in range(count):
            rect = QRectF(i * step, top, self.SEGMENT, height)
            if i < lit:
                painter.fillRect(rect.adjusted(-1.2, -1.2, 1.2, 1.2), with_alpha(color, 0.16))  # glow halo
                painter.fillRect(rect, with_alpha(color, 0.95))
            else:
                painter.fillRect(rect, QColor(0, 229, 255, 22))

        painter.setFont(mono_font(8.0))
        painter.setPen(QColor(TEXT))
        painter.setOpacity(0.6)
        painter.drawText(QRectF(0, 36, width, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._detail)
