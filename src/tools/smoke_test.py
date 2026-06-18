"""Manual hotkey + recording smoke test (no network, no Qt UI).

Run with:

    python -m src.tools.smoke_test

What it does:

1. Installs the Win32 low-level keyboard hook for Ctrl+Alt.
2. On press, starts a `sounddevice` PCM16 16 kHz mono capture.
3. On release, writes the captured audio to `./recordings/smoke-<ts>.wav` and
   prints the duration + byte count.
4. Press Ctrl+C in the terminal to exit.

This satisfies the Phase 1 acceptance criterion in the project spec:
"Test the hotkey and audio recording BEFORE wiring any API." If this script
works on your machine, the floating-panel app (`python -m src.main`) will
also capture audio correctly — they share the same `AudioRecorder` and
`LowLevelKeyboardHook` plumbing.
"""
from __future__ import annotations

import signal
import sys
import threading
import time
import wave
from pathlib import Path

# Allow `python src/tools/smoke_test.py` (direct) and `python -m src.tools.smoke_test`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.audio_recorder import AudioRecorder
from src.core.hotkey_monitor import HotkeyMonitor
from src.utils.constants import PROJECT_ROOT
from src.utils.logger import get_logger
from src.utils.win32 import enable_per_monitor_dpi_awareness

log = get_logger(__name__)

RECORDINGS_DIR = PROJECT_ROOT / "recordings"
SAMPLE_RATE = 16_000
CHANNELS = 1


def write_wav(pcm_bytes: bytes, sample_rate: int, channels: int) -> Path:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS_DIR / f"smoke-{int(time.time())}.wav"
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return path


def main() -> int:
    # Per-monitor DPI awareness isn't strictly needed for the smoke test, but
    # it brings this script's environment in line with the real app so any
    # awareness-related surprises surface here too.
    enable_per_monitor_dpi_awareness()

    recorder = AudioRecorder(sample_rate=SAMPLE_RATE, channels=CHANNELS)
    shutdown_event = threading.Event()

    def on_press() -> None:
        # The hook callback is on a Win32 pump thread, but `AudioRecorder` is
        # explicitly designed to be thread-safe via its own lock so this is
        # fine — no marshaling needed for the smoke test.
        print("\n>>> hotkey down — recording...")
        try:
            recorder.start()
        except Exception as start_error:
            print(f"!!! recorder.start failed: {start_error}")

    def on_release() -> None:
        print(">>> hotkey up — stopping...")
        pcm_bytes = recorder.stop()
        if not pcm_bytes:
            print("    (no audio captured)")
            return
        wav_path = write_wav(pcm_bytes, SAMPLE_RATE, CHANNELS)
        bytes_per_second = SAMPLE_RATE * CHANNELS * 2
        duration_seconds = len(pcm_bytes) / bytes_per_second
        print(
            f"    wrote {wav_path}  "
            f"({len(pcm_bytes)} bytes, {duration_seconds:.2f}s)"
        )

    hotkey = HotkeyMonitor(on_press=on_press, on_release=on_release)
    hotkey.start()

    signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())

    print(
        "HeyClicky smoke test running.\n"
        "  Hold Ctrl+Alt anywhere, speak, release.\n"
        "  WAVs land in ./recordings/.\n"
        "  Press Ctrl+C in this terminal to exit."
    )
    try:
        shutdown_event.wait()
    finally:
        hotkey.stop()
        recorder.cancel()
        print("\nSmoke test exited.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
