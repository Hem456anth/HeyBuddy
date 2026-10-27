# CLAUDE.md — HeyBuddy source of truth

Canonical reference for HeyBuddy's architecture, file layout, conventions and
Worker contract. Read this first; update it in the same change when you alter
anything it documents (see *Self-update* at the bottom).

HeyBuddy is the Windows port of [farzaa/clicky](https://github.com/farzaa/clicky),
extended in 0.3 with the core behaviour of **HeyClicky for Windows** (reply
programs that draw on screen, guided click-through steps, tools, background
jobs, notch, quick-ask, dictation), rebuilt clean-room.
`docs/REVERSE_ENGINEERING.md` maps Clicky; `docs/HEYCLICKY_TEARDOWN.md` maps
HeyClicky and lists what was and wasn't reproduced.

## Overview

A system-tray app (no taskbar button). The tray icon opens a frameless
floating panel. Holding **Ctrl+Alt** (low-level keyboard hook) streams mic
PCM to AssemblyAI realtime v3. On release, every monitor is captured
(≤1280 px JPEG, labeled with its pixel size) and sent with the transcript and
the last 10 exchanges to Claude through the Cloudflare Worker (`/chat`, SSE).
Claude answers with a **buddy program** (`say("...")` lines that own marks
such as `ring`, `arrow`, `target`; see *Buddy program*), may call local
**tools** (Anthropic tool use, executed client-side with approvals), and may
hand longer work to a **background job**. Each `say()` line is voiced with
ElevenLabs (`/tts`) and its marks are drawn on a click-through overlay while
it plays; `point()` makes the blue triangle buddy fly there. A `target()`
box waits for the user's click and then starts the next step automatically.
The client holds **no API keys**.

**Telugu mode** (0.4, `language_mode: "telugu"`, tray → Language): STT is
Gnani realtime (`te-IN`) via the Worker's `/gnani/stt-stream` WebSocket
(REST `/gnani/stt` retry if it comes back empty), the system prompt gets
`TELUGU_SYSTEM_PROMPT_ADDENDUM`, Claude's stream is cut into `say()` lines as
it arrives (`core/speech_chunker.py`), each line is normalised
(`core/telugu_normalizer.py`) and spoken with Gnani SSE TTS as raw PCM that
plays from the first chunk. English mode keeps the old path unchanged. See
`docs/TELUGU_MODE.md`.

Other surfaces: the **notch** (black pill, top centre; listening/thinking/
speaking strip; hover opens the panel), the **quick-ask** box (double-tap
Ctrl), **dictation** (hold Ctrl+Win, text typed at the caret), the panel's
**Jobs** tab, and an **Inno Setup** installer.

## State machine (`src/core/companion_manager.py`)

```
IDLE ──press──▶ LISTENING ──release──▶ PROCESSING ──audio starts──▶ RESPONDING ──audio ends──▶ IDLE
LISTENING ──other key pressed with chord──▶ IDLE          (cancel)
LISTENING ──no speech──▶ IDLE
LISTENING ──released before 0.5 s──▶ IDLE                (accidental brush)
PROCESSING/RESPONDING ──press / Esc──▶ LISTENING / IDLE  (interrupt: speech queue + drawings cancel)
IDLE ──Ctrl+Win held──▶ LISTENING (dictating) ──release──▶ PROCESSING ──paste at caret──▶ IDLE
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
| `GnaniSttReader` / `GnaniSttSender` / `GnaniSttClose` | `api.gnani_streaming` | Telugu mode: WS reader; 1,024-byte frames paced at 32 ms (2x when backlogged); background close after `stop()` |
| `TurnPipeline` / `TypedTurn` | `CompanionManager` | finalize STT, capture screens, Claude SSE rounds + tool execution |
| `BeatSpeaker` + `TTS` pool | `core.beat_speaker` | per-line TTS with prefetch, sequential playback, beat-start callbacks; in Telugu mode the pool fills `PcmStream` buffers from Gnani SSE |
| `Job-<id>` | `core.jobs.JobManager` | background job tool loop (one thread per job) |
| `ClipboardRestore` | `utils.win32` | puts the user's clipboard back after a paste |
| `AudioPlayer` / `AudioPlayerStream` | `core.audio_player` | ffmpeg MP3 decode + private `sd.OutputStream`; `play_pcm_stream` plays PCM chunks as they arrive |
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

Telugu mode (0.4), Gnani Vachana, secret `GNANI_API_KEY` (routes answer 503
without it), vars `GNANI_TTS_VOICE` (default `Varuni`), `GNANI_TTS_MODEL`
(default `timbre-v2.5`):

| Route | Body | Response |
| --- | --- | --- |
| `GET /gnani/stt-stream` (WebSocket) | headers `x-heybuddy-lang` (→ `lang_code`, default `te-IN`), `x-sample-rate`, VAD headers; binary PCM16 frames of 1,024 bytes | Gnani JSON events relayed (`connected`, `processing`, `transcript`, `error`) |
| `POST /gnani/stt` | WAV bytes ≤ 60 s, header `x-heybuddy-lang` | Gnani REST JSON (`transcript`) |
| `POST /gnani/tts` | `{text, language?, speed?, audio_config?}` (no voice/model) | audio bytes (container from `audio_config`) |
| `POST /gnani/tts-stream` | same | Gnani SSE (`start`, `chunk` base64 audio, `complete`, `error`) |

Optional hardening, all off unless set: `APP_SHARED_SECRET` (client header
`X-HeyBuddy-Key`, also on the WebSocket upgrade), `ALLOWED_MODELS`,
`MAX_TOKENS_CAP`. Non-POST → 405, except `GET /gnani/stt-stream`.

## Buddy program (`src/core/scene_program.py`)

The model answers with one fenced block (or a bare program starting with
`say(`/`step(`, as HeyClicky's brain does):

```buddy
say("click export up here")
ring(1150, 30, 70, 28, "export")
say("then pick pdf")
target(900, 120, 300, 40, "PDF")
```

* Parsed with `ast` (calls with literal args only; nothing is executed).
  JS habits are accepted: `true/false/null`, trailing `{straight: true}`,
  semicolons, `step(n)`.
* Each `say()` starts a **Beat**; marks attach to the latest beat and appear
  when its audio starts (`beat_started` signal → `overlay.show_beat`).
* Marks: `point ring frame highlight underline arrow num write trace target`.
  Control: `clear/startOver/erase` (fade before next beat), `noted` (memory),
  `seen`, `task` (notch title), `plan`.
* Coordinates are image pixels of the screenshot; `screen=N` picks image N,
  default cursor screen. `resolve_marks()` fills `mark.physical`.
* Legacy `[POINT:x,y:label:screenN]` tags still parse (one beat).
* `target()` click → `overlay.target_clicked` → after 0.7 s
  `manager.continue_after_target_click()` sends an `[app] the user clicked …`
  turn with a fresh screenshot.

## Tools and jobs (`src/core/tools.py`, `src/core/jobs.py`)

* Voice turns get `VOICE_TOOLS`, jobs get `JOB_TOOLS` (Anthropic `tools`
  schema). `claude_client.run_tool_loop()` streams a round, executes
  `tool_use` blocks, sends `tool_result`s, repeats (voice ≤ 6 rounds, jobs ≤ 30).
  Text of every round is spoken as it arrives.
* `run_command` (PowerShell) and writes outside the working folder/jobs dir
  go through `config.command_policy` / `write_policy` (`ask|allow|deny`);
  "ask" uses `ui/approval_dialog.ApprovalBroker` (blocks the worker thread,
  120 s timeout = no). `is_sensitive_path()` blocks credential stores, SSH/
  cloud keys, browser profiles, `.env`, HeyBuddy's own settings.
* Jobs live in `%APPDATA%\HeyBuddy\jobs\<slug>-<id>\{output,tmp,AGENTS.md}`,
  listed in `jobs.json`; the final message's `ARTIFACTS:` block and
  `add_artifact` calls register files shown in the Jobs tab.

## File layout

```
HeyBuddy/
├── CLAUDE.md, README.md, SECURITY.md, LICENSE
├── THIRD_PARTY_NOTICES.md            ← dependency inventory + licenses
├── docs/REVERSE_ENGINEERING.md       ← Clicky teardown + mapping to Windows
├── docs/HEYCLICKY_TEARDOWN.md        ← HeyClicky for Windows teardown + parity table
├── docs/TESTING.md                   ← test plan and latest results
├── docs/TELUGU_MODE.md               ← Telugu mode (Gnani): setup, endpoints, live numbers, limits
├── scripts/gnani_smoke.py            ← live Gnani smoke test (needs GNANI_API_KEY; never in CI)
├── installer/heybuddy.iss            ← Inno Setup per-user installer
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
    │   ├── claude_client.py          ← payload (multi-image, history cap), SSE rounds, tool loop
    │   ├── assemblyai_streaming.py   ← v3 WS session + TurnTranscriptAssembler
    │   ├── elevenlabs_client.py      ← /tts wrapper
    │   ├── gnani_streaming.py        ← Telugu mode: Gnani realtime STT session + REST fallback
    │   ├── gnani_tts.py              ← Telugu mode: Gnani SSE/REST TTS client (normalises text)
    │   └── transcription_provider.py ← batch interface (unused fallback surface)
    ├── core/
    │   ├── companion_manager.py      ← state machine, turns, tools, dictation, target follow-ups
    │   ├── scene_program.py          ← buddy program parser + mark resolution
    │   ├── beat_speaker.py           ← per-line speech queue, beat timing (MP3 or streamed PCM)
    │   ├── speech_chunker.py        ← cuts Claude's stream into say() lines / sentences (Telugu mode)
    │   ├── telugu_normalizer.py     ← digits/times/₹/% → spoken Telugu; strips markup before TTS
    │   ├── latency.py               ← per-turn stage timings (logs/latency.log, no content)
    │   ├── tools.py                  ← local tools + safety rules
    │   ├── jobs.py                   ← background jobs (JobManager)
    │   ├── memory_store.py           ← local memory.md
    │   ├── buddy_motion.py           ← pure-Python Clicky motion model (follow/fly/point/return)
    │   ├── screen_capture.py         ← all monitors, cursor-first, ≤1280 JPEG, labels
    │   ├── audio_recorder.py         ← sounddevice PCM16 16 kHz mono
    │   ├── audio_player.py           ← bundled ffmpeg decode, private output stream
    │   ├── hotkey_monitor.py         ← PTT + dictation chords, double-tap Ctrl, Esc (GestureTracker)
    │   └── chimes.py                 ← start/stop tones
    ├── models/
    │   ├── message.py                ← Message, PointMarker, CapturedScreen
    │   └── config.py                 ← AppConfig (tolerant load, atomic save)
    ├── ui/
    │   ├── overlay_window.py         ← per-screen click-through buddy + ink overlay, target clicks
    │   ├── ink_layer.py              ← mark painting + timings (ring, frame, arrow, target…)
    │   ├── notch_window.py           ← top-centre activity pill
    │   ├── quick_ask.py              ← double-tap Ctrl text box
    │   ├── approval_dialog.py        ← thread-safe approval prompts
    │   ├── jobs_view.py              ← Jobs tab
    │   ├── main_window.py            ← floating panel (Chat | Jobs tabs, status, waveform meter)
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
| `tools_enabled` | `true` | model may call local tools |
| `command_policy` / `write_policy` | `ask` | `ask`, `allow` or `deny` |
| `background_jobs_enabled` / `speak_job_results` | `true` | jobs + spoken result |
| `memory_enabled` | `true` | `memory.md` + `remember`/`noted` |
| `annotations_enabled` / `annotation_linger_seconds` | `true` / `12` | drawings and how long they stay |
| `guided_steps_enabled` | `true` | `target()` waits for the click and continues |
| `dictation_enabled` / `dictation_hotkey` | `true` / `ctrl+win` | hold to dictate |
| `quick_ask_enabled` | `true` | double-tap Ctrl box |
| `notch_enabled` | `true` | notch window (restart to apply) |
| `language_mode` | `english` | `english` or `telugu` (tray → Language) |
| `telugu.stt_lang_code` / `telugu.tts_language` | `te-IN` | Gnani STT session language / TTS `language` (`te-IN` or `auto`) |
| `telugu.min_silence_ms` | `400` | Gnani VAD silence closing a segment |
| `telugu.stream_speech` | `true` | speak each say() line as soon as it is written |
| `telugu.tts_speed` / `telugu.tts_sample_rate` | `1.0` / `24000` | Gnani TTS speed and PCM rate |

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
* Never ship API keys; everything goes through the Worker (Gnani too).
* Gnani usage follows docs.gnani.ai only (see `docs/TELUGU_MODE.md`); the
  Worker forwards only documented fields and headers.
* No HeyClicky code, prompts, scripts or assets: behaviour is reproduced from
  the teardown with original code.
* Every new capability that acts on the PC goes through `core/tools.py`
  and its policy gates.

## Self-update

When you add/remove a file under `src/`, change the state machine, threading,
Worker contract, coordinate rules or config keys, update the matching
section above in the same commit.
