"""HeyBuddy entry point.

Usage:
    python -m src.main                 # normal tray app
    python -m src.main --selfcheck     # headless-ish diagnostics, exits with a code

Boots:

1. Per-monitor DPI awareness (must happen before any window is shown).
2. The PyQt6 event loop on the main thread.
3. A `pystray.Icon` on its own daemon thread for the system tray. Menu
   callbacks marshal back to the Qt thread via `QMetaObject.invokeMethod`
   so we never mutate widgets off the main thread.
4. The Win32 low-level keyboard hook (Ctrl+Alt push-to-talk).
5. The `CompanionManager` state machine, the floating panel, and the
   per-monitor blue cursor buddy overlay.
A named mutex keeps a second copy from starting (two hooks = double turns).
"""
from __future__ import annotations

import math
import os
import signal
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from PyQt6.QtCore import QMetaObject, Qt, QTimer
from PyQt6.QtWidgets import QApplication

# Make `python src/main.py` work as well as `python -m src.main`. The
# package-relative imports below assume we're imported as `src.main`; if a
# user runs the file directly, prepend the repo root so the same imports work.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.claude_client import ClaudeClient
from src.api.cloudflare_proxy import CloudflareProxy
from src.api.elevenlabs_client import ElevenLabsClient
from src.core import chimes
from src.core.audio_player import AudioPlayer
from src.core.audio_recorder import AudioRecorder
from src.core.companion_manager import CompanionManager, CompanionState
from src.core.hotkey_monitor import HotkeyMonitor
from src.core.screen_capture import ScreenCapture
from src.models.config import AppConfig
from src.ui import theme
from src.ui.main_window import MainPanel
from src.ui.overlay_window import BuddyOverlayController
from src.utils.constants import APP_NAME, APP_VERSION, USER_DATA_DIR
from src.utils.logger import get_logger
from src.utils.win32 import acquire_single_instance_lock, enable_per_monitor_dpi_awareness

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tray icon image — pystray needs a PIL.Image, not a QIcon
# ---------------------------------------------------------------------------


# Maps CompanionState to the theme color the tray icon should render in.
# Mirrors `_STATE_LABELS` in main_window.py so the tray dot and the panel's
# status row always agree on what each state looks like.
_STATE_TO_TRAY_COLOR_HEX: dict[CompanionState, str] = {
    CompanionState.IDLE:       theme.Color.STATE_IDLE,
    CompanionState.LISTENING:  theme.Color.STATE_LISTENING,
    CompanionState.PROCESSING: theme.Color.STATE_PROCESSING,
    CompanionState.RESPONDING: theme.Color.STATE_RESPONDING,
    CompanionState.ERROR:      theme.Color.STATE_ERROR,
}


def _build_tray_image_for_color(color_hex: str) -> Image.Image:
    """64x64 tray icon: the buddy's tilted triangle, tinted by state.

    Same shape as the on-screen buddy so the tray and the cursor read as
    one character; the color mirrors the panel's status row (idle blue,
    listening, thinking, speaking, error).
    """
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    drawer = ImageDraw.Draw(image)
    red = int(color_hex[1:3], 16)
    green = int(color_hex[3:5], 16)
    blue = int(color_hex[5:7], 16)
    side = 50.0
    height = side * math.sqrt(3.0) / 2.0
    corners = [(0.0, -height / 1.5), (-side / 2.0, height / 3.0), (side / 2.0, height / 3.0)]
    angle = math.radians(-35.0)
    rotated = [
        (32 + x * math.cos(angle) - y * math.sin(angle), 32 + x * math.sin(angle) + y * math.cos(angle))
        for x, y in corners
    ]
    drawer.polygon(rotated, fill=(red, green, blue, 255))
    return image


# ---------------------------------------------------------------------------
# Marshaling tray callbacks onto the Qt thread
# ---------------------------------------------------------------------------


def _invoke_on_qt_thread(qt_target, slot_name: str) -> None:
    """Schedule `slot_name` to run on `qt_target`'s thread.

    pystray menu callbacks fire on the pystray thread. Calling a `QWidget`
    method from there is undefined behavior; `invokeMethod` with a
    `QueuedConnection` puts the call on the target object's event loop
    instead.
    """
    QMetaObject.invokeMethod(
        qt_target,
        slot_name,
        Qt.ConnectionType.QueuedConnection,
    )


