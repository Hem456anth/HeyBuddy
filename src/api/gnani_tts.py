"""Gnani Vachana TTS (Timbre) through the Worker — Telugu mode.

Two ways to get audio, both via the Worker so the client holds no key:

* `stream_pcm(text)` — `/gnani/tts-stream`, Gnani's SSE endpoint
  (`POST https://api.vachana.ai/api/v1/tts/sse`). Each `chunk` event carries
  base64 audio; we ask for `container: raw`, `encoding: linear_pcm`, 24 kHz
  mono, so chunks are PCM16 that `AudioPlayer.play_pcm_stream` plays as they
  arrive (playback starts on the first chunk). If the stream fails before any
  audio arrives, the same request goes to the REST endpoint
  (`/gnani/tts` -> `POST /api/v1/tts/inference`) and its body is yielded as
  one chunk.
* `synthesize(text)` — REST, MP3 container, for callers that want a whole
  clip (`AudioPlayer.play_mp3`), e.g. job announcements.

Every text first goes through `core.telugu_normalizer.normalize_for_tts`
(digits, times, ₹, % -> Telugu words; markdown/tags stripped), as Gnani's
docs ask numbers to be passed as spoken words.

The voice and model are chosen by the Worker (`GNANI_TTS_VOICE`,
`GNANI_TTS_MODEL`), mirroring how `ELEVENLABS_VOICE_ID` works.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Iterator

from ..core.telugu_normalizer import normalize_for_tts
from ..utils.constants import GNANI_TTS_SAMPLE_RATE
from ..utils.logger import get_logger
from .cloudflare_proxy import CloudflareProxy

log = get_logger(__name__)


def strip_wav_header(audio: bytes) -> bytes:
    """Return the PCM payload of a RIFF/WAVE clip (or the input unchanged)."""
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return audio
    position = 12
    while position + 8 <= len(audio):
        chunk_id = audio[position:position + 4]
        chunk_size = int.from_bytes(audio[position + 4:position + 8], "little")
        if chunk_id == b"data":
            return audio[position + 8:position + 8 + chunk_size]
        position += 8 + chunk_size + (chunk_size & 1)
    return b""


class GnaniTTSClient:
    def __init__(
        self,
        proxy: CloudflareProxy,
        language: str = "te-IN",
        sample_rate: int = GNANI_TTS_SAMPLE_RATE,
        speed: float = 1.0,
        normalize: bool = True,
    ) -> None:
        self.proxy = proxy
        self.language = language
        self.sample_rate = sample_rate
        self.speed = speed
        self.normalize = normalize

    def prepare_text(self, text: str) -> str:
        return normalize_for_tts(text) if self.normalize else (text or "").strip()

    def request_body(self, text: str, container: str = "raw") -> dict[str, Any]:
        audio_config: dict[str, Any] = {
            "sample_rate": self.sample_rate,
            "num_channels": 1,
            "sample_width": 2,
            "container": container,
        }
        if container != "mp3":
            audio_config["encoding"] = "linear_pcm"
        return {
            "text": text,
            "language": self.language,
            "speed": self.speed,
            "audio_config": audio_config,
        }

    # ----- streaming -----
    def stream_pcm(self, text: str) -> Iterator[bytes]:
        """Yield raw PCM16 mono chunks at `self.sample_rate`."""
        prepared = self.prepare_text(text)
        if not prepared:
            return
        body = self.request_body(prepared, container="raw")
        yielded_audio = False
        try:
            for event in self.proxy.gnani_tts_stream(body):
                pcm = self._audio_from_event(event.event, event.data)
                if pcm:
                    yielded_audio = True
                    yield pcm
        except Exception as stream_error:
            if yielded_audio:
                raise
            log.warning("Gnani SSE TTS failed before any audio (%s); falling back to REST", stream_error)
            audio = self.proxy.gnani_tts(body)
            pcm = strip_wav_header(audio)
            if pcm:
                yield pcm
            return
        if not yielded_audio:
            log.warning("Gnani SSE TTS returned no audio; falling back to REST")
            pcm = strip_wav_header(self.proxy.gnani_tts(body))
            if pcm:
                yield pcm

    @staticmethod
    def _audio_from_event(event_name: str, data: str) -> bytes:
        if not data:
            return b""
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return b""
        if not isinstance(payload, dict):
            return b""
        if event_name == "error" or payload.get("type") == "error" or payload.get("success") is False:
            detail = payload.get("error") or payload.get("message") or payload
            raise RuntimeError(f"Gnani TTS stream error: {detail}")
        encoded = payload.get("audio") or ""
        if not encoded:
            return b""
        try:
            return strip_wav_header(base64.b64decode(encoded))
        except (binascii.Error, ValueError) as error:
            raise RuntimeError("Gnani TTS sent an undecodable audio chunk") from error

    # ----- whole clip -----
    def synthesize(self, text: str) -> bytes:
        """MP3 bytes for the whole text (REST)."""
        prepared = self.prepare_text(text)
        if not prepared:
            return b""
        log.debug("Requesting Gnani TTS for %d chars", len(prepared))
        return self.proxy.gnani_tts(self.request_body(prepared, container="mp3"))
