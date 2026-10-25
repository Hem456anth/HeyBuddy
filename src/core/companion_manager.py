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
from typing import TYPE_CHECKING, Callable

from ..api.claude_client import ClaudeClient, ToolCall, run_tool_loop, stream_round
from ..api.cloudflare_proxy import CloudflareProxy
from ..api.elevenlabs_client import ElevenLabsClient
from ..models.config import AppConfig
from ..models.message import CapturedScreen, Message, PointMarker, Role
from ..utils import win32
from ..utils.constants import CONVERSATION_HISTORY_MAX_EXCHANGES, RECORDINGS_DIR
from ..utils.logger import get_logger
from .audio_player import AudioPlayer
from .audio_recorder import AudioRecorder
from .beat_speaker import BeatSpeaker
from .memory_store import MemoryStore
from .scene_program import Beat, display_text_for_partial, parse_reply_program, resolve_marks
from .screen_capture import ScreenCapture
from .tools import VOICE_TOOLS, ToolContext, ToolError, default_working_dir, execute_tool, tool_definitions

if TYPE_CHECKING:
    from .jobs import JobManager

MAX_TOOL_ROUNDS = 6
MINIMUM_HOLD_SECONDS = 0.5
RELEASE_TAIL_SECONDS = 0.15

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
    beat_started = pyqtSignal(object)             # Beat (resolved marks) as its line starts
    speech_finished = pyqtSignal()                # every beat of the turn has been spoken
    task_changed = pyqtSignal(str)                # task("...") from a guided walkthrough
    tool_activity = pyqtSignal(str)               # tool name being executed
    dictation_delivered = pyqtSignal(str)         # text typed at the caret

    def __init__(
        self,
        config: AppConfig,
        recorder: AudioRecorder,
        screen: ScreenCapture,
        proxy: CloudflareProxy,
        claude: ClaudeClient,
        tts: ElevenLabsClient,
        player: AudioPlayer,
        memory: MemoryStore | None = None,
        jobs: "JobManager | None" = None,
        approve: Callable[[str, str], bool] | None = None,
    ) -> None:
        super().__init__()
        self.memory = memory
        self.jobs = jobs
        self.approve = approve or (lambda _title, _detail: False)
        self._active_speaker: BeatSpeaker | None = None
        self._dictating = False
        self._press_started_at = 0.0
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
            self._press_started_at = time.monotonic()
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
            if time.monotonic() - self._press_started_at < MINIMUM_HOLD_SECONDS:
                # A brush of the keys, not a question (HeyClicky: 0.5 s).
                self._turn_generation += 1
                log.info("Push-to-talk released after < %.1f s; ignored", MINIMUM_HOLD_SECONDS)
                self._teardown_capture()
                self._set_state_locked(CompanionState.IDLE)
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
        # Keep the mic open a moment after release so the last syllable
        # isn't clipped (HeyClicky waits a 150 ms tail).
        time.sleep(RELEASE_TAIL_SECONDS)
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
        if self._dictating:
            self._deliver_dictation(generation, final_transcript)
            return
        if not final_transcript:
            log.info("No speech detected; back to IDLE")
            self._set_state_if_current(generation, CompanionState.IDLE)
            return
        self.transcription_final.emit(final_transcript)
        self._respond_to_user_text(generation, final_transcript, self.config.screen_capture_enabled)

    # ---- response path ----
    def _system_prompt(self) -> str:
        prompt = self.claude.system_prompt
        extras: list[str] = []
        if self.memory is not None and self.config.memory_enabled:
            block = self.memory.prompt_block()
            if block:
                extras.append(block)
        try:
            window = win32.get_foreground_window_info()
            if window.get("title") or window.get("process"):
                extras.append(f"the user's focused window: {window.get('title', '')} ({window.get('process', '')})")
        except Exception:
            pass
        extras.append(f"local time: {time.strftime('%A %Y-%m-%d %H:%M')}")
        return prompt + "\n\n" + "\n\n".join(extras)

    def _tool_context(self) -> ToolContext:
        return ToolContext(
            working_dir=default_working_dir(),
            approve=self.approve,
            command_policy=self.config.command_policy,
            write_policy=self.config.write_policy,
            allow_background_jobs=self.config.background_jobs_enabled and self.jobs is not None,
            start_job=(self.jobs.start if self.jobs is not None else None),
            memory=self.memory if self.config.memory_enabled else None,
        )

    def _respond_to_user_text(self, generation: int, text: str, include_screenshot: bool) -> None:
        user_message = Message(role=Role.USER, content=text)
        if not text.startswith("[app]"):
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

        payload = self.claude.build_payload(self._history + [user_message], screens=captured_screens)
        payload["system"] = self._system_prompt()
        if self.config.tools_enabled:
            names = tuple(
                name for name in VOICE_TOOLS
                if name != "start_background_job" or (self.config.background_jobs_enabled and self.jobs is not None)
            )
            if not self.config.memory_enabled:
                names = tuple(name for name in names if name != "remember")
            payload["tools"] = tool_definitions(names)

        spoken_parts: list[str] = []
        streamed_text: list[str] = []
        all_noted: list[str] = []
        speaker_holder: list[BeatSpeaker] = []
        turn_finished = threading.Event()

        def on_all_done() -> None:
            turn_finished.set()
            if is_current_turn():
                self.speech_finished.emit()
                self._set_state_if_current(generation, CompanionState.IDLE)

        def on_audio_started() -> None:
            self._set_state_if_current(generation, CompanionState.RESPONDING)

        def on_beat_started(beat: Beat) -> None:
            if not is_current_turn():
                return
            self.beat_started.emit(beat)
            markers = [
                PointMarker(
                    x=int(mark.params["x"]), y=int(mark.params["y"]), label=mark.label,
                    screen_index=mark.screen_index,
                    physical_x=int(mark.physical["x"]), physical_y=int(mark.physical["y"]),
                )
                for mark in beat.marks if mark.kind == "point" and mark.is_resolved
            ]
            if markers:
                self.pointing_requested.emit(markers)
                for marker in markers:
                    self.point_received.emit(marker)

        speaker = BeatSpeaker(
            synthesize=self.tts.synthesize if self.config.tts_enabled else None,
            play_mp3=self.player.play_mp3 if self.config.tts_enabled else None,
            on_beat_started=on_beat_started,
            on_audio_started=on_audio_started,
            on_all_done=on_all_done,
            on_error=lambda message: self.error_occurred.emit(message) if is_current_turn() else None,
        )
        speaker_holder.append(speaker)
        with self._state_lock:
            if not is_current_turn():
                speaker.cancel()
                return
            self._active_speaker = speaker

        def speak_round(round_text: str, more_coming: bool) -> None:
            if not is_current_turn():
                return
            program = resolve_marks(parse_reply_program(round_text), captured_screens)
            if program.parse_errors:
                log.info("Program parse issues: %s", "; ".join(program.parse_errors[:5]))
            if program.task_title:
                self.task_changed.emit(program.task_title)
            all_noted.extend(program.noted_facts)
            for beat in program.beats:
                if not self.config.annotations_enabled:
                    beat.marks = [mark for mark in beat.marks if mark.kind == "point"]
                if beat.text:
                    spoken_parts.append(beat.text)
                if beat.text or beat.marks:
                    speaker.add(beat)

        def on_text(piece: str) -> None:
            streamed_text.append(piece)
            if is_current_turn():
                self.assistant_partial.emit(display_text_for_partial("".join(streamed_text)))

        context = self._tool_context()

        def execute(call: ToolCall) -> tuple[str, bool]:
            streamed_text.append("\n")
            self.tool_activity.emit(call.name)
            try:
                return execute_tool(call.name, call.input, context), False
            except ToolError as refusal:
                return str(refusal), True

        try:
            if self.config.tools_enabled:
                run_tool_loop(
                    self.claude, payload, execute,
                    on_round_text=speak_round, on_text=on_text,
                    should_continue=is_current_turn, max_rounds=MAX_TOOL_ROUNDS,
                )
            else:
                result = stream_round(self.claude, payload, on_text=on_text, should_continue=is_current_turn)
                if result.text.strip():
                    speak_round(result.text, False)
        except Exception as claude_error:
            speaker.cancel()
            self._fail(generation, f"Claude request failed: {claude_error}")
            return
        if not is_current_turn():
            speaker.cancel()
            return

        if self.memory is not None and self.config.memory_enabled:
            for fact in all_noted:
                self.memory.add(fact)
        reply = Message(role=Role.ASSISTANT, content=" ".join(spoken_parts).strip() or "(no reply)")
        self._history.extend([user_message, reply])
        self._history = self._history[-(CONVERSATION_HISTORY_MAX_EXCHANGES * 2 + 4):]
        self.message_appended.emit(reply)
        log.info("Reply: %d chars spoken", len(reply.content))
        speaker.close()

    def continue_after_target_click(self, label: str) -> None:
        """The user clicked the control a target() asked for: next step."""
        if not self.config.guided_steps_enabled:
            return
        description = f'[app] the user clicked "{label}", the step your drawing asked for. here is their screen now; continue with the next step, or say it\'s done.'
        self.send_text(description, include_screenshot=True)

    def announce(self, text: str) -> None:
        """Speak a short notice (a finished job) if the buddy is idle."""
        text = (text or "").strip()
        if not text or not self.config.tts_enabled or self._state != CompanionState.IDLE:
            return

        def run() -> None:
            try:
                mp3_bytes = self.tts.synthesize(text)
            except Exception:
                log.exception("Announcement TTS failed")
                return
            if self._state == CompanionState.IDLE and not self.player.is_playing:
                self.player.play_mp3(mp3_bytes)

        threading.Thread(target=run, name="Announce", daemon=True).start()

    # ---- dictation (hold Ctrl+Win: speech is typed at the caret) ----
    def begin_dictation(self) -> None:
        if not self._enabled or not self.config.dictation_enabled:
            return
        with self._state_lock:
            if self._state != CompanionState.IDLE:
                return
            self._turn_generation += 1
            generation = self._turn_generation
            self._dictating = True
            self._press_started_at = time.monotonic()
            self._set_state_locked(CompanionState.LISTENING)
        threading.Thread(target=self._open_stt_and_start_recording, args=(generation,),
                         name="DictationCapture", daemon=True).start()

    def end_dictation(self) -> None:
        with self._state_lock:
            if self._state != CompanionState.LISTENING or not self._dictating:
                return
            generation = self._turn_generation
            self._set_state_locked(CompanionState.PROCESSING)
        threading.Thread(target=self._finish_capture_and_respond, args=(generation,),
                         name="DictationFinish", daemon=True).start()

    def _deliver_dictation(self, generation: int, transcript: str) -> None:
        self._dictating = False
        if transcript and generation == self._turn_generation:
            ok = win32.paste_text_into_focused_window(transcript + " ")
            self.dictation_delivered.emit(transcript if ok else "")
            if not ok:
                self.error_occurred.emit("Couldn't type into the focused window; the text is on your clipboard.")
        self._set_state_if_current(generation, CompanionState.IDLE)

    # ---- STT callbacks (reader thread) -----
    def _on_stt_partial(self, generation: int, partial: str) -> None:
        if generation != self._turn_generation:
            return
        self._latest_partial_transcript = partial
        self.transcription_partial.emit(partial)

    # ---- helpers -----
    def _interrupt_current_turn_locked(self) -> None:
        speaker = self._active_speaker
        self._active_speaker = None
        if speaker is not None:
            speaker.cancel()
        self.player.stop()
        self.turn_interrupted.emit()

    def _teardown_capture(self) -> None:
        self._dictating = False
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
