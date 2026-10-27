"""Thin HTTP wrapper around the shared Cloudflare Worker proxy.

The Worker is the same one used by macOS Clicky; it forwards requests to
Anthropic, ElevenLabs, and AssemblyAI without exposing API keys to the client.
HeyBuddy 0.4 adds Gnani routes for Telugu mode (`/gnani/tts`,
`/gnani/tts-stream`, and the `/gnani/stt-stream` WebSocket, whose URL comes
from `websocket_url()`); the English routes are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import requests

from ..utils.constants import (
    WORKER_CHAT_PATH,
    WORKER_GNANI_STT_REST_PATH,
    WORKER_GNANI_TTS_PATH,
    WORKER_GNANI_TTS_STREAM_PATH,
    WORKER_TRANSCRIBE_TOKEN_PATH,
    WORKER_TTS_PATH,
)
from ..utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class SSEEvent:
    """One server-sent event parsed from the Worker's `/chat` response.

    The Worker forwards Anthropic's stream verbatim, so events come through
    looking like:

        event: content_block_delta
        data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}

    Multiple `data:` lines per event are concatenated with newlines per the
    SSE spec; a blank line separates events.
    """
    event: str
    data: str


class CloudflareProxy:
    """Single transport for all Worker-mediated endpoints."""

    # Header checked by the Worker when its APP_SHARED_SECRET var is set.
    SHARED_SECRET_HEADER = "X-HeyBuddy-Key"

    def __init__(self, base_url: str, timeout: float = 60.0, shared_secret: str = "") -> None:
        # (connect, read) — fail fast on a dead Worker URL, but give Claude's
        # first streamed token / a long TTS clip room to arrive.
        self.timeout = (8.0, timeout)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "HeyBuddy-Windows"
        self.apply_shared_secret(shared_secret)
        # base_url normalization is shared with the runtime setter so
        # users get the same "auto-add https://" treatment whether they
        # type the URL into settings.json or into the Settings dialog.
        self.base_url = ""
        self.apply_worker_url(base_url)

    def apply_worker_url(self, new_base_url: str) -> None:
        """Swap the Worker base URL at runtime; re-applies normalization.

        Used by the Settings dialog to hot-swap the URL without
        restarting the app. The next /chat, /tts, or /transcribe-token
        request immediately uses the new URL. Existing in-flight HTTP
        requests on the underlying session aren't redirected — they
        finish against the URL they were issued with.
        """
        normalized = (new_base_url or "").strip()
        if normalized and "://" not in normalized:
            normalized = f"https://{normalized}"
        self.base_url = normalized.rstrip("/")
        log.info("Worker URL set to %s", self.base_url or "(empty)")

    def apply_shared_secret(self, shared_secret: str) -> None:
        cleaned = (shared_secret or "").strip()
        if cleaned:
            self._session.headers[self.SHARED_SECRET_HEADER] = cleaned
        else:
            self._session.headers.pop(self.SHARED_SECRET_HEADER, None)

    @property
    def shared_secret(self) -> str:
        return str(self._session.headers.get(self.SHARED_SECRET_HEADER, "") or "")

    def websocket_url(self, path: str) -> str:
        """`https://x.workers.dev` + path -> `wss://x.workers.dev/path`."""
        self._require_configured()
        if self.base_url.startswith("https://"):
            return "wss://" + self.base_url[len("https://"):] + path
        if self.base_url.startswith("http://"):
            return "ws://" + self.base_url[len("http://"):] + path
        return self.base_url + path

    def require_configured(self) -> None:
        self._require_configured()

    def _require_configured(self) -> None:
        if not self.base_url or "your-worker" in self.base_url:
            raise RuntimeError(
                "Worker URL is not set. Open Settings and paste your Cloudflare Worker URL."
            )

    # ---- /chat ----
    def chat(self, payload: dict[str, Any], stream: bool = False) -> requests.Response:
        self._require_configured()
        url = f"{self.base_url}{WORKER_CHAT_PATH}"
        log.debug("POST %s stream=%s", url, stream)
        response = self._session.post(url, json=payload, stream=stream, timeout=self.timeout)
        # Surface the upstream error body so the user sees the real problem
        # (e.g. "Your credit balance is too low" from Anthropic) instead of
        # an opaque "400 Bad Request". Skip for streaming responses — the
        # body iterator is the caller's responsibility there.
        if not stream and not response.ok:
            raise _build_http_error("/chat", response)
        return response

    def chat_stream(self, payload: dict[str, Any]) -> Iterator[SSEEvent]:
        """Yield parsed `SSEEvent`s from the Worker's `/chat` SSE response.

        We accumulate lines until a blank separator, then emit one event with
        the joined `data:` payload. This handles multi-line `data:` (which
        Anthropic doesn't currently emit, but the SSE spec allows) and skips
        comment lines (`:` prefix) that some intermediaries inject as
        keepalives.
        """
        response = self.chat({**payload, "stream": True}, stream=True)
        if not response.ok:
            raise _build_http_error("/chat", response)

        yield from iter_sse_events(response)

    # ---- /tts ----
    def tts(self, text: str) -> bytes:
        """Synthesize speech.

        Note: the Worker injects the voice id from its `ELEVENLABS_VOICE_ID`
        secret. The client must NOT send a `voice_id` field — the upstream
        Worker is a pass-through to ElevenLabs and including extra keys would
        leak into the ElevenLabs request body.
        """
        self._require_configured()
        url = f"{self.base_url}{WORKER_TTS_PATH}"
        log.debug("POST %s (%d chars)", url, len(text))
        resp = self._session.post(
            url,
            json={"text": text},
            timeout=self.timeout,
        )
        if not resp.ok:
            raise _build_http_error("/tts", resp)
        return resp.content

    # ---- /transcribe-token ----
    def transcription_token(self) -> str:
        """Short-lived AssemblyAI realtime token (used by realtime providers).

        The upstream Worker rejects any non-POST request at the very top of
        its handler, so this MUST be a POST even though the route is
        semantically a fetch.
        """
        self._require_configured()
        url = f"{self.base_url}{WORKER_TRANSCRIBE_TOKEN_PATH}"
        resp = self._session.post(url, json={}, timeout=self.timeout)
        if not resp.ok:
            raise _build_http_error("/transcribe-token", resp)
        data = resp.json()
        return data.get("token") or data.get("temp_token") or ""

    # ---- Gnani (Telugu mode) ----
    def gnani_tts(self, body: dict[str, Any]) -> bytes:
        """Whole clip from Gnani's REST TTS via the Worker (`/gnani/tts`).

        The Worker injects the API key, voice and model; the client sends
        only `text`, `language`, `speed` and `audio_config`.
        """
        self._require_configured()
        url = f"{self.base_url}{WORKER_GNANI_TTS_PATH}"
        response = self._session.post(url, json=body, timeout=self.timeout)
        if not response.ok:
            raise _build_http_error(WORKER_GNANI_TTS_PATH, response)
        return response.content

    def gnani_stt_rest(self, wav_bytes: bytes, lang_code: str) -> str:
        """Transcribe a whole WAV clip (<= 60 s) via `/gnani/stt` (REST)."""
        self._require_configured()
        url = f"{self.base_url}{WORKER_GNANI_STT_REST_PATH}"
        response = self._session.post(
            url, data=wav_bytes, timeout=self.timeout,
            headers={"content-type": "audio/wav", "x-heybuddy-lang": lang_code},
        )
        if not response.ok:
            raise _build_http_error(WORKER_GNANI_STT_REST_PATH, response)
        response.encoding = "utf-8"
        transcript = response.json().get("transcript") or ""
        if isinstance(transcript, dict):
            # multi_lang_codes answers {"te-IN": "...", "en-IN": "..."}.
            transcript = transcript.get(lang_code) or next(iter(transcript.values()), "")
        return str(transcript).strip()

    def gnani_tts_stream(self, body: dict[str, Any]) -> Iterator[SSEEvent]:
        """Gnani's SSE TTS via the Worker (`/gnani/tts-stream`)."""
        self._require_configured()
        url = f"{self.base_url}{WORKER_GNANI_TTS_STREAM_PATH}"
        response = self._session.post(url, json=body, stream=True, timeout=self.timeout)
        if not response.ok:
            raise _build_http_error(WORKER_GNANI_TTS_STREAM_PATH, response)
        try:
            yield from iter_sse_events(response)
        finally:
            response.close()

    def close(self) -> None:
        self._session.close()


def iter_sse_events(response: requests.Response) -> Iterator[SSEEvent]:
    """Parse a streamed `text/event-stream` response into `SSEEvent`s."""
    current_event = "message"
    data_lines: list[str] = []

    # Anthropic streams UTF-8 but sends `text/event-stream` without a
    # charset, which makes requests guess ISO-8859-1 and mangle every
    # non-ASCII character (Telugu, emoji, curly quotes...). Force UTF-8.
    response.encoding = "utf-8"
    for raw_line in response.iter_lines(decode_unicode=True):
        # `iter_lines` yields the chunk between newlines without the
        # newline. An empty string therefore signals the end-of-event
        # blank separator.
        if raw_line is None:
            continue
        if raw_line == "":
            if data_lines:
                yield SSEEvent(event=current_event, data="\n".join(data_lines))
                data_lines = []
                current_event = "message"
            continue
        if raw_line.startswith(":"):
            # Comment / keepalive line; ignore.
            continue
        if raw_line.startswith("event:"):
            current_event = raw_line[len("event:"):].strip()
            continue
        if raw_line.startswith("data:"):
            # SSE spec: strip exactly one leading space if present.
            payload_segment = raw_line[len("data:"):]
            if payload_segment.startswith(" "):
                payload_segment = payload_segment[1:]
            data_lines.append(payload_segment)
            continue
        # Unknown field; ignore per SSE spec.

    # Flush trailing event if the server closed without a final blank line.
    if data_lines:
        yield SSEEvent(event=current_event, data="\n".join(data_lines))


def _build_http_error(route: str, response: requests.Response) -> RuntimeError:
    """Compose a friendly exception including the upstream response body.

    The Worker passes through upstream API errors verbatim; their bodies
    contain the actionable diagnostic (e.g. "Your credit balance is too
    low" from Anthropic, "Invalid API key" from AssemblyAI). Without
    surfacing the body, the user only sees the HTTP status code and has
    to curl the Worker themselves to figure out what went wrong.
    """
    body_preview = (response.text or "").strip()
    if len(body_preview) > 500:
        body_preview = body_preview[:500] + "..."
    if not body_preview:
        body_preview = "(empty response body)"
    return RuntimeError(
        f"Worker {route} returned {response.status_code}: {body_preview}"
    )