def _build_tray_icon(
    panel: MainPanel,
    manager: CompanionManager,
    qt_app: QApplication,
    config: AppConfig,
    overlay: BuddyOverlayController,
) -> pystray.Icon:
    """Construct the pystray.Icon. Caller is responsible for `.run_detached()`."""

    def on_open_panel(_icon, _item):
        _invoke_on_qt_thread(panel, "toggle")

    def on_open_settings(_icon, _item):
        _invoke_on_qt_thread(panel, "open_settings")

    def on_toggle_companion(_icon, _item):
        # Marshal onto the Qt thread — toggle_enabled is decorated
        # @pyqtSlot so invokeMethod can find it by name.
        _invoke_on_qt_thread(manager, "toggle_enabled")

    def on_toggle_cursor_buddy(_icon, _item):
        # A plain bool flip is thread-safe enough; the overlay reads it on
        # its next frame. Persist so the choice survives restarts.
        config.show_cursor_buddy = not config.show_cursor_buddy
        try:
            config.save()
        except Exception:
            log.exception("Failed to save show_cursor_buddy")

    def on_clear_conversation(_icon, _item):
        _invoke_on_qt_thread(manager, "clear_history")

    def on_open_data_folder(_icon, _item):
        try:
            os.startfile(str(USER_DATA_DIR))  # type: ignore[attr-defined]
        except Exception:
            log.exception("Could not open %s", USER_DATA_DIR)

    def on_quit(icon, _item):
        # Stop the tray loop first so it doesn't try to call back into us
        # after the Qt loop exits.
        icon.stop()
        QMetaObject.invokeMethod(qt_app, "quit", Qt.ConnectionType.QueuedConnection)

    menu = pystray.Menu(
        pystray.MenuItem("Open", on_open_panel, default=True),
        # Checkable item — the check mark reflects manager.is_enabled at
        # the moment the menu opens. Clicking it flips the state via
        # toggle_enabled. Acts as a kill switch for the push-to-talk
        # hotkey (e.g. mute during a meeting without quitting).
        pystray.MenuItem(
            "Toggle Companion",
            on_toggle_companion,
            checked=lambda _item: manager.is_enabled,
        ),
        pystray.MenuItem(
            "Show Buddy Cursor",
            on_toggle_cursor_buddy,
            checked=lambda _item: config.show_cursor_buddy,
        ),
        pystray.MenuItem("Clear Conversation", on_clear_conversation),
        pystray.MenuItem("Settings", on_open_settings),
        pystray.MenuItem("Open Logs and Settings Folder", on_open_data_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon(
        name=APP_NAME,
        # Initial icon = IDLE color. Will be re-rendered per state by
        # the state_changed handler in main() once the manager exists
        # and the signal is connected.
        icon=_build_tray_image_for_color(
            _STATE_TO_TRAY_COLOR_HEX[CompanionState.IDLE],
        ),
        title=APP_NAME,
        menu=menu,
    )
    return icon


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_command_line(argv: list[str]) -> dict[str, object]:
    options: dict[str, object] = {"selfcheck": False, "selfcheck_seconds": 6.0}
    for argument in argv[1:]:
        if argument == "--selfcheck":
            options["selfcheck"] = True
        elif argument.startswith("--selfcheck-seconds="):
            options["selfcheck_seconds"] = float(argument.split("=", 1)[1])
    return options


def main() -> int:
    options = _parse_command_line(sys.argv)
    # Order matters: DPI awareness must be set before *any* HWND exists, and
    # before Qt reads the screens. A tier of "none"/"system" on a HiDPI
    # machine is the smoking gun for "points land off-target".
    dpi_awareness_tier = enable_per_monitor_dpi_awareness()
    log.info("%s %s starting (DPI awareness: %s)", APP_NAME, APP_VERSION, dpi_awareness_tier)

    if not options["selfcheck"] and not acquire_single_instance_lock():
        log.warning("Another HeyBuddy is already running; exiting.")
        return 0

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(APP_NAME)
    qt_app.setApplicationVersion(APP_VERSION)
    # We live in the tray; don't quit just because the panel was hidden.
    qt_app.setQuitOnLastWindowClosed(False)

    config = AppConfig.load()

    recorder = AudioRecorder(
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        block_size=config.audio.chunk_size,
        input_device_index=config.audio.input_device_index,
    )
    screen = ScreenCapture()
    proxy = CloudflareProxy(config.worker_url, shared_secret=config.worker_shared_secret)
    claude = ClaudeClient(proxy=proxy, model=config.model)
    tts = ElevenLabsClient(proxy=proxy)
    player = AudioPlayer()

    manager = CompanionManager(
        config=config,
        recorder=recorder,
        screen=screen,
        proxy=proxy,
        claude=claude,
        tts=tts,
        player=player,
    )

    panel = MainPanel(config, manager)
    overlay = BuddyOverlayController(config)
    queued = Qt.ConnectionType.QueuedConnection
    manager.state_changed.connect(overlay.set_companion_state, queued)
    manager.audio_level_changed.connect(overlay.set_audio_level, queued)
    manager.pointing_requested.connect(overlay.show_pointing, queued)
    manager.turn_interrupted.connect(overlay.cancel_pointing, queued)
    panel.overlay = overlay

    if options["selfcheck"]:
        from src.tools.selfcheck import run_selfcheck
        return run_selfcheck(
            qt_app=qt_app,
            config=config,
            manager=manager,
            overlay=overlay,
            panel=panel,
            duration_seconds=float(options["selfcheck_seconds"]),
        )

    hotkey = HotkeyMonitor(
        on_press=manager.begin_listening,
        on_release=manager.end_listening_and_process,
        on_cancel=manager.cancel_listening,
        chord_name=config.hotkey,
    )
    hotkey.start()
    # Exposed so the Settings dialog can rebind the chord without a restart.
    panel.hotkey_monitor = hotkey

    tray_icon = _build_tray_icon(panel, manager, qt_app, config, overlay)

    def _on_state_changed_repaint_tray(state: CompanionState) -> None:
        color_hex = _STATE_TO_TRAY_COLOR_HEX.get(state, theme.Color.STATE_IDLE)
        try:
            tray_icon.icon = _build_tray_image_for_color(color_hex)
            tray_icon.title = f"{APP_NAME} - {state.value}"
        except Exception:
            log.exception("Failed to update tray icon for state %s", state.value)

    manager.state_changed.connect(_on_state_changed_repaint_tray)

    # Start/stop chimes on the LISTENING edges.
    _previous_state_holder: list[CompanionState] = [CompanionState.IDLE]

    def _on_state_changed_play_chime(new_state: CompanionState) -> None:
        if new_state == CompanionState.LISTENING:
            chimes.play_start_chime()
        elif _previous_state_holder[0] == CompanionState.LISTENING and new_state == CompanionState.PROCESSING:
            chimes.play_stop_chime()
        _previous_state_holder[0] = new_state

    manager.state_changed.connect(_on_state_changed_play_chime)

    tray_thread = threading.Thread(target=tray_icon.run, name="PystrayThread", daemon=True)
    tray_thread.start()

    # Let Ctrl+C in a terminal exit cleanly: Python only runs signal handlers
    # between bytecodes, so tick a timer to give it the chance.
    signal.signal(signal.SIGINT, lambda *_: qt_app.quit())
    interpreter_nudge = QTimer()
    interpreter_nudge.start(250)
    interpreter_nudge.timeout.connect(lambda: None)

    # Show the panel on launch: Windows hides new tray icons under the
    # chevron, so without this the app looks like it never started.
    panel.show()
    panel.raise_()

    log.info("HeyBuddy started. Hold %s anywhere to talk.", config.hotkey.upper())
    try:
        return qt_app.exec()
    finally:
        hotkey.stop()
        manager.cancel()
        overlay.shutdown()
        try:
            tray_icon.stop()
        except Exception:
            pass
        log.info("HeyBuddy exited")


if __name__ == "__main__":
    raise SystemExit(main())
