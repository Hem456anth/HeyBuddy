"""AssemblyAI realtime v3 streaming transcription session.

Port of Clicky's `AssemblyAIStreamingTranscriptionProvider`:

    session = AssemblyAIStreamingSession(proxy, sample_rate=16000)
    session.start()                 # token from the Worker + websocket connect
    session.feed_pcm(chunk_bytes)   # every mic block while the hotkey is held
    transcript = session.stop()     # ForceEndpoint, wait for the final turn

Wire protocol (v3):

* URL `wss://streaming.assemblyai.com/v3/ws?sample_rate=16000&encoding=
  pcm_s16le&format_turns=true&speech_model=...&token=...`
* binary frames: PCM16 LE mono audio (50-1000 ms per frame)
* server JSON: `Begin`, `Turn` (`turn_order`, `transcript`, `end_of_turn`,
  `turn_is_formatted`), `Termination`, `Error`
* client JSON: `{"type":"ForceEndpoint"}` to finish the current turn now,
  `{"type":"Terminate"}` to close the session.

Why turns are stored by `turn_order`: if the user pauses while holding the
hotkey, AssemblyAI ends a turn and starts a new one. The first HeyBuddy port
kept only the *last* turn (and stopped waiting as soon as the *first* turn
ended), so "how do I... [pause] ...rename a file" came out as "rename a
file". We keep every turn, prefer the formatted version of each, and join
them, exactly like Clicky's `composeFullTranscript`.
"""
from __future__ import annotations

import json
import threading
from typing import Callable
from urllib.parse import urlencode

from websocket import WebSocket, WebSocketApp, WebSocketException

from ..utils.constants import DEFAULT_STT_SPEECH_MODEL
from ..utils.logger import get_logger
from .cloudflare_proxy import CloudflareProxy

log = get_logger(__name__)

ASSEMBLYAI_STREAMING_URL = "wss://streaming.assemblyai.com/v3/ws"

PartialCallback = Callable[[str], None]
FinalCallback = Callable[[str], None]
ErrorCallback = Callable[[str], None]


class TurnTranscriptAssembler:
    """Pure bookkeeping for v3 `Turn` messages (unit-tested on its own)."""

    def __init__(self) -> None:
        self._finished_turns: dict[int, tuple[str, bool]] = {}
        self._active_turn_order: int | None = None
        self._active_turn_text: str = ""

    def handle_turn(self, payload: dict) -> bool:
        """Record one Turn message. Returns True if it ended a turn."""
        transcript = (payload.get("transcript") or "").strip()
        is_formatted = bool(payload.get("turn_is_formatted"))
        is_end_of_turn = bool(payload.get("end_of_turn")) or is_formatted
        turn_order = payload.get("turn_order")
        if not isinstance(turn_order, int):
            if self._active_turn_order is not None:
                turn_order = self._active_turn_order
            else:
                turn_order = max(self._finished_turns.keys(), default=-1) + 1

        if is_end_of_turn:
            self._active_turn_order = None
            self._active_turn_text = ""
            if transcript:
                existing = self._finished_turns.get(turn_order)
                # Never let a later unformatted copy replace a formatted one.
                if existing is None or is_formatted or not existing[1]:
                    self._finished_turns[turn_order] = (transcript, is_formatted)
        else:
            self._active_turn_order = turn_order
            self._active_turn_text = transcript
        return is_end_of_turn

    def full_transcript(self) -> str:
        parts = [text for _order, (text, _formatted) in sorted(self._finished_turns.items())]
        if self._active_turn_text:
            parts.append(self._active_turn_text)
        return " ".join(part for part in parts if part).strip()

    @property
    def has_active_turn(self) -> bool:
        return bool(self._active_turn_text)


