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
# Desktop Window Manager — present on every supported Windows (Vista+).
# Wrapped in try/except so an ancient host without dwmapi merely loses the
# blur-behind backdrop instead of crashing at import.
try:
    _dwmapi = ctypes.windll.dwmapi
except OSError:  # pragma: no cover
    _dwmapi = None


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
# Windows key (the "Logo" key). Captured-chord users sometimes bind to
# this so we need a friendly name for it; not in HOTKEY_PRESETS because
# Windows reserves many Win+X shortcuts already.
VK_LWIN = 0x5B
VK_RWIN = 0x5C

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
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
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


def enable_per_monitor_dpi_awareness() -> str:
    """Opt the process into per-monitor DPI awareness v2 (with fallbacks).

    Call once at startup, before any window is shown. Without this, screen
    capture and overlay positioning silently use 96-DPI logical coordinates on
    HiDPI laptops, and the blue cursor lands hundreds of pixels off-target.

    Falls back through older awareness APIs because v2 only exists on
    Windows 10 1703+. We do not support anything older than that.

    Returns the tier actually applied, suitable for logging:

    * `"per-monitor-v2"` — the modern best path (Win 10 1703+).
      Each monitor's DPI is queried independently; widget pixels match
      what the user sees per display, even after dragging across screens.
    * `"per-monitor"` — older shcore API (Win 8.1+). Per-display DPI but
      changes after process start aren't picked up cleanly.
    * `"system"` — single global DPI scale. Cursor placement drifts when
      monitors have different DPIs.
    * `"none"` — call failed; coords will be treated as raw 96-DPI logical.
      `cycle-12` POINT-flight log will show a `dpi=1.00x` regardless of
      the user's real scale — diagnostic for "all my points land in the
      wrong place".
    """
    if sys.platform != "win32":
        return "none"
    try:
        # Preferred path: per-monitor v2 (Win10 1703+)
        if hasattr(_user32, "SetProcessDpiAwarenessContext"):
            _user32.SetProcessDpiAwarenessContext(
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
            )
            return "per-monitor-v2"
        # Fallback: shcore per-monitor (Win 8.1+)
        if _shcore is not None and hasattr(_shcore, "SetProcessDpiAwareness"):
            _shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            return "per-monitor"
        # Last resort: system-wide
        _user32.SetProcessDPIAware()
        return "system"
    except OSError:
        log.exception("Failed to set DPI awareness; cursor placement may drift")
        return "none"


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


