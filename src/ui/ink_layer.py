"""On-screen marks ("ink") drawn by the buddy while it talks.

Behaviour reconstructed from HeyClicky (see docs/HEYCLICKY_TEARDOWN.md):
marks are drawn in the cursor colour with a soft glow, each stroke is
"written" over `length / 600` seconds clamped to 0.14-0.7 s, marks arrive
with the spoken line that owns them, and a clear fades everything over
0.45 s. A `target` box stays until the user clicks inside it.

This module is HeyBuddy's own Qt implementation. Geometry is computed per
overlay window in its logical coordinates via a `to_local(x, y)` mapper,
so mixed-DPI monitors work the same way the buddy itself does.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from ..core.scene_program import Mark

INK_BLUE = QColor("#3380FF")
HIGHLIGHT_ALPHA = 95
FRAME_FILL_ALPHA = 18                 # ~7 % tint
STROKE_POINTS_PER_SECOND = 600.0
STROKE_MIN_SECONDS = 0.14
STROKE_MAX_SECONDS = 0.7
HIGHLIGHT_SWIPE_SECONDS = 0.35
FRAME_OPEN_SECONDS = 0.55
CLEAR_FADE_SECONDS = 0.45
LABEL_FADE_IN_SECONDS = 0.25
RING_PADDING = 6.0
STROKE_WIDTH = 3.0

LocalMapper = Callable[[float, float], QPointF]


def stroke_seconds(length_points: float) -> float:
    return max(STROKE_MIN_SECONDS, min(STROKE_MAX_SECONDS, length_points / STROKE_POINTS_PER_SECOND))


@dataclass
class LiveMark:
    mark: Mark
    born: float
    fade_started: float | None = None
    clicked: bool = False

    @property
    def is_target(self) -> bool:
        return self.mark.kind == "target"

    def physical_rect(self) -> tuple[float, float, float, float]:
        p = self.mark.physical
        return float(p.get("x", 0)), float(p.get("y", 0)), float(p.get("w", 0)), float(p.get("h", 0))

    def contains_physical(self, x: float, y: float, slack: float = 6.0) -> bool:
        left, top, width, height = self.physical_rect()
        return left - slack <= x <= left + width + slack and top - slack <= y <= top + height + slack

    def opacity(self, now: float) -> float:
        if self.fade_started is None:
            return 1.0
        return max(0.0, 1.0 - (now - self.fade_started) / CLEAR_FADE_SECONDS)


@dataclass
class InkLayer:
    """All live marks across monitors (physical coordinates)."""
    marks: list[LiveMark] = field(default_factory=list)

    def add(self, marks: list[Mark], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        stagger = 0.0
        for mark in marks:
            if mark.kind == "point" or not mark.is_resolved:
                continue  # points are the buddy's flight, not ink
            self.marks.append(LiveMark(mark=mark, born=now + stagger))
            stagger += 0.12

    def clear(self, now: float | None = None, keep_targets: bool = False) -> None:
        now = time.monotonic() if now is None else now
        for live in self.marks:
            if keep_targets and live.is_target and not live.clicked:
                continue
            if live.fade_started is None:
                live.fade_started = now

    def clear_now(self) -> None:
        self.marks.clear()

    def prune(self, now: float) -> None:
        self.marks = [live for live in self.marks if live.opacity(now) > 0.0]

    @property
    def has_marks(self) -> bool:
        return bool(self.marks)

    def is_animating(self, now: float) -> bool:
        for live in self.marks:
            if live.fade_started is not None or now - live.born < FRAME_OPEN_SECONDS + 0.3:
                return True
            if live.is_target:   # targets pulse
                return True
        return False

    def armed_targets(self) -> list[LiveMark]:
        return [live for live in self.marks if live.is_target and not live.clicked and live.fade_started is None]

    def hit_target(self, x: float, y: float) -> LiveMark | None:
        for live in self.armed_targets():
            if live.contains_physical(x, y):
                return live
        return None


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _polyline_length(points: list[QPointF]) -> float:
    return sum(math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in zip(points, points[1:]))


def _partial_polyline(points: list[QPointF], fraction: float) -> list[QPointF]:
    if fraction >= 1.0 or len(points) < 2:
        return points
    target = _polyline_length(points) * max(0.0, fraction)
    out = [points[0]]
    walked = 0.0
    for a, b in zip(points, points[1:]):
        segment = math.hypot(b.x() - a.x(), b.y() - a.y())
        if walked + segment >= target:
            t = (target - walked) / segment if segment else 0.0
            out.append(QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t))
            return out
        out.append(b)
        walked += segment
    return out


def _ellipse_points(rect: QRectF, steps: int = 72, overshoot: float = 0.08) -> list[QPointF]:
    """A hand-drawn loop: slightly more than a full turn, gently wobbling."""
    cx, cy = rect.center().x(), rect.center().y()
    rx, ry = rect.width() / 2.0, rect.height() / 2.0
    points = []
    total = int(steps * (1.0 + overshoot))
    for i in range(total + 1):
        angle = -math.pi * 0.75 + 2.0 * math.pi * i / steps
        wobble = 1.0 + 0.03 * math.sin(i * 0.17) + 0.04 * (i / total)
        points.append(QPointF(cx + rx * wobble * math.cos(angle), cy + ry * wobble * math.sin(angle)))
    return points


def _quadratic_points(start: QPointF, control: QPointF, end: QPointF, steps: int = 40) -> list[QPointF]:
    points = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        points.append(QPointF(
            mt * mt * start.x() + 2 * mt * t * control.x() + t * t * end.x(),
            mt * mt * start.y() + 2 * mt * t * control.y() + t * t * end.y(),
        ))
    return points


def _smooth(points: list[QPointF], iterations: int = 2) -> list[QPointF]:
    """Chaikin corner cutting for traces without `straight=True`."""
    for _ in range(iterations):
        if len(points) < 3:
            return points
        smoothed = [points[0]]
        for a, b in zip(points, points[1:]):
            smoothed.append(QPointF(0.75 * a.x() + 0.25 * b.x(), 0.75 * a.y() + 0.25 * b.y()))
            smoothed.append(QPointF(0.25 * a.x() + 0.75 * b.x(), 0.25 * a.y() + 0.75 * b.y()))
        smoothed.append(points[-1])
        points = smoothed
    return points


def _ink_pen(alpha: float, width: float = STROKE_WIDTH, style: Qt.PenStyle = Qt.PenStyle.SolidLine) -> QPen:
    color = QColor(INK_BLUE)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setStyle(style)
    return pen


def _draw_glowing_polyline(painter: QPainter, points: list[QPointF], alpha: float) -> None:
    if len(points) < 2:
        return
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(_ink_pen(alpha * 0.22, STROKE_WIDTH + 6.0))
    painter.drawPath(path)
    painter.setPen(_ink_pen(alpha, STROKE_WIDTH))
    painter.drawPath(path)


def _draw_label(painter: QPainter, anchor: QPointF, text: str, alpha: float) -> None:
    if not text:
        return
    font = QFont("Segoe UI Variable", 10)
    font.setWeight(QFont.Weight.Medium)
    painter.setFont(font)
    metrics = QFontMetricsF(font)
    width = metrics.horizontalAdvance(text) + 16.0
    height = metrics.height() + 6.0
    rect = QRectF(anchor.x(), anchor.y() - height - 4.0, width, height)
    fill = QColor(INK_BLUE)
    fill.setAlphaF(0.92 * alpha)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(fill))
    painter.drawRoundedRect(rect, height / 2.0, height / 2.0)
    text_color = QColor(255, 255, 255)
    text_color.setAlphaF(alpha)
    painter.setPen(text_color)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


# ---------------------------------------------------------------------------
# Painting
# ---------------------------------------------------------------------------


def paint_mark(painter: QPainter, live: LiveMark, to_local: LocalMapper, now: float, dpi_scale: float) -> None:
    age = now - live.born
    if age < 0:
        return
    alpha = live.opacity(now)
    if alpha <= 0.0:
        return
    mark = live.mark
    p = mark.physical
    label_alpha = alpha * min(1.0, age / LABEL_FADE_IN_SECONDS)

    def local_rect(pad: float = 0.0) -> QRectF:
        top_left = to_local(p["x"], p["y"])
        bottom_right = to_local(p["x"] + p.get("w", 0.0), p["y"] + p.get("h", 0.0))
        rect = QRectF(top_left, bottom_right).normalized()
        return rect.adjusted(-pad, -pad, pad, pad)

    kind = mark.kind
    if kind == "ring":
        rect = local_rect(RING_PADDING)
        points = _ellipse_points(rect)
        fraction = min(1.0, age / stroke_seconds(_polyline_length(points)))
        _draw_glowing_polyline(painter, _partial_polyline(points, fraction), alpha)
        _draw_label(painter, QPointF(rect.left(), rect.top()), mark.label, label_alpha)
    elif kind in ("frame", "target"):
        rect = local_rect(4.0)
        open_fraction = min(1.0, age / FRAME_OPEN_SECONDS)
        eased = 1.0 - (1.0 - open_fraction) ** 3
        grow = QRectF(rect.left(), rect.top(), rect.width() * max(0.05, eased), rect.height())
        fill = QColor(INK_BLUE)
        fill.setAlpha(int(FRAME_FILL_ALPHA * alpha))
        painter.setBrush(QBrush(fill))
        style = Qt.PenStyle.DashLine if kind == "frame" else Qt.PenStyle.SolidLine
        painter.setPen(_ink_pen(alpha, 2.0 if kind == "frame" else 2.5, style))
        painter.drawRoundedRect(grow, 8.0, 8.0)
        if kind == "target" and not live.clicked:
            pulse = 0.5 + 0.5 * math.sin(age * 4.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_ink_pen(alpha * (0.15 + 0.35 * pulse), 6.0))
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 10.0, 10.0)
        _draw_label(painter, QPointF(rect.left(), rect.top()), mark.label or ("click here" if kind == "target" else ""), label_alpha)
    elif kind == "highlight":
        rect = local_rect(2.0)
        fraction = min(1.0, age / HIGHLIGHT_SWIPE_SECONDS)
        fill = QColor(INK_BLUE)
        fill.setAlpha(int(HIGHLIGHT_ALPHA * alpha))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(QRectF(rect.left(), rect.top(), rect.width() * fraction, rect.height()), 3.0, 3.0)
    elif kind == "underline":
        start = to_local(p["x"], p["y"])
        end = to_local(p["x"] + p.get("w", 0.0), p["y"])
        points = [QPointF(start.x() + (end.x() - start.x()) * i / 20.0,
                          start.y() + 2.0 + 1.2 * math.sin(i * 0.8)) for i in range(21)]
        fraction = min(1.0, age / stroke_seconds(_polyline_length(points)))
        _draw_glowing_polyline(painter, _partial_polyline(points, fraction), alpha)
    elif kind == "arrow":
        start = to_local(p["x1"], p["y1"])
        end = to_local(p["x2"], p["y2"])
        dx, dy = end.x() - start.x(), end.y() - start.y()
        distance = math.hypot(dx, dy) or 1.0
        bend = float(mark.params.get("bend", 0.18))
        control = QPointF(start.x() + dx / 2.0 - dy * bend, start.y() + dy / 2.0 + dx * bend)
        points = _quadratic_points(start, control, end)
        fraction = min(1.0, age / stroke_seconds(_polyline_length(points)))
        drawn = _partial_polyline(points, fraction)
        _draw_glowing_polyline(painter, drawn, alpha)
        if fraction >= 1.0 and len(points) >= 2:
            tip, before = points[-1], points[-4]
            angle = math.atan2(tip.y() - before.y(), tip.x() - before.x())
            head = 12.0
            wing1 = QPointF(tip.x() - head * math.cos(angle - 0.45), tip.y() - head * math.sin(angle - 0.45))
            wing2 = QPointF(tip.x() - head * math.cos(angle + 0.45), tip.y() - head * math.sin(angle + 0.45))
            _draw_glowing_polyline(painter, [wing1, tip, wing2], alpha)
        _draw_label(painter, QPointF(start.x(), start.y()), mark.label, label_alpha)
        del distance
    elif kind == "num":
        center = to_local(p["x"], p["y"])
        radius = 12.0 * min(1.0, 0.4 + age / 0.2)
        painter.setPen(Qt.PenStyle.NoPen)
        fill = QColor(INK_BLUE)
        fill.setAlphaF(alpha)
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(center, radius, radius)
        font = QFont("Segoe UI Variable", 10)
        font.setBold(True)
        painter.setFont(font)
        white = QColor(255, 255, 255)
        white.setAlphaF(alpha)
        painter.setPen(white)
        painter.drawText(QRectF(center.x() - 12, center.y() - 12, 24, 24), Qt.AlignmentFlag.AlignCenter,
                         str(mark.params.get("n", "")))
    elif kind == "write":
        anchor = to_local(p["x"], p["y"])
        text = mark.label
        shown = text[: max(1, int(len(text) * min(1.0, age / max(0.3, len(text) * 0.04))))]
        font = QFont("Ink Free", 16)
        font.setStyleHint(QFont.StyleHint.Cursive)
        painter.setFont(font)
        path = QPainterPath()
        path.addText(anchor, font, shown)
        outline = QColor(0, 0, 0)
        outline.setAlphaF(0.55 * alpha)
        painter.setPen(QPen(outline, 3.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        fill = QColor(INK_BLUE).lighter(135)
        fill.setAlphaF(alpha)
        painter.setBrush(QBrush(fill))
        painter.drawPath(path)
    elif kind == "trace":
        flat = p.get("points") or []
        points = [to_local(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
        if mark.params.get("closed") and points:
            points.append(points[0])
        if not mark.params.get("straight"):
            points = _smooth(points)
        fraction = min(1.0, age / stroke_seconds(_polyline_length(points)))
        _draw_glowing_polyline(painter, _partial_polyline(points, fraction), alpha)
    del dpi_scale


def mark_monitor_key(live: LiveMark) -> tuple[float, float]:
    return float(live.mark.physical.get("monitor_left", 0)), float(live.mark.physical.get("monitor_top", 0))
