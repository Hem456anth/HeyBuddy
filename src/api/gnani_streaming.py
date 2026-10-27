"""Gnani Vachana realtime STT (Prisma v2.5) through the Worker — Telugu mode.

Same surface as `AssemblyAIStreamingSession`, so `CompanionManager` can use
either one:

    session = GnaniStreamingSession(proxy, lang_code="te-IN")
    session.start()                 # WebSocket to the Worker's /gnani/stt-stream
    session.feed_pcm(chunk_bytes)   # every mic block while the hotkey is held
    transcript = session.stop()     # pad with silence, wait for the last segment

Wire protocol (docs.gnani.ai "Speech-to-Text (Realtime)", read 2026-10-27):

* upstream `wss://api.vachana.ai/stt/v3/stream`; configuration is sent as
  upgrade headers. The Worker adds `x-api-key-id` and maps our
  `x-heybuddy-lang` header to Gnani's `lang_code` (one language per
  session); `x-sample-rate` and the VAD headers pass through.
* client -> server: binary frames of raw PCM16 LE mono, **exactly 1,024
  bytes** (512 samples), at real-time cadence (32 ms per frame at 16 kHz),
  "do not buffer and burst".
* server -> client JSON: `connected`, `processing` (VAD closed a segment),
  `transcript` (`text`, `segment_index`, `latency`, ...), `error`.

There is no documented "flush now" control frame (AssemblyAI has
`ForceEndpoint`), and segments are only closed by VAD silence. So on release
we append `min_silence_ms + 200 ms` of silent frames, then wait until every
`processing` has its `transcript`. Every segment is kept and joined in
`segment_index` order, so a pause mid-question never loses the first half.

Pacing: mic blocks (100 ms) are re-cut into 1,024-byte frames and a sender
thread sends one frame per 32 ms. Audio captured before the socket opened
(the mic starts first) and the trailing silence are a backlog; while more
than two frames are queued the sender runs at 2x real time to catch up, so
the backlog does not add its full length to the reply latency.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable

from websocket import WebSocket, WebSocketApp, WebSocketException

from ..utils.constants import GNANI_STT_FRAME_BYTES, WORKER_GNANI_STT_PATH
from .transcription_provider import pcm16_to_wav
from ..utils.logger import get_logger
from .cloudflare_proxy import CloudflareProxy

log = get_logger(__name__)

PartialCallback = Callable[[str], None]
FinalCallback = Callable[[str], None]
ErrorCallback = Callable[[str], None]

TRAILING_SILENCE_EXTRA_MS = 200
CATCH_UP_BACKLOG_FRAMES = 2
CATCH_UP_SPEED = 2.0
# How long after the last frame we wait for a `processing` that never came
# (i.e. the tail was silence and VAD has nothing to close).
NO_SEGMENT_GRACE_SECONDS = 1.0


class GnaniSegmentAssembler:
    """Pure bookkeeping for `processing` / `transcript` events (unit-tested)."""

    def __init__(self) -> None:
        self._segments: dict[int, str] = {}
        self.processing_count = 0
        self.transcript_count = 0

    def handle(self, payload: dict) -> str:
        """Record one server message; return its lower-case type."""
        message_type = str(payload.get("type") or "").lower()
        if message_type == "processing":
            self.processing_count += 1
        elif message_type == "transcript":
            self.transcript_count += 1
            text = (payload.get("text") or "").strip()
            index = payload.get("segment_index")
            if not isinstance(index, int):
                index = max(self._segments.keys(), default=-1) + 1
            if text:
                self._segments[index] = text
        return message_type

    @property
    def segments_pending(self) -> bool:
        return self.processing_count > self.transcript_count

    def full_transcript(self) -> str:
        return " ".join(text for _index, text in sorted(self._segments.items())).strip()


REST_MAX_SECONDS = 55  # Gnani REST accepts <= 60 s; keep the newest 55 s.


def transcribe_with_rest(proxy: CloudflareProxy, pcm16: bytes, sample_rate: int, lang_code: str) -> str:
    """Fallback when a realtime session ends with no transcript although the
    mic heard something: send the whole recording to Gnani's REST STT."""
    limit = REST_MAX_SECONDS * sample_rate * 2
    clip = pcm16[-limit:] if len(pcm16) > limit else pcm16
    return proxy.gnani_stt_rest(pcm16_to_wav(clip, sample_rate), lang_code)


