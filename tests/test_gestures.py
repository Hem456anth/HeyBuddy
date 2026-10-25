"""Double-tap Ctrl / Esc detection (pure logic)."""
from src.core.hotkey_monitor import GestureTracker

LCTRL, ALT, C, ESC = 0xA2, 0xA4, 0x43, 0x1B


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _tap(tracker, clock, hold=0.1, gap=0.1):
    tracker.feed(LCTRL, True)
    clock.now += hold
    tracker.feed(LCTRL, False)
    clock.now += gap


def test_double_tap_fires_once():
    clock, fired = Clock(), []
    tracker = GestureTracker(on_double_tap_ctrl=lambda: fired.append(1), clock=clock)
    _tap(tracker, clock)
    _tap(tracker, clock)
    assert fired == [1]
    _tap(tracker, clock)
    assert fired == [1]


def test_slow_taps_and_shortcuts_do_not_fire():
    clock, fired = Clock(), []
    tracker = GestureTracker(on_double_tap_ctrl=lambda: fired.append(1), clock=clock)
    _tap(tracker, clock, gap=0.8)
    _tap(tracker, clock)
    assert fired == []
    # Ctrl+C then Ctrl tap: not a double tap.
    tracker.feed(LCTRL, True); tracker.feed(C, True); tracker.feed(C, False); tracker.feed(LCTRL, False)
    clock.now += 0.1
    _tap(tracker, clock)
    assert fired == []
    # Ctrl+Alt push-to-talk is never a tap.
    tracker.feed(LCTRL, True); tracker.feed(ALT, True); clock.now += 0.1
    tracker.feed(ALT, False); tracker.feed(LCTRL, False); clock.now += 0.1
    _tap(tracker, clock)
    assert fired == []


def test_long_hold_is_not_a_tap_and_escape_fires():
    clock, fired, escapes = Clock(), [], []
    tracker = GestureTracker(on_double_tap_ctrl=lambda: fired.append(1), on_escape=lambda: escapes.append(1), clock=clock)
    _tap(tracker, clock, hold=0.6)
    _tap(tracker, clock)
    assert fired == []
    tracker.feed(ESC, True); tracker.feed(ESC, True); tracker.feed(ESC, False)
    assert escapes == [1]
