"""AssemblyAI realtime v3 streaming transcription session.

Lifecycle:

    session = AssemblyAIStreamingSession(proxy, sample_rate=16000)
    session.start()                                  # token fetch + WS connect
    session.feed_pcm(chunk_bytes)                    # called for every mic block
    transcript = session.stop(timeout=2.0)           # graceful flush + final text

Why a dedicated module:

* The `transcription_provider.py` interface stays small (batch-style) for
  testing and for the OpenAI/Apple fallbacks the upstream Mac app supports.
  Realtime streaming has a totally different lifecycle and deserves its own
  surface area.
* The websocket connection runs on a dedicated reader thread so PCM can be
  fed in from the audio thread without blocking, and the Qt thread stays
  free to update the panel as partial transcripts arrive.

Wire protocol (AssemblyAI v3 streaming):

* WS URL: `wss://streaming.assemblyai.com/v3/ws?sample_rate=<N>&token=<T>`
* Binary frames = raw PCM16 little-endian audio at the agreed sample rate.
* Text frames = JSON messages from the server, e.g.:
    {"type": "Begin", "id": "..."}
    {"type": "Turn", "transcript": "...", "end_of_turn": false, "turn_is_formatted": false}
    {"type": "Turn", "transcript": "...", "end_of_turn": true,  "turn_is_formatted": true}
    {"type": "Termination", "audio_duration_seconds": ...}
* To force a final turn on hotkey release, we send
  `{"type": "Terminate"}` and wait briefly for the formatted final.

If AssemblyAI changes the JSON shapes, this is the only file that needs
updating — `CompanionManager` only sees callbacks.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable
from urllib.parse import urlencode

from websocket import WebSocket, WebSocketApp, WebSocketException

from ..utils.logger import get_logger
from .cloudflare_proxy import CloudflareProxy

log = get_logger(__name__)

ASSEMBLYAI_STREAMING_URL = "wss://streaming.assemblyai.com/v3/ws"

# Type aliases for callback registration
PartialCallback = Callable[[str], None]
FinalCallback = Callable[[str], None]
ErrorCallback = Callable[[str], None]


class AssemblyAIStreamingSession:
    """One push-to-talk session: open, stream, finalize, close."""

    def __init__(
        self,
        proxy: CloudflareProxy,
        sample_rate: int = 16_000,
        on_partial: PartialCallback | None = None,
        on_final: FinalCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.proxy = proxy
        self.sample_rate = sample_rate
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_error = on_error

        self._ws: WebSocketApp | None = None
        self._reader_thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._connected_event = threading.Event()
        self._final_event = threading.Event()
        self._final_transcript: str = ""
        # Buffer of partial transcripts so we can fall back to the latest
        # in-flight partial if the server times out before sending a final.
        self._last_partial: str = ""
        self._stopped = False

    # ----- lifecycle -----
    def start(self, connect_timeout_seconds: float = 5.0) -> None:
        """Fetch a token and open the websocket. Blocks until WS is connected."""
        token = self.proxy.transcription_token()
        if not token:
            raise RuntimeError("Worker returned an empty AssemblyAI token")

        query = urlencode({"sample_rate": self.sample_rate, "token": token})
        url = f"{ASSEMBLYAI_STREAMING_URL}?{query}"
        log.debug("Opening AssemblyAI streaming WS (sample_rate=%d)", self.sample_rate)

        self._ws = WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        # `run_forever` blocks; run it on a dedicated reader thread.
        self._reader_thread = threading.Thread(
            target=self._ws.run_forever,
            name="AssemblyAIReader",
            daemon=True,
            # ping_interval keeps the connection alive through proxies that
            # close idle sockets after ~30s.
            kwargs={"ping_interval": 15, "ping_timeout": 5},
        )
        self._reader_thread.start()

        if not self._connected_event.wait(timeout=connect_timeout_seconds):
            raise TimeoutError("AssemblyAI websocket did not connect in time")

    def feed_pcm(self, pcm_chunk: bytes) -> None:
        """Forward raw PCM16 mono bytes to the server.

        Called from the audio thread. Must be cheap. We grab the lock just
        long enough to call `send` — `websocket-client` is not thread-safe
        across concurrent sends.
        """
        if self._stopped or self._ws is None:
            return
        try:
            with self._send_lock:
                self._ws.send(pcm_chunk, opcode=2)  # 2 == OPCODE_BINARY
        except WebSocketException:
            # The WS is gone; signal the manager via the error callback. We
            # intentionally don't raise — losing one chunk is recoverable, and
            # raising would kill the audio callback.
            log.exception("WS send failed; transcription will degrade")
            self._stopped = True
            self._emit_error("Transcription websocket disconnected mid-stream")

    def stop(self, final_wait_seconds: float = 2.0) -> str:
        """Ask the server for a final turn and wait briefly for it."""
        if self._stopped:
            return self._final_transcript or self._last_partial
        self._stopped = True

        # Politely ask the server to flush the in-flight turn and terminate.
        try:
            with self._send_lock:
                if self._ws is not None:
                    self._ws.send(json.dumps({"type": "Terminate"}))
        except Exception:
            log.debug("Terminate message send failed (already closed?)")

        # Wait for the server to send the final Turn (with end_of_turn=true)
        # or the connection to close. Fall back to the last partial on timeout
        # so the user's words aren't lost just because the server was slow.
        finalized = self._final_event.wait(timeout=final_wait_seconds)
        if not finalized:
            log.info("AssemblyAI did not finalize in time; using last partial")

        self._close_ws()
        return self._final_transcript or self._last_partial

    # ----- WS callbacks (reader thread) -----
    def _on_open(self, _ws: WebSocket) -> None:
        log.info("AssemblyAI WS open")
        self._connected_event.set()

    def _on_message(self, _ws: WebSocket, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            log.debug("Ignoring non-JSON WS message: %r", message[:120])
            return
        msg_type = payload.get("type") or payload.get("message_type")

        if msg_type == "Begin":
            return
        if msg_type == "Turn":
            transcript = (payload.get("transcript") or "").strip()
            is_end = bool(payload.get("end_of_turn"))
            if is_end:
                self._final_transcript = transcript
                self._final_event.set()
                if self._on_final:
                    self._safe_callback(self._on_final, transcript)
            else:
                self._last_partial = transcript
                if self._on_partial:
                    self._safe_callback(self._on_partial, transcript)
            return
        if msg_type == "Termination":
            self._final_event.set()
            return
        if msg_type == "Error":
            err_msg = payload.get("error") or "unknown AssemblyAI error"
            log.error("AssemblyAI error: %s", err_msg)
            self._emit_error(str(err_msg))
            self._final_event.set()
            return

    def _on_ws_error(self, _ws: WebSocket, error: Exception) -> None:
        log.error("AssemblyAI WS error: %s", error)
        self._emit_error(str(error))
        self._final_event.set()

    def _on_ws_close(self, _ws: WebSocket, status_code, reason) -> None:  # type: ignore[no-untyped-def]
        log.info("AssemblyAI WS closed (%s %s)", status_code, reason)
        self._final_event.set()

    # ----- helpers -----
    def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._reader_thread = None

    def _emit_error(self, message: str) -> None:
        if self._on_error:
            self._safe_callback(self._on_error, message)

    @staticmethod
    def _safe_callback(callback, *args) -> None:  # type: ignore[no-untyped-def]
        try:
            callback(*args)
        except Exception:
            log.exception("AssemblyAI session callback raised")
