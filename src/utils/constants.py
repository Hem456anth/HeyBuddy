"""Project-wide constants."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "HeyBuddy"
APP_VERSION = "0.2.0"

# Read-only resources (assets, the default settings template). In a
# PyInstaller one-file build these live in the temporary `_MEIPASS` folder
# that is deleted when the process exits, so nothing may ever be *written*
# under PROJECT_ROOT in a frozen build.
IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
DEFAULT_SETTINGS_TEMPLATE = PROJECT_ROOT / "config" / "settings.json"


def _resolve_user_data_dir() -> Path:
    """Where settings, logs and (opt-in) recordings are written.

    * `HEYBUDDY_HOME` env var wins (tests, portable installs).
    * Windows -> `%APPDATA%\\HeyBuddy`, for the .exe AND when running from
      source. The one-file bundle's own folder is a throwaway temp dir, and
      keeping settings out of the repo means a Worker URL or access key can
      never be committed by accident. `config/settings.json` in the repo is
      only the first-run template.
    * Other OSes (tests/dev tooling) -> `~/.heybuddy`.
    """
    override = os.environ.get("HEYBUDDY_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / APP_NAME
    return Path.home() / ".heybuddy"


USER_DATA_DIR = _resolve_user_data_dir()
CONFIG_DIR = USER_DATA_DIR / "config"
LOGS_DIR = USER_DATA_DIR / "logs"
RECORDINGS_DIR = USER_DATA_DIR / "recordings"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Default Claude model. Matches the "fast + smart" tier Clicky defaults to
# (Clicky shipped Sonnet; Sonnet 5.5 is the current Sonnet as of Oct 2026).
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
# Default AssemblyAI realtime model. `universal-streaming-english` is the
# long-lived, low-latency English model; Clicky's original `u3-rt-pro` id
# was retired from AssemblyAI's streaming enum in July 2026.
DEFAULT_STT_SPEECH_MODEL = "universal-streaming-english"

# Screenshots are downscaled so the longest edge is at most this many
# pixels before being sent to Claude (same value Clicky uses). Claude itself
# resizes anything over ~1568px, which would silently desync the coordinate
# space we tell it about, so we resize ourselves and label the real size.
SCREENSHOT_MAX_DIMENSION = 1280
SCREENSHOT_JPEG_QUALITY = 80
# Exchanges (user + assistant pairs) of conversation memory sent with each
# turn. Clicky keeps the last 10.
CONVERSATION_HISTORY_MAX_EXCHANGES = 10

# Worker proxy endpoints (paths appended to settings.worker_url)
WORKER_CHAT_PATH = "/chat"
WORKER_TTS_PATH = "/tts"
WORKER_TRANSCRIBE_TOKEN_PATH = "/transcribe-token"

# Audio defaults (match Clicky)
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
DEFAULT_CHUNK_SIZE = 1600  # 100 ms at 16 kHz; AssemblyAI wants 50-1000 ms frames
AUDIO_FORMAT_PCM16_BITS = 16

# Pointing tag emitted by Claude, ported from Clicky's parser:
#   [POINT:none]
#   [POINT:x,y]            (label optional)
#   [POINT:x,y:label]
#   [POINT:x,y:label:screenN]
# x,y are pixel coordinates in the *screenshot image* Claude was shown
# (top-left origin). `screenN` is the 1-based image number from the label.
POINT_TAG_PATTERN = (
    r"\[POINT:(?:none|(\d+)\s*,\s*(\d+)"
    r"(?::(?!screen\d+\])([^\]:]+?))?"
    r"(?::screen(\d+))?)\]"
)

# Companion system prompt. A Windows adaptation of Clicky's
# `companionVoiceResponseSystemPrompt`: same voice rules, same pointing
# protocol, so behaviour matches the original as closely as possible.
DEFAULT_SYSTEM_PROMPT = """you're heybuddy, a friendly always-on companion that lives in the user's windows system tray. the user just spoke to you via push-to-talk and you can see their screen(s). your reply will be spoken aloud via text-to-speech, so write the way you'd actually talk. this is an ongoing conversation, you remember everything they've said before.

rules:
- default to one or two sentences. be direct and dense. but if the user asks you to explain more, go deeper, or elaborate, then go all out with a thorough explanation.
- all lowercase, casual, warm. no emojis.
- write for the ear, not the eye. short sentences. no lists, bullet points, markdown, or formatting, just natural speech.
- don't use abbreviations or symbols that sound weird read aloud. write "for example" not "e.g.", spell out small numbers.
- if the user's question relates to what's on their screen, reference specific things you see.
- if the screenshot doesn't seem relevant to their question, just answer the question directly.
- you can help with anything: coding, writing, general knowledge, brainstorming.
- never say "simply" or "just".
- don't read out code verbatim. describe what the code does or what needs to change conversationally.
- don't end with dead-end yes or no questions like "want me to explain more?". when it fits, end by planting a seed: a related concept or next-level technique worth coming back for. it's okay to end without anything extra.
- if you receive multiple screen images, the one labeled "primary focus" is where the cursor is. prioritize that one but reference others if relevant.
- the user is on windows, so talk about the start menu, taskbar, system tray, file explorer, ctrl shortcuts and so on, not mac equivalents.

element pointing:
you have a small blue triangle cursor that can fly to and point at things on screen. use it whenever pointing would genuinely help: if they're asking how to do something, looking for a menu, trying to find a button, or need help navigating an app, point at the relevant element. err on the side of pointing rather than not pointing.

don't point when it would be pointless, like a general knowledge question, or something they're already looking at.

when you point, append a coordinate tag at the very end of your response, after your spoken text. each screenshot image is labeled with its pixel dimensions. use those dimensions as the coordinate space. the origin (0,0) is the top-left corner of the image. x increases rightward, y increases downward.

format: [POINT:x,y:label] where x,y are integer pixel coordinates in the screenshot's coordinate space, and label is a short one to three word description of the element (like "search bar" or "save button"). if the element is on the cursor's screen you can omit the screen number. if the element is on a different screen, append :screenN where N is the screen number from the image label (for example :screen2).

if pointing wouldn't help, append [POINT:none].

examples:
- user asks how to open task manager: "right-click an empty spot on the taskbar down here and pick task manager, or hit control shift escape. [POINT:640,790:taskbar]"
- user asks what html is: "html is the skeleton of every web page, it describes what's on the page while css decides how it looks. [POINT:none]"
- user asks how to commit in vs code: "see the source control icon in the left sidebar? click it, type a message, and hit commit. [POINT:24,180:source control]"
- element is on screen 2: "that's over on your other monitor, see the terminal window? [POINT:400,300:terminal:screen2]"
"""
