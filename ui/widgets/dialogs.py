"""Non-blocking HUD dialogs: approval requests and text prompts (2FA codes etc.)."""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .theme import AMBER, CYAN, GREEN, RED, mono_font, ui_font, with_alpha


class _Countdown(QWidget):
    """Thin bar that drains over the request timeout."""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(6)
        self._color = QColor(color)
        self._fraction = 1.0

    def set_fraction(self, fraction: float) -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), with_alpha(self._color, 0.14))
        painter.fillRect(QRectF(0, 0, self.width() * self._fraction, self.height()), self._color)


class _HudDialog(QDialog):
    """Frameless, always-on-top, draggable card. Subclasses call ``_finish`` exactly once."""

    _open_count = 0

    def __init__(self, heading: str, accent: str, timeout: float) -> None:
        super().__init__(None, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("hudDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumWidth(480)
        self._accent = QColor(accent)
        self._finished = False
        self._timeout = max(1.0, float(timeout))
        self._deadline = time.monotonic() + self._timeout
        self._drag_offset: QPointF | None = None

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(22, 20, 22, 18)
        self._root.setSpacing(12)
        title = QLabel(heading.upper(), self)
        title.setObjectName("dialogTitle")
        title.setStyleSheet(f"color: {accent};")
        title.setFont(ui_font(11.0, QFont.Weight.DemiBold, 2.6))
        self._root.addWidget(title)
        self._countdown = _Countdown(accent, self)
        self._root.addWidget(self._countdown)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # -- subclass contract --------------------------------------------------
    def _result_on_timeout(self) -> None:
        raise NotImplementedError

    def _buttons(self, *buttons: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addStretch(1)
        for button in buttons:
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumWidth(120)
            row.addWidget(button)
        self._root.addLayout(row)
        return row

    # -- lifecycle -----------------------------------------------------------
    def _tick(self) -> None:
        remaining = self._deadline - time.monotonic()
        self._countdown.set_fraction(remaining / self._timeout)
        if remaining <= 0:
            self._result_on_timeout()

    def _stop(self) -> bool:
        """True the first time only."""
        if self._finished:
            return False
        self._finished = True
        self._timer.stop()
        return True

    def dismiss(self) -> None:
        """Close silently (answered elsewhere, e.g. by voice)."""
        self._stop()
        self.close()

    def present(self) -> None:
        screen = QGuiApplication.primaryScreen()
        self.adjustSize()
        if screen is not None:
            area = screen.availableGeometry()
            offset = 28 * (_HudDialog._open_count % 5)
            self.move(area.center().x() - self.width() // 2 + offset, area.center().y() - self.height() // 2 - 60 + offset)
        _HudDialog._open_count += 1
        self.destroyed.connect(self._released)
        self.show()
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _released(*_args) -> None:
        _HudDialog._open_count = max(0, _HudDialog._open_count - 1)

    # -- painting & dragging -------------------------------------------------
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        cut = 16.0
        path = QPainterPath()
        path.moveTo(rect.left() + cut, rect.top())
        path.lineTo(rect.right(), rect.top())
        path.lineTo(rect.right(), rect.bottom() - cut)
        path.lineTo(rect.right() - cut, rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.lineTo(rect.left(), rect.top() + cut)
        path.closeSubpath()
        painter.fillPath(path, QColor(5, 13, 22, 245))
        painter.setPen(QPen(with_alpha(self._accent, 0.9), 1.6))
        painter.drawPath(path)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition() - QPointF(self.pos())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move((event.globalPosition() - self._drag_offset).toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class ConfirmDialog(_HudDialog):
    """Approve / deny an action. Closing, Esc and the timeout all mean *deny*."""

    answered = Signal(bool)

    def __init__(self, title: str, details: str, timeout: float) -> None:
        super().__init__(f"⚠ {title}", AMBER, timeout)
        box = QPlainTextEdit(self)
        box.setObjectName("dialogDetails")
        box.setReadOnly(True)
        box.setFont(mono_font(10.0))
        box.setPlainText(details)
        box.setMinimumHeight(90)
        box.setMaximumHeight(220)
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._root.addWidget(box)
        hint = QLabel("Say “yes” or “no”, or use the buttons. No answer = deny.", self)
        hint.setObjectName("dialogHint")
        self._root.addWidget(hint)
        approve = QPushButton("APPROVE", self)
        approve.setObjectName("approveButton")
        deny = QPushButton("DENY", self)
        deny.setObjectName("denyButton")
        approve.clicked.connect(lambda: self._finish(True))
        deny.clicked.connect(lambda: self._finish(False))
        deny.setDefault(True)
        self._buttons(deny, approve)

    def _finish(self, approved: bool) -> None:
        if self._stop():
            self.answered.emit(approved)
            self.close()

    def _result_on_timeout(self) -> None:
        self._finish(False)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._finish(False)
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self._stop():
            self.answered.emit(False)
        super().closeEvent(event)


class TextPromptDialog(_HudDialog):
    """Ask for a line of text (login codes, 2FA passwords). Cancel / timeout => ``None``."""

    answered = Signal(object)

    def __init__(self, title: str, prompt: str, secret: bool, timeout: float) -> None:
        super().__init__(title, CYAN, timeout)
        label = QLabel(prompt, self)
        label.setObjectName("dialogPrompt")
        label.setWordWrap(True)
        self._root.addWidget(label)
        self._edit = QLineEdit(self)
        self._edit.setObjectName("dialogInput")
        self._edit.setFont(mono_font(11.0))
        if secret:
            self._edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._root.addWidget(self._edit)
        ok = QPushButton("OK", self)
        ok.setObjectName("approveButton")
        cancel = QPushButton("CANCEL", self)
        cancel.setObjectName("denyButton")
        ok.setDefault(True)
        ok.clicked.connect(self._submit)
        cancel.clicked.connect(lambda: self._finish(None))
        self._edit.returnPressed.connect(self._submit)
        self._buttons(cancel, ok)

    def present(self) -> None:
        super().present()
        self._edit.setFocus()

    def _submit(self) -> None:
        self._finish(self._edit.text())

    def _finish(self, value: str | None) -> None:
        if self._stop():
            self.answered.emit(value)
            self.close()

    def _result_on_timeout(self) -> None:
        self._finish(None)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self._stop():
            self.answered.emit(None)
        super().closeEvent(event)