def apply_panel_window_styles(hwnd: int) -> None:
    """Mark the floating panel as non-activating and off-taskbar.

    Sibling to `apply_overlay_window_styles` with a different flag set:

    * `WS_EX_NOACTIVATE` — the panel never becomes the foreground/active
      window when shown or clicked. Prevents focus theft from whatever app
      the user was working in. Buttons inside the panel still receive
      clicks (Qt routes mouse events regardless of OS activation state).
    * `WS_EX_TOOLWINDOW` — no taskbar button, no Alt+Tab entry. Matches
      the macOS Clicky behavior (the app lives in the tray; the panel is
      ephemeral chrome, not an app window in its own right).

    Deliberately omitted vs. the overlay:

    * `WS_EX_TRANSPARENT` — we want clicks on the panel's buttons and
      title bar to register, not pass through.
    * `WS_EX_LAYERED` — the DWM backdrop (see `apply_blur_behind`) does
      our compositing; layered would conflict with it.

    Known trade-off: the embedded QLineEdit cannot receive keyboard
    input while the panel is non-active. Users who type rather than
    speak must explicitly bring the panel forward (e.g. click into it
    and have Qt invoke `activateWindow()`). Matches upstream Clicky's
    voice-first design.

    Idempotent — safe to call more than once.
    """
    ex_style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    # WS_EX_NOACTIVATE used to be set here too, but it made the panel's
    # text box impossible to type into. The panel now only avoids taking
    # focus when it *appears* (Qt's WA_ShowWithoutActivating); a click
    # activates it like any normal window.
    ex_style |= WS_EX_TOOLWINDOW
    ex_style &= ~WS_EX_NOACTIVATE
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
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
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
        on_cancel: Callable[[], None] | None = None,
        on_raw_key: Callable[[int, bool], None] | None = None,
    ) -> None:
        # Every key event (vk, is_down), for gesture detectors such as
        # double-tap Ctrl and Esc. Must be cheap; runs on the hook thread.
        self._on_raw_key = on_raw_key
        # `chord_vk_codes` is an iterable of groups; each group is satisfied by
        # any one VK in it. For Ctrl+Alt we pass [{VK_LCONTROL, VK_RCONTROL},
        # {VK_LMENU, VK_RMENU}] so either physical Ctrl + either physical Alt
        # fires the chord.
        self._chord_groups: list[set[int]] = [set(group) for group in chord_vk_codes]
        self._on_press = on_press
        self._on_release = on_release
        # Fired (instead of on_release) when another key is pressed while
        # the chord is held: the user is typing a shortcut such as
        # Ctrl+Alt+Del or an AltGr character, not talking to the buddy.
        self._on_cancel = on_cancel
        self._chord_cancelled = False
        self._chord_vk_universe: set[int] = set().union(*self._chord_groups) if self._chord_groups else set()
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
            injected = bool(kb.flags & 0x10)  # LLKHF_INJECTED: our own SendInput
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self._on_vk_down(vk)
                if self._on_raw_key is not None and not injected:
                    self._on_raw_key(vk, True)
            elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                self._on_vk_up(vk)
                if self._on_raw_key is not None and not injected:
                    self._on_raw_key(vk, False)
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
            if (
                self._chord_active
                and not self._chord_cancelled
                and vk not in self._chord_vk_universe
                and vk not in _GENERIC_MODIFIER_VKS
                and self._on_cancel is not None
            ):
                self._chord_cancelled = True
                fire = self._on_cancel
            elif not self._chord_active and self._chord_held():
                self._chord_active = True
                self._chord_cancelled = False
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
                if not self._chord_cancelled:
                    fire = self._on_release
                self._chord_cancelled = False
        if fire:
            try:
                fire()
            except Exception:
                log.exception("Hotkey on_release handler raised")


# Generic (side-less) modifier VKs Windows sometimes reports alongside the
# sided ones; they must never count as "another key" for cancellation.
_GENERIC_MODIFIER_VKS = {0x10, 0x11, 0x12}  # VK_SHIFT, VK_CONTROL, VK_MENU

# Named push-to-talk chord presets. The LL hook fires when EVERY group is
# satisfied by AT LEAST ONE held VK. So "ctrl+alt" means (any Ctrl) AND
# (any Alt) — the user can use either physical side.
HOTKEY_PRESETS: dict[str, tuple[tuple[int, ...], ...]] = {
    "ctrl+alt": ((VK_LCONTROL, VK_RCONTROL), (VK_LMENU, VK_RMENU)),
    "ctrl+shift": ((VK_LCONTROL, VK_RCONTROL), (VK_LSHIFT, VK_RSHIFT)),
    "alt+shift": ((VK_LMENU, VK_RMENU), (VK_LSHIFT, VK_RSHIFT)),
    "right_alt": ((VK_RMENU,),),
    "alt+space": ((VK_LMENU, VK_RMENU), (VK_SPACE,)),
    # Dictation default (HeyClicky uses hold Ctrl+Win for dictation).
    "ctrl+win": ((VK_LCONTROL, VK_RCONTROL), (VK_LWIN, VK_RWIN)),
    "ctrl+shift+space": ((VK_LCONTROL, VK_RCONTROL), (VK_LSHIFT, VK_RSHIFT), (VK_SPACE,)),
}

# Back-compat alias used by `core.hotkey_monitor` before Phase 3.
CTRL_ALT_CHORD = HOTKEY_PRESETS["ctrl+alt"]


