"""Speech-to-text providers.

A `TranscriptionProvider` consumes raw PCM-16 audio bytes (mono, 16 kHz by
default) plus a sample rate and returns the recognized text. The interface is
deliberately small so realtime streaming providers can be slotted in later.
"""
from __future__ import annotations

import io
import time
import wave
from abc import ABC, abstractmethod

import requests

from ..utils.logger import get_logger

log = get_logger(__name__)


class TranscriptionProvider(ABC):
    @abstractmethod
    def transcribe(self, pcm16_bytes: bytes, sample_rate: int) -> str:
        ...


def pcm16_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw 16-bit PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class AssemblyAIProvider(TranscriptionProvider):
    """Batch (non-realtime) AssemblyAI transcription.

    Flow:
        1. Upload the WAV bytes -> upload_url
        2. POST /transcript referencing upload_url
        3. Poll /transcript/{id} until status == 'completed'

    The API key is supplied via a short-lived token from the Worker proxy. In
    Phase 1 we assume the user pasted a real AssemblyAI key into config; once
    the Worker exposes a streaming token endpoint we'll swap to realtime.
    """

    BASE = "https://api.assemblyai.com/v2"

    def __init__(self, api_key: str, language: str = "en", poll_interval: float = 1.0) -> None:
        self.api_key = api_key
        self.language = language
        self.poll_interval = poll_interval
        self._headers = {"authorization": api_key}

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int) -> str:
        if not self.api_key:
            raise RuntimeError("AssemblyAI API key is not configured")
        wav = pcm16_to_wav(pcm16_bytes, sample_rate)

        log.debug("Uploading %d bytes of audio to AssemblyAI", len(wav))
        up = requests.post(
            f"{self.BASE}/upload",
            headers=self._headers,
            data=wav,
            timeout=60,
        )
        up.raise_for_status()
        audio_url = up.json()["upload_url"]

        body = {"audio_url": audio_url, "language_code": self.language}
        start = requests.post(
            f"{self.BASE}/transcript",
            headers=self._headers,
            json=body,
            timeout=30,
        )
        start.raise_for_status()
        transcript_id = start.json()["id"]

        # Poll until done
        poll_url = f"{self.BASE}/transcript/{transcript_id}"
        while True:
            poll = requests.get(poll_url, headers=self._headers, timeout=30)
            poll.raise_for_status()
            data = poll.json()
            status = data.get("status")
            if status == "completed":
                return (data.get("text") or "").strip()
            if status == "error":
                raise RuntimeError(f"AssemblyAI error: {data.get('error')}")
            time.sleep(self.poll_interval)


class NullProvider(TranscriptionProvider):
    """Used in tests / when STT is disabled."""

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int) -> str:
        return ""
