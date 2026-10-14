"""The blue cursor buddy: click-through overlays, one per monitor.

Port of Clicky's `OverlayWindowManager` + `BlueCursorView`
(`OverlayWindow.swift`). What the user sees:

* a small glowing blue triangle that rides next to the mouse pointer;
* while push-to-talk is held it turns into a 5-bar waveform driven by the
  mic level; while Claude thinks it becomes a spinner;
* when Claude answers with `[POINT:...]` it swoops along a bezier arc to
  the element, types a little speech bubble with the label, holds, and
  flies back to the mouse.

Design notes:

* One frameless, translucent, top-most window per `QScreen` (like Clicky's
  one `NSPanel` per `NSScreen`). A single window spanning a mixed-DPI
  desktop does not work with Qt 6: each screen has its own device-pixel
  ratio, so physical -> widget coordinates are only well-defined per
  screen. Motion is computed in physical desktop pixels by
  `core.buddy_motion.BuddyNavigator`; each window converts to its own
  logical coordinates when painting.
* Every window is WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE |
  WS_EX_TOOLWINDOW (clicks fall through, never steals focus, not on the
  taskbar) and excluded from screen capture so Claude never sees the
  buddy in its screenshots.
* A 60 fps QTimer drives the frame clock (Clicky uses a 16 ms Timer).
  Only the small rectangle around the buddy is repainted each frame.
"""
from __future__ import annotations

import math
import time
from typing import Iterable

from PyQt6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, pyqtSlot
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QScreen,
    QShowEvent,
)
from PyQt6.QtWidgets import QWidget

from ..core.buddy_motion import BuddyNavigator, BuddyPose, NavigationMode, PointingTarget
from ..core.companion_manager import CompanionState
from ..models.config import AppConfig
from ..models.message import PointMarker
from ..utils.logger import get_logger
from ..utils.win32 import (
    MonitorInfo,
    apply_overlay_window_styles,
    enumerate_monitors,
    exclude_window_from_capture,
    get_cursor_position,
)

log = get_logger(__name__)

BUDDY_BLUE = QColor("#3380FF")              # Clicky's DS.Colors.overlayCursorBlue
TRIANGLE_SIZE_POINTS = 16.0
WAVEFORM_BAR_PROFILE = (0.4, 0.7, 1.0, 0.7, 0.4)
FRAME_INTERVAL_MS = 16
TRANSIENT_FADE_SECONDS = 0.35
TRANSIENT_LINGER_SECONDS = 1.0               # Clicky waits 1 s after everything ends
REPAINT_PADDING_POINTS = 48.0


def _monitor_containing(monitors: Iterable[MonitorInfo], x: float, y: float) -> MonitorInfo | None:
    monitors = list(monitors)
    for monitor in monitors:
        if monitor.left <= x < monitor.left + monitor.width and monitor.top <= y < monitor.top + monitor.height:
            return monitor
    if not monitors:
        return None

    def distance_squared(monitor: MonitorInfo) -> float:
        nearest_x = min(max(x, monitor.left), monitor.left + monitor.width)
        nearest_y = min(max(y, monitor.top), monitor.top + monitor.height)
        return (x - nearest_x) ** 2 + (y - nearest_y) ** 2

    return min(monitors, key=distance_squared)


def _match_monitor_for_screen(screen: QScreen, monitors: list[MonitorInfo], fallback_index: int) -> MonitorInfo | None:
    """Pair a QScreen with the Win32 monitor it represents.

    Qt 6 on Windows keeps each screen's *native* top-left as the origin of
    `QScreen.geometry()` and scales only the size, so the top-left is the
    reliable key. Fall back to matching physical size, then to order.
    """
    geometry = screen.geometry()
    for monitor in monitors:
        if monitor.left == geometry.x() and monitor.top == geometry.y():
            return monitor
    device_pixel_ratio = screen.devicePixelRatio()
    for monitor in monitors:
        if (
            abs(monitor.width - geometry.width() * device_pixel_ratio) <= 2
            and abs(monitor.height - geometry.height() * device_pixel_ratio) <= 2
        ):
            return monitor
    if 0 <= fallback_index < len(monitors):
        return monitors[fallback_index]
    return None


