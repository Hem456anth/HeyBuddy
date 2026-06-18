"""Push-to-talk audio capture built on `sounddevice`.

We record raw 16-bit PCM mono at 16 kHz to match the format AssemblyAI's
realtime v3 endpoint expects (no resampling needed at upload time). The
sounddevice callback runs on its own thread; we accumulate frames into a
`deque[bytes]` under a lock and drain them on `stop()`.

This module is deliberately framework-agnostic: it returns raw bytes and a
sample rate. The `CompanionManager` is responsible for handing those bytes to
a transcription provider in Phase 2.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Callable

import numpy as np
import sounddevice as sd

from ..utils.logger import get_logger

log = get_logger(__name__)

# Type alias for the per-block push callback. Receives raw PCM16 little-endian
# bytes; called on sounddevice's audio thread. Implementations must NOT block
# (or they will cause input overflows). Pushing to a thread-safe queue or a
# websocket sender is fine; a synchronous HTTP call is not.
PcmChunkCallback = Callable[[bytes], None]


class AudioRecorder:
    """Threaded mic recorder. Idempotent start; cancel discards the buffer.

    Two consumption modes, used together:

    * Buffered: every captured block is appended to an internal deque and
      returned by `stop()` as a single bytes object.
    * Streamed: if an `on_chunk` callback is registered (`set_chunk_listener`),
      each block is also handed to that callback as it arrives. This is what
      AssemblyAI streaming uses to forward PCM into the websocket without
      waiting for the user to release the hotkey.
    """

    def __init__(
        self,
        sample_rate: int = 16_000,
        channels: int = 1,
        block_size: int = 1024,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = block_size
        self._stream: sd.InputStream | None = None
        self._frames: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._recording = False
        self._on_chunk: PcmChunkCallback | None = None

    def set_chunk_listener(self, on_chunk: PcmChunkCallback | None) -> None:
        """Register (or clear) a per-block PCM callback.

        Safe to call between recordings; setting to `None` reverts to
        buffered-only mode. The callback fires on the sounddevice audio
        thread — it must not block.
        """
        with self._lock:
            self._on_chunk = on_chunk

    @property
    def is_recording(self) -> bool:
        return self._recording

    # ----- lifecycle -----
    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._frames.clear()
            # `dtype="int16"` makes sounddevice deliver PCM16 directly so we
            # don't pay for a float32 -> int16 conversion in Python.
            try:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="int16",
                    blocksize=self.block_size,
                    callback=self._on_block,
                )
                self._stream.start()
            except Exception:
                log.exception("Failed to open input stream")
                self._stream = None
                raise
            self._recording = True
            log.info(
                "Recording started (rate=%d, ch=%d)", self.sample_rate, self.channels,
            )

    def stop(self) -> bytes:
        """Stop and return the captured PCM-16 byte string."""
        with self._lock:
            if not self._recording:
                return b"".join(self._frames)
            self._recording = False
        self._teardown_stream()
        pcm = b"".join(self._frames)
        log.info(
            "Recording stopped (%d bytes / %.2fs)",
            len(pcm), self._duration_seconds(len(pcm)),
        )
        return pcm

    def cancel(self) -> None:
        """Stop without returning audio (discard buffer)."""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
        self._teardown_stream()
        self._frames.clear()
        log.info("Recording cancelled")

    # ----- callback (sounddevice thread) -----
    def _on_block(self, indata: np.ndarray, frames: int, time_info, status) -> None:  # type: ignore[no-untyped-def]
        # `status` flags overflow/underflow; log and continue. Dropping frames
        # is fine; raising would tear down the stream and lose audio entirely.
        if status:
            log.debug("sounddevice status: %s", status)
        # `indata` is a (frames, channels) int16 ndarray. `bytes(indata)` gives
        # us little-endian PCM16 which is exactly what AssemblyAI wants.
        chunk = bytes(indata)
        self._frames.append(chunk)
        # Snapshot the callback reference without holding the lock for the
        # whole callback duration — set_chunk_listener can replace it any time.
        listener = self._on_chunk
        if listener is not None:
            try:
                listener(chunk)
            except Exception:
                # Never let a listener crash take down the audio thread; that
                # would silently stop recording for the rest of the session.
                log.exception("on_chunk listener raised")

    # ----- helpers -----
    def _teardown_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.exception("Error while closing input stream")
            finally:
                self._stream = None

    def _duration_seconds(self, byte_count: int) -> float:
        bytes_per_second = self.sample_rate * self.channels * 2
        return byte_count / bytes_per_second if bytes_per_second else 0.0
