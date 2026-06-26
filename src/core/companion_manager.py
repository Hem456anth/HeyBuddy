"""Central state machine that orchestrates the push-to-talk loop.

States:

    IDLE -> LISTENING -> PROCESSING -> RESPONDING -> IDLE

Phase 2 wiring (this file's responsibility):

* On hotkey press → transition to LISTENING. Open an AssemblyAI streaming
  session (token from /transcribe-token, websocket to streaming.assemblyai.com)
  and start the recorder. The recorder pushes each PCM block into the
  websocket via `set_chunk_listener`.
* On hotkey release → transition to PROCESSING. Stop the recorder, ask the
  AssemblyAI session to flush a final turn, capture a screenshot, then stream
  Claude's reply via SSE (parsing POINT tags out of the accumulated text).
* Transition to RESPONDING. Synthesize TTS through the Worker, play it back,
  and queue the parsed POINT markers into the cursor overlay (one bezier
  flight per marker, sequentially).
* When playback finishes → IDLE.

Threading: every network call lives on a one-off `TurnPipeline` thread so the
Qt event loop never blocks. UI updates flow back through `pyqtSignal`s.
"""
from __future__ import annotations

import threading
import time
import wave
from enum import Enum
from pathlib import Path
from typing import Iterable

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from ..api.assemblyai_streaming import AssemblyAIStreamingSession
from ..api.claude_client import ClaudeClient
from ..api.cloudflare_proxy import CloudflareProxy
from ..api.elevenlabs_client import ElevenLabsClient
from ..models.config import AppConfig
from ..models.message import Message, PointMarker, Role
from ..utils.constants import PROJECT_ROOT
from ..utils.logger import get_logger
from .audio_player import AudioPlayer
from .audio_recorder import AudioRecorder
from .screen_capture import ScreenCapture

log = get_logger(__name__)


class CompanionState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"
    ERROR = "error"


