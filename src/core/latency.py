"""Per-turn stage timings (HeyBuddy 0.4), logged without any content.

One line per turn goes to `logs/latency.log` (and the main log):

    turn=7 mode=telugu release->stt_final=612ms stt_final->first_token=1430ms
    first_token->first_audio=820ms release->first_audio=2862ms

Stages: `release` (hotkey up), `stt_final` (transcript ready), `request`
(Claude request sent), `first_token` (first streamed text), `first_chunk`
(first TTS audio bytes received), `first_audio` (playback started). Only
durations are recorded: never the transcript, the reply or audio.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

STAGES = ("release", "stt_final", "request", "first_token", "first_chunk", "first_audio")
REPORTED_SPANS = (
    ("release", "stt_final"),
    ("stt_final", "first_token"),
    ("request", "first_token"),
    ("first_token", "first_chunk"),
    ("first_token", "first_audio"),
    ("release", "first_audio"),
    ("request", "first_audio"),
)


class TurnLatency:
    def __init__(self, turn: int, mode: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.turn = turn
        self.mode = mode
        self._clock = clock
        self._marks: dict[str, float] = {}
        self._lock = threading.Lock()
        self._reported = False

    def mark(self, stage: str) -> None:
        """Record the first time a stage is reached (later calls are ignored)."""
        if stage not in STAGES:
            raise ValueError(f"unknown latency stage {stage!r}")
        with self._lock:
            if stage not in self._marks:
                self._marks[stage] = self._clock()

    def has(self, stage: str) -> bool:
        return stage in self._marks

    def spans_ms(self) -> dict[str, int]:
        with self._lock:
            marks = dict(self._marks)
        spans: dict[str, int] = {}
        for start, end in REPORTED_SPANS:
            if start in marks and end in marks:
                spans[f"{start}->{end}"] = int(round((marks[end] - marks[start]) * 1000))
        return spans

    def format_line(self) -> str:
        spans = " ".join(f"{name}={value}ms" for name, value in self.spans_ms().items())
        return f"turn={self.turn} mode={self.mode} {spans}".strip()

    def report(self, logger: logging.Logger) -> bool:
        """Log once per turn. Returns False if already reported."""
        with self._lock:
            if self._reported:
                return False
            self._reported = True
        logger.info(self.format_line())
        return True
