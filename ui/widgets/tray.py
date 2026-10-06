"""System-tray icon (generated, no image files) and its menu."""
from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .theme import CYAN


def make_icon(size: int = 64) -> QIcon:
    """Arc-reactor style ring drawn with QPainter at several sizes."""
    icon = QIcon()
    for px in (16, 24, 32, 48, size):
        pixmap = QPixmap(px, px)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = px / 2.0
        cyan = QColor(CYAN)
        painter.setPen(QPen(cyan, max(1.2, px * 0.075)))
        painter.drawEllipse(QPointF(c, c), px * 0.40, px * 0.40)
        painter.setPen(QPen(QColor(255, 255, 255, 235), max(1.2, px * 0.075), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for start in (20, 140, 260):
            rect = QRectF(c - px * 0.27, c - px * 0.27, px * 0.54, px * 0.54)
            painter.drawArc(rect, start * 16, 60 * 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(cyan)
        painter.drawEllipse(QPointF(c, c), px * 0.11, px * 0.11)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class Tray(QObject):
    toggle_window = Signal()
    mute_toggled = Signal(bool)
    quit_requested = Signal()

    def __init__(self, parent: QWidget, tooltip: str) -> None:
        super().__init__(parent)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.icon = QSystemTrayIcon(make_icon(), parent)
        self.icon.setToolTip(tooltip)
        menu = QMenu()
        menu.addAction("Show / Hide", self.toggle_window.emit)
        self._mute = menu.addAction("Mute voice")
        self._mute.setCheckable(True)
        self._mute.toggled.connect(self.mute_toggled.emit)
        menu.addSeparator()
        menu.addAction("Quit J.A.R.V.I.S", self.quit_requested.emit)
        self._menu = menu  # keep a reference: the tray does not take ownership
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._activated)
        if self.available:
            self.icon.show()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick, QSystemTrayIcon.ActivationReason.Trigger):
            self.toggle_window.emit()

    def notify(self, title: str, message: str) -> bool:
        if not self.available:
            return False
        self.icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)
        return True

    def hide(self) -> None:
        self.icon.hide()
