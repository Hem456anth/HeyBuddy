"""Quick-ask box: double-tap Ctrl, type a question, press Enter.

HeyClicky opens a small text box on a Ctrl double-tap so the user can type
instead of talk (same turn pipeline, screenshot included). HeyBuddy's box
appears just below the notch on the screen under the cursor; Esc or
clicking elsewhere closes it.
"""
from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication, QKeyEvent
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QWidget

from ..utils.logger import get_logger
from ..utils.win32 import exclude_window_from_capture, force_foreground_window
from . import theme

log = get_logger(__name__)


class _AskLine(QLineEdit):
    escape_pressed = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            return
        super().keyPressEvent(event)


class QuickAskWindow(QWidget):
    submitted = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(520, 52)
        container = QWidget(self)
        container.setObjectName("quickAsk")
        container.setGeometry(0, 0, 520, 52)
        container.setStyleSheet(
            f"#quickAsk {{ background: #191A1F; border: 1px solid rgba(255,255,255,26); border-radius: 14px; }}"
            f"QLineEdit {{ background: transparent; border: none; color: #ECECEF; font-size: 15px; }}"
            f"QLabel {{ color: {theme.Color.TEXT_DIM}; font-size: 11px; }}"
        )
        layout = QHBoxLayout(container)
        layout.setContentsMargins(16, 6, 12, 6)
        self.line = _AskLine(container)
        self.line.setPlaceholderText("Ask HeyBuddy about your screen...")
        self.line.returnPressed.connect(self._submit)
        self.line.escape_pressed.connect(self.hide)
        hint = QLabel("Enter to ask, Esc to close", container)
        layout.addWidget(self.line, 1)
        layout.addWidget(hint)

    def open_near_notch(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            geometry = screen.geometry()
            self.move(QPoint(geometry.x() + (geometry.width() - self.width()) // 2, geometry.y() + 44))
        self.line.clear()
        self.show()
        self.raise_()
        self.activateWindow()
        try:
            hwnd = int(self.winId())
            exclude_window_from_capture(hwnd)
            force_foreground_window(hwnd)
        except Exception:
            log.exception("Quick ask focus failed")
        self.line.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _submit(self) -> None:
        text = self.line.text().strip()
        self.hide()
        if text:
            self.submitted.emit(text)

    def changeEvent(self, event) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if event.type() == event.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            self.hide()
