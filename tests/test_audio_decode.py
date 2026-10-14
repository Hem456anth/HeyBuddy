import pytest

from src.core.audio_player import decode_mp3_to_pcm16, resolve_ffmpeg_executable
from src.tools.selfcheck import _make_test_mp3


def test_bundled_ffmpeg_decodes_mp3():
    if not resolve_ffmpeg_executable():
        pytest.skip("no ffmpeg available on this host")
    mp3_bytes = _make_test_mp3()
    if not mp3_bytes:
        pytest.skip("ffmpeg could not synthesize a test tone here")
    samples = decode_mp3_to_pcm16(mp3_bytes)
    assert samples.dtype.name == "int16"
    assert len(samples) > 10_000  # ~0.6 s at 24 kHz
