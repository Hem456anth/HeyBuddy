"""Gnani TTS client against a local mock Worker (SSE + REST), no network."""
from __future__ import annotations

import struct

import pytest

from src.api.cloudflare_proxy import CloudflareProxy
from src.api.gnani_tts import GnaniTTSClient, strip_wav_header
from tests.mock_gnani import MockGnaniWorker


def _wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def test_strip_wav_header():
    pcm = b"\x01\x02" * 10
    assert strip_wav_header(_wav(pcm)) == pcm
    assert strip_wav_header(pcm) == pcm
    assert strip_wav_header(b"") == b""


def test_request_body_is_normalized_and_has_no_voice_or_key():
    client = GnaniTTSClient(CloudflareProxy("x.workers.dev"), language="te-IN", speed=1.05)
    body = client.request_body(client.prepare_text("**మొత్తం** ₹250"), container="raw")
    assert body["text"] == "మొత్తం రెండు వందల యాభై రూపాయలు"
    assert body["language"] == "te-IN"
    assert body["speed"] == 1.05
    assert body["audio_config"] == {"sample_rate": 24000, "num_channels": 1, "sample_width": 2,
                                    "container": "raw", "encoding": "linear_pcm"}
    assert "voice" not in body and "model" not in body
    mp3 = client.request_body("x", container="mp3")["audio_config"]
    assert mp3["container"] == "mp3" and "encoding" not in mp3


def test_stream_pcm_yields_chunks_in_order():
    chunks = [b"\x01\x00" * 50, b"\x02\x00" * 50, b"\x03\x00" * 50]
    worker = MockGnaniWorker(tts_chunks=chunks)
    try:
        client = GnaniTTSClient(CloudflareProxy(worker.url, shared_secret="k"))
        received = list(client.stream_pcm("Settings లో 2 options ఉన్నాయి"))
        assert received == chunks
        request = worker.tts_stream_requests[0]
        assert request["text"] == "Settings లో రెండు options ఉన్నాయి"
        assert request["audio_config"]["container"] == "raw"
        assert worker.request_headers[0]["x-heybuddy-key"] == "k"
        assert worker.tts_rest_requests == []
    finally:
        worker.close()


def test_wav_chunks_are_unwrapped():
    pcm = b"\x05\x00" * 30
    worker = MockGnaniWorker(tts_chunks=[_wav(pcm)])
    try:
        assert list(GnaniTTSClient(CloudflareProxy(worker.url)).stream_pcm("హలో")) == [pcm]
    finally:
        worker.close()


def test_falls_back_to_rest_when_sse_fails_before_audio():
    worker = MockGnaniWorker()
    worker.sse_http_status = 503
    worker.rest_audio = _wav(b"\x07\x00" * 20)
    try:
        received = list(GnaniTTSClient(CloudflareProxy(worker.url)).stream_pcm("హలో"))
        assert received == [b"\x07\x00" * 20]
        assert len(worker.tts_stream_requests) == 1
        assert worker.tts_rest_requests[0]["text"] == "హలో"
    finally:
        worker.close()


def test_error_event_before_audio_falls_back_after_audio_raises():
    worker = MockGnaniWorker(tts_chunks=[b"\x01\x00" * 10, b"\x02\x00" * 10])
    try:
        client = GnaniTTSClient(CloudflareProxy(worker.url))
        worker.sse_error_after_chunks = 0
        assert list(client.stream_pcm("ఒకటి")) == [worker.rest_audio]
        worker.sse_error_after_chunks = 1
        stream = client.stream_pcm("రెండు")
        assert next(stream) == b"\x01\x00" * 10
        with pytest.raises(RuntimeError, match="Gnani TTS stream error"):
            next(stream)
    finally:
        worker.close()


def test_empty_text_makes_no_request_and_synthesize_uses_mp3():
    worker = MockGnaniWorker()
    worker.rest_audio = b"ID3gnani"
    try:
        client = GnaniTTSClient(CloudflareProxy(worker.url))
        assert list(client.stream_pcm("```buddy\npoint(1, 2)\n```")) == []
        assert client.synthesize("  ") == b""
        assert worker.tts_stream_requests == [] and worker.tts_rest_requests == []
        assert client.synthesize("పని పూర్తయింది") == b"ID3gnani"
        assert worker.tts_rest_requests[0]["audio_config"]["container"] == "mp3"
    finally:
        worker.close()
