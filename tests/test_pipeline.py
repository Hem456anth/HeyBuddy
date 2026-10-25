"""End-to-end turn through the real CompanionManager + CloudflareProxy +
ClaudeClient against a local mock Worker (no API keys, no Windows)."""
import time

import pytest

PyQt6 = pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.api.claude_client import ClaudeClient  # noqa: E402
from src.api.cloudflare_proxy import CloudflareProxy  # noqa: E402
from src.api.elevenlabs_client import ElevenLabsClient  # noqa: E402
from src.core.companion_manager import CompanionManager, CompanionState  # noqa: E402
from src.models.config import AppConfig  # noqa: E402
from src.models.message import CapturedScreen  # noqa: E402
from src.tools.selfcheck import _MockWorker  # noqa: E402


class _FakeScreen:
    def capture_all_screens(self):
        return [
            CapturedScreen(b"\xff\xd8a", "image/jpeg", "screen 1 of 2 - cursor (image dimensions: 1280x800 pixels)",
                           1, True, 1280, 800, 0, 0, 2880, 1800, 1.5),
            CapturedScreen(b"\xff\xd8b", "image/jpeg", "screen 2 of 2 (image dimensions: 1280x720 pixels)",
                           2, False, 1280, 720, 2880, 0, 1920, 1080, 1.0),
        ]


class _FakePlayer:
    def __init__(self):
        self.played = []

    def play_mp3(self, mp3_bytes, on_finished=None, on_started=None):
        self.played.append(mp3_bytes)
        if on_started:
            on_started()
        if on_finished:
            on_finished()

    def stop(self):
        return None


class _FakeRecorder:
    def cancel(self):
        return None

    def set_chunk_listener(self, listener):
        return None

    def set_level_listener(self, listener):
        return None


def _pump_until(app, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        app.processEvents()
        time.sleep(0.01)
    return predicate()


def test_typed_turn_end_to_end():
    app = QApplication.instance() or QApplication([])
    reply = "that's on your other monitor, see the terminal? [POINT:640,360:terminal:screen2]"
    worker = _MockWorker(lambda payload: reply, b"ID3fake-mp3")
    try:
        config = AppConfig()
        proxy = CloudflareProxy(worker.url)
        player = _FakePlayer()
        manager = CompanionManager(config, _FakeRecorder(), _FakeScreen(), proxy,
                                   ClaudeClient(proxy, config.model), ElevenLabsClient(proxy), player)
        pointing, partials, states = [], [], []
        manager.pointing_requested.connect(pointing.append)
        manager.assistant_partial.connect(partials.append)
        manager.state_changed.connect(lambda state: states.append(state))
        manager.send_text("where's my terminal?")
        assert _pump_until(app, lambda: bool(pointing) and manager.state == CompanionState.IDLE)
        marker = pointing[0][0]
        # screen2 is 1920x1080 at x=2880, shown as 1280x720: (640,360) is its centre.
        assert (marker.physical_x, marker.physical_y) == (2880 + 960, 540)
        assert marker.label == "terminal"
        assert manager.history[-1].content == "that's on your other monitor, see the terminal?"
        assert all("[POINT" not in partial and "[POI" not in partial for partial in partials)
        assert player.played == [b"ID3fake-mp3"]
        assert CompanionState.RESPONDING in states
        request = worker.chat_requests[0]
        assert request["stream"] is True
        assert sum(1 for block in request["messages"][-1]["content"] if block["type"] == "image") == 2
    finally:
        worker.close()


def test_missing_worker_url_gives_friendly_error():
    app = QApplication.instance() or QApplication([])
    config = AppConfig()
    proxy = CloudflareProxy("https://your-worker.workers.dev")
    manager = CompanionManager(config, _FakeRecorder(), _FakeScreen(), proxy,
                               ClaudeClient(proxy, config.model), ElevenLabsClient(proxy), _FakePlayer())
    errors = []
    manager.error_occurred.connect(errors.append)
    manager.send_text("hi", include_screenshot=False)
    assert _pump_until(app, lambda: bool(errors))
    assert "Worker URL is not set" in errors[0]
    assert manager.state == CompanionState.IDLE


def test_buddy_program_turn_with_tool_and_memory(tmp_path, monkeypatch):
    import src.core.companion_manager as manager_module
    import src.core.tools as tools_module
    from src.core.memory_store import MemoryStore
    from src.tools.selfcheck import _has_tool_result
    from tests import fake_win32

    monkeypatch.setattr(manager_module, "win32", fake_win32)
    monkeypatch.setattr(tools_module, "win32", fake_win32)

    app = QApplication.instance() or QApplication([])

    def reply(payload):
        if not _has_tool_result(payload):
            return {"text": '```buddy\nsay("one sec")\n```',
                    "tool_use": {"id": "w", "name": "get_active_window", "input": {}}}
        return ('```buddy\nsay("you are in notepad.")\nring(600, 380, 80, 40, "menu")\n'
                'say("click file.")\ntarget(10, 10, 60, 30, "file")\nnoted("Uses Notepad.")\n```')

    worker = _MockWorker(reply, b"ID3fake-mp3")
    try:
        config = AppConfig()
        proxy = CloudflareProxy(worker.url)
        player = _FakePlayer()
        memory = MemoryStore(tmp_path / "memory.md")
        manager = CompanionManager(config, _FakeRecorder(), _FakeScreen(), proxy,
                                   ClaudeClient(proxy, config.model), ElevenLabsClient(proxy), player,
                                   memory=memory)
        beats, finished = [], []
        manager.beat_started.connect(beats.append)
        manager.speech_finished.connect(lambda: finished.append(1))
        manager.send_text("what app am i in?")
        assert _pump_until(app, lambda: bool(finished) and manager.state == CompanionState.IDLE)
        assert [b.text for b in beats] == ["one sec", "you are in notepad.", "click file."]
        ring = beats[1].marks[0]
        # cursor screen: 1280x800 image of a 2880x1800 monitor (x2.25)
        assert (ring.physical["x"], ring.physical["w"]) == (1350, 180)
        assert beats[2].marks[0].kind == "target"
        assert len(player.played) == 3
        assert manager.history[-1].content == "one sec you are in notepad. click file."
        assert memory.facts()[0].endswith("Uses Notepad.")
        tool_result = worker.chat_requests[1]["messages"][-1]["content"][0]
        assert "notepad.exe" in tool_result["content"]
        assert "the user's focused window" in worker.chat_requests[0]["system"]
    finally:
        worker.close()
