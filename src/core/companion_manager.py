"""Central state machine that orchestrates the push-to-talk loop.

Port of Clicky's `CompanionManager`:

    IDLE --press--> LISTENING --release--> PROCESSING --audio starts--> RESPONDING --audio ends--> IDLE

* Press: open an AssemblyAI realtime session and start the mic; PCM blocks
  stream into the websocket while the hotkey is held.
* Release: finalize the transcript, capture *all* monitors, stream Claude's
  reply, resolve `[POINT:...]` tags against the screenshots that produced
  them, start TTS, and send the resolved points to the cursor overlay.
* Pressing the hotkey again while HeyBuddy is thinking or talking
  interrupts it (Clicky cancels `currentResponseTask` the same way). Each
  turn carries a generation number; work from a superseded generation is
  dropped silently instead of racing the new turn.
* Pressing another key while the chord is held (Ctrl+Alt+Del, an AltGr
  character) cancels the recording instead of sending it.

Threading: network and capture work runs on short-lived worker threads.
Everything the UI needs is emitted as a Qt signal (queued to the UI thread).
"""
from __future__ import annotations

import threading
import time
import wave
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from ..api.assemblyai_streaming import AssemblyAIStreamingSession
from ..api.claude_client import ClaudeClient
from ..api.cloudflare_proxy import CloudflareProxy
from ..api.elevenlabs_client import ElevenLabsClient
from ..models.config import AppConfig
from ..models.message import CapturedScreen, Message, PointMarker, Role
from ..utils.constants import RECORDINGS_DIR
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


def resolve_point_markers(
    markers: list[PointMarker],
    screens: list[CapturedScreen],
) -> list[PointMarker]:
    """Attach physical desktop coordinates to each marker.

    `screenN` picks the Nth image of this turn; a missing or out-of-range
    screen number means the cursor's screen (Clicky's fallback).
    """
    if not screens:
        return []
    cursor_screen = next((screen for screen in screens if screen.is_cursor_screen), screens[0])
    resolved: list[PointMarker] = []
    for marker in markers:
        target_screen = cursor_screen
        if marker.screen_index is not None and 1 <= marker.screen_index <= len(screens):
            target_screen = screens[marker.screen_index - 1]
        physical_x, physical_y = target_screen.image_point_to_physical(marker.x, marker.y)
        resolved.append(PointMarker(
            x=marker.x,
            y=marker.y,
            label=marker.label,
            screen_index=target_screen.screen_number,
            physical_x=physical_x,
            physical_y=physical_y,
        ))
    return resolved


