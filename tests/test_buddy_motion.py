import math
import random

from src.core.buddy_motion import (
    BuddyNavigator,
    NavigationMode,
    PointingTarget,
    arc_control_point,
    flight_duration_seconds,
    quadratic_bezier,
    smoothstep,
)


def test_flight_duration_clamped_like_clicky():
    assert flight_duration_seconds(10) == 0.6
    assert flight_duration_seconds(800) == 1.0
    assert flight_duration_seconds(5000) == 1.4


def test_bezier_endpoints_and_arc_goes_up():
    start, end = (0.0, 500.0), (1000.0, 500.0)
    control = arc_control_point(start, end)
    assert control == (500.0, 500.0 - 80.0)
    assert quadratic_bezier(start, control, end, 0.0) == start
    assert quadratic_bezier(start, control, end, 1.0) == end
    assert quadratic_bezier(start, control, end, 0.5)[1] < 500.0
    assert smoothstep(0.5) == 0.5


def test_follow_offset_scales_with_dpi():
    navigator = BuddyNavigator(scale_at=lambda x, y: 1.5)
    pose = navigator.tick(0.0, (100.0, 100.0))
    assert (pose.x, pose.y) == (100.0 + 35 * 1.5, 100.0 + 25 * 1.5)
    assert pose.rotation_degrees == -35.0


def test_full_navigation_cycle():
    navigator = BuddyNavigator(random_source=random.Random(1))
    cursor = (100.0, 100.0)
    navigator.tick(0.0, cursor)
    navigator.point_at([PointingTarget(900.0, 600.0, "save button")], 0.0, cursor)
    assert navigator.mode == NavigationMode.FLYING
    mid = navigator.tick(0.3, cursor)
    assert mid.scale > 1.0 and mid.mode == NavigationMode.FLYING
    arrived = navigator.tick(1.5, cursor)
    assert arrived.mode == NavigationMode.POINTING
    assert (arrived.x, arrived.y) == (908.0, 612.0)
    typing = navigator.tick(1.5 + 0.2, cursor)
    assert 0 < len(typing.bubble_text) <= len("save button")
    full = navigator.tick(1.5 + 1.0, cursor)
    assert full.bubble_text == "save button" and full.bubble_opacity == 1.0
    navigator.tick(1.5 + 5.0, cursor)  # hold + fade done -> returning
    assert navigator.mode == NavigationMode.RETURNING
    done = navigator.tick(1.5 + 7.0, cursor)
    assert navigator.mode == NavigationMode.FOLLOWING
    assert math.isclose(done.x, 135.0) and math.isclose(done.y, 125.0)


def test_moving_mouse_cancels_return_flight():
    navigator = BuddyNavigator(random_source=random.Random(2))
    navigator.tick(0.0, (0.0, 0.0))
    navigator.point_at([PointingTarget(500.0, 500.0, "x")], 0.0, (0.0, 0.0))
    navigator.tick(2.0, (0.0, 0.0))
    navigator.tick(6.0, (0.0, 0.0))
    assert navigator.mode == NavigationMode.RETURNING
    navigator.tick(6.1, (400.0, 0.0))
    assert navigator.mode == NavigationMode.FOLLOWING


def test_multiple_targets_visited_before_return():
    navigator = BuddyNavigator(random_source=random.Random(3))
    navigator.tick(0.0, (0.0, 0.0))
    navigator.point_at([PointingTarget(300, 300, "a"), PointingTarget(900, 300, "b")], 0.0, (0.0, 0.0))
    navigator.tick(2.0, (0.0, 0.0))
    assert navigator.mode == NavigationMode.POINTING
    navigator.tick(6.0, (0.0, 0.0))
    assert navigator.mode == NavigationMode.FLYING  # on to "b", not home


def test_target_clamped_to_screen():
    navigator = BuddyNavigator(clamp_to_screen=lambda x, y: (min(x, 1900.0), min(y, 1060.0)))
    navigator.tick(0.0, (0.0, 0.0))
    navigator.point_at([PointingTarget(1915, 1075, "edge")], 0.0, (0.0, 0.0))
    pose = navigator.tick(2.0, (0.0, 0.0))
    assert (pose.x, pose.y) == (1900.0, 1060.0)
