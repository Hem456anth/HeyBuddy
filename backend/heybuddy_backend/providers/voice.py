"""TTS / STT provider interfaces + Gradium implementations.

Gradium (verified against https://docs.gradium.ai, accessed 2026-10-25):
  base URL  https://api.gradium.ai/api      auth header  x-api-key: <GRADIUM_API_KEY>
  TTS  POST /post/speech/tts   JSON {text, voice_id, output_format in wav|pcm|opus|..., only_audio, model_name?}
       only_audio=true -> raw audio bytes (audio/wav for wav). Pre-stream errors: HTTP 500 plain text.
  STT  POST /post/speech/asr?json_config={"language":"en"}  body = WAV bytes (Content-Type audio/wav)
       response application/x-ndjson lines: {"type":"text","text":...} | {"type":"end_text"} | {"type":"error"}
"""
from __future__ import annotations

import json
from typing import Protocol

import httpx

from .http import UpstreamError, timeout, with_retries


class TTSProvider(Protocol):
    enabled: bool
    media_type: str

    async def synthesize(self, text: str, voice: str | None = None) -> bytes: ...


class STTProvider(Protocol):
    enabled: bool

    async def transcribe(self, audio: bytes, content_type: str) -> str: ...


class DisabledTTS:
    enabled = False
    media_type = "audio/wav"

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        raise UpstreamError("tts", 503, "server text-to-speech is not configured")


class DisabledSTT:
    enabled = False

    async def transcribe(self, audio: bytes, content_type: str) -> str:
        raise UpstreamError("stt", 503, "server speech-to-text is not configured")


class GradiumTTS:
    enabled = True
    media_type = "audio/wav"
    name = "gradium-tts"

    def __init__(self, api_key: str, base_url: str, default_voice: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._voice = default_voice
        self._client = client

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        body = {"text": text, "voice_id": voice or self._voice, "output_format": "wav", "only_audio": True}

        async def attempt() -> httpx.Response:
            return await self._client.post(f"{self._base_url}/post/speech/tts", json=body,
                                           headers={"x-api-key": self._api_key}, timeout=timeout(30))

        response = await with_retries(attempt)
        if response.status_code != 200:
            raise UpstreamError(self.name, response.status_code, response.text[:300])
        return response.content


class GradiumSTT:
    enabled = True
    name = "gradium-stt"

    def __init__(self, api_key: str, base_url: str, language: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._language = language
        self._client = client

    async def transcribe(self, audio: bytes, content_type: str = "audio/wav") -> str:
        params = {"json_config": json.dumps({"language": self._language})}

        async def attempt() -> httpx.Response:
            return await self._client.post(f"{self._base_url}/post/speech/asr", params=params, content=audio,
                                           headers={"x-api-key": self._api_key, "Content-Type": content_type},
                                           timeout=timeout(60))

        response = await with_retries(attempt)
        if response.status_code != 200:
            raise UpstreamError(self.name, response.status_code, response.text[:300])
        parts: list[str] = []
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "text" and message.get("text"):
                parts.append(str(message["text"]).strip())
            elif message.get("type") == "error":
                raise UpstreamError(self.name, 502, str(message.get("message", "transcription failed"))[:300])
        return " ".join(part for part in parts if part).strip()
