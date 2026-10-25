"""Global keyboard gestures, as HeyClicky defines them (modifier-only):

* hold Ctrl+Alt          push-to-talk (chord configurable)
* hold Ctrl+Win          dictation: speech is typed at the caret
* double-tap Ctrl        open the quick "ask" box (each tap <= 400 ms,
                         second tap starts <= 500 ms after the first ends)
* Esc                    cancel whatever the buddy is doing (never swallowed)

`GestureTracker` is pure Python and unit-tested; `HotkeyMonitor` wires it
and the chord detectors to `utils.win32.LowLevelKeyboardHook`.
"""
from __future__ import annotations

import time
from typing import Callable

from ..utils.logger import get_logger
from ..utils.win32 import LowLevelKeyboardHook, resolve_hotkey_chord

log = get_logger(__name__)

VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_GENERIC_CONTROL = 0x11
VK_ESCAPE = 0x1B
CONTROL_VKS = {VK_LCONTROL, VK_RCONTROL, VK_GENERIC_CONTROL}
TAP_MAX_SECONDS = 0.40
TAP_GAP_MAX_SECONDS = 0.50


class GestureTracker:
    """Detects double-tap Ctrl and Esc from raw key events."""

    def __init__(
        self,
        on_double_tap_ctrl: Callable[[], None] | None = None,
        on_escape: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.on_double_tap_ctrl = on_double_tap_ctrl
        self.on_escape = on_escape
        self._clock = clock
        self._held: set[int] = set()
        self._ctrl_down_at: float | None = None
        self._tap_polluted = False
        self._last_tap_end: float | None = None

    def feed(self, vk: int, is_down: bool) -> None:
        now = self._clock()
        if is_down:
            if vk in self._held:
                return  # auto-repeat
            self._held.add(vk)
            if vk == VK_ESCAPE and self.on_escape is not None:
                self.on_escape()
            if vk in CONTROL_VKS:
                if self._ctrl_down_at is None:
                    self._ctrl_down_at = now
                    self._tap_polluted = bool(self._held - CONTROL_VKS)
            else:
                # Any other key while (or between) taps means a shortcut.
                self._tap_polluted = True
                self._last_tap_end = None
            return

        self._held.discard(vk)
        if vk in CONTROL_VKS and not (self._held & CONTROL_VKS):
            down_at, self._ctrl_down_at = self._ctrl_down_at, None
            if down_at is None or self._tap_polluted or now - down_at > TAP_MAX_SECONDS:
                self._last_tap_end = None
                self._tap_polluted = False
                return
            if self._last_tap_end is not None and down_at - self._last_tap_end <= TAP_GAP_MAX_SECONDS:
                self._last_tap_end = None
                if self.on_double_tap_ctrl is not None:
                    self.on_double_tap_ctrl()
            else:
                self._last_tap_end = now


class HotkeyMonitor:
    def __init__(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        chord_name: str = "ctrl+alt",
        on_cancel: Callable[[], None] | None = None,
        on_dictation_press: Callable[[], None] | None = None,
        on_dictation_release: Callable[[], None] | None = None,
        dictation_chord_name: str | None = None,
        on_double_tap_ctrl: Callable[[], None] | None = None,
        on_escape: Callable[[], None] | None = None,
    ) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel
        self._chord_name = chord_name
        self._on_dictation_press = on_dictation_press
        self._on_dictation_release = on_dictation_release
        self._dictation_chord_name = dictation_chord_name
        self.gestures = GestureTracker(on_double_tap_ctrl=on_double_tap_ctrl, on_escape=on_escape)
        self._hook = self._build_hook(chord_name)
        self._dictation_hook = self._build_dictation_hook()

    def _build_hook(self, chord_name: str) -> LowLevelKeyboardHook:
        return LowLevelKeyboardHook(
            chord_vk_codes=resolve_hotkey_chord(chord_name),
            on_press=self._on_press,
            on_release=self._on_release,
            on_cancel=self._on_cancel,
            on_raw_key=self.gestures.feed,
        )

    def _build_dictation_hook(self) -> LowLevelKeyboardHook | None:
        if not self._dictation_chord_name or self._on_dictation_press is None:
            return None
        if self._dictation_chord_name == self._chord_name:
            log.warning("Dictation chord equals push-to-talk chord; dictation disabled")
            return None

        def pressed() -> None:
            if "win" in self._dictation_chord_name:
                try:
                    from ..utils.win32 import suppress_start_menu_on_win_release
                    suppress_start_menu_on_win_release()
                except Exception:
                    log.exception("Start menu suppression failed")
            self._on_dictation_press()

        return LowLevelKeyboardHook(
            chord_vk_codes=resolve_hotkey_chord(self._dictation_chord_name),
            on_press=pressed,
            on_release=self._on_dictation_release or (lambda: None),
            on_cancel=self._on_dictation_release,
        )

    def start(self) -> None:
        log.info("Arming hotkeys: talk=%s dictation=%s", self._chord_name, self._dictation_chord_name)
        self._hook.start()
        if self._dictation_hook is not None:
            self._dictation_hook.start()

    def stop(self) -> None:
        self._hook.stop()
        if self._dictation_hook is not None:
            self._dictation_hook.stop()

    def rebind(self, chord_name: str, dictation_chord_name: str | None = None) -> None:
        """Swap chords at runtime. Used by the Settings panel."""
        self.stop()
        self._chord_name = chord_name
        if dictation_chord_name is not None:
            self._dictation_chord_name = dictation_chord_name or None
        self._hook = self._build_hook(chord_name)
        self._dictation_hook = self._build_dictation_hook()
        self.start()
