"""The animated radar / arc-reactor core that visualises the assistant's state."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QConicalGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QWidget

from .theme import STATE_COLORS, mix, with_alpha

BANDS = 64

# Target animation parameters per state; the live values glide toward these.
#   speed   ring rotation multiplier        sweep   radar sweep opacity
#   ripples listening ripples               dots    orbiting thinking dots
#   brackets lock-on brackets               bars    spectrum bar gain
#   micro   idle shimmer of the spectrum    pulse   core breathing/reactivity
STATE_PARAMS: dict[str, dict[str, float]] = {
    "IDLE": dict(speed=0.35, sweep=0.10, ripples=0.0, dots=0.0, brackets=0.0, bars=0.0, micro=1.0, pulse=0.0),
    "LISTENING": dict(speed=0.70, sweep=0.16, ripples=1.0, dots=0.0, brackets=0.0, bars=1.0, micro=0.5, pulse=1.0),
    "THINKING": dict(speed=2.60, sweep=0.46, ripples=0.0, dots=1.0, brackets=0.0, bars=0.0, micro=0.3, pulse=0.3),
    "SPEAKING": dict(speed=0.90, sweep=0.14, ripples=0.0, dots=0.0, brackets=0.0, bars=1.0, micro=0.4, pulse=1.0),
    "EXECUTING": dict(speed=3.40, sweep=0.30, ripples=0.0, dots=0.0, brackets=1.0, bars=0.0, micro=0.6, pulse=0.4),
}


def _polar(cx: float, cy: float, radius: float, degrees: float) -> QPointF:
    angle = math.radians(degrees)
    return QPointF(cx + radius * math.cos(angle), cy - radius * math.sin(angle))


class CoreOrb(QWidget):
    """Time-based animation (frame-rate independent). Tests can drive it with :meth:`advance`."""

    def __init__(self, fps: int = 30, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._state = "IDLE"
        self._color = QColor(STATE_COLORS["IDLE"])
        self._params = dict(STATE_PARAMS["IDLE"])
        self._targets = [0.0] * BANDS
        self._bars = [0.0] * BANDS
        self._levels_stamp = 0.0
        self._energy = 0.0
        self._t = 0.0
        self._angles = [0.0] * 5
        self._last = time.perf_counter()
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / max(10, min(fps, 120))))
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ control
    def set_state(self, state: str) -> None:
        if state in STATE_PARAMS and state != self._state:
            self._state = state

    def state(self) -> str:
        return self._state

    def set_levels(self, levels) -> None:
        """Spectrum bands 0..1 (any length); resampled to ``BANDS``."""
        values = [max(0.0, min(1.0, float(v))) for v in levels] if levels else []
        if not values:
            self._targets = [0.0] * BANDS
        else:
            n = len(values)
            self._targets = [values[min(n - 1, int(i * n / BANDS))] for i in range(BANDS)]
        self._levels_stamp = time.perf_counter()

    def showEvent(self, event) -> None:
        self._last = time.perf_counter()
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    # --------------------------------------------------------------- animation
    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(0.1, now - self._last)
        self._last = now
        self.advance(dt)
        self.update()

    def advance(self, dt: float) -> None:
        k = 1.0 - math.exp(-dt / 0.35)
        for key, target in STATE_PARAMS[self._state].items():
            self._params[key] += (target - self._params[key]) * k
        self._color = mix(self._color, QColor(STATE_COLORS[self._state]), 1.0 - math.exp(-dt / 0.22))
        self._t += dt
        speed = self._params["speed"]
        for index, rate in enumerate((12.0, -20.0, 34.0, -52.0, 95.0)):
            self._angles[index] = (self._angles[index] + dt * rate * speed) % 360.0

        stale = (time.perf_counter() - self._levels_stamp) > 0.3
        micro_gain = self._params["micro"]
        total = 0.0
        for i in range(BANDS):
            target = 0.0 if stale else self._targets[i] * self._params["bars"]
            shimmer = (0.09 + 0.06 * math.sin(self._t * 1.7 + i * 0.55) + 0.03 * math.sin(self._t * 3.1 + i * 1.3)) * micro_gain
            target = max(target, shimmer)
            value = self._bars[i]
            self._bars[i] = value + (target - value) * (0.55 if target > value else 0.16)
            total += self._bars[i]
        self._energy += (total / BANDS - self._energy) * (1.0 - math.exp(-dt / 0.08))

    # ----------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width, height = float(self.width()), float(self.height())
        cx, cy = width / 2.0, height / 2.0
        radius = min(width, height) / 2.0 - 8.0
        if radius < 40:
            return
        color = self._color
        energy = min(1.0, self._energy * 1.6)
        params = self._params

        self._paint_backdrop(painter, cx, cy, radius, color, energy)
        self._paint_sweep(painter, cx, cy, radius, color, params["sweep"])
        self._paint_rings(painter, cx, cy, radius, color)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        self._paint_spectrum(painter, cx, cy, radius, color)
        self._paint_ripples(painter, cx, cy, radius, color, params["ripples"])
        self._paint_dots(painter, cx, cy, radius, color, params["dots"])
        self._paint_core(painter, cx, cy, radius, color, energy, params["pulse"])
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self._paint_brackets(painter, cx, cy, radius, color, params["brackets"])

    def _paint_backdrop(self, p: QPainter, cx, cy, radius, color, energy) -> None:
        glow = QRadialGradient(QPointF(cx, cy), radius * 1.08)
        glow.setColorAt(0.0, with_alpha(color, 0.20 + 0.18 * energy))
        glow.setColorAt(0.55, with_alpha(color, 0.07))
        glow.setColorAt(1.0, with_alpha(color, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(cx, cy), radius * 1.08, radius * 1.08)
        p.setPen(QPen(with_alpha(color, 0.10), 1.0))
        p.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))
        p.drawLine(QPointF(cx, cy - radius), QPointF(cx, cy + radius))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for fraction in (0.36, 0.52):
            p.setPen(QPen(with_alpha(color, 0.12), 1.0, Qt.PenStyle.DotLine))
            p.drawEllipse(QPointF(cx, cy), radius * fraction, radius * fraction)

    def _paint_sweep(self, p: QPainter, cx, cy, radius, color, strength: float) -> None:
        if strength < 0.01:
            return
        gradient = QConicalGradient(QPointF(cx, cy), self._angles[4])
        gradient.setColorAt(0.0, with_alpha(color, strength))
        gradient.setColorAt(0.14, with_alpha(color, 0.0))
        gradient.setColorAt(1.0, with_alpha(color, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(gradient))
        p.drawEllipse(QPointF(cx, cy), radius * 0.86, radius * 0.86)
        lead = _polar(cx, cy, radius * 0.86, self._angles[4])
        p.setPen(QPen(with_alpha(color, min(1.0, strength * 2.2)), 1.5))
        p.drawLine(QPointF(cx, cy), lead)

    def _arc(self, p: QPainter, cx, cy, r, start: float, span: float) -> None:
        p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), int(start * 16), int(span * 16))

    def _paint_rings(self, p: QPainter, cx, cy, radius, color) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        # outer ring + three slow bright arcs
        p.setPen(QPen(with_alpha(color, 0.30), 1.2))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        p.setPen(QPen(with_alpha(color, 0.95), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for k in range(3):
            self._arc(p, cx, cy, radius, self._angles[0] + k * 120.0, 46.0)
        # tick ring
        p.setPen(QPen(with_alpha(color, 0.65), 1.2))
        ticks = 90
        for i in range(ticks):
            degrees = self._angles[1] + i * 360.0 / ticks
            long_tick = i % 6 == 0
            p.drawLine(_polar(cx, cy, radius * 0.885, degrees), _polar(cx, cy, radius * (0.945 if long_tick else 0.915), degrees))
        # segmented ring
        p.setPen(QPen(with_alpha(color, 0.80), 4.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        for k in range(8):
            self._arc(p, cx, cy, radius * 0.78, self._angles[2] + k * 45.0, 30.0)
        # inner thin ring with notches
        p.setPen(QPen(with_alpha(color, 0.45), 1.0))
        p.drawEllipse(QPointF(cx, cy), radius * 0.66, radius * 0.66)
        p.setPen(QPen(with_alpha(color, 0.9), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for k in range(4):
            self._arc(p, cx, cy, radius * 0.66, self._angles[3] + k * 90.0, 12.0)

    def _paint_spectrum(self, p: QPainter, cx, cy, radius, color) -> None:
        inner = radius * 0.47
        bright = mix(color, QColor(255, 255, 255), 0.35)
        for i in range(BANDS):
            level = self._bars[i]
            degrees = 90.0 - i * 360.0 / BANDS
            length = radius * (0.025 + 0.20 * level)
            pen = QPen(with_alpha(bright, 0.30 + 0.70 * min(1.0, level * 1.4)), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(_polar(cx, cy, inner, degrees), _polar(cx, cy, inner + length, degrees))

    def _paint_ripples(self, p: QPainter, cx, cy, radius, color, strength: float) -> None:
        if strength < 0.02:
            return
        start = radius * 0.2
        for k in range(3):
            phase = (self._t * 0.55 + k / 3.0) % 1.0
            r = start + phase * (radius * 0.76 - start)
            alpha = (1.0 - phase) ** 1.6 * 0.55 * strength * (0.55 + self._energy)
            p.setPen(QPen(with_alpha(color, min(1.0, alpha)), 2.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r, r)

    def _paint_dots(self, p: QPainter, cx, cy, radius, color, strength: float) -> None:
        if strength < 0.02:
            return
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(6):
            base = self._angles[3] * -1.6 + k * 60.0
            for trail in range(5):
                alpha = (1.0 - trail / 5.0) * 0.9 * strength
                p.setBrush(with_alpha(color, alpha))
                size = 4.2 - trail * 0.6
                p.drawEllipse(_polar(cx, cy, radius * 0.72, base - trail * 7.0), size, size)

    def _paint_core(self, p: QPainter, cx, cy, radius, color, energy, pulse) -> None:
        breathe = 1.0 + 0.05 * math.sin(self._t * 2.2) + 0.30 * energy * pulse
        core = radius * 0.19 * breathe
        halo = QRadialGradient(QPointF(cx, cy), core * 2.6)
        halo.setColorAt(0.0, with_alpha(color, 0.75))
        halo.setColorAt(0.45, with_alpha(color, 0.28))
        halo.setColorAt(1.0, with_alpha(color, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), core * 2.6, core * 2.6)
        sphere = QRadialGradient(QPointF(cx - core * 0.25, cy - core * 0.3), core * 1.1)
        sphere.setColorAt(0.0, QColor(255, 255, 255, 250))
        sphere.setColorAt(0.35, with_alpha(mix(color, QColor(255, 255, 255), 0.55), 0.95))
        sphere.setColorAt(1.0, with_alpha(color, 0.85))
        p.setBrush(QBrush(sphere))
        p.drawEllipse(QPointF(cx, cy), core, core)

    def _paint_brackets(self, p: QPainter, cx, cy, radius, color, strength: float) -> None:
        if strength < 0.02:
            return
        p.save()
        p.translate(cx, cy)
        p.rotate(-self._angles[3] * 0.5)
        half = radius * (0.62 + 0.03 * math.sin(self._t * 6.0))
        arm = radius * 0.14
        p.setPen(QPen(with_alpha(color, 0.95 * strength), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            corner = QPointF(sx * half, sy * half)
            p.drawLine(corner, QPointF(corner.x() - sx * arm, corner.y()))
            p.drawLine(corner, QPointF(corner.x(), corner.y() - sy * arm))
        p.restore()
