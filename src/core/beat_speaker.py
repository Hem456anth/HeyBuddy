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
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from ..utils.logger import get_logger
from .scene_program import Beat

log = get_logger(__name__)

SILENT_WORDS_PER_SECOND = 2.6
SILENT_MIN_SECONDS_WITH_MARKS = 1.2
_CLOSE = object()


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
    ) -> None:
        self._synthesize = synthesize
        self._play_mp3 = play_mp3
        self._on_beat_started = on_beat_started
        self._on_audio_started = on_audio_started
        self._on_all_done = on_all_done
        self._on_error = on_error
        self._silent_pacing = silent_pacing
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._cancelled = threading.Event()
        self._voice_failed = synthesize is None or play_mp3 is None
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="TTS")
        self._audio_announced = False
        self._thread = threading.Thread(target=self._run, name="BeatSpeaker", daemon=True)
        self._thread.start()

    def add(self, beat: Beat) -> None:
        if not self._cancelled.is_set():
            future = None
            if not self._voice_failed and beat.text.strip():
                future = self._executor.submit(self._synthesize, beat.text)  # type: ignore[arg-type]
            self._queue.put((beat, future))

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

    def _speak_one(self, beat: Beat, future: "Future[bytes] | None") -> None:
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
