"""A local stand-in for the HeyBuddy Worker's Gnani routes (and /chat).

No network, no API key. Speaks exactly what the Worker exposes:

* `GET /gnani/stt-stream` — a WebSocket (hand-rolled RFC 6455 server, stdlib
  only). It records the upgrade headers and every binary frame, sends
  Gnani's `connected` event, and imitates VAD: after some non-silent frames,
  a run of `silence_frames_to_close` all-zero frames closes a segment and
  produces `processing` + `transcript` with the next text from
  `segment_texts`.
* `POST /gnani/tts-stream` — Gnani-style SSE (`start`, `chunk` with base64
  audio, `complete`), or an HTTP error / an `error` event when told to.
* `POST /gnani/tts` — REST: returns `rest_audio` bytes.
* `POST /chat` — Anthropic-style SSE from `reply_factory`, with an optional
  delay between events so tests can see speech start before the reply ends.
* `POST /tts` — ElevenLabs stand-in (English path), returns `mp3_bytes`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from src.tools.selfcheck import build_sse_events

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_exact(stream, count: int) -> bytes:
    data = b""
    while len(data) < count:
        piece = stream.read(count - len(data))
        if not piece:
            raise ConnectionError("socket closed")
        data += piece
    return data


def _read_frame(stream) -> tuple[int, bytes]:
    first, second = _read_exact(stream, 2)
    opcode = first & 0x0F
    masked = second & 0x80
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(stream, 8))[0]
    mask = _read_exact(stream, 4) if masked else b""
    payload = _read_exact(stream, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _frame(opcode: int, payload: bytes) -> bytes:
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack(">H", length)
    else:
        header += bytes([127]) + struct.pack(">Q", length)
    return header + payload


class MockGnaniWorker:
    def __init__(
        self,
        segment_texts: list[str] | None = None,
        tts_chunks: list[bytes] | None = None,
        reply_factory: Callable[[dict], Any] | None = None,
        chat_event_delay: float = 0.0,
        mp3_bytes: bytes = b"ID3fake-mp3",
    ) -> None:
        self.segment_texts = list(segment_texts or [])
        self.tts_chunks = list(tts_chunks if tts_chunks is not None else [b"\x01\x00" * 600, b"\x02\x00" * 600])
        self.reply_factory = reply_factory or (lambda payload: "ok")
        self.chat_event_delay = chat_event_delay
        self.mp3_bytes = mp3_bytes
        self.rest_audio = b"\x03\x00" * 400
        self.silence_frames_to_close = 8
        self.sse_http_status = 200
        self.sse_error_after_chunks: int | None = None
        self.send_stt_error: str | None = None

        self.ws_headers: list[dict[str, str]] = []
        self.ws_frames: list[bytes] = []
        self.ws_frame_times: list[float] = []
        self.tts_stream_requests: list[dict] = []
        self.tts_stream_request_times: list[float] = []
        self.tts_rest_requests: list[dict] = []
        self.stt_rest_requests: list[dict] = []
        self.rest_transcript: Any = "REST నుండి వచ్చిన వాక్యం"
        self.elevenlabs_requests: list[dict] = []
        self.chat_requests: list[dict] = []
        self.chat_finished_times: list[float] = []
        self.request_headers: list[dict[str, str]] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            # ----- WebSocket -----
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/gnani/stt-stream" or self.headers.get("upgrade", "").lower() != "websocket":
                    self.send_response(404)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                mock.ws_headers.append({key.lower(): value for key, value in self.headers.items()})
                accept = base64.b64encode(
                    hashlib.sha1((self.headers["sec-websocket-key"] + _WS_GUID).encode()).digest()
                ).decode()
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                send_lock = threading.Lock()

                def send_json(payload: dict) -> None:
                    with send_lock:
                        self.wfile.write(_frame(0x1, json.dumps(payload).encode("utf-8")))
                        self.wfile.flush()

                send_json({"type": "connected", "message": "STT service ready",
                           "timestamp": "2026-10-27T00:00:00Z", "config": {"sample_rate": 16000, "chunk_size": 512}})
                if mock.send_stt_error:
                    send_json({"type": "error", "timestamp": "t", "message": mock.send_stt_error})
                speech_frames = 0
                silent_run = 0
                segment_index = 0
                try:
                    while True:
                        opcode, payload = _read_frame(self.rfile)
                        if opcode == 0x8:
                            with send_lock:
                                self.wfile.write(_frame(0x8, payload[:2] or b"\x03\xe8"))
                                self.wfile.flush()
                            return
                        if opcode == 0x9:
                            with send_lock:
                                self.wfile.write(_frame(0xA, payload))
                            continue
                        if opcode != 0x2:
                            continue
                        mock.ws_frames.append(payload)
                        mock.ws_frame_times.append(time.monotonic())
                        if any(payload):
                            speech_frames += 1
                            silent_run = 0
                            continue
                        silent_run += 1
                        if speech_frames and silent_run >= mock.silence_frames_to_close:
                            speech_frames = 0
                            send_json({"type": "processing", "timestamp": "t"})
                            text = mock.segment_texts[segment_index] if segment_index < len(mock.segment_texts) else ""
                            send_json({"type": "transcript", "timestamp": "t", "text": text,
                                       "audio_duration_ms": 900, "segment_id": f"s{segment_index}",
                                       "segment_index": segment_index, "latency": 120})
                            segment_index += 1
                except (ConnectionError, OSError):
                    return

            # ----- HTTP -----
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                mock.request_headers.append({key.lower(): value for key, value in self.headers.items()})
                if self.path == "/gnani/tts-stream":
                    mock.tts_stream_requests.append(json.loads(body or b"{}"))
                    mock.tts_stream_request_times.append(time.monotonic())
                    if mock.sse_http_status != 200:
                        self._send_bytes(mock.sse_http_status, b'{"success":false}', "application/json")
                        return
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    self._sse("start", {"status": "streaming_started", "text": "x"})
                    for index, chunk in enumerate(mock.tts_chunks, start=1):
                        if mock.sse_error_after_chunks is not None and index > mock.sse_error_after_chunks:
                            self._sse("error", {"type": "error", "message": "synthesis failed"})
                            return
                        self._sse("chunk", {"chunk_index": index, "audio": base64.b64encode(chunk).decode(),
                                            "is_final": False})
                        time.sleep(0.005)
                    if mock.sse_error_after_chunks is not None and mock.sse_error_after_chunks >= len(mock.tts_chunks) and not mock.tts_chunks:
                        self._sse("error", {"type": "error", "message": "synthesis failed"})
                        return
                    self._sse("complete", {"chunk_index": len(mock.tts_chunks) + 1, "audio": "", "is_final": True})
                elif self.path == "/gnani/stt":
                    mock.stt_rest_requests.append({"lang": self.headers.get("x-heybuddy-lang"), "bytes": len(body),
                                                   "is_wav": body[:4] == b"RIFF"})
                    payload = json.dumps({"success": True, "transcript": mock.rest_transcript}, ensure_ascii=False)
                    self._send_bytes(200, payload.encode("utf-8"), "application/json")
                elif self.path == "/gnani/tts":
                    mock.tts_rest_requests.append(json.loads(body or b"{}"))
                    self._send_bytes(200, mock.rest_audio, "application/octet-stream")
                elif self.path == "/tts":
                    mock.elevenlabs_requests.append(json.loads(body or b"{}"))
                    self._send_bytes(200, mock.mp3_bytes, "audio/mpeg")
                elif self.path == "/chat":
                    payload = json.loads(body or b"{}")
                    mock.chat_requests.append(payload)
                    reply = mock.reply_factory(payload)
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    for name, data in build_sse_events(reply):
                        self._sse(name, data)
                        if mock.chat_event_delay:
                            time.sleep(mock.chat_event_delay)
                    mock.chat_finished_times.append(time.monotonic())
                else:
                    self._send_bytes(404, b"", "text/plain")

            def _sse(self, name: str, data: dict) -> None:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()

            def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, name="MockGnaniWorker", daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
