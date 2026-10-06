"""Terminal-style activity console with batched appends and a LIVE jump button."""
from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu, QPlainTextEdit, QPushButton, QWidget

from .theme import LEVEL_COLORS, SOURCE_COLORS, TEXT_DIM, mono_font


class LogConsole(QPlainTextEdit):
    """``[HH:MM:SS] SOURCE: message`` lines, colour-coded by level."""

    MAX_LINES = 3000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("logConsole")
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setMaximumBlockCount(self.MAX_LINES)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setFont(mono_font(9.5))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        self._pending: list[str] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)  # a flood of lines costs one repaint per 50 ms, not per line
        self._timer.timeout.connect(self._flush)

        self._live = QPushButton("▼ LIVE", self)
        self._live.setObjectName("liveButton")
        self._live.setCursor(Qt.CursorShape.PointingHandCursor)
        self._live.hide()
        self._live.clicked.connect(self.scroll_to_end)
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._sync_live)
        bar.rangeChanged.connect(self._sync_live)

    # ------------------------------------------------------------------ API
    def add_line(self, source: str, message: str, level: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        text_color = LEVEL_COLORS.get(level, LEVEL_COLORS["info"])
        source_color = SOURCE_COLORS.get(level, SOURCE_COLORS["info"])
        body = html.escape(message).replace("\n", "<br>")
        self._pending.append(
            f'<span style="color:{TEXT_DIM}">[{stamp}]</span> '
            f'<span style="color:{source_color};font-weight:600">{html.escape(source)}:</span> '
            f'<span style="color:{text_color}">{body}</span>'
        )
        if not self._timer.isActive():
            self._timer.start()

    def scroll_to_end(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear_log(self) -> None:
        self._pending.clear()
        self.clear()

    # ------------------------------------------------------------- internals
    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _flush(self) -> None:
        if not self._pending:
            return
        follow = self._at_bottom()
        lines, self._pending = self._pending, []
        self.setUpdatesEnabled(False)
        try:
            for line in lines[-self.MAX_LINES:]:
                self.appendHtml(line)
        finally:
            self.setUpdatesEnabled(True)
        if follow:
            self.scroll_to_end()

    def _sync_live(self, *_args) -> None:
        self._live.setVisible(not self._at_bottom())
        self._place_live()

    def _place_live(self) -> None:
        self._live.adjustSize()
        self._live.move(self.viewport().width() - self._live.width() - 14, self.height() - self._live.height() - 10)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_live()

    def _show_menu(self, position) -> None:
        menu = QMenu(self)
        menu.addAction("Copy all", lambda: QGuiApplication.clipboard().setText(self.toPlainText()))
        menu.addAction("Clear", self.clear_log)
        menu.exec(self.mapToGlobal(position))
