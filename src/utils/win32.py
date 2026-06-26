"""All Win32 / ctypes interop for HeyBuddy.

Per the project conventions documented in `CLAUDE.md`, every `ctypes` call in
the codebase lives here. Other modules import named helpers from this file.
If you find yourself reaching for `ctypes` elsewhere, add a helper here first.

Contents:

* `enable_per_monitor_dpi_awareness()` — opt the process into per-monitor DPI
  awareness so screen capture and overlay coordinates match what the user sees.
* `LowLevelKeyboardHook` — installs a `WH_KEYBOARD_LL` hook on a dedicated
  thread with its own message pump and fires Python callbacks for press/release
  of arbitrary key chords (used for Ctrl+Alt push-to-talk).
* `apply_overlay_window_styles(hwnd)` — flips the extended window styles to
  make the blue cursor overlay click-through, non-activating, and absent from
  Alt+Tab.
* `get_cursor_position()` — wraps `GetCursorPos` to return the current cursor
  position in *physical* pixels on the virtual screen.
* `get_monitor_under_point(x, y)` — returns the monitor handle, monitor
  rectangle, and DPI scale for the screen that contains (x, y).
* `enumerate_monitors()` — returns a list of `MonitorInfo` records for all
  attached displays, in 1-based screen order.
* `physical_to_logical(x, y)` — divides physical coordinates by the local
  monitor's DPI scale to produce the logical coordinates Claude reasons in.
* `logical_to_physical(x, y, monitor_index)` — inverse, used when an incoming
  `[POINT:x,y:label:screenN]` tag must be flown to a real pixel.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Iterable

from .logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Library handles
# ---------------------------------------------------------------------------

# Use stdcall (`windll`) — these are Win32 API entry points, not C ABI calls.
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
# shcore exports per-monitor DPI helpers; absent on very old builds we don't
# bother supporting.
try:
    _shcore = ctypes.windll.shcore
except OSError:  # pragma: no cover — Windows 7 without KB; we don't ship there
    _shcore = None


# ---------------------------------------------------------------------------
# Constants (subset we actually use; named to match the Win32 docs)
# ---------------------------------------------------------------------------

# Window extended styles for the cursor overlay.
WS_EX_TRANSPARENT = 0x00000020   # mouse events pass through to the window beneath
WS_EX_LAYERED = 0x00080000       # required to use WS_EX_TRANSPARENT reliably
WS_EX_TOOLWINDOW = 0x00000080    # no Alt+Tab entry, no taskbar button
WS_EX_NOACTIVATE = 0x08000000    # never receive activation focus
GWL_EXSTYLE = -20

# Keyboard low-level hook.
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104           # Alt-modified keydown
WM_SYSKEYUP = 0x0105             # Alt-modified keyup
WM_QUIT = 0x0012

# Virtual-key codes for push-to-talk chord presets.
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4                  # left Alt
VK_RMENU = 0xA5                  # right Alt
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_SPACE = 0x20

# Per-monitor DPI awareness contexts (SetProcessDpiAwarenessContext).
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

# MonitorFromPoint flags
MONITOR_DEFAULTTONEAREST = 0x00000002

# GetDpiForMonitor types
MDT_EFFECTIVE_DPI = 0


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _POINT),
    ]


@dataclass(frozen=True)
class MonitorInfo:
    """Snapshot of one display, populated by `enumerate_monitors`.

    Indexes are 1-based to match the way upstream Clicky labels monitors when
    asking Claude to emit `[POINT:x,y:label:screenN]` tags.
    """
    index: int
    left: int
    top: int
    width: int
    height: int
    dpi_scale: float
    is_primary: bool


# ---------------------------------------------------------------------------
# Function prototypes — declared once so ctypes can argument-check calls
# ---------------------------------------------------------------------------

# We assign prototypes lazily to keep import time fast and to tolerate older
# Windows builds that lack some entry points.

_user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.GetCursorPos.argtypes = (ctypes.POINTER(_POINT),)
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.MonitorFromPoint.argtypes = (_POINT, wintypes.DWORD)
_user32.MonitorFromPoint.restype = wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = (wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO))
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = (
    ctypes.POINTER(_MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
)
_user32.GetMessageW.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = (ctypes.POINTER(_MSG),)
_user32.TranslateMessage.restype = wintypes.BOOL
_user32.DispatchMessageW.argtypes = (ctypes.POINTER(_MSG),)
_user32.DispatchMessageW.restype = ctypes.c_long
_user32.PostThreadMessageW.argtypes = (
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)
_user32.PostThreadMessageW.restype = wintypes.BOOL
_user32.CallNextHookEx.argtypes = (
    ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
)
_user32.CallNextHookEx.restype = ctypes.c_long
# SetWindowsHookExW signature:
#   HHOOK SetWindowsHookExW(int idHook, HOOKPROC lpfn, HINSTANCE hmod, DWORD)
# Without argtypes declared, ctypes defaults to `c_int` for every argument
# and overflows on 64-bit Windows where `hmod` is a 64-bit pointer
# (raises "OverflowError: int too long to convert"). Declare them as
# c_void_p so the module handle round-trips correctly.
_user32.SetWindowsHookExW.argtypes = (
    ctypes.c_int,
    ctypes.c_void_p,     # HOOKPROC — _HookProc instances expose their address
    ctypes.c_void_p,     # HINSTANCE / HMODULE — pointer-sized on x64
    wintypes.DWORD,
)
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL

_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE

# DPI awareness entry points. Same lesson as SetWindowsHookExW: declare
# argtypes so the pointer-typed `DPI_AWARENESS_CONTEXT` survives on x64.
if hasattr(_user32, "SetProcessDpiAwarenessContext"):
    _user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
    _user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
if hasattr(_user32, "SetProcessDPIAware"):
    _user32.SetProcessDPIAware.argtypes = ()
    _user32.SetProcessDPIAware.restype = wintypes.BOOL
if _shcore is not None:
    if hasattr(_shcore, "SetProcessDpiAwareness"):
        _shcore.SetProcessDpiAwareness.argtypes = (ctypes.c_int,)
        _shcore.SetProcessDpiAwareness.restype = ctypes.c_long  # HRESULT
    if hasattr(_shcore, "GetDpiForMonitor"):
        # GetDpiForMonitor(HMONITOR, MONITOR_DPI_TYPE, *UINT, *UINT) -> HRESULT
        _shcore.GetDpiForMonitor.argtypes = (
            wintypes.HMONITOR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        )
        _shcore.GetDpiForMonitor.restype = ctypes.c_long


# ---------------------------------------------------------------------------
# DPI / monitor helpers
# ---------------------------------------------------------------------------


def enable_per_monitor_dpi_awareness() -> None:
    """Opt the process into per-monitor DPI awareness v2.

    Call once at startup, before any window is shown. Without this, screen
    capture and overlay positioning silently use 96-DPI logical coordinates on
    HiDPI laptops, and the blue cursor lands hundreds of pixels off-target.

    Falls back through older awareness APIs because v2 only exists on
    Windows 10 1703+. We do not support anything older than that.
    """
    if sys.platform != "win32":
        return
    try:
        # Preferred path: per-monitor v2 (Win10 1703+)
        if hasattr(_user32, "SetProcessDpiAwarenessContext"):
            _user32.SetProcessDpiAwarenessContext(
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
            )
            return
        # Fallback: shcore per-monitor (Win 8.1+)
        if _shcore is not None and hasattr(_shcore, "SetProcessDpiAwareness"):
            _shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            return
        # Last resort: system-wide
        _user32.SetProcessDPIAware()
    except OSError:
        log.exception("Failed to set DPI awareness; cursor placement may drift")


def get_cursor_position() -> tuple[int, int]:
    """Current cursor position in physical pixels on the virtual desktop."""
    pt = _POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _dpi_scale_for_monitor_handle(hmon: int) -> float:
    """Return the effective DPI scale (1.0 == 96 DPI) for a monitor handle."""
    if _shcore is None or not hasattr(_shcore, "GetDpiForMonitor"):
        return 1.0
    dpi_x = wintypes.UINT(0)
    dpi_y = wintypes.UINT(0)
    _shcore.GetDpiForMonitor(
        hmon,
        MDT_EFFECTIVE_DPI,
        ctypes.byref(dpi_x),
        ctypes.byref(dpi_y),
    )
    # Average X/Y to be tolerant of misconfigured non-square pixels; in practice
    # they're always equal on real hardware.
    return ((dpi_x.value + dpi_y.value) / 2) / 96.0


def get_monitor_under_point(x: int, y: int) -> tuple[int, _RECT, float]:
    """Return (HMONITOR, monitor rect, DPI scale) for the screen containing (x, y).

    Used to pick the screenshot's monitor and to scale incoming POINT
    coordinates that Claude emitted in a different monitor's logical space.
    """
    pt = _POINT(x, y)
    hmon = _user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    _user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    return hmon, info.rcMonitor, _dpi_scale_for_monitor_handle(hmon)


def enumerate_monitors() -> list[MonitorInfo]:
    """Enumerate every attached monitor in a stable 1-based order.

    The primary monitor is always index 1 so we can talk to Claude about
    `screen1`, `screen2`, etc. and get back tags whose `screenN` we can map
    deterministically.
    """
    # Local imports keep ctypes machinery out of cold module load.
    EnumProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(_RECT),
        wintypes.LPARAM,
    )
    monitors: list[tuple[int, _RECT, float, bool]] = []

    MONITORINFOF_PRIMARY = 0x00000001

    def _cb(hmon, _hdc, _lprect, _lparam):  # type: ignore[no-untyped-def]
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        _user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        is_primary = bool(info.dwFlags & MONITORINFOF_PRIMARY)
        monitors.append((hmon, info.rcMonitor, _dpi_scale_for_monitor_handle(hmon), is_primary))
        return True

    _user32.EnumDisplayMonitors.argtypes = (
        wintypes.HDC, ctypes.POINTER(_RECT), EnumProc, wintypes.LPARAM,
    )
    _user32.EnumDisplayMonitors.restype = wintypes.BOOL
    _user32.EnumDisplayMonitors(None, None, EnumProc(_cb), 0)

    # Primary first so `screen1` is always the user's main display.
    monitors.sort(key=lambda m: (not m[3], m[1].left, m[1].top))
    result: list[MonitorInfo] = []
    for i, (_hmon, rect, scale, is_primary) in enumerate(monitors, start=1):
        result.append(
            MonitorInfo(
                index=i,
                left=rect.left,
                top=rect.top,
                width=rect.right - rect.left,
                height=rect.bottom - rect.top,
                dpi_scale=scale,
                is_primary=is_primary,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Overlay window styles
# ---------------------------------------------------------------------------


def apply_overlay_window_styles(hwnd: int) -> None:
    """Mark `hwnd` as click-through, non-activating, off-taskbar, layered.

    Idempotent: re-applies the OR'd flags rather than overwriting, so it can
    be called after Qt re-shows the overlay (Qt sometimes drops ex-styles on
    reparenting).
    """
    ex_style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex_style |= (
        WS_EX_LAYERED
        | WS_EX_TRANSPARENT
        | WS_EX_TOOLWINDOW
        | WS_EX_NOACTIVATE
    )
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)


# ---------------------------------------------------------------------------
# DPI mapping for POINT coordinates
# ---------------------------------------------------------------------------


def logical_to_physical_on_monitor(
    logical_x: int,
    logical_y: int,
    monitor: MonitorInfo,
) -> tuple[int, int]:
    """Convert Claude-emitted logical coords on a specific monitor to pixels.

    Claude is told to reason in logical coordinates (post-DPI scale), so a
    1920x1080 monitor at 150% scale still gets tags like `[POINT:960,540…]`
    rather than `[POINT:1440,810…]`. We undo the scale here before driving
    the cursor.
    """
    px = monitor.left + int(logical_x * monitor.dpi_scale)
    py = monitor.top + int(logical_y * monitor.dpi_scale)
    return px, py


# ---------------------------------------------------------------------------
# Low-level keyboard hook
# ---------------------------------------------------------------------------

# `LowLevelKeyboardProc` signature: LRESULT CALLBACK fn(int code, WPARAM, LPARAM)
_HookProc = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
)


class LowLevelKeyboardHook:
    """Background `WH_KEYBOARD_LL` hook with chord (multi-key) detection.

    The hook callback runs on the hook owner's thread, which **must** have a
    Win32 message loop. We spin up our own thread, install the hook there, and
    pump messages until `stop()`. Press/release callbacks are invoked from the
    pump thread; consumers must marshal back to the Qt thread themselves.

    Why low-level (vs. `RegisterHotKey` or the `keyboard` library):

    * `RegisterHotKey` reports press but not release — useless for push-to-talk.
    * The `keyboard` library installs its own hook globally, which conflicts
      poorly with our overlay and AssemblyAI hook, and requires elevation in
      some configurations. The low-level hook is the official path.
    """

    def __init__(
        self,
        chord_vk_codes: Iterable[Iterable[int]],
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        # `chord_vk_codes` is an iterable of groups; each group is satisfied by
        # any one VK in it. For Ctrl+Alt we pass [{VK_LCONTROL, VK_RCONTROL},
        # {VK_LMENU, VK_RMENU}] so either physical Ctrl + either physical Alt
        # fires the chord.
        self._chord_groups: list[set[int]] = [set(group) for group in chord_vk_codes]
        self._on_press = on_press
        self._on_release = on_release
        self._pressed_vks: set[int] = set()
        self._chord_active = False
        self._hook_handle: int | None = None
        self._thread_id: int = 0
        self._thread: threading.Thread | None = None
        # Keep a strong ref to the ctypes callback object so it is not GC'd while
        # the OS is calling into it; loss of this reference crashes the process.
        self._proc_ref: ctypes._CFuncPtr | None = None  # type: ignore[name-defined]
        self._lock = threading.Lock()

    # ----- public API -----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_pump,
            args=(ready,),
            name="Win32HookPump",
            daemon=True,
        )
        self._thread.start()
        # Wait briefly for the hook to be installed so `start()` is observably
        # done before we return to the caller.
        ready.wait(timeout=2.0)

    def stop(self) -> None:
        if self._thread_id:
            # Post a quit to our pump thread; it will tear down the hook.
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = 0

    # ----- pump thread -----
    def _run_pump(self, ready: threading.Event) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        self._proc_ref = _HookProc(self._hook_callback)
        module = _kernel32.GetModuleHandleW(None)
        self._hook_handle = _user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc_ref, module, 0,
        )
        if not self._hook_handle:
            log.error("SetWindowsHookExW failed; push-to-talk will not work")
            ready.set()
            return
        log.info("Low-level keyboard hook installed on thread %d", self._thread_id)
        ready.set()

        msg = _MSG()
        try:
            while True:
                rc = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if rc <= 0:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook_handle:
                _user32.UnhookWindowsHookEx(self._hook_handle)
                self._hook_handle = None
            log.info("Low-level keyboard hook removed")

    # ----- hook callback (runs on pump thread) -----
    def _hook_callback(self, code: int, wparam: int, lparam: int) -> int:
        if code < 0:
            return _user32.CallNextHookEx(None, code, wparam, lparam)
        try:
            kb = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT))[0]
            vk = kb.vkCode
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self._on_vk_down(vk)
            elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                self._on_vk_up(vk)
        except Exception:
            # Never let an exception cross the OS boundary; that crashes the
            # process. Log and pass the event on.
            log.exception("LL keyboard callback raised")
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def _chord_held(self) -> bool:
        return all(any(vk in self._pressed_vks for vk in group) for group in self._chord_groups)

    def _on_vk_down(self, vk: int) -> None:
        fire = None
        with self._lock:
            self._pressed_vks.add(vk)
            if not self._chord_active and self._chord_held():
                self._chord_active = True
                fire = self._on_press
        if fire:
            try:
                fire()
            except Exception:
                log.exception("Hotkey on_press handler raised")

    def _on_vk_up(self, vk: int) -> None:
        fire = None
        with self._lock:
            self._pressed_vks.discard(vk)
            if self._chord_active and not self._chord_held():
                self._chord_active = False
                fire = self._on_release
        if fire:
            try:
                fire()
            except Exception:
                log.exception("Hotkey on_release handler raised")


# Named push-to-talk chord presets. The LL hook fires when EVERY group is
# satisfied by AT LEAST ONE held VK. So "ctrl+alt" means (any Ctrl) AND
# (any Alt) — the user can use either physical side.
HOTKEY_PRESETS: dict[str, tuple[tuple[int, ...], ...]] = {
    "ctrl+alt": ((VK_LCONTROL, VK_RCONTROL), (VK_LMENU, VK_RMENU)),
    "ctrl+shift": ((VK_LCONTROL, VK_RCONTROL), (VK_LSHIFT, VK_RSHIFT)),
    "alt+shift": ((VK_LMENU, VK_RMENU), (VK_LSHIFT, VK_RSHIFT)),
    "right_alt": ((VK_RMENU,),),
    "alt+space": ((VK_LMENU, VK_RMENU), (VK_SPACE,)),
}

# Back-compat alias used by `core.hotkey_monitor` before Phase 3.
CTRL_ALT_CHORD = HOTKEY_PRESETS["ctrl+alt"]


def resolve_hotkey_chord(name: str) -> tuple[tuple[int, ...], ...]:
    """Map a friendly hotkey name (`config.hotkey`) to a VK chord.

    Unknown names fall back to Ctrl+Alt so the app stays usable even after a
    bad settings.json edit. The settings panel only exposes preset names, so
    in practice we never hit the fallback path through the UI.
    """
    return HOTKEY_PRESETS.get(name.strip().lower().replace(" ", ""), CTRL_ALT_CHORD)


# ---------------------------------------------------------------------------
# Autostart (HKCU Run key)
#
# Lives in `win32.py` because it's a Windows-only registry concern even
# though `winreg` is stdlib (not `ctypes`). Keeping all OS-specific surface
# in one module means anyone porting HeyBuddy to mac/Linux only has to
# shim this file.
# ---------------------------------------------------------------------------

import winreg  # noqa: E402  — kept here so the module's purpose stays obvious

_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_VALUE_NAME = "HeyBuddy"


def is_autostart_enabled() -> bool:
    """True if our Run entry exists, regardless of what command it holds."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as key:
            winreg.QueryValueEx(key, _AUTOSTART_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("Failed to read autostart registry value")
        return False


def enable_autostart(command_line: str) -> bool:
    """Write `command_line` into the HKCU Run key under our value name."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key, _AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, command_line,
            )
        log.info("Autostart enabled: %s", command_line)
        return True
    except OSError:
        log.exception("Failed to enable autostart")
        return False


def disable_autostart() -> bool:
    """Remove our Run entry. No-op if it isn't there."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, _AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass
        log.info("Autostart disabled")
        return True
    except OSError:
        log.exception("Failed to disable autostart")
        return False


def autostart_command_for_current_process() -> str:
    """Compose the command line that re-launches whatever is running now.

    * Frozen build (`sys.frozen` set by PyInstaller) → just the .exe path.
    * Dev mode → `pythonw.exe -m src.main`, run from the project directory.
      We pick `pythonw.exe` (no console window) so autostart doesn't leave a
      stray terminal on the user's desktop every login.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = sys.executable
    if pythonw.lower().endswith("python.exe"):
        pythonw = pythonw[: -len("python.exe")] + "pythonw.exe"
    project_root = str(PROJECT_ROOT_FOR_AUTOSTART)
    return f'"{pythonw}" -m src.main'


# Late import to avoid a circular dependency on constants at module load.
# `constants.PROJECT_ROOT` doesn't import this module so this is one-way safe.
from .constants import PROJECT_ROOT as PROJECT_ROOT_FOR_AUTOSTART  # noqa: E402
