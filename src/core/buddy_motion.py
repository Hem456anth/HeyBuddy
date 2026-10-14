"""Blue-buddy motion model (pure Python, no Qt, unit-tested).

A frame-by-frame port of Clicky's `BlueCursorView` navigation logic from
`OverlayWindow.swift`:

* FOLLOWING   - the triangle trails the mouse at a (+35, +25) point offset
                with a springy catch-up, rotated -35 degrees like a pointer.
* FLYING      - quadratic bezier arc to the target (sits +8, +12 beside the
                element, clamped 20 px inside the screen). Duration
                distance/800 s clamped to 0.6-1.4 s, smoothstep easing, arc
                height min(distance * 0.2, 80), rotation follows the curve's
                tangent, scale pulses to 1.3x at mid-flight.
* POINTING    - parks at the target and "types" a speech bubble (the
                label from the POINT tag, or a random "right here!" phrase)
                at 30-60 ms per character, holds 3 s, fades out over 0.5 s.
* RETURNING   - flies back to the mouse; moving the mouse more than 100 px
                during the return cancels it and snaps back to following.

Multiple POINT tags in one answer are visited in order before returning.

All positions are physical desktop pixels (the process is per-monitor DPI
aware). Lengths that Clicky specifies in points are multiplied by the DPI
scale of the monitor they apply to, supplied by a `scale_at(x, y)` callback.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

FOLLOW_OFFSET_X = 35.0
FOLLOW_OFFSET_Y = 25.0
TARGET_OFFSET_X = 8.0
TARGET_OFFSET_Y = 12.0
SCREEN_EDGE_PADDING = 20.0
DEFAULT_ROTATION_DEGREES = -35.0
FLIGHT_POINTS_PER_SECOND = 800.0
FLIGHT_MIN_SECONDS = 0.6
FLIGHT_MAX_SECONDS = 1.4
ARC_HEIGHT_RATIO = 0.2
ARC_HEIGHT_MAX_POINTS = 80.0
FLIGHT_SCALE_PULSE = 0.3
BUBBLE_CHAR_MIN_SECONDS = 0.03
BUBBLE_CHAR_MAX_SECONDS = 0.06
BUBBLE_HOLD_SECONDS = 3.0
BUBBLE_FADE_SECONDS = 0.5
RETURN_CANCEL_DISTANCE_POINTS = 100.0
# Exponential catch-up rate for cursor following (per second). Approximates
# SwiftUI's `.spring(response: 0.2, dampingFraction: 0.6)` feel.
FOLLOW_CATCH_UP_RATE = 18.0

NAVIGATION_POINTER_PHRASES = (
    "right here!",
    "this one!",
    "over here!",
    "click this!",
    "here it is!",
    "found it!",
)

Point = tuple[float, float]
ScaleAt = Callable[[float, float], float]
ClampTo = Callable[[float, float], Point]


def smoothstep(linear_progress: float) -> float:
    t = max(0.0, min(1.0, linear_progress))
    return t * t * (3.0 - 2.0 * t)


def quadratic_bezier(start: Point, control: Point, end: Point, t: float) -> Point:
    one_minus_t = 1.0 - t
    x = one_minus_t * one_minus_t * start[0] + 2.0 * one_minus_t * t * control[0] + t * t * end[0]
    y = one_minus_t * one_minus_t * start[1] + 2.0 * one_minus_t * t * control[1] + t * t * end[1]
    return x, y


def quadratic_bezier_tangent(start: Point, control: Point, end: Point, t: float) -> Point:
    one_minus_t = 1.0 - t
    tangent_x = 2.0 * one_minus_t * (control[0] - start[0]) + 2.0 * t * (end[0] - control[0])
    tangent_y = 2.0 * one_minus_t * (control[1] - start[1]) + 2.0 * t * (end[1] - control[1])
    return tangent_x, tangent_y


def flight_duration_seconds(distance_in_points: float) -> float:
    return min(max(distance_in_points / FLIGHT_POINTS_PER_SECOND, FLIGHT_MIN_SECONDS), FLIGHT_MAX_SECONDS)


def arc_control_point(start: Point, end: Point, scale: float = 1.0) -> Point:
    """Midpoint lifted upward (negative y) so the buddy swoops in an arc."""
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    arc_height = min(distance * ARC_HEIGHT_RATIO, ARC_HEIGHT_MAX_POINTS * scale)
    return (start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0 - arc_height


class NavigationMode(str, Enum):
    FOLLOWING = "following"
    FLYING = "flying"
    POINTING = "pointing"
    RETURNING = "returning"


@dataclass
class PointingTarget:
    x: float
    y: float
    label: str = ""


@dataclass
class BuddyPose:
    """Everything the renderer needs for one frame."""
    x: float
    y: float
    rotation_degrees: float = DEFAULT_ROTATION_DEGREES
    scale: float = 1.0
    bubble_text: str = ""
    bubble_opacity: float = 0.0
    bubble_scale: float = 1.0
    mode: NavigationMode = NavigationMode.FOLLOWING


@dataclass
class _Flight:
    start: Point
    control: Point
    end: Point
    started_at: float
    duration: float


@dataclass
class _Bubble:
    phrase: str
    started_at: float
    character_times: list[float] = field(default_factory=list)


class BuddyNavigator:
    """Owns the buddy's position and navigation mode; advance with `tick()`."""

    def __init__(
        self,
        scale_at: ScaleAt | None = None,
        clamp_to_screen: ClampTo | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self._scale_at: ScaleAt = scale_at or (lambda _x, _y: 1.0)
        self._clamp_to_screen: ClampTo = clamp_to_screen or (lambda x, y: (x, y))
        self._random = random_source or random.Random()
        self.mode = NavigationMode.FOLLOWING
        self.position: Point | None = None
        self.rotation_degrees = DEFAULT_ROTATION_DEGREES
        self.flight_scale = 1.0
        self._flight: _Flight | None = None
        self._bubble: _Bubble | None = None
        self._pending_targets: list[PointingTarget] = []
        self._current_target: PointingTarget | None = None
        self._return_cancel_anchor: Point | None = None
        self._last_tick_time: float | None = None

    # ----- commands -----
    def point_at(self, targets: list[PointingTarget], now: float, cursor: Point) -> None:
        if not targets:
            return
        if self.position is None:
            self.position = self._follow_position(cursor)
        self._pending_targets = list(targets)
        self._fly_to_next_target(now)

    def cancel(self) -> None:
        """Abandon any navigation and go back to following the mouse."""
        self._flight = None
        self._bubble = None
        self._pending_targets = []
        self._current_target = None
        self._return_cancel_anchor = None
        self.mode = NavigationMode.FOLLOWING
        self.rotation_degrees = DEFAULT_ROTATION_DEGREES
        self.flight_scale = 1.0

    @property
    def is_navigating(self) -> bool:
        return self.mode != NavigationMode.FOLLOWING

    # ----- per-frame update -----
    def tick(self, now: float, cursor: Point) -> BuddyPose:
        elapsed_since_last_tick = 0.0 if self._last_tick_time is None else max(0.0, now - self._last_tick_time)
        self._last_tick_time = now

        if self.mode == NavigationMode.FOLLOWING:
            target_position = self._follow_position(cursor)
            if self.position is None:
                self.position = target_position
            else:
                blend = 1.0 - math.exp(-FOLLOW_CATCH_UP_RATE * elapsed_since_last_tick)
                self.position = (
                    self.position[0] + (target_position[0] - self.position[0]) * blend,
                    self.position[1] + (target_position[1] - self.position[1]) * blend,
                )
            return BuddyPose(self.position[0], self.position[1], DEFAULT_ROTATION_DEGREES, 1.0)

        if self.mode == NavigationMode.RETURNING and self._return_cancel_anchor is not None:
            scale = self._scale_at(cursor[0], cursor[1])
            moved = math.hypot(cursor[0] - self._return_cancel_anchor[0], cursor[1] - self._return_cancel_anchor[1])
            if moved > RETURN_CANCEL_DISTANCE_POINTS * scale:
                self.cancel()
                return self.tick(now, cursor)

        if self.mode in (NavigationMode.FLYING, NavigationMode.RETURNING) and self._flight is not None:
            self._advance_flight(now)
            if self._flight is None:
                if self.mode == NavigationMode.FLYING:
                    self._arrive_at_target(now)
                else:
                    self.cancel()
                    return self.tick(now, cursor)

        bubble_text, bubble_opacity, bubble_scale = "", 0.0, 1.0
        if self.mode == NavigationMode.POINTING and self._bubble is not None:
            bubble_text, bubble_opacity, bubble_scale, bubble_done = self._bubble_frame(now)
            if bubble_done:
                self._bubble = None
                if self._pending_targets:
                    self._fly_to_next_target(now)
                else:
                    self._start_return(now, cursor)

        assert self.position is not None
        return BuddyPose(
            self.position[0], self.position[1], self.rotation_degrees, self.flight_scale,
            bubble_text, bubble_opacity, bubble_scale, self.mode,
        )

    # ----- internals -----
    def _follow_position(self, cursor: Point) -> Point:
        scale = self._scale_at(cursor[0], cursor[1])
        return cursor[0] + FOLLOW_OFFSET_X * scale, cursor[1] + FOLLOW_OFFSET_Y * scale

    def _fly_to_next_target(self, now: float) -> None:
        target = self._pending_targets.pop(0)
        self._current_target = target
        scale = self._scale_at(target.x, target.y)
        destination = self._clamp_to_screen(
            target.x + TARGET_OFFSET_X * scale, target.y + TARGET_OFFSET_Y * scale,
        )
        self.mode = NavigationMode.FLYING
        self._start_flight(destination, now)

    def _start_return(self, now: float, cursor: Point) -> None:
        self.mode = NavigationMode.RETURNING
        self._current_target = None
        self._return_cancel_anchor = cursor
        self._start_flight(self._follow_position(cursor), now)

    def _start_flight(self, destination: Point, now: float) -> None:
        start = self.position if self.position is not None else destination
        scale = self._scale_at(start[0], start[1])
        distance_in_points = math.hypot(destination[0] - start[0], destination[1] - start[1]) / max(scale, 0.1)
        self._flight = _Flight(
            start=start,
            control=arc_control_point(start, destination, scale),
            end=destination,
            started_at=now,
            duration=flight_duration_seconds(distance_in_points),
        )

    def _advance_flight(self, now: float) -> None:
        flight = self._flight
        assert flight is not None
        linear_progress = (now - flight.started_at) / flight.duration if flight.duration > 0 else 1.0
        if linear_progress >= 1.0:
            self.position = flight.end
            self.flight_scale = 1.0
            self._flight = None
            return
        eased = smoothstep(linear_progress)
        self.position = quadratic_bezier(flight.start, flight.control, flight.end, eased)
        tangent_x, tangent_y = quadratic_bezier_tangent(flight.start, flight.control, flight.end, eased)
        if tangent_x or tangent_y:
            # +90 because the triangle's tip points up at 0 degrees.
            self.rotation_degrees = math.degrees(math.atan2(tangent_y, tangent_x)) + 90.0
        self.flight_scale = 1.0 + math.sin(linear_progress * math.pi) * FLIGHT_SCALE_PULSE

    def _arrive_at_target(self, now: float) -> None:
        self.mode = NavigationMode.POINTING
        self.rotation_degrees = DEFAULT_ROTATION_DEGREES
        self.flight_scale = 1.0
        label = (self._current_target.label if self._current_target else "").strip()
        phrase = label or self._random.choice(NAVIGATION_POINTER_PHRASES)
        character_times: list[float] = []
        elapsed = 0.0
        for _ in phrase:
            character_times.append(elapsed)
            elapsed += self._random.uniform(BUBBLE_CHAR_MIN_SECONDS, BUBBLE_CHAR_MAX_SECONDS)
        self._bubble = _Bubble(phrase=phrase, started_at=now, character_times=character_times)

    def _bubble_frame(self, now: float) -> tuple[str, float, float, bool]:
        bubble = self._bubble
        assert bubble is not None
        elapsed = now - bubble.started_at
        visible_characters = sum(1 for character_time in bubble.character_times if character_time <= elapsed)
        typing_finished_at = (bubble.character_times[-1] if bubble.character_times else 0.0) + BUBBLE_CHAR_MAX_SECONDS
        # Scale-bounce entrance: 0.5 -> 1.0 with a little overshoot.
        entrance_progress = min(1.0, elapsed / 0.35)
        bubble_scale = 0.5 + 0.5 * (1.0 + 0.25 * math.sin(entrance_progress * math.pi)) * entrance_progress
        bubble_scale = min(bubble_scale, 1.1) if entrance_progress < 1.0 else 1.0
        fade_starts_at = typing_finished_at + BUBBLE_HOLD_SECONDS
        if elapsed < fade_starts_at:
            return bubble.phrase[:visible_characters], 1.0, bubble_scale, False
        fade_progress = (elapsed - fade_starts_at) / BUBBLE_FADE_SECONDS
        if fade_progress >= 1.0:
            return "", 0.0, 1.0, True
        return bubble.phrase, 1.0 - fade_progress, 1.0, False
