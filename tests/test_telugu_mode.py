"""Telugu mode end to end: toggle, Gnani STT -> Claude -> streamed Gnani TTS.

Everything runs against `tests.mock_gnani.MockGnaniWorker` (local WebSocket
+ SSE). Synthetic text only.
"""
from __future__ import annotations

import json
import re
import threading
import time

import pytest

PyQt6 = pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.api.claude_client import ClaudeClient  # noqa: E402
from src.api.cloudflare_proxy import CloudflareProxy  # noqa: E402
from src.api.elevenlabs_client import ElevenLabsClient  # noqa: E402
from src.core import companion_manager as manager_module  # noqa: E402
from src.core.companion_manager import CompanionManager, CompanionState  # noqa: E402
from src.core.latency import TurnLatency  # noqa: E402
from src.models.config import AppConfig  # noqa: E402
from src.utils.constants import LOGS_DIR, TELUGU_SYSTEM_PROMPT_ADDENDUM  # noqa: E402
from tests import fake_win32  # noqa: E402
from tests.mock_gnani import MockGnaniWorker  # noqa: E402
from tests.test_pipeline import _FakeScreen, _pump_until  # noqa: E402


class _StreamPlayer:
    """Records both playback paths; plays instantly."""

    def __init__(self):
        self.mp3_clips: list[bytes] = []
        self.pcm_clips: list[bytes] = []
        self.pcm_sample_rates: list[int] = []
        self.is_playing = False

    def play_mp3(self, mp3_bytes, on_finished=None, on_started=None):
        self.mp3_clips.append(mp3_bytes)
        if on_started:
            on_started()
        if on_finished:
            on_finished()

    def play_pcm_stream(self, chunks, sample_rate=24000, on_finished=None, on_started=None):
        collected = b""
        for index, chunk in enumerate(chunks):
            if index == 0 and on_started:
                on_started()
            collected += chunk
        self.pcm_clips.append(collected)
        self.pcm_sample_rates.append(sample_rate)
        if on_finished:
            on_finished()

    def stop(self):
        return None


class _ScriptedRecorder:
    """Feeds synthetic PCM blocks to the chunk listener like the mic does."""

    def __init__(self, blocks: list[bytes]):
        self.blocks = blocks
        self._listener = None
        self._thread = None
        self._stop = threading.Event()

    def set_chunk_listener(self, listener):
        self._listener = listener

    def set_level_listener(self, listener):
        return None

    def start(self):
        self._stop.clear()

        def run():
            for block in self.blocks:
                if self._stop.is_set():
                    return
                listener = self._listener
                if listener:
                    listener(block)
                time.sleep(0.02)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread:
            self._thread.join(timeout=5)
        self._stop.set()
        return b"".join(self.blocks)

    def cancel(self):
        self._stop.set()


def _manager(worker, config, player, recorder=None):
    proxy = CloudflareProxy(worker.url)
    return CompanionManager(config, recorder or _ScriptedRecorder([]), _FakeScreen(), proxy,
                            ClaudeClient(proxy, config.model), ElevenLabsClient(proxy), player)


@pytest.fixture(autouse=True)
def _no_real_windows(monkeypatch):
    monkeypatch.setattr(manager_module, "win32", fake_win32)


def test_toggle_persists_and_emits(tmp_path):
    app = QApplication.instance() or QApplication([])
    worker = MockGnaniWorker()
    try:
        config = AppConfig()
        assert config.language_mode == "english" and not config.is_telugu_mode
        manager = _manager(worker, config, _StreamPlayer())
        seen = []
        manager.language_mode_changed.connect(seen.append)
        saved = []
        config.save = lambda path=None: saved.append(config.language_mode)  # type: ignore[method-assign]
        assert manager.toggle_language_mode() == "telugu"
        assert config.is_telugu_mode and saved == ["telugu"] and seen == ["telugu"]
        assert manager.set_language_mode("telugu") is False   # no-op, no second save
        assert manager.toggle_language_mode() == "english"
        assert saved == ["telugu", "english"]
        with pytest.raises(ValueError):
            manager.set_language_mode("klingon")
        app.processEvents()
    finally:
        worker.close()


