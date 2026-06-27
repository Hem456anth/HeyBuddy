"""Persisted user configuration."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..utils.constants import (
    DEFAULT_CHANNELS,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SAMPLE_RATE,
    SETTINGS_FILE,
)


@dataclass
class AudioConfig:
    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    chunk_size: int = DEFAULT_CHUNK_SIZE
    # sounddevice device index; `None` = system default input. The Settings
    # panel populates a dropdown from `sd.query_devices()` and persists the
    # selected device's index here. Stored as the integer index (not the
    # device name) so the choice survives renames; the panel re-resolves
    # the name on each open. A saved index that no longer exists (mic
    # unplugged) falls through to AudioRecorder.start's exception path
    # rather than silently picking a wrong device.
    input_device_index: int | None = None


@dataclass
class UIConfig:
    panel_width: int = 420
    panel_height: int = 560
    always_on_top: bool = True
    theme: str = "dark"
    # Last known panel position in physical desktop pixels. `None` means
    # "no saved position — center on the primary monitor". The MainPanel
    # writes these (debounced) whenever the user drags the window and
    # validates them against the live virtual-screen rect on launch so a
    # disconnected monitor doesn't trap the panel offscreen.
    panel_x: int | None = None
    panel_y: int | None = None


@dataclass
class TranscriptionConfig:
    provider: str = "assemblyai"
    language: str = "en"


@dataclass
class AppConfig:
    # Cloudflare Worker base URL. The client holds no API keys; the Worker
    # injects them into upstream requests.
    worker_url: str = "https://your-worker.workers.dev"
    # Phase 3 supports preset chord rebinding via Settings. Names must match
    # `utils.win32.HOTKEY_PRESETS` keys.
    hotkey: str = "ctrl+alt"
    model: str = "claude-sonnet-4-6"
    tts_enabled: bool = True
    screen_capture_enabled: bool = True
    # When True, the floating panel auto-hides when listening starts. The
    # cursor overlay remains the only visual feedback for the turn. Matches
    # upstream Clicky's "Show Cliky off" mode.
    transient_cursor_mode: bool = False
    # Persisted purely so the Settings panel can show the right checkbox
    # state; actual autostart state is the registry, set via utils.win32.
    autostart_enabled: bool = False
    audio: AudioConfig = field(default_factory=AudioConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    # NOTE: there is no `voice_id` here. The Worker injects
    # ELEVENLABS_VOICE_ID into outbound /tts requests; the client cannot
    # override it. See `CLAUDE.md` Worker contract.

    # ----- I/O helpers -----
    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        target = path or SETTINGS_FILE
        if not target.exists():
            cfg = cls()
            cfg.save(target)
            return cfg
        with target.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls._from_dict(raw)

    def save(self, path: Path | None = None) -> None:
        target = path or SETTINGS_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        audio_raw = raw.get("audio") or {}
        ui_raw = raw.get("ui") or {}
        tr_raw = raw.get("transcription") or {}
        return cls(
            worker_url=raw.get("worker_url", cls.worker_url),
            hotkey=raw.get("hotkey", cls.hotkey),
            model=raw.get("model", cls.model),
            tts_enabled=raw.get("tts_enabled", True),
            screen_capture_enabled=raw.get("screen_capture_enabled", True),
            transient_cursor_mode=raw.get("transient_cursor_mode", False),
            autostart_enabled=raw.get("autostart_enabled", False),
            audio=AudioConfig(**audio_raw),
            ui=UIConfig(**ui_raw),
            transcription=TranscriptionConfig(**tr_raw),
        )