def resolve_hotkey_chord(name: str) -> tuple[tuple[int, ...], ...]:
    """Map a friendly hotkey name (`config.hotkey`) to a VK chord.

    Two name formats are accepted:

    * Preset name (e.g. `"ctrl+alt"`, `"alt+space"`) — looked up in
      `HOTKEY_PRESETS`. Each preset's groups merge OS-side L/R variants
      (any Ctrl + any Alt).
    * Custom serialized form `"vk:N,N,N"` produced by the Settings
      dialog's Capture button. Each comma-separated VK becomes its own
      required group — the user's captured chord is reproduced exactly,
      no L/R merging.

    Unknown names fall back to Ctrl+Alt so the app stays usable even after
    a bad settings.json edit.
    """
    normalized = name.strip().lower().replace(" ", "")
    if normalized.startswith("vk:"):
        return parse_vk_chord(normalized)
    return HOTKEY_PRESETS.get(normalized, CTRL_ALT_CHORD)


def parse_vk_chord(serialized: str) -> tuple[tuple[int, ...], ...]:
    """Parse a `"vk:N,N,N"` string into the LL-hook chord-group format.

    Each VK becomes its own group of one (every VK must be held), which
    is the exact-match semantics a freshly-captured chord wants. Empty
    payload or non-numeric tokens silently fall back to Ctrl+Alt — the
    same defensive policy `resolve_hotkey_chord` uses for unknown
    preset names.
    """
    payload = serialized[len("vk:"):]
    vks: list[int] = []
    for token in payload.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            vks.append(int(token))
        except ValueError:
            log.warning("Ignoring non-numeric token in chord %r: %r", serialized, token)
    if not vks:
        return CTRL_ALT_CHORD
    return tuple((vk,) for vk in vks)


def format_vk_chord(vk_codes: tuple[int, ...]) -> str:
    """Serialize a flat tuple of VK codes into the `"vk:N,N,N"` form.

    Order is preserved as given; callers that want a canonical order
    should sort before calling.
    """
    return "vk:" + ",".join(str(int(v)) for v in vk_codes)


# Friendly-name table for the common chord-component VKs. Keys not in
# this table fall back to `VK_<hex>` so the user always sees *something*
# legible instead of an opaque integer.
_VK_FRIENDLY_NAMES: dict[int, str] = {
    VK_LCONTROL: "Ctrl",  VK_RCONTROL: "Ctrl",
    VK_LMENU:    "Alt",   VK_RMENU:    "Alt",
    VK_LSHIFT:   "Shift", VK_RSHIFT:   "Shift",
    VK_LWIN:     "Win",   VK_RWIN:     "Win",
    VK_SPACE:    "Space",
}


def chord_display_name(name: str) -> str:
    """Return a human-readable label for either a preset or a vk: chord.

    For preset names, returns the preset itself (the Settings panel's
    HOTKEY_LABELS map provides nicer display strings — this helper
    handles the vk: case specifically). For vk: chords, friendly names
    are de-duplicated so Ctrl + Ctrl + Alt (left + right + alt) prints
    as "Ctrl + Alt".
    """
    normalized = name.strip().lower().replace(" ", "")
    if not normalized.startswith("vk:"):
        return name
    chord = parse_vk_chord(normalized)
    parts: list[str] = []
    seen: set[str] = set()
    for group in chord:
        for vk in group:
            friendly = _VK_FRIENDLY_NAMES.get(vk, f"VK_{vk:#x}")
            if friendly not in seen:
                seen.add(friendly)
                parts.append(friendly)
    return " + ".join(parts) if parts else "Custom"


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
    # The Run key launches with System32 as the working directory, so
    # `-m src.main` can't find the package. Point at the script itself;
    # `src/main.py` puts the repo root on sys.path when run directly.
    main_script_path = PROJECT_ROOT_FOR_AUTOSTART / "src" / "main.py"
    return f'"{pythonw}" "{main_script_path}"'


# Late import to avoid a circular dependency on constants at module load.
# `constants.PROJECT_ROOT` doesn't import this module so this is one-way safe.
from .constants import PROJECT_ROOT as PROJECT_ROOT_FOR_AUTOSTART  # noqa: E402


