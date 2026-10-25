"""The notch: a black pill at the top centre of the screen under the cursor.

Reconstructed from HeyClicky's Windows "notch" (docs/HEYCLICKY_TEARDOWN.md,
"UI"): a pure-black pill, about 60 x 8 when idle, that widens into an
activity strip (about 360 x 32) while the buddy listens (cyan bars driven
by the mic), thinks (pink dots) or speaks (orange bars). Hovering it for
0.45 s opens the main panel; clicking it toggles the panel. It is excluded
from screen capture like every other HeyBuddy surface.

Drawn with HeyBuddy's own QPainter code; sizes are logical points.
"""
from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget

from ..core.companion_manager import CompanionState
from ..utils.logger import get_logger
from ..utils.win32 import apply_panel_window_styles, exclude_window_from_capture

log = get_logger(__name__)

IDLE_SIZE = (60.0, 8.0)
ACTIVE_SIZE = (360.0, 32.0)
JOB_SIZE = (300.0, 26.0)
WINDOW_SIZE = (380, 40)          # fixed window; the pill animates inside it
HOVER_OPEN_SECONDS = 0.45
SPRING_RATE = 14.0               # exponential approach per second
LISTEN_COLOR = QColor("#22D3EE")
THINK_COLOR = QColor("#F472B6")
SPEAK_COLOR = QColor("#FB923C")
TEXT_COLOR = QColor("#ECECEF")


class NotchWindow(QWidget):
    hover_opened = pyqtSignal()
    clicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setFixedSize(*WINDOW_SIZE)
        self._state = CompanionState.IDLE
        self._level = 0.0
        self._caption = ""
        self._job_caption = ""
        self._job_until = 0.0
        self._size = list(IDLE_SIZE)
        self._hover_since: float | None = None
        self._hover_fired = False
        self._last_tick = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._screen_timer = QTimer(self)
        self._screen_timer.setInterval(500)
        self._screen_timer.timeout.connect(self._follow_cursor_screen)
        self._screen_timer.start()
        self._follow_cursor_screen()

    # ----- inputs -----
    def set_state(self, state: CompanionState) -> None:
        self._state = state
        if state == CompanionState.LISTENING:
            self._caption = ""

    def set_level(self, level: float) -> None:
        self._level = max(float(level), self._level * 0.75)

    def set_caption(self, text: str) -> None:
        self._caption = " ".join((text or "").split())

    def flash_job(self, text: str, seconds: float = 6.0) -> None:
        self._job_caption = text
        self._job_until = time.monotonic() + seconds

    # ----- geometry -----
    def _follow_cursor_screen(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.geometry()
        target = QPoint(geometry.x() + (geometry.width() - self.width()) // 2, geometry.y())
        if self.pos() != target:
            self.move(target)

    def _target_size(self) -> tuple[float, float]:
        if self._state != CompanionState.IDLE:
            return ACTIVE_SIZE
        if self._job_caption and time.monotonic() < self._job_until:
            return JOB_SIZE
        return IDLE_SIZE

    def pill_rect(self) -> QRectF:
        width, height = self._size
        return QRectF((self.width() - width) / 2.0, 0.0, width, height)

    # ----- animation -----
    def _tick(self) -> None:
        now = time.monotonic()
        elapsed = min(0.1, now - self._last_tick)
        self._last_tick = now
        target_width, target_height = self._target_size()
        blend = 1.0 - math.exp(-SPRING_RATE * elapsed)
        self._size[0] += (target_width - self._size[0]) * blend
        self._size[1] += (target_height - self._size[1]) * blend
        if self._state != CompanionState.LISTENING:
            self._level *= 0.9
        # hover-to-open
        inside = self.pill_rect().adjusted(-6, 0, 6, 8).contains(self.mapFromGlobal(QCursor.pos()).toPointF())
        if inside:
            if self._hover_since is None:
                self._hover_since = now
            elif not self._hover_fired and now - self._hover_since >= HOVER_OPEN_SECONDS:
                self._hover_fired = True
                self.hover_opened.emit()
        else:
            self._hover_since = None
            self._hover_fired = False
        self.update()

    # ----- events -----
    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        try:
            hwnd = int(self.winId())
            apply_panel_window_styles(hwnd)
            exclude_window_from_capture(hwnd)
        except Exception:
            log.exception("Notch window styles failed")

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if self.pill_rect().adjusted(-6, 0, 6, 8).contains(event.position()):
            self.clicked.emit()

    # ----- painting -----
    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.pill_rect()
        radius = rect.height() / 2.0
        path = QPainterPath()
        # Flat top edge flush with the screen, rounded bottom, small concave
        # "shoulders" so it reads as part of the bezel.
        shoulder = min(6.0, radius)
        path.moveTo(rect.left() - shoulder, rect.top())
        path.quadTo(rect.left(), rect.top(), rect.left(), rect.top() + shoulder)
        path.lineTo(rect.left(), rect.bottom() - radius)
        path.quadTo(rect.left(), rect.bottom(), rect.left() + radius, rect.bottom())
        path.lineTo(rect.right() - radius, rect.bottom())
        path.quadTo(rect.right(), rect.bottom(), rect.right(), rect.bottom() - radius)
        path.lineTo(rect.right(), rect.top() + shoulder)
        path.quadTo(rect.right(), rect.top(), rect.right() + shoulder, rect.top())
        path.closeSubpath()
        painter.fillPath(path, QColor(0, 0, 0))
        if rect.height() < 18:
            if self._state == CompanionState.IDLE and self._job_caption and time.monotonic() < self._job_until:
                pass
            else:
                painter.end()
                return
        now = time.monotonic()
        visual_right = rect.left() + 18.0
        center_y = rect.center().y()
        if self._state == CompanionState.LISTENING:
            self._paint_bars(painter, visual_right, center_y, LISTEN_COLOR, self._level, now)
        elif self._state == CompanionState.PROCESSING:
            self._paint_dots(painter, visual_right, center_y, now)
        elif self._state == CompanionState.RESPONDING:
            self._paint_bars(painter, visual_right, center_y, SPEAK_COLOR, 0.55 + 0.45 * abs(math.sin(now * 5)), now)
        caption = self._caption if self._state != CompanionState.IDLE else self._job_caption
        if caption and rect.height() >= 18:
            font = QFont("Segoe UI Variable", 9)
            painter.setFont(font)
            painter.setPen(TEXT_COLOR)
            text_rect = QRectF(visual_right + 40.0, rect.top(), rect.right() - visual_right - 52.0, rect.height())
            elided = QFontMetricsF(font).elidedText(caption, Qt.TextElideMode.ElideLeft, text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        painter.end()

    @staticmethod
    def _paint_bars(painter: QPainter, left: float, center_y: float, color: QColor, level: float, now: float) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for index, weight in enumerate((0.4, 0.7, 1.0, 0.7, 0.4)):
            jitter = 0.85 + 0.15 * math.sin(now * 9 + index * 1.3)
            height = 4.0 + 14.0 * max(0.08, min(1.0, level)) * weight * jitter
            painter.drawRoundedRect(QRectF(left + index * 6.0, center_y - height / 2.0, 3.0, height), 1.5, 1.5)

    @staticmethod
    def _paint_dots(painter: QPainter, left: float, center_y: float, now: float) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(THINK_COLOR)
        for index in range(3):
            phase = now * 6.0 - index * 0.7
            lift = 3.0 * max(0.0, math.sin(phase))
            painter.drawEllipse(QRectF(left + index * 9.0, center_y - 2.5 - lift, 5.0, 5.0))
