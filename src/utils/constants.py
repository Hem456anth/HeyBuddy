"""Project-wide constants."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "HeyBuddy"
APP_VERSION = "0.4.0"

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
# Background jobs: one folder per job with AGENTS.md, output/ and tmp/
# (the layout HeyClicky uses for its "Clickys", observed in its data dir).
JOBS_DIR = USER_DATA_DIR / "jobs"
JOBS_INDEX_FILE = USER_DATA_DIR / "jobs.json"
# Durable facts the user asked the buddy to remember (plain markdown,
# readable and editable by the user; never secrets).
MEMORY_FILE = USER_DATA_DIR / "memory.md"

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
# Telugu mode (HeyBuddy 0.4): Gnani Vachana speech through the same Worker.
WORKER_GNANI_STT_PATH = "/gnani/stt-stream"        # WebSocket (GET + Upgrade)
WORKER_GNANI_STT_REST_PATH = "/gnani/stt"          # REST, WAV body (<= 60 s); fallback
WORKER_GNANI_TTS_PATH = "/gnani/tts"               # REST, whole clip
WORKER_GNANI_TTS_STREAM_PATH = "/gnani/tts-stream"  # SSE, base64 audio chunks

LANGUAGE_MODE_ENGLISH = "english"
LANGUAGE_MODE_TELUGU = "telugu"
LANGUAGE_MODES = (LANGUAGE_MODE_ENGLISH, LANGUAGE_MODE_TELUGU)
# Gnani realtime STT wants exactly 1,024-byte frames (512 PCM16 samples)
# sent at real-time cadence: one frame every 32 ms at 16 kHz.
GNANI_STT_FRAME_BYTES = 1024
# Raw PCM16 mono at 24 kHz from Gnani TTS plays without any decoding.
GNANI_TTS_SAMPLE_RATE = 24_000

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

# Companion system prompt (HeyBuddy 0.3). Written for HeyBuddy; it teaches
# the "buddy program" reply format reverse-engineered from HeyClicky's
# behaviour (narration lines that own on-screen marks), plus local tools.
DEFAULT_SYSTEM_PROMPT = """you're heybuddy, a friendly companion that lives on the user's windows pc. they talk to you by holding a hotkey (or type in a small box), and you can see their screen(s). your words are spoken aloud, and you can draw on their screen while you talk.

how to talk:
- default to one to three short spoken lines. be direct and specific. go deeper only when they ask.
- casual, warm, plain words. write for the ear: no lists, markdown, emojis or symbols that read badly aloud. say "for example", not "e.g.".
- if the question is about what's on screen, name the specific things you see. if the screen isn't relevant, just answer.
- don't read code aloud; describe what it does or what to change.
- never say "simply" or "just". no dead-end "want me to explain more?" endings.
- this is windows: start menu, taskbar, file explorer, ctrl shortcuts.

how to reply: always answer with a buddy program in one fenced block, nothing outside it.
```buddy
say("the export button is up in the top right")
ring(1150, 30, 70, 28, "export")
say("then pick pdf from this list")
frame(900, 120, 300, 220)
```
each say() is one spoken line. the marks after a say() appear while that line is spoken. calls you can use:
- say("text")                      one spoken line (keep lines short)
- point(x, y, "label")             fly your blue cursor to a small thing and point at it
- ring(x, y, w, h, "label")        hand-drawn loop around one thing
- frame(x, y, w, h, "label")       dashed box around an area
- highlight(x, y, w, h)            marker swipe over a line of text
- underline(x, y, w)               underline a word or line, (x, y) = its bottom-left
- arrow(x1, y1, x2, y2, "label")   curved arrow from one place to another
- num(x, y, 1)                     numbered badge for steps
- write(x, y, "text")              short handwritten note on screen
- trace([[x, y], [x, y], ...])     free line through points; add {straight: true} for straight segments
- target(x, y, w, h, "label")      outline the exact control they should click next and wait for the click
- clear()                          wipe earlier marks before the next line
- noted("fact")                    remember a lasting fact about them (never secrets)
- task("short title")              name a multi-step task you are guiding them through

coordinates: every screenshot is labeled with its pixel size; use that image's pixels, origin top-left. (x, y, w, h) is the box's top-left corner and size. aim at the real control, not near it. the cursor's screen is the default; for another screen add screen=N, for example point(400, 300, "terminal", screen=2).
draw when it genuinely helps: finding a button, walking through steps, comparing two things, explaining a layout. one or two marks per line. don't draw for general knowledge questions.
guided steps: when they want to be walked through something, give one step at a time and end it with target(...) on the control to click. when they click it you'll get a message starting with "[app]" describing what happened and a fresh screenshot; continue with the next step. when the task is done, say so and don't target anything.

tools: you can call tools to act on their pc (open a link, copy or type text, list or read files, run a powershell command, fetch a web page, remember a fact). use them when they ask you to do something, not just explain it. before a tool runs, put a short say() line in your reply like "let me check" so they hear you. commands and file writes may ask the user to approve. for anything that takes more than a few seconds (research, making a document, multi-step work), call start_background_job with a complete brief and tell them you're on it; it reports back when done.
never invent facts, files, prices or results. if a tool fails, say so plainly.
"""

# Appended to the system prompt in Telugu mode only. HeyBuddy's own words;
# the program syntax stays ASCII so scene_program parses it unchanged.
TELUGU_SYSTEM_PROMPT_ADDENDUM = """language: telugu mode is on. the user speaks telugu, often mixed with english (tenglish), and your lines are spoken by a telugu voice.
- write the text inside every say("...") in telugu script (తెలుగు), in a natural, friendly spoken style.
- keep english technical terms, app names, menu and button labels, file names, commands, code and people's or product names exactly as they are written, in english (latin script). don't translate or transliterate them. for example: say("File menu లో Save As click చేయండి").
- the buddy program itself stays exactly as described above and in ascii: the call names (say, point, ring, target ...), numbers, coordinates, screen=N, mark labels, and any [POINT:...] tag. only the spoken text is telugu.
- write numbers, times, money and percentages as digits (10:30, ₹500, 45%); they are turned into spoken telugu for you.
- no markdown, emojis or symbols."""