def test_language_mode_round_trips_and_bad_values_fall_back(tmp_path):
    path = tmp_path / "settings.json"
    config = AppConfig()
    config.language_mode = "telugu"
    config.telugu.tts_language = "auto"
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.is_telugu_mode and loaded.telugu.tts_language == "auto"
    assert loaded.telugu.stt_lang_code == "te-IN"
    path.write_text(json.dumps({"language_mode": "french", "telugu": {"min_silence_ms": 300, "junk": 1}}))
    loaded = AppConfig.load(path)
    assert loaded.language_mode == "english" and loaded.telugu.min_silence_ms == 300


TELUGU_REPLY = (
    "```buddy\n"
    'say("మీ File menu ఇక్కడ ఉంది")\n'
    'ring(600, 380, 80, 40, "File")\n'
    'say("Battery 45% ఉంది, 10:30 కి charge చేయండి")\n'
    'say("అంతే!")\n'
    "```"
)


def test_telugu_typed_turn_streams_gnani_speech_while_claude_is_still_writing():
    app = QApplication.instance() or QApplication([])
    worker = MockGnaniWorker(reply_factory=lambda payload: TELUGU_REPLY, chat_event_delay=0.01,
                             tts_chunks=[b"\x01\x00" * 300, b"\x02\x00" * 300])
    try:
        config = AppConfig()
        config.language_mode = "telugu"
        config.tools_enabled = False
        player = _StreamPlayer()
        manager = _manager(worker, config, player)
        beats, finished = [], []
        manager.beat_started.connect(beats.append)
        manager.speech_finished.connect(lambda: finished.append(1))
        manager.send_text("నా File menu ఎక్కడ?")
        assert _pump_until(app, lambda: bool(finished) and manager.state == CompanionState.IDLE, 15)

        system_prompt = worker.chat_requests[0]["system"]
        assert TELUGU_SYSTEM_PROMPT_ADDENDUM in system_prompt
        assert [beat.text for beat in beats] == [
            "మీ File menu ఇక్కడ ఉంది", "Battery 45% ఉంది, 10:30 కి charge చేయండి", "అంతే!",
        ]
        assert beats[0].marks[0].kind == "ring" and beats[0].marks[0].is_resolved
        spoken = [request["text"] for request in worker.tts_stream_requests]
        # Lines are prefetched two at a time, so requests may arrive in any order.
        assert sorted(spoken) == sorted([
            "మీ File menu ఇక్కడ ఉంది",
            "Battery నలభై ఐదు శాతం ఉంది, పది గంటల ముప్పై నిమిషాలు కి charge చేయండి",
            "అంతే!",
        ])
        assert not any(re.search(r"\d", text) for text in spoken)
        assert all(request["language"] == "te-IN" for request in worker.tts_stream_requests)
        assert worker.elevenlabs_requests == []
        assert player.pcm_clips == [b"\x01\x00" * 300 + b"\x02\x00" * 300] * 3
        assert player.pcm_sample_rates == [24000] * 3
        # Sentence chunking: the first line went to TTS before Claude finished.
        assert worker.tts_stream_request_times[0] < worker.chat_finished_times[0]
    finally:
        worker.close()


def test_telugu_voice_turn_uses_gnani_stt_and_logs_timings_without_content():
    app = QApplication.instance() or QApplication([])
    speech = b"\x10\x00" * 1600
    silence = b"\x00\x00" * 1600
    worker = MockGnaniWorker(segment_texts=["నా Downloads folder తెరువు"],
                             reply_factory=lambda payload: '```buddy\nsay("సరే, తెరుస్తున్నా")\n```')
    try:
        config = AppConfig()
        config.language_mode = "telugu"
        config.tools_enabled = False
        config.screen_capture_enabled = False
        config.telugu.min_silence_ms = 100
        player = _StreamPlayer()
        manager = _manager(worker, config, player, _ScriptedRecorder([speech] * 8 + [silence]))
        finals, finished = [], []
        manager.transcription_final.connect(finals.append)
        manager.speech_finished.connect(lambda: finished.append(1))
        manager.begin_listening()
        assert _pump_until(app, lambda: manager._active_stt_session is not None, 8)
        time.sleep(0.6)  # hold longer than MINIMUM_HOLD_SECONDS
        manager.end_listening_and_process()
        assert _pump_until(app, lambda: bool(finished) and manager.state == CompanionState.IDLE, 20)
        assert finals == ["నా Downloads folder తెరువు"]
        assert worker.ws_headers[0]["x-heybuddy-lang"] == "te-IN"
        assert all(len(frame) == 1024 for frame in worker.ws_frames)
        assert worker.chat_requests[0]["messages"][-1]["content"] == "నా Downloads folder తెరువు"
        assert worker.tts_stream_requests[0]["text"] == "సరే, తెరుస్తున్నా"

        log_text = (LOGS_DIR / "latency.log").read_text(encoding="utf-8")
        line = [entry for entry in log_text.splitlines() if "mode=telugu" in entry][-1]
        for span in ("release->stt_final=", "stt_final->first_token=", "first_token->first_audio=",
                     "release->first_audio="):
            assert span in line
        assert "Downloads" not in log_text and "తెరు" not in log_text
    finally:
        worker.close()