class GnaniStreamingSession:
    """One push-to-talk session against Gnani realtime STT."""

    LANGUAGE_HEADER = "x-heybuddy-lang"

    def __init__(
        self,
        proxy: CloudflareProxy,
        lang_code: str = "te-IN",
        sample_rate: int = 16_000,
        min_silence_ms: int = 400,
        on_partial: PartialCallback | None = None,
        on_final: FinalCallback | None = None,
        on_error: ErrorCallback | None = None,
        frame_interval_seconds: float | None = None,
    ) -> None:
        self.proxy = proxy
        self.lang_code = lang_code
        self.sample_rate = sample_rate
        self.min_silence_ms = int(min_silence_ms)
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_error = on_error
        samples_per_frame = GNANI_STT_FRAME_BYTES // 2
        self.frame_interval_seconds = (
            frame_interval_seconds if frame_interval_seconds is not None
            else samples_per_frame / float(sample_rate)
        )

        self._ws: WebSocketApp | None = None
        self._reader_thread: threading.Thread | None = None
        self._sender_thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._state = threading.Condition()
        self._connected_event = threading.Event()
        self._closed_event = threading.Event()
        self._frames: "queue.Queue[bytes]" = queue.Queue()
        self._pending_bytes = bytearray()
        self._last_frame_sent_at = 0.0
        self._assembler = GnaniSegmentAssembler()
        self._accepting_audio = True
        self._stopped = False
        self._error_reported = False

    # ----- request shape (unit-tested) -----
    def build_url(self) -> str:
        return self.proxy.websocket_url(WORKER_GNANI_STT_PATH)

    def build_headers(self) -> list[str]:
        headers = [
            f"{self.LANGUAGE_HEADER}: {self.lang_code}",
            f"x-sample-rate: {self.sample_rate}",
            f"x-min-silence-ms: {self.min_silence_ms}",
        ]
        secret = self.proxy.shared_secret
        if secret:
            headers.append(f"{CloudflareProxy.SHARED_SECRET_HEADER}: {secret}")
        return headers

    # ----- lifecycle -----
    def start(self, connect_timeout_seconds: float = 6.0) -> None:
        url = self.build_url()
        log.debug("Opening Gnani STT WS (lang=%s, rate=%d)", self.lang_code, self.sample_rate)
        self._ws = WebSocketApp(
            url,
            header=self.build_headers(),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self._reader_thread = threading.Thread(
            target=self._ws.run_forever, name="GnaniSttReader", daemon=True,
            kwargs={"ping_interval": 15, "ping_timeout": 5},
        )
        self._reader_thread.start()
        if not self._connected_event.wait(timeout=connect_timeout_seconds):
            self._close_ws()
            raise TimeoutError("Gnani STT websocket did not connect in time")
        if self._closed_event.is_set():
            self._close_ws()
            raise RuntimeError("Gnani STT websocket closed right after opening")
        self._sender_thread = threading.Thread(target=self._send_loop, name="GnaniSttSender", daemon=True)
        self._sender_thread.start()

    def feed_pcm(self, pcm_chunk: bytes) -> None:
        """Re-cut mic blocks into exact 1,024-byte frames for the sender."""
        if not self._accepting_audio or self._stopped:
            return
        with self._state:
            self._pending_bytes.extend(pcm_chunk)
            while len(self._pending_bytes) >= GNANI_STT_FRAME_BYTES:
                frame = bytes(self._pending_bytes[:GNANI_STT_FRAME_BYTES])
                del self._pending_bytes[:GNANI_STT_FRAME_BYTES]
                self._frames.put(frame)

    def stop(self, final_wait_seconds: float = 3.0) -> str:
        """Pad with silence so VAD closes the last segment, wait for it, close."""
        if self._stopped:
            transcript = self._assembler.full_transcript()
            self._close_ws()
            return transcript
        self._accepting_audio = False
        with self._state:
            if self._pending_bytes:
                padding = GNANI_STT_FRAME_BYTES - len(self._pending_bytes)
                self._frames.put(bytes(self._pending_bytes) + b"\x00" * padding)
                self._pending_bytes.clear()
        silence_ms = self.min_silence_ms + TRAILING_SILENCE_EXTRA_MS
        frame_ms = 1000.0 * (GNANI_STT_FRAME_BYTES // 2) / self.sample_rate
        for _ in range(max(1, int(round(silence_ms / frame_ms)))):
            self._frames.put(b"\x00" * GNANI_STT_FRAME_BYTES)

        deadline = time.monotonic() + final_wait_seconds
        # 1. let the sender drain the queue (backlog + silence).
        while not self._frames.empty() and time.monotonic() < deadline:
            if self._closed_event.is_set() or self._sender_thread is None:
                break
            time.sleep(0.01)
        drained_at = time.monotonic()
        # 2. wait for every segment VAD opened to come back as a transcript.
        with self._state:
            while time.monotonic() < deadline and not self._closed_event.is_set():
                if self._assembler.segments_pending:
                    self._state.wait(timeout=0.05)
                    continue
                if time.monotonic() - drained_at >= NO_SEGMENT_GRACE_SECONDS:
                    break
                self._state.wait(timeout=0.05)
            if self._assembler.segments_pending:
                log.info("Gnani STT: a segment was still processing at the deadline")
        self._stopped = True
        transcript = self._assembler.full_transcript()
        # websocket-client's close handshake can wait up to its 3 s timeout;
        # the transcript is final, so don't make the reply wait for it.
        self._close_ws_in_background()
        return transcript

    def cancel(self) -> None:
        self._accepting_audio = False
        self._stopped = True
        self._close_ws()

    # ----- sender thread -----
    def _send_loop(self) -> None:
        while not self._stopped and not self._closed_event.is_set():
            try:
                frame = self._frames.get(timeout=0.05)
            except queue.Empty:
                continue
            interval = self.frame_interval_seconds
            if self._frames.qsize() > CATCH_UP_BACKLOG_FRAMES:
                interval /= CATCH_UP_SPEED
            wait = self._last_frame_sent_at + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            if not self._send_binary(frame):
                return
            self._last_frame_sent_at = time.monotonic()

    def _send_binary(self, frame: bytes) -> bool:
        websocket_app = self._ws
        if websocket_app is None:
            return False
        try:
            with self._send_lock:
                websocket_app.send(frame, opcode=2)
            return True
        except (WebSocketException, OSError, AttributeError):
            if not self._stopped:
                log.exception("Gnani STT send failed")
                self._emit_error("Transcription connection dropped mid-sentence")
            return False

    # ----- WS callbacks (reader thread) -----
    def _on_open(self, _ws: WebSocket) -> None:
        log.info("Gnani STT WS open")
        self._connected_event.set()

    def _on_message(self, _ws: WebSocket, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            log.debug("Ignoring non-JSON Gnani message (%d chars)", len(message))
            return
        if not isinstance(payload, dict):
            return
        with self._state:
            message_type = self._assembler.handle(payload)
            transcript_so_far = self._assembler.full_transcript()
            self._state.notify_all()
        if message_type == "connected":
            log.debug("Gnani STT ready (config=%s)", payload.get("config"))
        elif message_type == "transcript":
            log.debug("Gnani STT segment %s (latency %s ms)", payload.get("segment_index"), payload.get("latency"))
            if transcript_so_far and self._on_partial:
                self._safe_callback(self._on_partial, transcript_so_far)
            if self._on_final:
                self._safe_callback(self._on_final, transcript_so_far)
        elif message_type == "error":
            error_text = str(payload.get("message") or "unknown Gnani STT error")
            log.error("Gnani STT error: %s", error_text)
            self._emit_error(error_text)

    def _on_ws_error(self, _ws: WebSocket, error: Exception) -> None:
        if self._stopped:
            log.debug("Gnani STT WS error after stop: %s", error)
        else:
            log.error("Gnani STT WS error: %s", error)
            self._emit_error(str(error))
        self._mark_closed()

    def _on_ws_close(self, _ws: WebSocket, status_code, reason) -> None:  # type: ignore[no-untyped-def]
        log.info("Gnani STT WS closed (%s %s)", status_code, reason)
        self._mark_closed()
        if not self._stopped and status_code not in (None, 1000):
            self._emit_error(f"Transcription closed ({status_code}: {reason})")

    # ----- helpers -----
    def _mark_closed(self) -> None:
        self._closed_event.set()
        with self._state:
            self._state.notify_all()

    def _close_ws_in_background(self) -> None:
        self._stopped = True
        threading.Thread(target=self._close_ws, name="GnaniSttClose", daemon=True).start()

    def _close_ws(self) -> None:
        self._stopped = True
        websocket_app = self._ws
        self._ws = None
        if websocket_app is not None:
            try:
                websocket_app.close(timeout=1.0)
            except Exception:
                pass
        for thread in (self._reader_thread, self._sender_thread):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._reader_thread = None
        self._sender_thread = None

    def _emit_error(self, message: str) -> None:
        if self._error_reported:
            return
        self._error_reported = True
        if self._on_error:
            self._safe_callback(self._on_error, message)

    @staticmethod
    def _safe_callback(callback, *args) -> None:  # type: ignore[no-untyped-def]
        try:
            callback(*args)
        except Exception:
            log.exception("Gnani STT callback raised")