class AssemblyAIStreamingSession:
    """One push-to-talk session: open, stream, finalize, close."""

    def __init__(
        self,
        proxy: CloudflareProxy,
        sample_rate: int = 16_000,
        speech_model: str = DEFAULT_STT_SPEECH_MODEL,
        on_partial: PartialCallback | None = None,
        on_final: FinalCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self.proxy = proxy
        self.sample_rate = sample_rate
        self.speech_model = speech_model
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_error = on_error

        self._ws: WebSocketApp | None = None
        self._reader_thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected_event = threading.Event()
        self._closed_event = threading.Event()
        # Set when the turn that was in flight at release time has ended.
        self._release_turn_finished_event = threading.Event()
        self._awaiting_release_final = False
        self._assembler = TurnTranscriptAssembler()
        self._stopped = False
        self._error_reported = False

    def build_url(self, token: str) -> str:
        query = urlencode({
            "sample_rate": self.sample_rate,
            "encoding": "pcm_s16le",
            "format_turns": "true",
            "speech_model": self.speech_model,
            "token": token,
        })
        return f"{ASSEMBLYAI_STREAMING_URL}?{query}"

    # ----- lifecycle -----
    def start(self, connect_timeout_seconds: float = 6.0) -> None:
        """Fetch a token and open the websocket. Blocks until connected."""
        token = self.proxy.transcription_token()
        if not token:
            raise RuntimeError("Worker returned an empty AssemblyAI token")
        log.debug("Opening AssemblyAI WS (model=%s, rate=%d)", self.speech_model, self.sample_rate)
        self._ws = WebSocketApp(
            self.build_url(token),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self._reader_thread = threading.Thread(
            target=self._ws.run_forever,
            name="AssemblyAIReader",
            daemon=True,
            kwargs={"ping_interval": 15, "ping_timeout": 5},
        )
        self._reader_thread.start()
        if not self._connected_event.wait(timeout=connect_timeout_seconds):
            self._close_ws()
            raise TimeoutError("AssemblyAI websocket did not connect in time")

    def feed_pcm(self, pcm_chunk: bytes) -> None:
        """Forward raw PCM16 bytes. Called from the audio thread; never blocks long."""
        if self._stopped or self._ws is None:
            return
        try:
            with self._send_lock:
                self._ws.send(pcm_chunk, opcode=2)  # binary frame
        except (WebSocketException, OSError):
            log.exception("WS send failed; transcription will degrade")
            self._stopped = True
            self._emit_error("Transcription connection dropped mid-sentence")

    def stop(self, final_wait_seconds: float = 2.5) -> str:
        """Finish the in-flight turn, wait briefly for it, close, return text."""
        if self._stopped:
            transcript = self._assembler.full_transcript()
            self._close_ws()
            return transcript
        self._stopped = True
        with self._state_lock:
            needs_wait = self._assembler.has_active_turn
            self._awaiting_release_final = needs_wait
        # ForceEndpoint makes AssemblyAI end the current turn immediately
        # (instead of waiting for its silence detector), which is what keeps
        # the release-to-answer latency low.
        self._send_json({"type": "ForceEndpoint"})
        if needs_wait:
            if not self._release_turn_finished_event.wait(timeout=final_wait_seconds):
                log.info("AssemblyAI did not finalize in time; using partial text")
        else:
            # Nothing in flight; give a short grace period for audio that was
            # sent but not yet acknowledged with a first partial.
            self._release_turn_finished_event.wait(timeout=0.35)
        self._send_json({"type": "Terminate"})
        transcript = self._assembler.full_transcript()
        self._close_ws()
        return transcript

    def cancel(self) -> None:
        self._stopped = True
        self._send_json({"type": "Terminate"})
        self._close_ws()

    # ----- WS callbacks (reader thread) -----
    def _on_open(self, _ws: WebSocket) -> None:
        log.info("AssemblyAI WS open")
        self._connected_event.set()

    def _on_message(self, _ws: WebSocket, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            log.debug("Ignoring non-JSON WS message: %r", message[:120])
            return
        message_type = str(payload.get("type") or payload.get("message_type") or "").lower()

        if message_type == "turn":
            with self._state_lock:
                ended_turn = self._assembler.handle_turn(payload)
                transcript_so_far = self._assembler.full_transcript()
                if ended_turn and self._awaiting_release_final:
                    self._release_turn_finished_event.set()
            if transcript_so_far and self._on_partial:
                self._safe_callback(self._on_partial, transcript_so_far)
            if ended_turn and self._on_final:
                self._safe_callback(self._on_final, transcript_so_far)
            return
        if message_type == "termination":
            self._release_turn_finished_event.set()
            return
        if message_type == "error":
            error_text = payload.get("error") or payload.get("message") or "unknown AssemblyAI error"
            log.error("AssemblyAI error: %s", error_text)
            self._emit_error(str(error_text))
            self._release_turn_finished_event.set()

    def _on_ws_error(self, _ws: WebSocket, error: Exception) -> None:
        # Errors after we asked to stop are just the socket closing.
        if self._stopped:
            log.debug("AssemblyAI WS error after stop: %s", error)
        else:
            log.error("AssemblyAI WS error: %s", error)
            self._emit_error(str(error))
        self._release_turn_finished_event.set()

    def _on_ws_close(self, _ws: WebSocket, status_code, reason) -> None:  # type: ignore[no-untyped-def]
        log.info("AssemblyAI WS closed (%s %s)", status_code, reason)
        self._closed_event.set()
        self._release_turn_finished_event.set()
        if not self._stopped and status_code not in (None, 1000):
            self._emit_error(f"Transcription closed ({status_code}: {reason})")

    # ----- helpers -----
    def _send_json(self, message: dict) -> None:
        try:
            with self._send_lock:
                if self._ws is not None and self._ws.sock is not None and self._ws.sock.connected:
                    self._ws.send(json.dumps(message))
        except Exception:
            log.debug("Could not send %s (socket already closed?)", message.get("type"))

    def _close_ws(self) -> None:
        websocket_app = self._ws
        self._ws = None
        if websocket_app is not None:
            try:
                websocket_app.close()
            except Exception:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._reader_thread = None

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
            log.exception("AssemblyAI session callback raised")
