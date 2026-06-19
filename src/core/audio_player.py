"""Playback for ElevenLabs MP3 responses (used in Phase 2).

The recorder is on `sounddevice`; the player matches. pydub handles MP3 -> PCM
decoding so we don't pull `simpleaudio` (needs a C toolchain) or `playsound`
(flaky on Windows). The decoded `AudioSegment` gives us raw PCM that
sounddevice plays via `sd.play`.

ffmpeg discovery: pydub shells out to `ffmpeg` on PATH to decode MP3. Rather
than make the user install ffmpeg system-wide, we depend on `imageio-ffmpeg`
which ships a static ffmpeg binary inside the venv via pip. At import time
we point `AudioSegment.converter` at that binary so playback works
out-of-the-box on a fresh venv with no PATH manipulation. If
`imageio-ffmpeg` isn't installed we fall back silently to whatever's on
PATH (pydub's default behavior), so the module is still importable in
environments that have ffmpeg installed system-wide.
"""
from __future__ import annotations

import io
import threading

import numpy as np
import sounddevice as sd
from pydub import AudioSegment

from ..utils.logger import get_logger

log = get_logger(__name__)


def _configure_pydub_ffmpeg() -> None:
    """Point pydub at the imageio-ffmpeg static binary if available.

    Called once at module import. Idempotent — re-running is harmless.
    """
    try:
        import imageio_ffmpeg
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        log.debug(
            "imageio-ffmpeg not available; pydub will look for ffmpeg on PATH"
        )
        return
    AudioSegment.converter = ffmpeg_path
    # pydub also uses ffprobe for some operations; ffprobe ships in the same
    # imageio-ffmpeg bundle on most platforms but the package exposes only
    # ffmpeg. We leave ffprobe alone — pydub falls back to ffmpeg for the
    # probe operations we actually use (mp3 -> raw PCM).
    log.info("pydub configured to use bundled ffmpeg: %s", ffmpeg_path)


_configure_pydub_ffmpeg()


class AudioPlayer:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_finished_callback = None

    def play_mp3(self, mp3_bytes: bytes, on_finished=None) -> None:
        """Decode and play MP3 bytes on a background thread."""
        if not mp3_bytes:
            if on_finished:
                on_finished()
            return
        self.stop()
        self._on_finished_callback = on_finished
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._play_worker,
            args=(mp3_bytes,),
            name="AudioPlayer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            try:
                sd.stop()
            except Exception:
                # `sd.stop` raises if no stream is active; that's fine here.
                pass
            self._thread.join(timeout=1.0)
        self._thread = None

    def _play_worker(self, mp3_bytes: bytes) -> None:
        try:
            decoded = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
            decoded = decoded.set_sample_width(2)   # 16-bit PCM
            samples = np.frombuffer(decoded.raw_data, dtype=np.int16)
            if decoded.channels > 1:
                samples = samples.reshape(-1, decoded.channels)
            sd.play(samples, samplerate=decoded.frame_rate)
            # `sd.wait` blocks until the buffer drains; we poll the stop flag
            # in slices so cancellation is responsive.
            while sd.get_stream().active and not self._stop_event.is_set():
                sd.sleep(50)
            if self._stop_event.is_set():
                sd.stop()
        except Exception:
            log.exception("AudioPlayer crashed")
        finally:
            callback = self._on_finished_callback
            self._on_finished_callback = None
            if callback:
                try:
                    callback()
                except Exception:
                    log.exception("AudioPlayer on_finished callback raised")
