import json

from src.models.config import AppConfig


def test_unknown_keys_and_missing_sections_are_tolerated(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"worker_url": "x.workers.dev", "future_key": 1, "audio": {"sample_rate": 16000, "bogus": 2}}))
    config = AppConfig.load(path)
    assert config.worker_url == "x.workers.dev"
    assert config.audio.sample_rate == 16000
    assert config.save_recordings is False


def test_corrupt_file_is_backed_up_and_defaults_used(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    config = AppConfig.load(path)
    assert config.model == "claude-sonnet-5-5"
    assert (tmp_path / "settings.broken.json").exists()
    assert json.loads(path.read_text())["model"] == "claude-sonnet-5-5"


def test_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    config = AppConfig()
    config.show_cursor_buddy = False
    config.transcription.speech_model = "universal-3-5-pro"
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.show_cursor_buddy is False
    assert loaded.transcription.speech_model == "universal-3-5-pro"
