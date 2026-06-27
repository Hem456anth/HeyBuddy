"""HeyBuddy entry point (Phase 1).

Boots:

1. Per-monitor DPI awareness (must happen before any window is shown).
2. The PyQt6 event loop on the main thread.
3. A `pystray.Icon` on its own daemon thread for the system tray. Menu
   callbacks marshal back to the Qt thread via `QMetaObject.invokeMethod`
   so we never mutate widgets off the main thread.
4. The Win32 low-level keyboard hook (Ctrl+Alt push-to-talk).
5. The `CompanionManager` state machine, the floating panel, and the
   cursor overlay.

Phase 1 deliberately does NOT construct any API client — the Worker URL is
still a placeholder and we want to verify hotkey + recording end-to-end
before adding network failure modes on top.
"""
from __future__ import annotations

import signal
import sys
import threading
from io import BytesIO
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from PyQt6.QtCore import Q_ARG, QMetaObject, Qt, QTimer
from PyQt6.QtGui import QPainter, QPixmap
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
from src.ui.overlay_window import CursorOverlay
from src.utils.constants import APP_NAME, ASSETS_DIR
from src.utils.logger import get_logger
from src.utils.win32 import enable_per_monitor_dpi_awareness

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
    """Render a 64x64 RGBA tray icon: a filled circle in the given color.

    Cycle 17 rewrites the tray icon per state instead of using a static
    asset, so the tray dot mirrors the panel's status row at a glance.
    The `assets/tray.png` override that the original `_load_tray_image`
    supported is dropped here — a static asset can't change color per
    state. Users who want a custom asset can extend this helper to
    composite a state-colored badge onto the asset; out of scope for
    cycle 17.
    """
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    drawer = ImageDraw.Draw(image)
    r = int(color_hex[1:3], 16)
    g = int(color_hex[3:5], 16)
    b = int(color_hex[5:7], 16)
    drawer.ellipse((8, 8, 56, 56), fill=(r, g, b, 255))
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
        pystray.MenuItem("Settings", on_open_settings),
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


def main() -> int:
    # Order matters here: DPI awareness must be set before *any* HWND exists.
    # The return value names which tier the OS accepted so an operator can
    # diagnose "all my points land off-target" by grepping for this line in
    # logs/heybuddy.log — a tier of "none" or "system" on a HiDPI machine
    # is the smoking gun.
    dpi_awareness_tier = enable_per_monitor_dpi_awareness()
    log.info("DPI awareness: %s", dpi_awareness_tier)

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(APP_NAME)
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
    proxy = CloudflareProxy(config.worker_url)
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
    overlay = CursorOverlay(config)
    # POINT markers from Claude → cursor overlay queue (queued connection so the
    # call hops to the Qt thread regardless of which thread the signal fires on).
    manager.point_received.connect(
        overlay.point_at,
        Qt.ConnectionType.QueuedConnection,
    )

    hotkey = HotkeyMonitor(
        on_press=manager.begin_listening,
        on_release=manager.end_listening_and_process,
        chord_name=config.hotkey,
    )
    hotkey.start()
    # Expose the hotkey monitor on the panel so the Settings dialog can rebind
    # the chord without re-launching the app.
    panel.hotkey_monitor = hotkey

    tray_icon = _build_tray_icon(panel, manager, qt_app)

    # Re-render the tray icon on every state transition so it mirrors the
    # panel's status row at a glance. The handler reads from
    # _STATE_TO_TRAY_COLOR_HEX (same theme tokens as `_StatusRow`) so a
    # future re-theme moves both indicators together. pystray accepts
    # `icon.icon = <PIL.Image>` from any thread — its internal queue
    # marshals the redraw onto the tray loop.
    def _on_state_changed_repaint_tray(state: CompanionState) -> None:
        color_hex = _STATE_TO_TRAY_COLOR_HEX.get(
            state, theme.Color.STATE_IDLE,
        )
        try:
            tray_icon.icon = _build_tray_image_for_color(color_hex)
        except Exception:
            log.exception("Failed to update tray icon for state %s", state.value)

    manager.state_changed.connect(_on_state_changed_repaint_tray)

    # Audio cues: rising tone on IDLE -> LISTENING, falling tone on
    # LISTENING -> anything else. Tracking the previous state in a
    # one-element list (a mutable closure cell) is the smallest way to
    # detect the LISTENING-edge transitions; using a module-level dict
    # or a class would be heavier without adding clarity here. The
    # manager only emits state_changed on actual transitions, so
    # `new_state == LISTENING` reliably means "entering listening".
    _previous_state_holder: list[CompanionState] = [CompanionState.IDLE]

    def _on_state_changed_play_chime(new_state: CompanionState) -> None:
        if new_state == CompanionState.LISTENING:
            chimes.play_start_chime()
        elif _previous_state_holder[0] == CompanionState.LISTENING:
            chimes.play_stop_chime()
        _previous_state_holder[0] = new_state

    manager.state_changed.connect(_on_state_changed_play_chime)

    # `run_detached` returns immediately and runs the tray loop on its own
    # thread, which is what we want when sharing the process with Qt.
    tray_thread = threading.Thread(
        target=tray_icon.run,
        name="PystrayThread",
        daemon=True,
    )
    tray_thread.start()

    # Let Ctrl+C in the terminal exit cleanly. Python's signal handlers only
    # run between bytecode instructions, so we tick a QTimer to give the
    # interpreter a chance to notice.
    signal.signal(signal.SIGINT, lambda *_: qt_app.quit())
    interpreter_nudge = QTimer()
    interpreter_nudge.start(250)
    interpreter_nudge.timeout.connect(lambda: None)

    # Show the panel once on launch so the user immediately sees the app is
    # running. Windows hides new tray icons under the chevron by default, so
    # without this the only feedback is the (often invisible) tray icon and
    # a silent terminal. Closing the panel sends it back to tray-only mode.
    panel.show()
    panel.raise_()

    log.info("HeyBuddy started. Hold Ctrl+Alt anywhere to talk.")
    try:
        return qt_app.exec()
    finally:
        hotkey.stop()
        manager.cancel()
        try:
            tray_icon.stop()
        except Exception:
            pass
        log.info("HeyBuddy exited")


if __name__ == "__main__":
    raise SystemExit(main())
