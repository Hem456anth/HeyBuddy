# CLAUDE.md — HeyBuddy source of truth

Canonical reference for HeyBuddy's architecture, file layout, conventions and
Worker contract. Read this first; update it in the same change when you alter
anything it documents (see *Self-update* at the bottom).

HeyBuddy is the Windows port of [farzaa/clicky](https://github.com/farzaa/clicky).
`docs/REVERSE_ENGINEERING.md` explains how every Clicky subsystem maps here.

## Overview

A system-tray app (no taskbar button). The tray icon opens a frameless
floating panel. Holding **Ctrl+Alt** (low-level keyboard hook) streams mic
PCM to AssemblyAI realtime v3. On release, every monitor is captured
(≤1280 px JPEG, labeled with its pixel size) and sent with the transcript and
the last 10 exchanges to Claude through the Cloudflare Worker (`/chat`, SSE).
Claude replies in speakable text and may append `[POINT:x,y:label:screenN]`
in screenshot-pixel space; the reply is voiced with ElevenLabs (`/tts`) while a
blue triangle buddy, which normally rides next to the mouse, flies to each
point, shows a label bubble and flies back. The client holds **no API keys**.

## State machine (`src/core/companion_manager.py`)

```
IDLE ──press──▶ LISTENING ──release──▶ PROCESSING ──audio starts──▶ RESPONDING ──audio ends──▶ IDLE
LISTENING ──other key pressed with chord──▶ IDLE          (cancel)
LISTENING ──no speech──▶ IDLE
PROCESSING/RESPONDING ──press──▶ LISTENING               (interrupt: TTS stops, pointing cancels)
ANY ──error──▶ ERROR ──▶ IDLE                            (error banner)
```

Every turn gets a generation number; results from a superseded generation
are dropped. History holds completed exchanges only.

## Threading model

| Thread | Owner | Purpose |
| --- | --- | --- |
| `main` | Qt | UI, overlay frame clock (16 ms QTimer), all widget mutation |
| `Win32HookPump` | `utils.win32.LowLevelKeyboardHook` | `WH_KEYBOARD_LL` + `GetMessageW`; press/release/cancel callbacks |
| sounddevice input callback | `core.audio_recorder` | PCM blocks (100 ms) → STT socket (buffered until open) + level meter |
| `StartCapture` | `CompanionManager` | starts mic, fetches token, opens STT websocket |
| `AssemblyAIReader` | `api.assemblyai_streaming` | `WebSocketApp.run_forever`, Turn messages |
| `TurnPipeline` / `TypedTurn` | `CompanionManager` | finalize STT, capture screens, Claude SSE, TTS download |
| `AudioPlayer` | `core.audio_player` | ffmpeg MP3 decode + private `sd.OutputStream` |
| `PystrayThread` | pystray | tray loop; menu callbacks marshal to Qt via `QMetaObject.invokeMethod` |

Rules: never touch a QWidget off the main thread (emit a signal); never sleep
on the Qt thread; all `ctypes` lives in `src/utils/win32.py`.

## Coordinates (the part that is easy to get wrong)

* The process is **per-monitor DPI aware v2** (set before any HWND exists).
  `GetCursorPos`, monitor rectangles and mss captures are all **physical
  desktop pixels**.
* Claude sees each monitor downscaled to ≤1280 px and answers in **image
  pixels**. `CapturedScreen.image_point_to_physical()` maps back.
* Screens are numbered per turn, **cursor screen first** (Clicky's order);
  an omitted `:screenN` means the cursor screen.
* The overlay is **one window per QScreen**. Motion runs in physical pixels
  (`core/buddy_motion.py`); each window converts with
  `(physical − monitor.topLeft) / devicePixelRatio`. Lengths Clicky gives in
  points (offsets, arc height, cancel distance) are multiplied by the DPI
  scale of the monitor they apply to.

## Worker contract (`worker/src/index.ts`, wire-compatible with upstream)

| Route | Body | Response |
| --- | --- | --- |
| `POST /chat` | Anthropic Messages JSON (`model`, `system`, `messages`, `max_tokens`, `stream: true`) | Anthropic SSE passthrough |
| `POST /tts` | `{"text": "..."}` (≤5,000 chars; no `voice_id`, Worker injects `ELEVENLABS_VOICE_ID`) | `audio/mpeg` |
| `POST /transcribe-token` | empty | `{"token": "..."}` (AssemblyAI, 480 s) |

Optional hardening, all off unless set: `APP_SHARED_SECRET` (client header
`X-HeyBuddy-Key`), `ALLOWED_MODELS`, `MAX_TOKENS_CAP`. Non-POST → 405.

## POINT protocol

```
[POINT:none]  [POINT:x,y]  [POINT:x,y:label]  [POINT:x,y:label:screenN]
```
Regex: `utils/constants.py::POINT_TAG_PATTERN`. Tags are stripped before
speaking/display; a half-streamed tag is hidden from the panel.

## File layout

```
HeyBuddy/
├── CLAUDE.md, README.md, SECURITY.md, LICENSE
├── docs/REVERSE_ENGINEERING.md       ← Clicky teardown + mapping to Windows
├── requirements-desktop.txt / requirements-dev.txt
├── heybuddy.spec, version_info.txt, build.bat   ← PyInstaller one-file build + self-check
├── .github/workflows/windows-build.yml          ← tests, build, self-check exe, release
├── config/settings.json              ← first-run template only (live: %APPDATA%\HeyBuddy)
├── assets/heybuddy.ico|png
├── worker/                           ← Cloudflare Worker (+ root wrangler.toml/package.json for auto-detect)
├── tests/                            ← pytest; fake_win32.py stands in off-Windows
└── src/
    ├── main.py                       ← bootstrap, tray, wiring, --selfcheck
    ├── api/
    │   ├── cloudflare_proxy.py       ← HTTP + SSE transport, UTF-8, access key
    │   ├── claude_client.py          ← payload (multi-image, history cap), SSE deltas, POINT parsing
    │   ├── assemblyai_streaming.py   ← v3 WS session + TurnTranscriptAssembler
    │   ├── elevenlabs_client.py      ← /tts wrapper
    │   └── transcription_provider.py ← batch interface (unused fallback surface)
    ├── core/
    │   ├── companion_manager.py      ← state machine, turn generations, point resolution
    │   ├── buddy_motion.py           ← pure-Python Clicky motion model (follow/fly/point/return)
    │   ├── screen_capture.py         ← all monitors, cursor-first, ≤1280 JPEG, labels
    │   ├── audio_recorder.py         ← sounddevice PCM16 16 kHz mono
    │   ├── audio_player.py           ← bundled ffmpeg decode, private output stream
    │   ├── hotkey_monitor.py         ← chord → press/release/cancel
    │   └── chimes.py                 ← start/stop tones
    ├── models/
    │   ├── message.py                ← Message, PointMarker, CapturedScreen
    │   └── config.py                 ← AppConfig (tolerant load, atomic save)
    ├── ui/
    │   ├── overlay_window.py         ← per-screen click-through buddy overlay
    │   ├── main_window.py            ← floating panel (chat, status, waveform meter)
    │   ├── settings_panel.py         ← settings dialog + diagnostics
    │   └── theme.py                  ← design tokens
    ├── tools/
    │   ├── selfcheck.py              ← end-to-end self-test with a mock Worker
    │   └── smoke_test.py             ← hotkey + mic only
    └── utils/
        ├── constants.py              ← paths, defaults, system prompt, POINT regex
        ├── logger.py                 ← rotating file (+ console when one exists)
        ├── permissions.py            ← mic test + Worker ping
        └── win32.py                  ← ALL ctypes: DPI, monitors, hook, styles, capture exclusion, mutex, autostart, DWM
```

## Configuration (`%APPDATA%\HeyBuddy\config\settings.json`)

| key | default | meaning |
| --- | --- | --- |
| `worker_url` | placeholder | Worker base URL (`https://` added if missing) |
| `worker_shared_secret` | `""` | sent as `X-HeyBuddy-Key` |
| `hotkey` | `ctrl+alt` | preset or captured `vk:` chord |
| `model` | `claude-sonnet-5-5` | Anthropic model id |
| `tts_enabled` / `screen_capture_enabled` | `true` | voice / screenshots |
| `show_cursor_buddy` | `true` | buddy always visible (else only during turns) |
| `transient_cursor_mode` | `false` | panel hides while listening |
| `save_recordings` | `false` | keep `.wav` per question |
| `transcription.speech_model` | `universal-streaming-english` | AssemblyAI model |
| `audio.chunk_size` | `1600` | samples per block (100 ms) |

`HEYBUDDY_HOME` overrides the data directory (tests, portable use).

## Running, testing, building

```powershell
pip install -r requirements-dev.txt
python -m pytest -q                      # tests (Linux or Windows)
python -m src.main                       # run
python -m src.main --selfcheck           # end-to-end self-test, exit code
build.bat                                # tests + PyInstaller + self-check of the exe
```

## Conventions

* Clarity over concision; descriptive names; no single-letter names.
* `from __future__ import annotations` and type hints everywhere.
* Comments explain *why* (Win32 quirks, threading constraints).
* No `print` in app code (use `utils.logger`); no `time.sleep` on the Qt thread.
* No `ctypes`/pywin32 outside `src/utils/win32.py`.
* UI colors/radii/spacing come from `src/ui/theme.py` (the overlay uses
  Clicky's exact `#3380FF`, defined at the top of `overlay_window.py`).
* Pure logic (parsing, mapping, motion) stays Qt- and Win32-free so it is
  unit-testable on any OS.
* Never ship API keys; everything goes through the Worker.

## Self-update

When you add/remove a file under `src/`, change the state machine, threading,
Worker contract, coordinate rules or config keys, update the matching
section above in the same commit.
