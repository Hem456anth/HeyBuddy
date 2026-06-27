"""Settings dialog: Worker URL, model, hotkey, modes, autostart, diagnostics."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..models.config import AppConfig
from ..utils.logger import get_logger
from ..utils.permissions import check_microphone, ping_worker
from ..utils.win32 import (
    HOTKEY_PRESETS,
    autostart_command_for_current_process,
    disable_autostart,
    enable_autostart,
    is_autostart_enabled,
)
from . import theme

log = get_logger(__name__)


def _enumerate_input_devices() -> list[tuple[int, str]]:
    """Return [(device_index, display_name), ...] for usable input devices.

    Wrapped in try/except because sounddevice raises if no audio host is
    present (rare, but it happens on stripped-down Windows installs and
    in CI). A failure returns an empty list; the Settings panel then
    shows only "System default", which is still functional.
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
    except Exception:
        log.exception("sounddevice.query_devices failed; mic picker will be empty")
        return []
    inputs: list[tuple[int, str]] = []
    for index, device in enumerate(devices):
        # Only show devices that can actually record.
        if int(device.get("max_input_channels", 0)) <= 0:
            continue
        name = device.get("name") or f"device {index}"
        inputs.append((index, name))
    return inputs


class SettingsPanel(QDialog):
    """Modal settings dialog.

    Emits `settings_saved(AppConfig)` after persisting. The owner is expected
    to re-apply anything that needs a runtime poke (theme, hotkey rebind);
    this dialog does NOT mutate live runtime objects directly.
    """

    settings_saved = pyqtSignal(object)  # AppConfig

    AVAILABLE_MODELS = [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]

    HOTKEY_LABELS = {
        "ctrl+alt": "Ctrl + Alt  (default)",
        "ctrl+shift": "Ctrl + Shift",
        "alt+shift": "Alt + Shift",
        "right_alt": "Right Alt only",
        "alt+space": "Alt + Space",
    }

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("HeyBuddy Settings")
        self.config = config
        # Apply the shared button styling so the dialog's buttons (including
        # the auto-created Save/Cancel from QDialogButtonBox) look like the
        # ones in MainPanel and respond to hover / press.
        self.setStyleSheet(theme.button_stylesheet())

        form = QFormLayout()

        # ---- Worker URL + ping ----
        self.worker_url = QLineEdit(config.worker_url)
        worker_row = QHBoxLayout()
        worker_row.addWidget(self.worker_url, 1)
        self.ping_btn = QPushButton("Ping")
        self.ping_btn.setToolTip(
            "POST /transcribe-token to verify the Worker is reachable."
        )
        self.ping_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ping_btn.clicked.connect(self._ping_worker)
        worker_row.addWidget(self.ping_btn)
        form.addRow("Worker URL", worker_row)

        # ---- Hotkey preset ----
        self.hotkey = QComboBox()
        for preset_name in HOTKEY_PRESETS.keys():
            self.hotkey.addItem(self.HOTKEY_LABELS.get(preset_name, preset_name), preset_name)
        current_preset_index = self.hotkey.findData(config.hotkey)
        if current_preset_index >= 0:
            self.hotkey.setCurrentIndex(current_preset_index)
        form.addRow("Push-to-talk", self.hotkey)

        # ---- Model ----
        self.model = QComboBox()
        self.model.addItems(self.AVAILABLE_MODELS)
        if config.model in self.AVAILABLE_MODELS:
            self.model.setCurrentText(config.model)
        else:
            self.model.addItem(config.model)
            self.model.setCurrentText(config.model)
        form.addRow("Model", self.model)

        # ---- Behavior toggles ----
        self.tts_enabled = QCheckBox("Speak responses with ElevenLabs")
        self.tts_enabled.setChecked(config.tts_enabled)
        form.addRow(self.tts_enabled)

        self.screen_enabled = QCheckBox("Send screenshot with each turn")
        self.screen_enabled.setChecked(config.screen_capture_enabled)
        form.addRow(self.screen_enabled)

        self.transient_mode = QCheckBox(
            "Transient cursor mode (hide panel during turns)"
        )
        self.transient_mode.setChecked(config.transient_cursor_mode)
        form.addRow(self.transient_mode)

        self.autostart = QCheckBox("Launch HeyBuddy at Windows sign-in")
        # Trust the registry, not the persisted config, on dialog open — the
        # user may have toggled it from outside the app.
        self.autostart.setChecked(is_autostart_enabled())
        form.addRow(self.autostart)

        # ---- Microphone picker ----
        # Populated from sounddevice.query_devices(); "System default"
        # (data=None) is always the first row so users have a known-good
        # fallback if their saved device disappeared (e.g. USB mic
        # unplugged). The selected item's `data` carries the integer
        # device index that AudioRecorder feeds to sd.InputStream.
        self.mic_device = QComboBox()
        self.mic_device.addItem("System default", None)
        for device_index, device_name in _enumerate_input_devices():
            self.mic_device.addItem(device_name, device_index)
        # Restore the saved selection. findData returns -1 if the saved
        # index is no longer present — fall back to "System default".
        saved_device_index = config.audio.input_device_index
        restore_idx = self.mic_device.findData(saved_device_index)
        if restore_idx < 0:
            restore_idx = 0
        self.mic_device.setCurrentIndex(restore_idx)
        form.addRow("Microphone", self.mic_device)

        # ---- Diagnostics: mic test ----
        self.mic_btn = QPushButton("Test microphone")
        self.mic_btn.setToolTip(
            "Record half a second and report whether anything came through."
        )
        self.mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_btn.clicked.connect(self._test_microphone)
        form.addRow("Diagnostics", self.mic_btn)

        # ---- Status line for diagnostic feedback ----
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#8a93a3; font-size:11px;")

        # ---- Buttons ----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        # Save/Cancel are auto-created by QDialogButtonBox, so we can't
        # setCursor at construction. Iterate the box's children and stamp
        # the pointer cursor on each one — matches the rest of the app.
        for button in buttons.buttons():
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)

    # ----- diagnostics -----
    def _ping_worker(self) -> None:
        self._set_status("Pinging Worker...", ok=True)
        # Use the *current text*, not the persisted config — the user may be
        # mid-edit when they want to test.
        result = ping_worker(self.worker_url.text())
        self._set_status(f"{result.title}: {result.detail}", ok=result.ok)

    def _test_microphone(self) -> None:
        self._set_status("Capturing 0.4s of audio...", ok=True)
        result = check_microphone()
        self._set_status(f"{result.title}: {result.detail}", ok=result.ok)

    def _set_status(self, message: str, ok: bool) -> None:
        color = "#9ee493" if ok else "#ff8787"
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color:{color}; font-size:11px;")

    # ----- save -----
    def _save(self) -> None:
        self.config.worker_url = self.worker_url.text().strip()
        self.config.model = self.model.currentText().strip()
        new_hotkey = self.hotkey.currentData() or "ctrl+alt"
        hotkey_changed = new_hotkey != self.config.hotkey
        self.config.hotkey = new_hotkey
        self.config.tts_enabled = self.tts_enabled.isChecked()
        self.config.screen_capture_enabled = self.screen_enabled.isChecked()
        self.config.transient_cursor_mode = self.transient_mode.isChecked()
        # Mic picker: currentData() returns None for "System default" or
        # an int for a real device. Track whether it changed so we only
        # poke the live recorder when needed.
        new_mic_index = self.mic_device.currentData()
        mic_changed = new_mic_index != self.config.audio.input_device_index
        self.config.audio.input_device_index = new_mic_index

        # Push autostart change through to the registry. We persist the
        # checkbox state into config as a hint for the next dialog opening,
        # but the registry is the source of truth.
        want_autostart = self.autostart.isChecked()
        if want_autostart and not is_autostart_enabled():
            ok = enable_autostart(autostart_command_for_current_process())
            if not ok:
                QMessageBox.warning(
                    self,
                    "Autostart",
                    "Failed to write the autostart registry entry. "
                    "Try running HeyBuddy as your user (not as admin).",
                )
        elif not want_autostart and is_autostart_enabled():
            disable_autostart()
        self.config.autostart_enabled = is_autostart_enabled()

        self.config.save()

        # Rebind the live hotkey monitor so the change takes effect
        # immediately — the owning panel hung the monitor off itself in
        # main.py specifically so we could reach it here.
        if hotkey_changed:
            owning_panel = self.parent()
            monitor = getattr(owning_panel, "hotkey_monitor", None)
            if monitor is not None:
                try:
                    monitor.rebind(new_hotkey)
                except Exception:
                    pass

        # Hot-swap the recorder's input device so the next push-to-talk
        # uses the new mic without a restart. Reached via the panel's
        # manager → recorder chain. set_input_device only pins the index
        # for subsequent start() calls; any in-flight recording finishes
        # on the old device, which is the correct behavior.
        if mic_changed:
            owning_panel = self.parent()
            recorder = getattr(getattr(owning_panel, "manager", None), "recorder", None)
            if recorder is not None:
                try:
                    recorder.set_input_device(new_mic_index)
                except Exception:
                    log.exception("Failed to hot-swap recorder input device")

        self.settings_saved.emit(self.config)
        self.accept()
