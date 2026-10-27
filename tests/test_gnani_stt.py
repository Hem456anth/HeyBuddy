"""Gnani realtime STT session against a local mock WebSocket (no network, no key)."""
from __future__ import annotations

import time

from src.api.cloudflare_proxy import CloudflareProxy
from src.api.gnani_streaming import GnaniSegmentAssembler, GnaniStreamingSession
from tests.mock_gnani import MockGnaniWorker

SPEECH_BLOCK = b"\x10\x00" * 1600   # 100 ms of non-silent PCM16 at 16 kHz (3,200 bytes)
SILENT_BLOCK = b"\x00\x00" * 1600


def test_url_and_headers_follow_the_worker_contract():
    proxy = CloudflareProxy("x.workers.dev", shared_secret="s3cret")
    session = GnaniStreamingSession(proxy, lang_code="te-IN", sample_rate=16000, min_silence_ms=400)
    assert session.build_url() == "wss://x.workers.dev/gnani/stt-stream"
    headers = session.build_headers()
    assert "x-heybuddy-lang: te-IN" in headers
    assert "x-sample-rate: 16000" in headers
    assert "x-min-silence-ms: 400" in headers
    assert "X-HeyBuddy-Key: s3cret" in headers
    # The API key is the Worker's business; the client never sends one.
    assert not any(header.lower().startswith("x-api-key-id") for header in headers)
    local = CloudflareProxy("http://127.0.0.1:9")
    assert GnaniStreamingSession(local).build_url() == "ws://127.0.0.1:9/gnani/stt-stream"


def test_segment_assembler_orders_segments_and_tracks_pending():
    assembler = GnaniSegmentAssembler()
    assembler.handle({"type": "connected"})
    assembler.handle({"type": "processing"})
    assert assembler.segments_pending
    assembler.handle({"type": "processing"})
    assembler.handle({"type": "transcript", "text": "రెండవ భాగం", "segment_index": 1})
    assembler.handle({"type": "transcript", "text": "మొదటి భాగం", "segment_index": 0})
    assert not assembler.segments_pending
    assert assembler.full_transcript() == "మొదటి భాగం రెండవ భాగం"
    assembler.handle({"type": "transcript", "text": "  ", "segment_index": 2})
    assert assembler.full_transcript() == "మొదటి భాగం రెండవ భాగం"


def test_streams_exact_frames_and_joins_segments():
    worker = MockGnaniWorker(segment_texts=["నా Downloads folder", "ఎక్కడ ఉంది?"])
    try:
        partials, errors = [], []
        session = GnaniStreamingSession(
            CloudflareProxy(worker.url, shared_secret="k"), lang_code="te-IN", min_silence_ms=100,
            on_partial=partials.append, on_error=errors.append, frame_interval_seconds=0.002,
        )
        session.start()
        for _ in range(4):
            session.feed_pcm(SPEECH_BLOCK)
        for _ in range(4):      # a pause long enough for the mock VAD to close segment 0
            session.feed_pcm(SILENT_BLOCK)
        for _ in range(3):
            session.feed_pcm(SPEECH_BLOCK)
        session.feed_pcm(b"\x10\x00" * 100)  # a ragged tail, not a multiple of 1,024 bytes
        transcript = session.stop(final_wait_seconds=5.0)
        assert transcript == "నా Downloads folder ఎక్కడ ఉంది?"
        assert errors == []
        assert partials[-1] == transcript
        headers = worker.ws_headers[0]
        assert headers["x-heybuddy-lang"] == "te-IN"
        assert headers["x-sample-rate"] == "16000"
        assert headers["x-heybuddy-key"] == "k"
        assert worker.ws_frames, "no audio reached the socket"
        assert all(len(frame) == 1024 for frame in worker.ws_frames)
        # 11 blocks of 3,200 bytes + 200-byte tail, padded up, then trailing silence.
        audio_frames = -(-(11 * 3200 + 200) // 1024)
        assert len(worker.ws_frames) >= audio_frames + 1
    finally:
        worker.close()


def test_frames_are_paced_not_burst():
    worker = MockGnaniWorker(segment_texts=["హలో"])
    try:
        session = GnaniStreamingSession(CloudflareProxy(worker.url), min_silence_ms=100)
        session.start()
        started = time.monotonic()
        for _ in range(2):
            session.feed_pcm(SPEECH_BLOCK)   # 0.2 s of audio = 7 frames, queued at once
        assert session.stop(final_wait_seconds=5.0) == "హలో"
        times = worker.ws_frame_times
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        # Backlog is sent at 2x real time (16 ms), never as one burst.
        assert min(gaps) >= 0.012
        assert times[-1] - started >= 0.12
    finally:
        worker.close()


def test_server_error_event_is_reported():
    worker = MockGnaniWorker()
    worker.send_stt_error = "STT engine failed to initialize"
    try:
        errors = []
        session = GnaniStreamingSession(CloudflareProxy(worker.url), on_error=errors.append,
                                        frame_interval_seconds=0.002)
        session.start()
        deadline = time.monotonic() + 3
        while not errors and time.monotonic() < deadline:
            time.sleep(0.02)
        session.cancel()
        assert errors == ["STT engine failed to initialize"]
    finally:
        worker.close()


def test_silence_only_returns_empty_quickly():
    worker = MockGnaniWorker(segment_texts=["unused"])
    try:
        session = GnaniStreamingSession(CloudflareProxy(worker.url), min_silence_ms=100,
                                        frame_interval_seconds=0.002)
        session.start()
        session.feed_pcm(SILENT_BLOCK)
        started = time.monotonic()
        assert session.stop(final_wait_seconds=5.0) == ""
        assert time.monotonic() - started < 2.0
    finally:
        worker.close()


def test_rest_transcription_sends_wav_and_handles_multi_language_answers():
    from src.api.gnani_streaming import REST_MAX_SECONDS, transcribe_with_rest

    worker = MockGnaniWorker()
    try:
        proxy = CloudflareProxy(worker.url)
        assert transcribe_with_rest(proxy, SPEECH_BLOCK * 5, 16000, "te-IN") == "REST నుండి వచ్చిన వాక్యం"
        request = worker.stt_rest_requests[0]
        assert request["lang"] == "te-IN" and request["is_wav"]
        worker.rest_transcript = {"en-IN": "naa files", "te-IN": "నా ఫైల్స్"}
        assert transcribe_with_rest(proxy, SPEECH_BLOCK, 16000, "te-IN") == "నా ఫైల్స్"
        # Longer than Gnani's 60 s REST limit: only the newest 55 s are sent.
        transcribe_with_rest(proxy, b"\x01\x00" * 16000 * 70, 16000, "te-IN")
        assert worker.stt_rest_requests[-1]["bytes"] == 44 + REST_MAX_SECONDS * 16000 * 2
    finally:
        worker.close()
