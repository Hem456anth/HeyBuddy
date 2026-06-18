"""Project-wide constants."""
from __future__ import annotations

from pathlib import Path

APP_NAME = "HeyClicky"
APP_VERSION = "0.1.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
ASSETS_DIR = PROJECT_ROOT / "assets"
LOGS_DIR = PROJECT_ROOT / "logs"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Worker proxy endpoints (paths appended to settings.worker_url)
WORKER_CHAT_PATH = "/chat"
WORKER_TTS_PATH = "/tts"
WORKER_TRANSCRIBE_TOKEN_PATH = "/transcribe-token"

# Audio defaults (match Clicky)
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
DEFAULT_CHUNK_SIZE = 1024
AUDIO_FORMAT_PCM16_BITS = 16

# Pointing tag emitted by Claude: [POINT:x,y:label:screenN]
# `screenN` is optional so partial tags still parse; if absent we assume screen1.
POINT_TAG_PATTERN = r"\[POINT:(\d+),(\d+):([^:\]]+)(?::screen(\d+))?\]"

# System prompt that teaches Claude how to point at screen elements. Mirrors
# the upstream Clicky prompt: coordinates are in each monitor's *logical*
# pixel space (post-DPI scale) and we enumerate monitors with 1-based indexes.
DEFAULT_SYSTEM_PROMPT = (
    "You are HeyClicky, a friendly Windows desktop AI companion. "
    "You can see the user's screen via screenshots and help them learn. "
    "When you want to point at something on screen, include a tag of the form "
    "[POINT:x,y:label:screenN] where x,y are logical pixel coordinates inside "
    "that monitor (post-DPI scale), label is a short caption, and screenN is "
    "the 1-based monitor index from the screenshot caption (e.g. screen1). "
    "Keep spoken responses brief and conversational."
)
