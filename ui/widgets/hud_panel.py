"""Translucent HUD panel with chamfered corners, corner brackets and a glowing title."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from .theme import CYAN, ui_font, with_alpha


class HudPanel(QFrame):
    """Container drawn like a sci-fi readout card; put content in :meth:`body`."""

    CUT = 12  # chamfer on the top-left and bottom-right corners

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("hudPanel")
        self._title = title.upper()
        self._body = QVBoxLayout(self)
        self._body.setContentsMargins(14, 40, 14, 14)
        self._body.setSpacing(10)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def body(self) -> QVBoxLayout:
        return self._body

    def set_title(self, title: str) -> None:
        self._title = title.upper()
        self.update()

    def _outline(self, rect: QRectF) -> QPainterPath:
        cut = self.CUT
        path = QPainterPath()
        path.moveTo(rect.left() + cut, rect.top())
        path.lineTo(rect.right(), rect.top())
        path.lineTo(rect.right(), rect.bottom() - cut)
        path.lineTo(rect.right() - cut, rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.lineTo(rect.left(), rect.top() + cut)
        path.closeSubpath()
        return path

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        outline = self._outline(rect)

        fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        fill.setColorAt(0.0, QColor(10, 28, 44, 190))
        fill.setColorAt(1.0, QColor(5, 14, 24, 200))
        painter.fillPath(outline, fill)
        painter.setPen(QPen(with_alpha(CYAN, 0.30), 1.0))
        painter.drawPath(outline)

        bright = QPen(with_alpha(CYAN, 0.95), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap)
        painter.setPen(bright)
        arm = 16.0
        tl = QPointF(rect.left(), rect.top() + self.CUT)  # chamfer ends
        painter.drawLine(tl, QPointF(tl.x(), tl.y() + arm - 4))
        painter.drawLine(QPointF(rect.left() + self.CUT, rect.top()), QPointF(rect.left() + self.CUT + arm - 4, rect.top()))
        painter.drawLine(QPointF(rect.right(), rect.top()), QPointF(rect.right() - arm, rect.top()))
        painter.drawLine(QPointF(rect.right(), rect.top()), QPointF(rect.right(), rect.top() + arm))
        painter.drawLine(QPointF(rect.left(), rect.bottom()), QPointF(rect.left() + arm, rect.bottom()))
        painter.drawLine(QPointF(rect.left(), rect.bottom()), QPointF(rect.left(), rect.bottom() - arm))
        br = QPointF(rect.right(), rect.bottom() - self.CUT)
        painter.drawLine(br, QPointF(br.x(), br.y() - arm + 4))
        painter.drawLine(QPointF(rect.right() - self.CUT, rect.bottom()), QPointF(rect.right() - self.CUT - arm + 4, rect.bottom()))

        painter.fillRect(QRectF(rect.left() + 16, rect.top() + 14, 3, 14), QColor(CYAN))
        painter.setPen(QColor(CYAN))
        painter.setFont(ui_font(9.0, QFont.Weight.DemiBold, 2.6))
        painter.drawText(QPointF(rect.left() + 28, rect.top() + 26), self._title)

        rule = QLinearGradient(rect.left() + 14, 0, rect.right() - 14, 0)
        rule.setColorAt(0.0, with_alpha(CYAN, 0.55))
        rule.setColorAt(1.0, with_alpha(CYAN, 0.0))
        painter.setPen(QPen(rule, 1.0))
        painter.drawLine(QPointF(rect.left() + 14, rect.top() + 34), QPointF(rect.right() - 14, rect.top() + 34))