class CompanionManager(QObject):
    """Owns the state machine + all the moving pieces."""

    # ---- Qt signals (UI listens to these) ----
    state_changed = pyqtSignal(object)            # CompanionState
    message_appended = pyqtSignal(object)         # Message
    assistant_partial = pyqtSignal(str)           # text delta during streaming
    transcription_partial = pyqtSignal(str)       # partial STT text
    transcription_final = pyqtSignal(str)         # final STT text
    point_received = pyqtSignal(object)           # PointMarker
    error_occurred = pyqtSignal(str)
    # Per-block mic RMS (0.0..1.0) while LISTENING. Emitted from the audio
    # thread; Qt's queued-connection mechanism marshals it to the UI thread
    # for the waveform meter widget. No signal is emitted outside LISTENING.
    audio_level_changed = pyqtSignal(float)

    # Recordings dropped here so operators can sanity-check Phase 1+2 audio.
    RECORDINGS_DIR = PROJECT_ROOT / "recordings"

    def __init__(
        self,
        config: AppConfig,
        recorder: AudioRecorder,
        screen: ScreenCapture,
        proxy: CloudflareProxy,
        claude: ClaudeClient,
        tts: ElevenLabsClient,
        player: AudioPlayer,
    ) -> None:
        super().__init__()
        self.config = config
        self.recorder = recorder
        self.screen = screen
        self.proxy = proxy
        self.claude = claude
        self.tts = tts
        self.player = player

        self._state = CompanionState.IDLE
        self._history: list[Message] = []
        self._state_lock = threading.Lock()

        # Held only while a turn is in flight (LISTENING through RESPONDING).
        self._active_stt_session: AssemblyAIStreamingSession | None = None
        # Accumulator for transcription_partial updates so the panel can
        # show "...what the user said so far".
        self._latest_partial_transcript: str = ""

    # ---- public API ----
    @property
    def state(self) -> CompanionState:
        return self._state

    @property
    def history(self) -> list[Message]:
        return list(self._history)

    def begin_listening(self) -> None:
        """Hotkey press handler. Cheap; safe to call from the hook thread."""
        with self._state_lock:
            if self._state != CompanionState.IDLE:
                log.debug("begin_listening ignored in state %s", self._state.value)
                return
            self._set_state_locked(CompanionState.LISTENING)
        # The websocket connect and first PCM block can both take a moment;
        # move both off the hook thread so the OS doesn't think the hook is
        # hung (the keyboard hook has a hard time limit).
        threading.Thread(
            target=self._open_stt_and_start_recording,
            name="StartCapture",
            daemon=True,
        ).start()

    def end_listening_and_process(self) -> None:
        """Hotkey release handler. Runs the post-capture pipeline off the UI thread."""
        with self._state_lock:
            if self._state != CompanionState.LISTENING:
                # Release without matching press; cancel any stray recording.
                self.recorder.cancel()
                return
            self._set_state_locked(CompanionState.PROCESSING)
        threading.Thread(
            target=self._process_turn,
            name="TurnPipeline",
            daemon=True,
        ).start()

    @pyqtSlot()
    def cancel(self) -> None:
        """Abort whatever is in flight and return to IDLE."""
        try:
            self.recorder.cancel()
            self.player.stop()
            stt = self._active_stt_session
            self._active_stt_session = None
            if stt is not None:
                try:
                    stt.stop(final_wait_seconds=0.1)
                except Exception:
                    log.exception("Error stopping STT session during cancel")
        finally:
            with self._state_lock:
                self._set_state_locked(CompanionState.IDLE)

    def send_text(self, text: str, include_screenshot: bool | None = None) -> None:
        """Send a typed turn directly to Claude (bypasses voice).

        Useful for testing without speaking and for the panel's text box. Runs
        the same PROCESSING → RESPONDING transitions as the voice path.
        """
        if include_screenshot is None:
            include_screenshot = self.config.screen_capture_enabled
        threading.Thread(
            target=self._send_text_turn,
            args=(text, include_screenshot),
            name="TypedTurn",
            daemon=True,
        ).start()

    # ---- capture path ----
    def _open_stt_and_start_recording(self) -> None:
        """Start the STT session, then the recorder.

        Order matters: if the WS fails we want to know *before* we start
        capturing PCM (otherwise the user records into a black hole). On
        WS failure we fall back to buffered audio + post-hoc transcription
        (Phase 3 will add a batch fallback; Phase 2 surfaces the error).
        """
        self._latest_partial_transcript = ""
        try:
            session = AssemblyAIStreamingSession(
                proxy=self.proxy,
                sample_rate=self.config.audio.sample_rate,
                on_partial=self._on_stt_partial,
                on_final=self._on_stt_final,
                on_error=lambda msg: self._fail(f"Transcription: {msg}"),
            )
            session.start()
        except Exception as stt_open_error:
            self._fail(f"Could not open transcription session: {stt_open_error}")
            return
        self._active_stt_session = session
        # Push every captured block straight into the websocket.
        self.recorder.set_chunk_listener(session.feed_pcm)
        # Also drive the panel's waveform meter from the same audio thread.
        # The signal emit hop marshals onto the UI thread for free.
        self.recorder.set_level_listener(self.audio_level_changed.emit)
        try:
            self.recorder.start()
        except Exception as recorder_error:
            # Tear the WS back down so we don't leak a token.
            session.stop(final_wait_seconds=0.1)
            self._active_stt_session = None
            self.recorder.set_chunk_listener(None)
            self.recorder.set_level_listener(None)
            self._fail(f"Microphone error: {recorder_error}")

    def _process_turn(self) -> None:
        """End-of-recording pipeline.

        Stop the recorder + WS, persist a wav, capture a screenshot, stream
        Claude's reply, then move to RESPONDING for TTS + cursor pointing.
        """
        pcm_bytes = self.recorder.stop()
        self.recorder.set_chunk_listener(None)
        self.recorder.set_level_listener(None)
        session = self._active_stt_session
        self._active_stt_session = None
        final_transcript = ""
        if session is not None:
            try:
                final_transcript = session.stop(final_wait_seconds=2.0)
            except Exception:
                log.exception("Error finalizing STT session")
                final_transcript = self._latest_partial_transcript

        # Fall back to the last partial if the final never arrived.
        if not final_transcript:
            final_transcript = self._latest_partial_transcript

        # Always persist the wav — invaluable for debugging "Claude responded
        # to nothing" cases.
        if pcm_bytes:
            try:
                self._persist_recording(pcm_bytes)
            except Exception:
                log.exception("Failed to persist recording (continuing)")

        final_transcript = (final_transcript or "").strip()
        if not final_transcript:
            log.info("No transcript; returning to IDLE")
            self._set_state(CompanionState.IDLE)
            return
        self.transcription_final.emit(final_transcript)
        self._send_text_turn(
            final_transcript,
            include_screenshot=self.config.screen_capture_enabled,
        )

    # ---- response path ----
    def _send_text_turn(self, text: str, include_screenshot: bool) -> None:
        # Append the user turn to history + UI before doing any network so the
        # panel updates immediately.
        user_message = Message(role=Role.USER, content=text)
        self._history.append(user_message)
        self.message_appended.emit(user_message)

        screenshot_bytes: bytes | None = None
        if include_screenshot:
            try:
                screenshot_bytes = self.screen.capture_at_cursor().png_bytes
            except Exception:
                log.exception("Screen capture failed; sending without image")

        # Make sure we're in PROCESSING even when called via send_text() which
        # bypasses _process_turn.
        self._set_state(CompanionState.PROCESSING)

        try:
            reply: Message = self.claude.stream_full_reply(
                self._history,
                screenshot=screenshot_bytes,
                on_partial=self.assistant_partial.emit,
            )
        except Exception as claude_error:
            self._fail(f"Claude request failed: {claude_error}")
            return

        self._history.append(reply)
        self.message_appended.emit(reply)

        # Fire pointing markers into the overlay queue *before* TTS so the dot
        # starts flying as Claude starts speaking. The overlay queue handles
        # sequencing between multiple points.
        for marker in reply.points:
            self.point_received.emit(marker)

        if self.config.tts_enabled and reply.content.strip():
            self._set_state(CompanionState.RESPONDING)
            try:
                mp3_bytes = self.tts.synthesize(reply.content)
            except Exception as tts_error:
                log.exception("TTS synthesis failed")
                self._fail(f"TTS failed: {tts_error}")
                return
            # When playback finishes, the player calls back into us; jump
            # back to IDLE there rather than here so RESPONDING actually
            # reflects the speaking window.
            self.player.play_mp3(
                mp3_bytes,
                on_finished=lambda: self._set_state(CompanionState.IDLE),
            )
        else:
            self._set_state(CompanionState.IDLE)

    # ---- STT callbacks (reader thread) -----
    def _on_stt_partial(self, partial: str) -> None:
        self._latest_partial_transcript = partial
        self.transcription_partial.emit(partial)

    def _on_stt_final(self, final_text: str) -> None:
        # Stash the final but don't transition here — `_process_turn` owns the
        # PROCESSING/RESPONDING transitions so the ordering stays single-source.
        self._latest_partial_transcript = final_text

    # ---- helpers -----
    def _persist_recording(self, pcm_bytes: bytes) -> Path:
        self.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        filename = self.RECORDINGS_DIR / f"{int(time.time())}.wav"
        with wave.open(str(filename), "wb") as wav_file:
            wav_file.setnchannels(self.config.audio.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.config.audio.sample_rate)
            wav_file.writeframes(pcm_bytes)
        log.info("Recording persisted to %s", filename)
        return filename

    def _set_state(self, new_state: CompanionState) -> None:
        with self._state_lock:
            self._set_state_locked(new_state)

    def _set_state_locked(self, new_state: CompanionState) -> None:
        if new_state == self._state:
            return
        log.info("State %s -> %s", self._state.value, new_state.value)
        self._state = new_state
        self.state_changed.emit(new_state)

    def _fail(self, message: str) -> None:
        log.error(message)
        self.error_occurred.emit(message)
        # Tear down any in-flight side resources so the user can retry cleanly.
        self.recorder.cancel()
        self.recorder.set_chunk_listener(None)
        self.recorder.set_level_listener(None)
        session = self._active_stt_session
        self._active_stt_session = None
        if session is not None:
            try:
                session.stop(final_wait_seconds=0.1)
            except Exception:
                pass
        self._set_state(CompanionState.ERROR)
        self._set_state(CompanionState.IDLE)