class CompanionManager(QObject):
    """Owns the state machine + all the moving pieces."""

    # ---- Qt signals (UI listens to these) ----
    state_changed = pyqtSignal(object)            # CompanionState
    message_appended = pyqtSignal(object)         # Message
    assistant_partial = pyqtSignal(str)           # spoken text so far (tags hidden)
    transcription_partial = pyqtSignal(str)       # live STT text
    transcription_final = pyqtSignal(str)
    point_received = pyqtSignal(object)           # PointMarker (back-compat, one per tag)
    pointing_requested = pyqtSignal(object)       # list[PointMarker], resolved
    turn_interrupted = pyqtSignal()               # overlay should abandon pointing
    error_occurred = pyqtSignal(str)
    audio_level_changed = pyqtSignal(float)       # mic RMS 0..1 while LISTENING

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
        self._state_lock = threading.RLock()
        # Completed exchanges only (user, assistant, user, assistant, ...),
        # so an interrupted turn never leaves two user messages in a row.
        self._history: list[Message] = []
        self._enabled: bool = True
        self._turn_generation = 0
        self._active_stt_session: AssemblyAIStreamingSession | None = None
        self._latest_partial_transcript: str = ""

    # ---- public API ----
    @property
    def state(self) -> CompanionState:
        return self._state

    @property
    def history(self) -> list[Message]:
        return list(self._history)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @pyqtSlot()
    def toggle_enabled(self) -> None:
        self._enabled = not self._enabled
        log.info("Companion %s", "enabled" if self._enabled else "disabled")
        if not self._enabled:
            self.cancel()

    @pyqtSlot()
    def clear_history(self) -> None:
        self._history.clear()
        log.info("Conversation history cleared")

    def begin_listening(self) -> None:
        """Hotkey press. Called on the hook thread, so it must stay cheap."""
        if not self._enabled:
            return
        with self._state_lock:
            if self._state == CompanionState.LISTENING:
                return
            if self._state in (CompanionState.PROCESSING, CompanionState.RESPONDING):
                log.info("Interrupting %s turn for a new question", self._state.value)
                self._interrupt_current_turn_locked()
            self._turn_generation += 1
            generation = self._turn_generation
            self._set_state_locked(CompanionState.LISTENING)
        threading.Thread(
            target=self._open_stt_and_start_recording,
            args=(generation,),
            name="StartCapture",
            daemon=True,
        ).start()

    def end_listening_and_process(self) -> None:
        """Hotkey release."""
        with self._state_lock:
            if self._state != CompanionState.LISTENING:
                return
            generation = self._turn_generation
            self._set_state_locked(CompanionState.PROCESSING)
        threading.Thread(
            target=self._finish_capture_and_respond,
            args=(generation,),
            name="TurnPipeline",
            daemon=True,
        ).start()

    def cancel_listening(self) -> None:
        """Another key was pressed with the chord: the user meant a shortcut."""
        with self._state_lock:
            if self._state != CompanionState.LISTENING:
                return
            self._turn_generation += 1
            log.info("Push-to-talk cancelled (another key pressed with the chord)")
            self._teardown_capture()
            self._set_state_locked(CompanionState.IDLE)

    @pyqtSlot()
    def cancel(self) -> None:
        """Abort whatever is in flight and return to IDLE."""
        with self._state_lock:
            self._turn_generation += 1
            self._interrupt_current_turn_locked()
            self._teardown_capture()
            self._set_state_locked(CompanionState.IDLE)

    def send_text(self, text: str, include_screenshot: bool | None = None) -> None:
        """Typed turn from the panel; same pipeline as voice, minus STT."""
        cleaned_text = (text or "").strip()
        if not cleaned_text:
            return
        if include_screenshot is None:
            include_screenshot = self.config.screen_capture_enabled
        with self._state_lock:
            if self._state == CompanionState.LISTENING:
                return
            if self._state in (CompanionState.PROCESSING, CompanionState.RESPONDING):
                self._interrupt_current_turn_locked()
            self._turn_generation += 1
            generation = self._turn_generation
            self._set_state_locked(CompanionState.PROCESSING)
        threading.Thread(
            target=self._respond_to_user_text,
            args=(generation, cleaned_text, include_screenshot),
            name="TypedTurn",
            daemon=True,
        ).start()

    # ---- capture path ----
    def _open_stt_and_start_recording(self, generation: int) -> None:
        self._latest_partial_transcript = ""
        session = AssemblyAIStreamingSession(
            proxy=self.proxy,
            sample_rate=self.config.audio.sample_rate,
            speech_model=self.config.transcription.speech_model,
            on_partial=lambda text: self._on_stt_partial(generation, text),
            on_error=lambda message: self._fail(generation, f"Transcription: {message}"),
        )
        # Start the mic FIRST and buffer, so the first words aren't lost while
        # the token + websocket handshake (~300-800 ms) is in flight. Buffered
        # audio is flushed into the socket as soon as it opens.
        pending_audio_chunks: list[bytes] = []
        pending_lock = threading.Lock()
        socket_ready = threading.Event()

        def forward_or_buffer(pcm_chunk: bytes) -> None:
            if socket_ready.is_set():
                session.feed_pcm(pcm_chunk)
                return
            with pending_lock:
                if socket_ready.is_set():
                    session.feed_pcm(pcm_chunk)
                else:
                    pending_audio_chunks.append(pcm_chunk)

        self.recorder.set_chunk_listener(forward_or_buffer)
        self.recorder.set_level_listener(self.audio_level_changed.emit)
        try:
            self.recorder.start()
        except Exception as recorder_error:
            self.recorder.set_chunk_listener(None)
            self.recorder.set_level_listener(None)
            self._fail(generation, f"Microphone error: {recorder_error}")
            return

        try:
            session.start()
        except Exception as stt_open_error:
            self._fail(generation, f"Could not start transcription: {stt_open_error}")
            return

        with self._state_lock:
            if generation != self._turn_generation:
                session.cancel()
                return
            self._active_stt_session = session
        with pending_lock:
            for buffered_chunk in pending_audio_chunks:
                session.feed_pcm(buffered_chunk)
            pending_audio_chunks.clear()
            socket_ready.set()

    def _finish_capture_and_respond(self, generation: int) -> None:
        pcm_bytes = self.recorder.stop()
        self.recorder.set_chunk_listener(None)
        self.recorder.set_level_listener(None)
        # The user can release before the websocket finished opening; give
        # the opener a moment to publish the session.
        deadline = time.monotonic() + 3.0
        while self._active_stt_session is None and time.monotonic() < deadline:
            if generation != self._turn_generation or self._state != CompanionState.PROCESSING:
                return
            time.sleep(0.05)
        with self._state_lock:
            session = self._active_stt_session
            self._active_stt_session = None

        final_transcript = ""
        if session is not None:
            try:
                final_transcript = session.stop()
            except Exception:
                log.exception("Error finalizing STT session")
        if not final_transcript:
            final_transcript = self._latest_partial_transcript
        final_transcript = (final_transcript or "").strip()

        if pcm_bytes and self.config.save_recordings:
            try:
                self._persist_recording(pcm_bytes)
            except Exception:
                log.exception("Failed to persist recording (continuing)")

        if generation != self._turn_generation:
            return
        if not final_transcript:
            log.info("No speech detected; back to IDLE")
            self._set_state_if_current(generation, CompanionState.IDLE)
            return
        self.transcription_final.emit(final_transcript)
        self._respond_to_user_text(generation, final_transcript, self.config.screen_capture_enabled)

    # ---- response path ----
    def _respond_to_user_text(self, generation: int, text: str, include_screenshot: bool) -> None:
        user_message = Message(role=Role.USER, content=text)
        self.message_appended.emit(user_message)
        self._set_state_if_current(generation, CompanionState.PROCESSING)

        captured_screens: list[CapturedScreen] = []
        if include_screenshot:
            try:
                captured_screens = self.screen.capture_all_screens()
            except Exception:
                log.exception("Screen capture failed; asking without screenshots")

        def is_current_turn() -> bool:
            return generation == self._turn_generation

        try:
            reply = self.claude.stream_full_reply(
                self._history + [user_message],
                screens=captured_screens,
                on_partial=lambda spoken_so_far: (
                    self.assistant_partial.emit(spoken_so_far) if is_current_turn() else None
                ),
                should_continue=is_current_turn,
            )
        except Exception as claude_error:
            self._fail(generation, f"Claude request failed: {claude_error}")
            return
        if not is_current_turn():
            return

        reply.points = resolve_point_markers(reply.points, captured_screens)
        self._history.extend([user_message, reply])
        self.message_appended.emit(reply)
        log.info(
            "Reply: %d chars, %d point(s)%s", len(reply.content), len(reply.points),
            " (explicit none)" if reply.explicitly_no_point else "",
        )

        if self.config.tts_enabled and reply.content.strip():
            try:
                mp3_bytes = self.tts.synthesize(reply.content)
            except Exception as tts_error:
                log.exception("TTS synthesis failed")
                # The answer is still on screen and the cursor can still point;
                # a voice failure shouldn't throw the whole turn away.
                self.error_occurred.emit(f"Voice unavailable: {tts_error}")
                self._emit_pointing(reply.points)
                self._set_state_if_current(generation, CompanionState.IDLE)
                return
            if not is_current_turn():
                return

            def on_audio_started() -> None:
                if is_current_turn():
                    self._set_state_if_current(generation, CompanionState.RESPONDING)
                    # Fly while the buddy talks, like Clicky.
                    self._emit_pointing(reply.points)

            self.player.play_mp3(
                mp3_bytes,
                on_started=on_audio_started,
                on_finished=lambda: self._set_state_if_current(generation, CompanionState.IDLE),
            )
        else:
            self._emit_pointing(reply.points)
            self._set_state_if_current(generation, CompanionState.IDLE)

    def _emit_pointing(self, markers: list[PointMarker]) -> None:
        if not markers:
            return
        self.pointing_requested.emit(list(markers))
        for marker in markers:
            self.point_received.emit(marker)

    # ---- STT callbacks (reader thread) -----
    def _on_stt_partial(self, generation: int, partial: str) -> None:
        if generation != self._turn_generation:
            return
        self._latest_partial_transcript = partial
        self.transcription_partial.emit(partial)

    # ---- helpers -----
    def _interrupt_current_turn_locked(self) -> None:
        self.player.stop()
        self.turn_interrupted.emit()

    def _teardown_capture(self) -> None:
        self.recorder.cancel()
        self.recorder.set_chunk_listener(None)
        self.recorder.set_level_listener(None)
        session = self._active_stt_session
        self._active_stt_session = None
        if session is not None:
            threading.Thread(target=session.cancel, name="SttCancel", daemon=True).start()

    def _persist_recording(self, pcm_bytes: bytes) -> Path:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        filename = RECORDINGS_DIR / f"{int(time.time())}.wav"
        with wave.open(str(filename), "wb") as wav_file:
            wav_file.setnchannels(self.config.audio.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.config.audio.sample_rate)
            wav_file.writeframes(pcm_bytes)
        log.info("Recording persisted to %s", filename)
        return filename

    def _set_state_if_current(self, generation: int, new_state: CompanionState) -> None:
        with self._state_lock:
            if generation == self._turn_generation:
                self._set_state_locked(new_state)

    def _set_state(self, new_state: CompanionState) -> None:
        with self._state_lock:
            self._set_state_locked(new_state)

    def _set_state_locked(self, new_state: CompanionState) -> None:
        if new_state == self._state:
            return
        log.info("State %s -> %s", self._state.value, new_state.value)
        self._state = new_state
        self.state_changed.emit(new_state)

    def _fail(self, generation: int, message: str) -> None:
        with self._state_lock:
            if generation != self._turn_generation:
                log.debug("Ignoring error from superseded turn: %s", message)
                return
            log.error(message)
            self._teardown_capture()
            self._set_state_locked(CompanionState.ERROR)
            self._set_state_locked(CompanionState.IDLE)
        self.error_occurred.emit(message)