class _ScreenOverlayWindow(QWidget):
    """Transparent, click-through canvas covering exactly one monitor."""

    def __init__(self, controller: "BuddyOverlayController", screen: QScreen, monitor: MonitorInfo) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self._controller = controller
        self.monitor = monitor
        self.qt_screen = screen
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self._last_dirty_rect: QRect | None = None
        self._styles_applied = False

    # ----- coordinate mapping -----
    @property
    def device_pixel_ratio(self) -> float:
        return float(self.qt_screen.devicePixelRatio() or 1.0)

    def physical_to_local(self, physical_x: float, physical_y: float) -> QPointF:
        ratio = self.device_pixel_ratio
        return QPointF((physical_x - self.monitor.left) / ratio, (physical_y - self.monitor.top) / ratio)

    def contains_physical(self, physical_x: float, physical_y: float) -> bool:
        return (
            self.monitor.left <= physical_x < self.monitor.left + self.monitor.width
            and self.monitor.top <= physical_y < self.monitor.top + self.monitor.height
        )

    # ----- native setup -----
    def showEvent(self, event: QShowEvent) -> None:  # type: ignore[override]
        super().showEvent(event)
        try:
            hwnd = int(self.winId())
            apply_overlay_window_styles(hwnd)
            excluded = exclude_window_from_capture(hwnd)
            if not self._styles_applied:
                self._styles_applied = True
                log.info(
                    "Buddy overlay on monitor %d (%dx%d @ %.2fx): click-through styles applied, "
                    "excluded from capture=%s",
                    self.monitor.index, self.monitor.width, self.monitor.height,
                    self.device_pixel_ratio, excluded,
                )
        except Exception:
            log.exception("Failed to apply overlay window styles")

    # ----- repaint scheduling -----
    def schedule_repaint_around(self, rect: QRect | None) -> None:
        dirty = rect
        if self._last_dirty_rect is not None:
            dirty = self._last_dirty_rect if dirty is None else dirty.united(self._last_dirty_rect)
        self._last_dirty_rect = rect
        if dirty is not None:
            self.update(dirty)

    # ----- painting -----
    def paintEvent(self, _event) -> None:  # type: ignore[override]
        frame = self._controller.current_frame
        if frame is None or frame.opacity <= 0.001:
            return
        pose = frame.pose
        if not self.contains_physical(pose.x, pose.y):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(frame.opacity)
        center = self.physical_to_local(pose.x, pose.y)
        state = frame.companion_state
        if state == CompanionState.LISTENING and pose.mode == NavigationMode.FOLLOWING:
            _paint_waveform(painter, center, frame.audio_level, frame.clock)
        elif state == CompanionState.PROCESSING and pose.mode == NavigationMode.FOLLOWING:
            _paint_spinner(painter, center, frame.clock)
        else:
            _paint_triangle(painter, center, pose.rotation_degrees, pose.scale)
        if pose.bubble_text and pose.bubble_opacity > 0.0:
            _paint_bubble(painter, center, pose.bubble_text, pose.bubble_opacity * frame.opacity, pose.bubble_scale)
        painter.end()


class _Frame:
    __slots__ = ("pose", "opacity", "companion_state", "audio_level", "clock")

    def __init__(self, pose: BuddyPose, opacity: float, companion_state: CompanionState,
                 audio_level: float, clock: float) -> None:
        self.pose = pose
        self.opacity = opacity
        self.companion_state = companion_state
        self.audio_level = audio_level
        self.clock = clock


