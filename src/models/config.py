"""Persisted user configuration."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from ..utils.constants import (
    DEFAULT_CHANNELS,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SETTINGS_TEMPLATE,
    DEFAULT_STT_SPEECH_MODEL,
    SETTINGS_FILE,
)


def _known_kwargs(dataclass_type: type, raw: Any) -> dict[str, Any]:
    """Drop keys a dataclass doesn't know, so an old/new settings.json
    never crashes startup with `TypeError: unexpected keyword`."""
    if not isinstance(raw, dict):
        return {}
    allowed = {f.name for f in fields(dataclass_type)}
    return {key: value for key, value in raw.items() if key in allowed}


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
    # AssemblyAI realtime model id (see constants.DEFAULT_STT_SPEECH_MODEL).
    speech_model: str = DEFAULT_STT_SPEECH_MODEL


@dataclass
class AppConfig:
    # Cloudflare Worker base URL. The client holds no API keys; the Worker
    # injects them into upstream requests.
    worker_url: str = "https://your-worker.workers.dev"
    # Phase 3 supports preset chord rebinding via Settings. Names must match
    # `utils.win32.HOTKEY_PRESETS` keys.
    hotkey: str = "ctrl+alt"
    model: str = DEFAULT_CLAUDE_MODEL
    tts_enabled: bool = True
    screen_capture_enabled: bool = True
    # When True, the floating panel auto-hides when listening starts. The
    # cursor overlay remains the only visual feedback for the turn. Matches
    # upstream Clicky's "Show Cliky off" mode.
    transient_cursor_mode: bool = False
    # Persisted purely so the Settings panel can show the right checkbox
    # state; actual autostart state is the registry, set via utils.win32.
    autostart_enabled: bool = False
    # Informational-only ElevenLabs voice ID. The LIVE voice is whatever
    # the Cloudflare Worker has set as `ELEVENLABS_VOICE_ID` (see
    # worker/wrangler.toml). The client does NOT send this value in
    # /tts requests — the Worker's `handleTTS` ignores any extra body
    # keys and always uses its env var. This field is here so the
    # Settings panel can display what the user *intends* to deploy,
    # for record-keeping; changing it does NOT change the actual voice.
    voice_id: str = ""
    # Show the blue triangle buddy next to the mouse all the time (Clicky's
    # "Show Clicky" toggle). When False the buddy only fades in for a turn.
    show_cursor_buddy: bool = True
    # Persist each push-to-talk recording as a .wav under the user data dir.
    # Off by default: recordings are private voice data. Turn on to debug STT.
    save_recordings: bool = False
    # Optional shared secret sent as `X-HeyBuddy-Key` to a Worker that has
    # `APP_SHARED_SECRET` set. Not a strong secret (it ships in the client
    # config) but it stops drive-by abuse of a public Worker URL.
    worker_shared_secret: str = ""
    audio: AudioConfig = field(default_factory=AudioConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)

    # ----- I/O helpers -----
    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        target = path or SETTINGS_FILE
        if not target.exists():
            # First run: seed from the template shipped next to the code
            # (lets a packager pre-fill worker_url), else pure defaults.
            cfg = cls()
            if path is None and DEFAULT_SETTINGS_TEMPLATE.exists():
                try:
                    with DEFAULT_SETTINGS_TEMPLATE.open("r", encoding="utf-8") as fh:
                        cfg = cls._from_dict(json.load(fh))
                except (OSError, json.JSONDecodeError):
                    cfg = cls()
            cfg.save(target)
            return cfg
        try:
            with target.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # A half-written or hand-mangled file must not brick the app.
            # Keep the broken copy for the user and start from defaults.
            backup = target.with_suffix(".broken.json")
            try:
                target.replace(backup)
            except OSError:
                pass
            cfg = cls()
            cfg.save(target)
            return cfg
        return cls._from_dict(raw if isinstance(raw, dict) else {})

    def save(self, path: Path | None = None) -> None:
        """Atomic write (temp file + replace) so a crash can't corrupt it."""
        target = path or SETTINGS_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_path = tempfile.mkstemp(
            prefix="settings.", suffix=".tmp", dir=str(target.parent),
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
            os.replace(temp_path, target)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        top_level = _known_kwargs(cls, raw)
        top_level["audio"] = AudioConfig(**_known_kwargs(AudioConfig, raw.get("audio")))
        top_level["ui"] = UIConfig(**_known_kwargs(UIConfig, raw.get("ui")))
        top_level["transcription"] = TranscriptionConfig(
            **_known_kwargs(TranscriptionConfig, raw.get("transcription")),
        )
        return cls(**top_level)