def test_empty_realtime_transcript_falls_back_to_gnani_rest():
    app = QApplication.instance() or QApplication([])
    speech = b"\x10\x08" * 1600
    worker = MockGnaniWorker(segment_texts=[],  # realtime answers nothing useful
                             reply_factory=lambda payload: '```buddy\nsay("సరే")\n```')
    try:
        config = AppConfig()
        config.language_mode = "telugu"
        config.tools_enabled = False
        config.screen_capture_enabled = False
        config.telugu.min_silence_ms = 100
        manager = _manager(worker, config, _StreamPlayer(), _ScriptedRecorder([speech] * 8))
        finals, finished = [], []
        manager.transcription_final.connect(finals.append)
        manager.speech_finished.connect(lambda: finished.append(1))
        manager.begin_listening()
        assert _pump_until(app, lambda: manager._active_stt_session is not None, 8)
        time.sleep(0.6)
        manager.end_listening_and_process()
        assert _pump_until(app, lambda: bool(finished) and manager.state == CompanionState.IDLE, 20)
        assert finals == ["REST నుండి వచ్చిన వాక్యం"]
        assert worker.stt_rest_requests and worker.stt_rest_requests[0]["is_wav"]
    finally:
        worker.close()


def test_english_mode_is_untouched_by_telugu_support():
    app = QApplication.instance() or QApplication([])
    worker = MockGnaniWorker(reply_factory=lambda payload: "your terminal is on the left.")
    try:
        config = AppConfig()
        config.tools_enabled = False
        player = _StreamPlayer()
        manager = _manager(worker, config, player)
        finished = []
        manager.speech_finished.connect(lambda: finished.append(1))
        manager.send_text("where's my terminal?")
        assert _pump_until(app, lambda: bool(finished) and manager.state == CompanionState.IDLE, 10)
        assert TELUGU_SYSTEM_PROMPT_ADDENDUM not in worker.chat_requests[0]["system"]
        assert worker.elevenlabs_requests == [{"text": "your terminal is on the left."}]
        assert worker.tts_stream_requests == [] and worker.tts_rest_requests == []
        assert player.mp3_clips == [b"ID3fake-mp3"] and player.pcm_clips == []
        from src.api.assemblyai_streaming import AssemblyAIStreamingSession
        assert isinstance(manager._new_stt_session(manager._turn_generation), AssemblyAIStreamingSession)
        config.language_mode = "telugu"
        from src.api.gnani_streaming import GnaniStreamingSession
        assert isinstance(manager._new_stt_session(manager._turn_generation), GnaniStreamingSession)
    finally:
        worker.close()


def test_turn_latency_spans_and_single_report():
    ticks = iter([1.0, 1.5, 1.6, 2.3, 2.9, 3.2])
    latency = TurnLatency(3, "telugu", clock=lambda: next(ticks))
    for stage in ("release", "stt_final", "request", "first_token", "first_chunk", "first_audio"):
        latency.mark(stage)
    latency.mark("release")  # ignored: first mark wins
    spans = latency.spans_ms()
    assert spans["release->stt_final"] == 500
    assert spans["stt_final->first_token"] == 800
    assert spans["first_token->first_audio"] == 900
    assert spans["release->first_audio"] == 2200

    class _Recorder:
        def __init__(self):
            self.lines = []

        def info(self, line):
            self.lines.append(line)

    recorder = _Recorder()
    assert latency.report(recorder) is True
    assert latency.report(recorder) is False
    assert recorder.lines[0].startswith("turn=3 mode=telugu release->stt_final=500ms")
    with pytest.raises(ValueError):
        latency.mark("bogus")
