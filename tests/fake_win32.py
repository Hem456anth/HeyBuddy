"""Minimal stand-in for `src.utils.win32` on non-Windows test hosts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

WS_EX_TRANSPARENT = 0x20
WS_EX_LAYERED = 0x80000
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x08000000
WDA_NONE = 0
WDA_EXCLUDEFROMCAPTURE = 0x11


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    left: int
    top: int
    width: int
    height: int
    dpi_scale: float
    is_primary: bool


FAKE_MONITORS: list[MonitorInfo] = [
    MonitorInfo(1, 0, 0, 2880, 1800, 1.5, True),
    MonitorInfo(2, 2880, 0, 1920, 1080, 1.0, False),
]
FAKE_CURSOR: list[int] = [100, 100]


def enumerate_monitors() -> list[MonitorInfo]:
    return list(FAKE_MONITORS)


def get_cursor_position() -> tuple[int, int]:
    return FAKE_CURSOR[0], FAKE_CURSOR[1]


def enable_per_monitor_dpi_awareness() -> str:
    return "fake"


def apply_overlay_window_styles(hwnd: int) -> None:
    return None


def apply_panel_window_styles(hwnd: int) -> None:
    return None


def exclude_window_from_capture(hwnd: int) -> bool:
    return True


def apply_blur_behind(hwnd: int) -> str:
    return "none"


def acquire_single_instance_lock(name: str = "") -> bool:
    return True


class LowLevelKeyboardHook:
    def __init__(self, chord_vk_codes: Iterable[Iterable[int]], on_press: Callable[[], None],
                 on_release: Callable[[], None], on_cancel: Callable[[], None] | None = None) -> None:
        self.on_press, self.on_release, self.on_cancel = on_press, on_release, on_cancel

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


def resolve_hotkey_chord(name: str):
    return ((0xA2, 0xA3), (0xA4, 0xA5))


def chord_display_name(name: str) -> str:
    return name.upper()


HOTKEY_PRESETS = {"ctrl+alt": ((0xA2, 0xA3), (0xA4, 0xA5))}


def autostart_command_for_current_process() -> str:
    return "heybuddy"


def disable_autostart() -> bool:
    return True


def enable_autostart(command_line: str) -> bool:
    return True


def format_vk_chord(vk_codes) -> str:
    return "vk:" + "+".join(str(code) for code in vk_codes)


def is_autostart_enabled() -> bool:
    return False


def get_window_ex_style(hwnd: int) -> int:
    return WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_LAYERED


def get_window_display_affinity(hwnd: int) -> int:
    return WDA_EXCLUDEFROMCAPTURE


def set_window_display_affinity(hwnd: int, affinity: int) -> bool:
    return True