# ---------------------------------------------------------------------------
# DWM blur-behind / Mica / Acrylic backdrop
#
# The frameless panel is drawn over a transparent root QWidget so a drop
# shadow can render. With nothing behind it, the chrome looks like it's
# floating in space. DWM can composite a blurred view of whatever the user
# has on their desktop behind our window — the classic "translucent panel"
# look modern Windows apps use.
#
# Three flavors, in priority order (most modern first):
#
# 1. `DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)`
#    Windows 11 22H2+. Acrylic — the bright, frosted-glass look. Best
#    quality, modest CPU/GPU cost. Returns non-zero on older builds.
# 2. `DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MAINWINDOW)`
#    Windows 11 22H2+. Mica — the more subtle desktop-tint look. Used as a
#    secondary attempt if Acrylic isn't available.
# 3. `DwmEnableBlurBehindWindow` — Windows 10+ (in practice every Win10
#    install). Coarser gaussian blur, no per-pixel tint. The fallback that
#    actually fires on most Win10 hosts.
#
# If all three fail (e.g. very old Windows, DWM disabled, classic theme),
# the caller is told `"none"` and should leave the panel chrome opaque so
# the dark slab still reads correctly.
# ---------------------------------------------------------------------------

# DwmSetWindowAttribute attribute IDs.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38

# Values for DWMWA_SYSTEMBACKDROP_TYPE.
DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2          # Mica
DWMSBT_TRANSIENTWINDOW = 3     # Acrylic
DWMSBT_TABBEDWINDOW = 4        # Tabbed Mica

# Flags for DWM_BLURBEHIND.dwFlags.
DWM_BB_ENABLE = 0x01
DWM_BB_BLURREGION = 0x02
DWM_BB_TRANSITIONONMAXIMIZED = 0x04


class _DWM_BLURBEHIND(ctypes.Structure):
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("fEnable", wintypes.BOOL),
        ("hRgnBlur", wintypes.HRGN),
        ("fTransitionOnMaximized", wintypes.BOOL),
    ]


