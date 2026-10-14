"""Thin facade over `utils.win32.LowLevelKeyboardHook`.

`CompanionManager` doesn't want to know about VK codes or the Win32 message
pump. It just wants press/release/cancel callbacks for the user's chord.
Hotkey names come from `config.hotkey` and are resolved to a VK chord via
`utils.win32.resolve_hotkey_chord`.
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
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel
        self._chord_name = chord_name
        self._hook = self._build_hook(chord_name)

    def _build_hook(self, chord_name: str) -> LowLevelKeyboardHook:
        return LowLevelKeyboardHook(
            chord_vk_codes=resolve_hotkey_chord(chord_name),
            on_press=self._on_press,
            on_release=self._on_release,
            on_cancel=self._on_cancel,
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
        self._hook = self._build_hook(chord_name)
        self.start()
