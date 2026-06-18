"""Thin facade over `utils.win32.LowLevelKeyboardHook`.

`CompanionManager` doesn't want to know about VK codes or the Win32 message
pump. It just wants press/release callbacks for the user's configured chord.
This module is that translation layer.

Hotkey names come from `config.hotkey` (a friendly string like `"ctrl+alt"`)
and are resolved to a VK chord via `utils.win32.resolve_hotkey_chord`. Phase
3 added preset support so the Settings panel can change the chord without
needing a chord-capture UI.
"""
from __future__ import annotations

from typing import Callable

from ..utils.logger import get_logger
from ..utils.win32 import LowLevelKeyboardHook, resolve_hotkey_chord

log = get_logger(__name__)


class HotkeyMonitor:
    def __init__(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        chord_name: str = "ctrl+alt",
    ) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._chord_name = chord_name
        self._hook = LowLevelKeyboardHook(
            chord_vk_codes=resolve_hotkey_chord(chord_name),
            on_press=on_press,
            on_release=on_release,
        )

    def start(self) -> None:
        log.info("Arming push-to-talk hotkey: %s", self._chord_name)
        self._hook.start()

    def stop(self) -> None:
        self._hook.stop()

    def rebind(self, chord_name: str) -> None:
        """Swap the chord at runtime. Used by the Settings panel."""
        self.stop()
        self._chord_name = chord_name
        self._hook = LowLevelKeyboardHook(
            chord_vk_codes=resolve_hotkey_chord(chord_name),
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self.start()