# Declare argtypes so the HWND survives x64 (same lesson as the keyboard hook).
if _dwmapi is not None:
    _dwmapi.DwmEnableBlurBehindWindow.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(_DWM_BLURBEHIND),
    )
    # HRESULT is a signed 32-bit; c_long is the correct ctypes mapping.
    _dwmapi.DwmEnableBlurBehindWindow.restype = ctypes.c_long
    _dwmapi.DwmSetWindowAttribute.argtypes = (
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long


def apply_blur_behind(hwnd: int) -> str:
    """Apply the best available DWM backdrop to `hwnd`. Return its name.

    The returned string is one of:

    * `"acrylic"`      — Win 11 22H2+ system backdrop
    * `"mica"`         — Win 11 22H2+ system backdrop (fallback within modern API)
    * `"blur-behind"`  — Win 10 legacy blur
    * `"none"`         — nothing applied (very old Windows, classic theme,
                         DWM disabled, or call failed)

    Callers should switch their chrome to a translucent background only when
    the return value is not `"none"`. Otherwise the dark slab disappears
    against the desktop because nothing is being composited behind it.
    """
    if _dwmapi is None:
        return "none"

    hwnd_handle = wintypes.HWND(hwnd)

    # Tier 1: modern Acrylic via DwmSetWindowAttribute. Returns S_OK (0)
    # on success; non-zero on older builds where the attribute is unknown.
    try:
        backdrop_value = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        rc = _dwmapi.DwmSetWindowAttribute(
            hwnd_handle,
            wintypes.DWORD(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(backdrop_value),
            wintypes.DWORD(ctypes.sizeof(backdrop_value)),
        )
        if rc == 0:
            log.info("DWM backdrop: Acrylic applied to hwnd=%#x", hwnd)
            return "acrylic"
    except OSError:
        log.exception("DwmSetWindowAttribute(Acrylic) raised")

    # Tier 2: Mica fallback within the same modern API. Same Windows 11
    # build requirement, but worth trying in case Acrylic was rejected for
    # window-style reasons but Mica accepted.
    try:
        backdrop_value = ctypes.c_int(DWMSBT_MAINWINDOW)
        rc = _dwmapi.DwmSetWindowAttribute(
            hwnd_handle,
            wintypes.DWORD(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(backdrop_value),
            wintypes.DWORD(ctypes.sizeof(backdrop_value)),
        )
        if rc == 0:
            log.info("DWM backdrop: Mica applied to hwnd=%#x", hwnd)
            return "mica"
    except OSError:
        log.exception("DwmSetWindowAttribute(Mica) raised")

    # Tier 3: legacy DwmEnableBlurBehindWindow. Works on all modern Win10.
    try:
        bb = _DWM_BLURBEHIND()
        bb.dwFlags = DWM_BB_ENABLE
        bb.fEnable = True
        bb.hRgnBlur = None
        bb.fTransitionOnMaximized = False
        rc = _dwmapi.DwmEnableBlurBehindWindow(hwnd_handle, ctypes.byref(bb))
        if rc == 0:
            log.info("DWM backdrop: legacy blur-behind applied to hwnd=%#x", hwnd)
            return "blur-behind"
    except OSError:
        log.exception("DwmEnableBlurBehindWindow raised")

    log.info("DWM backdrop: none available, chrome stays opaque")
    return "none"


# ---------------------------------------------------------------------------
# Capture exclusion (keep HeyBuddy's own windows out of screenshots)
# ---------------------------------------------------------------------------

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 2004+ (build 19041)

if hasattr(_user32, "SetWindowDisplayAffinity"):
    _user32.SetWindowDisplayAffinity.argtypes = (wintypes.HWND, wintypes.DWORD)
    _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL


def exclude_window_from_capture(hwnd: int) -> bool:
    """Hide `hwnd` from screenshots/screen recording but not from the user.

    The Windows equivalent of Clicky passing its own windows to the
    ScreenCaptureKit filter: the blue buddy and the panel never show up in
    the images sent to Claude, so it can't "point at itself". On builds
    older than 2004 the call fails and we simply keep going (Claude may then
    see the buddy in its screenshot, which is harmless).
    """
    if not hasattr(_user32, "SetWindowDisplayAffinity"):
        return False
    ok = bool(_user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
    if not ok:
        log.debug("SetWindowDisplayAffinity(EXCLUDEFROMCAPTURE) failed for hwnd=%#x", hwnd)
    return ok


# ---------------------------------------------------------------------------
# Single instance guard
# ---------------------------------------------------------------------------

ERROR_ALREADY_EXISTS = 183
# A separate handle with use_last_error=True so ctypes snapshots
# GetLastError() immediately after the call (the shared windll handle
# doesn't, and any intervening call could clobber it).
_kernel32_with_last_error = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32_with_last_error.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32_with_last_error.CreateMutexW.restype = wintypes.HANDLE
_single_instance_mutex_handle = None


def acquire_single_instance_lock(name: str = "Local\\HeyBuddySingleInstance") -> bool:
    """Return False if another HeyBuddy is already running for this user.

    Two copies would install two keyboard hooks and two overlays, and every
    push-to-talk would start two turns. The mutex handle is kept for the
    life of the process and released by the OS on exit.
    """
    global _single_instance_mutex_handle
    handle = _kernel32_with_last_error.CreateMutexW(None, False, name)
    already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    _single_instance_mutex_handle = handle
    return not already_running


_user32.GetWindowDisplayAffinity.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetWindowDisplayAffinity.restype = wintypes.BOOL


def get_window_ex_style(hwnd: int) -> int:
    """Raw GWL_EXSTYLE bits (diagnostics / self-check)."""
    return int(_user32.GetWindowLongW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF


def get_window_display_affinity(hwnd: int) -> int:
    """Current SetWindowDisplayAffinity value, or -1 if it can't be read."""
    affinity = wintypes.DWORD(0)
    if not _user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)):
        return -1
    return int(affinity.value)


def set_window_display_affinity(hwnd: int, affinity: int) -> bool:
    return bool(_user32.SetWindowDisplayAffinity(hwnd, affinity))


# ---------------------------------------------------------------------------
# Clipboard, synthetic input, foreground window, mouse buttons
# (used by the tools layer, dictation and guided `target()` steps)
# ---------------------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
VK_LBUTTON = 0x01
VK_ESCAPE = 0x1B
# An unassigned VK. Tapping it while Win is held makes Windows treat the
# Win press as a chord, so releasing Win does not pop the Start menu.
VK_NONE_ASSIGNED = 0xFF

_kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
_kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
_user32.OpenClipboard.argtypes = (wintypes.HWND,)
_user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardData.argtypes = (wintypes.UINT,)
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
)
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT


def _key_input(vk: int, key_up: bool) -> _INPUT:
    event = _INPUT()
    event.type = INPUT_KEYBOARD
    event.union.ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if key_up else 0, 0, 0)
    return event


def _send_keys(sequence: list[tuple[int, bool]]) -> bool:
    events = (_INPUT * len(sequence))(*[_key_input(vk, up) for vk, up in sequence])
    sent = _user32.SendInput(len(sequence), events, ctypes.sizeof(_INPUT))
    return sent == len(sequence)


def set_clipboard_text(text: str) -> bool:
    """Put Unicode text on the clipboard (works from any thread)."""
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    for _attempt in range(10):
        if _user32.OpenClipboard(None):
            break
        import time as _time
        _time.sleep(0.02)
    else:
        return False
    try:
        _user32.EmptyClipboard()
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = _kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        _user32.CloseClipboard()


def get_clipboard_text() -> str:
    if not _user32.OpenClipboard(None):
        return ""
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = _kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer) if pointer else ""
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def paste_text_into_focused_window(text: str) -> bool:
    """Insert text where the user's caret is: clipboard + Ctrl+V.

    The previous clipboard text is restored afterwards so dictation does
    not clobber what the user had copied.
    """
    previous = ""
    try:
        previous = get_clipboard_text()
    except Exception:
        previous = ""
    if not set_clipboard_text(text):
        return False
    import time as _time
    _time.sleep(0.03)
    ok = _send_keys([(VK_CONTROL, False), (VK_V, False), (VK_V, True), (VK_CONTROL, True)])
    if previous:
        def _restore() -> None:
            _time.sleep(0.6)
            set_clipboard_text(previous)
        threading.Thread(target=_restore, name="ClipboardRestore", daemon=True).start()
    return ok


