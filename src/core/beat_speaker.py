"""Speaks beats one after another and tells the overlay when each starts.

HeyClicky voices every `say()` line as its own utterance and reveals the
marks that line owns while it is spoken. `BeatSpeaker` does the same on
top of HeyBuddy's TTS (ElevenLabs through the Worker):

* beats can be appended while earlier ones play (a "let me check" line
  from the first tool round, then the answer);
* the next line is synthesized while the current one plays, so there is
  no gap between lines;
* with voice off (or after a TTS failure) beats are paced at about 2.6
  words per second, the reading speed HeyClicky's pen uses, so marks
  still appear in step with the text.

Telugu mode (0.4) passes `stream_synthesize` + `play_pcm_stream` instead
of `synthesize` + `play_mp3`: each line's audio is fetched as a stream of
PCM chunks (Gnani SSE) into a `PcmStream` buffer, and playback starts on
the first chunk while the rest is still arriving. The next line's stream
is opened while the current one plays, as with MP3 prefetch.
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Iterable, Iterator

from ..utils.logger import get_logger
from .scene_program import Beat

log = get_logger(__name__)

SILENT_WORDS_PER_SECOND = 2.6
SILENT_MIN_SECONDS_WITH_MARKS = 1.2
_CLOSE = object()
STREAM_FIRST_CHUNK_TIMEOUT_SECONDS = 30.0


class PcmStream:
    """Thread-safe buffer between a TTS producer and the audio player."""

    def __init__(self) -> None:
        self._chunks: "queue.Queue[bytes | None]" = queue.Queue()
        self.first_chunk_or_end = threading.Event()
        self.finished = threading.Event()
        self.error: BaseException | None = None
        self.bytes_received = 0

    def put(self, chunk: bytes) -> None:
        if chunk:
            self.bytes_received += len(chunk)
            self._chunks.put(chunk)
            self.first_chunk_or_end.set()

    def finish(self, error: BaseException | None = None) -> None:
        self.error = error
        self.finished.set()
        self._chunks.put(None)
        self.first_chunk_or_end.set()

    def iter_chunks(self, cancelled: threading.Event) -> Iterator[bytes]:
        while True:
            try:
                item = self._chunks.get(timeout=0.1)
            except queue.Empty:
                if cancelled.is_set():
                    return
                continue
            if item is None or cancelled.is_set():
                return
            yield item


class BeatSpeaker:
    def __init__(
        self,
        synthesize: Callable[[str], bytes] | None,
        play_mp3: Callable[..., None] | None,
        on_beat_started: Callable[[Beat], None],
        on_audio_started: Callable[[], None],
        on_all_done: Callable[[], None],
        on_error: Callable[[str], None],
        silent_pacing: bool = True,
        stream_synthesize: Callable[[str], Iterable[bytes]] | None = None,
        play_pcm_stream: Callable[..., None] | None = None,
        on_first_chunk: Callable[[], None] | None = None,
    ) -> None:
        self._synthesize = synthesize
        self._play_mp3 = play_mp3
        self._stream_synthesize = stream_synthesize
        self._play_pcm_stream = play_pcm_stream
        self._streaming = stream_synthesize is not None and play_pcm_stream is not None
        self._on_first_chunk = on_first_chunk
        self._on_beat_started = on_beat_started
        self._on_audio_started = on_audio_started
        self._on_all_done = on_all_done
        self._on_error = on_error
        self._silent_pacing = silent_pacing
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._cancelled = threading.Event()
        self._voice_failed = not self._streaming and (synthesize is None or play_mp3 is None)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="TTS")
        self._audio_announced = False
        self._thread = threading.Thread(target=self._run, name="BeatSpeaker", daemon=True)
        self._thread.start()

    def add(self, beat: Beat) -> None:
        if not self._cancelled.is_set():
            future = None
            if not self._voice_failed and beat.text.strip():
                if self._streaming:
                    stream = PcmStream()
                    self._executor.submit(self._produce_stream, beat.text, stream)
                    future = stream
                else:
                    future = self._executor.submit(self._synthesize, beat.text)  # type: ignore[arg-type]
            self._queue.put((beat, future))

    def _produce_stream(self, text: str, stream: PcmStream) -> None:
        try:
            for chunk in self._stream_synthesize(text):  # type: ignore[misc]
                if self._cancelled.is_set():
                    break
                stream.put(chunk)
            stream.finish()
        except BaseException as error:  # handed to the speaking thread
            stream.finish(error)

    def close(self) -> None:
        self._queue.put(_CLOSE)

    def cancel(self) -> None:
        self._cancelled.set()
        self._queue.put(_CLOSE)
        self._executor.shutdown(wait=False, cancel_futures=True)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _CLOSE or self._cancelled.is_set():
                    break
                beat, future = item  # type: ignore[misc]
                self._speak_one(beat, future)
        except Exception as error:  # never leave the turn hanging
            log.exception("BeatSpeaker crashed")
            self._on_error(f"Speech failed: {error}")
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            if not self._cancelled.is_set():
                self._on_all_done()

    def _speak_one(self, beat: Beat, future: "Future[bytes] | PcmStream | None") -> None:
        if isinstance(future, PcmStream):
            if self._speak_streamed(beat, future):
                return
            future = None
        mp3_bytes = b""
        if future is not None and not self._voice_failed:
            try:
                mp3_bytes = future.result(timeout=60)
            except Exception as error:
                log.exception("TTS synthesis failed")
                self._voice_failed = True
                self._on_error(f"Voice unavailable: {error}")
        if self._cancelled.is_set():
            return
        if mp3_bytes and self._play_mp3 is not None:
            finished = threading.Event()
            beat_shown = threading.Event()

            def started() -> None:
                beat_shown.set()
                if not self._audio_announced:
                    self._audio_announced = True
                    self._on_audio_started()
                self._on_beat_started(beat)

            self._play_mp3(mp3_bytes, on_started=started, on_finished=finished.set)
            while not finished.wait(0.1):
                if self._cancelled.is_set():
                    return
            if not beat_shown.is_set():  # decode failed: still show the marks
                self._on_beat_started(beat)
            return
        self._speak_silently(beat)

    def _speak_streamed(self, beat: Beat, stream: PcmStream) -> bool:
        """Play a streamed line. False = no audio, caller paces it silently."""
        deadline = time.monotonic() + STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
        while not stream.first_chunk_or_end.wait(0.05):
            if self._cancelled.is_set():
                return True
            if time.monotonic() > deadline:
                break
        if self._cancelled.is_set():
            return True
        if stream.bytes_received == 0:
            if stream.error is not None or not stream.finished.is_set():
                reason = stream.error or "timed out"
                log.error("Streaming TTS produced no audio: %s", reason)
                self._voice_failed = True
                self._streaming = False
                self._on_error(f"Voice unavailable: {reason}")
            return False
        if self._on_first_chunk is not None:
            try:
                self._on_first_chunk()
            except Exception:
                log.exception("on_first_chunk callback raised")
            self._on_first_chunk = None
        finished = threading.Event()
        beat_shown = threading.Event()

        def started() -> None:
            beat_shown.set()
            if not self._audio_announced:
                self._audio_announced = True
                self._on_audio_started()
            self._on_beat_started(beat)

        self._play_pcm_stream(  # type: ignore[misc]
            stream.iter_chunks(self._cancelled), on_started=started, on_finished=finished.set,
        )
        while not finished.wait(0.1):
            if self._cancelled.is_set():
                return True
        if not beat_shown.is_set():
            self._on_beat_started(beat)
        if stream.error is not None:
            log.warning("Streaming TTS ended early: %s", stream.error)
        return True

    def _speak_silently(self, beat: Beat) -> None:
        # Silent beat: reveal marks now, then hold for a reading-speed pause.
        if not self._audio_announced:
            self._audio_announced = True
            self._on_audio_started()
        self._on_beat_started(beat)
        if not self._silent_pacing:
            return
        words = len(beat.text.split())
        hold = words / SILENT_WORDS_PER_SECOND
        if beat.marks:
            hold = max(hold, SILENT_MIN_SECONDS_WITH_MARKS)
        deadline = time.monotonic() + hold
        while time.monotonic() < deadline:
            if self._cancelled.wait(0.05):
                return
