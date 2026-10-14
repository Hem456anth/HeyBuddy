"""Playback for ElevenLabs MP3 responses.

MP3 -> PCM decoding shells out to the static ffmpeg binary that the
`imageio-ffmpeg` wheel ships, so users never install ffmpeg themselves.

Why not pydub (what the first port used):

* pydub imports `audioop`, which was removed from the standard library in
  Python 3.13, so the app could not even start on a current Python.
* pydub launches ffmpeg without `CREATE_NO_WINDOW`; inside the windowed
  (no-console) .exe every spoken reply flashed a black console window.

Playback uses a private `sounddevice.OutputStream` instead of the global
`sd.play()` so a start/stop chime can never cut the spoken reply off (both
used to share sounddevice's single implicit stream).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from typing import Callable

import numpy as np
import sounddevice as sd

from ..utils.logger import get_logger

log = get_logger(__name__)

# Decode target: mono 24 kHz PCM16 is plenty for speech and keeps buffers small.
PLAYBACK_SAMPLE_RATE = 24_000
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def resolve_ffmpeg_executable() -> str | None:
    """Bundled imageio-ffmpeg binary first, then whatever is on PATH."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        log.debug("imageio-ffmpeg unavailable; looking for ffmpeg on PATH")
    return shutil.which("ffmpeg")


def decode_mp3_to_pcm16(mp3_bytes: bytes, sample_rate: int = PLAYBACK_SAMPLE_RATE) -> np.ndarray:
    """Decode MP3 bytes into a mono int16 numpy array."""
    ffmpeg_executable = resolve_ffmpeg_executable()
    if not ffmpeg_executable:
        raise RuntimeError("ffmpeg not found (install imageio-ffmpeg)")
    completed = subprocess.run(
        [
            ffmpeg_executable, "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(sample_rate),
            "pipe:1",
        ],
        input=mp3_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed to decode TTS audio: {completed.stderr.decode(errors='replace')[:300]}"
        )
    return np.frombuffer(completed.stdout, dtype=np.int16)


class AudioPlayer:
    """Plays one clip at a time; `stop()` cuts it off immediately."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_playing = False

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    def play_mp3(self, mp3_bytes: bytes, on_finished: Callable[[], None] | None = None,
                 on_started: Callable[[], None] | None = None) -> None:
        """Decode and play MP3 bytes on a background thread."""
        self.stop()
        if not mp3_bytes:
            if on_finished:
                on_finished()
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._play_worker,
            args=(mp3_bytes, self._stop_event, on_started, on_finished),
            name="AudioPlayer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        worker = self._thread
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=1.5)
        self._thread = None

    def _play_worker(
        self,
        mp3_bytes: bytes,
        stop_event: threading.Event,
        on_started: Callable[[], None] | None,
        on_finished: Callable[[], None] | None,
    ) -> None:
        was_stopped = False
        try:
            samples = decode_mp3_to_pcm16(mp3_bytes)
            if stop_event.is_set():
                was_stopped = True
                return
            self._is_playing = True
            if on_started:
                try:
                    on_started()
                except Exception:
                    log.exception("AudioPlayer on_started callback raised")
            block_frames = 1024
            with sd.OutputStream(
                samplerate=PLAYBACK_SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=block_frames,
            ) as output_stream:
                for block_start in range(0, len(samples), block_frames):
                    if stop_event.is_set():
                        was_stopped = True
                        break
                    output_stream.write(samples[block_start:block_start + block_frames])
        except Exception:
            log.exception("AudioPlayer failed")
        finally:
            self._is_playing = False
            # A stopped clip was superseded on purpose; whoever stopped it owns
            # the next state transition, so don't fire on_finished.
            if on_finished and not was_stopped:
                try:
                    on_finished()
                except Exception:
                    log.exception("AudioPlayer on_finished callback raised")