def suppress_start_menu_on_win_release() -> None:
    """Tap an unassigned key so the coming Win key-up isn't a lone Win tap."""
    _send_keys([(VK_NONE_ASSIGNED, False), (VK_NONE_ASSIGNED, True)])


def is_left_mouse_button_down() -> bool:
    return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def get_foreground_window_info() -> dict[str, str]:
    """Title and process image name of the window the user is working in."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return {"title": "", "process": ""}
    length = _user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    process_id = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    process_name = ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    process_handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value)
    if process_handle:
        try:
            size = wintypes.DWORD(1024)
            path_buffer = ctypes.create_unicode_buffer(1024)
            if _kernel32.QueryFullProcessImageNameW(process_handle, 0, path_buffer, ctypes.byref(size)):
                process_name = path_buffer.value.rsplit("\\", 1)[-1]
        finally:
            _kernel32.CloseHandle(process_handle)
    return {"title": buffer.value, "process": process_name}


_user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
_user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
_user32.BringWindowToTop.argtypes = (wintypes.HWND,)


def force_foreground_window(hwnd: int) -> bool:
    """Give keyboard focus to our quick-ask box when a global hotkey opens it.

    Windows only lets the process that received the last input steal the
    foreground; a hotkey seen by a low-level hook doesn't count. Briefly
    attaching to the foreground thread's input queue is the documented
    workaround.
    """
    foreground = _user32.GetForegroundWindow()
    if not foreground or foreground == hwnd:
        return bool(_user32.SetForegroundWindow(hwnd))
    foreground_thread = _user32.GetWindowThreadProcessId(foreground, None)
    our_thread = _kernel32.GetCurrentThreadId()
    attached = bool(_user32.AttachThreadInput(our_thread, foreground_thread, True))
    try:
        _user32.BringWindowToTop(hwnd)
        return bool(_user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            _user32.AttachThreadInput(our_thread, foreground_thread, False)