class BuddyOverlayController(QObject):
    """Owns the per-screen windows, the frame clock and the navigator."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._monitors: list[MonitorInfo] = []
        self._windows: list[_ScreenOverlayWindow] = []
        self._navigator = BuddyNavigator(scale_at=self._scale_at, clamp_to_screen=self._clamp_to_screen)
        self._companion_state = CompanionState.IDLE
        self._audio_level = 0.0
        self._opacity = 1.0 if config.show_cursor_buddy else 0.0
        self._last_activity_time = 0.0
        self.current_frame: _Frame | None = None
        self._frame_timer = QTimer(self)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.setInterval(FRAME_INTERVAL_MS)
        self._frame_timer.timeout.connect(self._on_frame)
        self._last_frame_time = time.monotonic()

        application = QGuiApplication.instance()
        if application is not None:
            application.screenAdded.connect(lambda _screen: QTimer.singleShot(500, self.rebuild_windows))
            application.screenRemoved.connect(lambda _screen: QTimer.singleShot(500, self.rebuild_windows))
        self.rebuild_windows()
        self._frame_timer.start()

    # ----- window management -----
    @pyqtSlot()
    def rebuild_windows(self) -> None:
        for window in self._windows:
            window.hide()
            window.deleteLater()
        self._windows = []
        self._monitors = enumerate_monitors()
        for index, screen in enumerate(QGuiApplication.screens()):
            monitor = _match_monitor_for_screen(screen, self._monitors, index)
            if monitor is None:
                continue
            window = _ScreenOverlayWindow(self, screen, monitor)
            self._windows.append(window)
        log.info("Buddy overlay covering %d screen(s)", len(self._windows))
        self._sync_window_visibility()

    def _scale_at(self, x: float, y: float) -> float:
        monitor = _monitor_containing(self._monitors, x, y)
        return monitor.dpi_scale if monitor and monitor.dpi_scale > 0 else 1.0

    def _clamp_to_screen(self, x: float, y: float) -> tuple[float, float]:
        monitor = _monitor_containing(self._monitors, x, y)
        if monitor is None:
            return x, y
        padding = 20.0 * (monitor.dpi_scale or 1.0)
        return (
            max(monitor.left + padding, min(x, monitor.left + monitor.width - padding)),
            max(monitor.top + padding, min(y, monitor.top + monitor.height - padding)),
        )

    # ----- inputs from the app -----
    @pyqtSlot(object)
    def set_companion_state(self, state: CompanionState) -> None:
        self._companion_state = state
        self._last_activity_time = time.monotonic()
        if state == CompanionState.LISTENING:
            # A new question always wins over an old pointing animation.
            self._navigator.cancel()

    @pyqtSlot(float)
    def set_audio_level(self, level: float) -> None:
        # Fast attack, slower release, like a VU meter.
        level = max(0.0, min(1.0, float(level)))
        self._audio_level = level if level > self._audio_level else self._audio_level * 0.7 + level * 0.3

    @pyqtSlot(object)
    def show_pointing(self, markers: list[PointMarker]) -> None:
        targets = [
            PointingTarget(float(marker.physical_x), float(marker.physical_y), marker.label)
            for marker in markers
            if marker.is_resolved
        ]
        if not targets:
            return
        for marker in markers:
            log.info(
                "POINT flight: screen%s image=(%d,%d) -> physical=(%s,%s) label=%r",
                marker.screen_index, marker.x, marker.y, marker.physical_x, marker.physical_y, marker.label,
            )
        cursor_x, cursor_y = get_cursor_position()
        self._last_activity_time = time.monotonic()
        self._navigator.point_at(targets, time.monotonic(), (float(cursor_x), float(cursor_y)))

    @pyqtSlot(object)
    def point_at(self, marker: PointMarker) -> None:
        """Back-compat single-marker entry point."""
        self.show_pointing([marker])

    @pyqtSlot()
    def cancel_pointing(self) -> None:
        self._navigator.cancel()

    @pyqtSlot()
    def apply_config(self) -> None:
        self._last_activity_time = time.monotonic()

    # ----- frame clock -----
    def _target_opacity(self, now: float) -> float:
        if self.config.show_cursor_buddy:
            return 1.0
        busy = self._companion_state != CompanionState.IDLE or self._navigator.is_navigating
        if busy:
            self._last_activity_time = now
            return 1.0
        return 1.0 if now - self._last_activity_time < TRANSIENT_LINGER_SECONDS else 0.0

    def _on_frame(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_frame_time
        self._last_frame_time = now

        target_opacity = self._target_opacity(now)
        if self._opacity != target_opacity:
            step = elapsed / TRANSIENT_FADE_SECONDS
            if self._opacity < target_opacity:
                self._opacity = min(target_opacity, self._opacity + step)
            else:
                self._opacity = max(target_opacity, self._opacity - step)
        self._sync_window_visibility()
        if self._opacity <= 0.0 and not self._navigator.is_navigating:
            self.current_frame = None
            return

        cursor_x, cursor_y = get_cursor_position()
        pose = self._navigator.tick(now, (float(cursor_x), float(cursor_y)))
        if self._companion_state != CompanionState.LISTENING:
            self._audio_level *= 0.85
        self.current_frame = _Frame(pose, self._opacity, self._companion_state, self._audio_level, now)

        for window in self._windows:
            if not window.isVisible():
                continue
            if window.contains_physical(pose.x, pose.y):
                center = window.physical_to_local(pose.x, pose.y)
                padding = REPAINT_PADDING_POINTS * max(pose.scale, 1.0)
                bubble_width = 12.0 + 7.5 * len(pose.bubble_text) + 24.0 if pose.bubble_text else 0.0
                dirty = QRectF(
                    center.x() - padding, center.y() - padding,
                    padding * 2 + bubble_width, padding * 2 + 12.0,
                ).toAlignedRect()
                window.schedule_repaint_around(dirty)
            else:
                window.schedule_repaint_around(None)

    def _sync_window_visibility(self) -> None:
        should_show = self._opacity > 0.0 or self._navigator.is_navigating
        for window in self._windows:
            if should_show and not window.isVisible():
                window.show()
            elif not should_show and window.isVisible():
                window.hide()

    def shutdown(self) -> None:
        self._frame_timer.stop()
        for window in self._windows:
            window.hide()
            window.deleteLater()
        self._windows = []


# Back-compat name used by older code and docs.
CursorOverlay = BuddyOverlayController


# ---------------------------------------------------------------------------
# Painters (all sizes in logical points; Qt scales them per screen)
# ---------------------------------------------------------------------------


def _paint_glow(painter: QPainter, center: QPointF, radius: float, alpha: int) -> None:
    gradient = QRadialGradient(center, radius)
    inner = QColor(BUDDY_BLUE)
    inner.setAlpha(alpha)
    outer = QColor(BUDDY_BLUE)
    outer.setAlpha(0)
    gradient.setColorAt(0.0, inner)
    gradient.setColorAt(1.0, outer)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawEllipse(center, radius, radius)


def _paint_triangle(painter: QPainter, center: QPointF, rotation_degrees: float, scale: float) -> None:
    # Clicky's glow radius grows from 8 to ~14 during flight.
    _paint_glow(painter, center, (8.0 + (scale - 1.0) * 20.0) * 2.0 * scale, 150)
    size = TRIANGLE_SIZE_POINTS
    height = size * math.sqrt(3.0) / 2.0
    path = QPainterPath()
    path.moveTo(0.0, -height / 1.5)
    path.lineTo(-size / 2.0, height / 3.0)
    path.lineTo(size / 2.0, height / 3.0)
    path.closeSubpath()
    painter.save()
    painter.translate(center)
    painter.rotate(rotation_degrees)
    painter.scale(scale, scale)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(BUDDY_BLUE)
    painter.drawPath(path)
    painter.restore()


def _paint_waveform(painter: QPainter, center: QPointF, audio_level: float, clock: float) -> None:
    _paint_glow(painter, center, 18.0, 90)
    bar_width, bar_spacing = 2.0, 2.0
    total_width = len(WAVEFORM_BAR_PROFILE) * bar_width + (len(WAVEFORM_BAR_PROFILE) - 1) * bar_spacing
    normalized_level = max(audio_level - 0.008, 0.0)
    eased_level = math.pow(min(normalized_level * 2.85, 1.0), 0.76)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(BUDDY_BLUE)
    for bar_index, profile in enumerate(WAVEFORM_BAR_PROFILE):
        phase = clock * 3.6 + bar_index * 0.35
        idle_pulse = (math.sin(phase) + 1.0) / 2.0 * 1.5
        bar_height = 3.0 + eased_level * 10.0 * profile + idle_pulse
        left = center.x() - total_width / 2.0 + bar_index * (bar_width + bar_spacing)
        painter.drawRoundedRect(QRectF(left, center.y() - bar_height / 2.0, bar_width, bar_height), 1.5, 1.5)


def _paint_spinner(painter: QPainter, center: QPointF, clock: float) -> None:
    _paint_glow(painter, center, 16.0, 90)
    radius = 7.0
    angle_degrees = (clock / 0.8 * 360.0) % 360.0
    gradient = QConicalGradient(center, -angle_degrees)
    transparent_blue = QColor(BUDDY_BLUE)
    transparent_blue.setAlpha(0)
    gradient.setColorAt(0.0, BUDDY_BLUE)
    gradient.setColorAt(1.0, transparent_blue)
    pen = QPen(QBrush(gradient), 2.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    arc_rect = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
    # trim(0.15 -> 0.85) = a 252 degree arc, rotating.
    painter.drawArc(arc_rect, int(-angle_degrees * 16), int(252 * 16))


def _paint_bubble(painter: QPainter, center: QPointF, text: str, opacity: float, scale: float) -> None:
    font = QFont("Segoe UI", 9)
    font.setWeight(QFont.Weight.Medium)
    metrics = QFontMetricsF(font)
    width = metrics.horizontalAdvance(text) + 16.0
    height = metrics.height() + 8.0
    # Clicky: x = cursor.x + 10 + width / 2 (center), y = cursor.y + 18.
    bubble_center = QPointF(center.x() + 10.0 + width / 2.0, center.y() + 18.0)
    painter.save()
    painter.setOpacity(max(0.0, min(1.0, opacity)))
    painter.translate(bubble_center)
    painter.scale(scale, scale)
    rect = QRectF(-width / 2.0, -height / 2.0, width, height)
    glow = QColor(BUDDY_BLUE)
    glow.setAlpha(int(90 + (1.0 - min(scale, 1.0)) * 120))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(glow)
    painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 8, 8)
    painter.setBrush(BUDDY_BLUE)
    painter.drawRoundedRect(rect, 6, 6)
    painter.setFont(font)
    painter.setPen(QColor(255, 255, 255))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()
