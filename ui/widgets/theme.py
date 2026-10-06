"""Shared palette, fonts and colour helpers for the HUD widgets."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont

BG = "#04070D"
CYAN = "#00E5FF"
GREEN = "#00E676"
AMBER = "#FFB300"
RED = "#FF3D71"
TEXT = "#B8E8F5"
TEXT_DIM = "#5E8A99"

STATE_COLORS = {
    "IDLE": "#00E5FF",
    "LISTENING": "#1DE9B6",
    "THINKING": "#B388FF",
    "SPEAKING": "#80F0FF",
    "EXECUTING": "#FFB300",
}
STATE_TEXT = {
    "IDLE": "[ IDLE ]",
    "LISTENING": "[ LISTENING ]",
    "THINKING": "[ THINKING / PROCESSING ]",
    "SPEAKING": "[ SPEAKING ]",
    "EXECUTING": "[ EXECUTING ACTION ]",
}
LEVEL_COLORS = {
    "info": "#7FD8E8",
    "ok": "#00E676",
    "warn": "#FFB300",
    "error": "#FF3D71",
    "action": "#FFFFFF",
    "dim": "#5E8A99",
}
SOURCE_COLORS = {
    "info": "#00E5FF",
    "ok": "#00E676",
    "warn": "#FFB300",
    "error": "#FF3D71",
    "action": "#FF7AF5",
    "dim": "#3E8CA0",
}
STATUS_COLORS = {
    "online": GREEN,
    "connecting": AMBER,
    "error": RED,
    "offline": "#6B7F89",
    "disabled": "#3C4A52",
}
STATUS_TEXT = {
    "online": "ONLINE",
    "connecting": "CONNECTING",
    "error": "ERROR",
    "offline": "OFFLINE",
    "disabled": "DISABLED",
}


def with_alpha(color: QColor | str, alpha: float) -> QColor:
    """Copy of ``color`` with alpha in 0..1."""
    out = QColor(color)
    out.setAlphaF(max(0.0, min(1.0, alpha)))
    return out


def mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


def level_color(percent: float) -> QColor:
    """Cyan -> amber (70 %) -> red (90 %) for load bars."""
    cyan, amber, red = QColor(CYAN), QColor(AMBER), QColor(RED)
    if percent < 60:
        return cyan
    if percent < 75:
        return mix(cyan, amber, (percent - 60) / 15)
    if percent < 88:
        return amber
    return mix(amber, red, (percent - 88) / 8)


def ui_font(size: float = 10.0, weight: QFont.Weight = QFont.Weight.Normal, spacing: float = 0.0) -> QFont:
    font = QFont()
    font.setFamilies(["Bahnschrift", "Segoe UI", "Arial"])
    font.setPointSizeF(size)
    font.setWeight(weight)
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return font


def mono_font(size: float = 9.5) -> QFont:
    font = QFont()
    font.setFamilies(["Cascadia Mono", "Cascadia Code", "Consolas", "Courier New"])
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSizeF(size)
    return font
