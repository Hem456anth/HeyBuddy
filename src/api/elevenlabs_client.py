"""ElevenLabs text-to-speech via the Cloudflare Worker."""
from __future__ import annotations

from ..utils.logger import get_logger
from .cloudflare_proxy import CloudflareProxy

log = get_logger(__name__)


class ElevenLabsClient:
    """ElevenLabs TTS via the Worker.

    The voice is selected by the Worker via its `ELEVENLABS_VOICE_ID` env var.
    The client has no knob to override it — changing the voice means
    redeploying the Worker. See `CLAUDE.md` Worker contract.
    """

    def __init__(self, proxy: CloudflareProxy) -> None:
        self.proxy = proxy

    def synthesize(self, text: str) -> bytes:
        """Return MP3 bytes for the given text."""
        if not text.strip():
            return b""
        log.debug("Requesting TTS for %d chars", len(text))
        return self.proxy.tts(text)
