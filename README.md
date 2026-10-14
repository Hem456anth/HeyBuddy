# HeyBuddy

A native Windows port of [farzaa/clicky](https://github.com/farzaa/clicky): an
AI buddy that lives in your system tray, sees your screen, listens while you
hold **Ctrl+Alt**, answers out loud, and flies a little blue cursor to the
button you're looking for.

![states: following, listening, thinking, pointing](docs/buddy-states.png)

> Credits: a Windows reimplementation of @farzaa's macOS Clicky (MIT). The
> Cloudflare Worker is wire-compatible with upstream.

## What it does

1. Hold **Ctrl+Alt** anywhere and ask a question ("how do I turn on dark mode here?").
   The blue triangle next to your mouse turns into a live waveform.
2. Let go. HeyBuddy captures every monitor and sends your question and the
   screenshots to Claude. The triangle becomes a spinner.
3. Claude answers out loud (ElevenLabs). If something on screen matters, the
   buddy swoops over to it, shows a little label, then flies back.

Press Ctrl+Alt again at any time to interrupt and ask something else.
Pressing any other key while holding the chord (Ctrl+Alt+Del, AltGr
characters) cancels instead of sending.

## Requirements

* Windows 10 version 2004 (build 19041) or later, or Windows 11, x64
* A microphone
* A free Cloudflare account plus API keys for
  [Anthropic](https://console.anthropic.com),
  [AssemblyAI](https://www.assemblyai.com) and
  [ElevenLabs](https://elevenlabs.io)
* For building from source: Python 3.11–3.13 x64 and Node.js 18+ (Worker only)

## 1. Deploy the Worker (holds your API keys)

```bash
cd worker
npm install
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put ASSEMBLYAI_API_KEY
npx wrangler secret put ELEVENLABS_API_KEY
# voice: edit ELEVENLABS_VOICE_ID in worker/wrangler.toml
npx wrangler deploy
```

Optional but recommended (see [SECURITY.md](SECURITY.md)):

```bash
npx wrangler secret put APP_SHARED_SECRET   # paste the same value into HeyBuddy → Settings → Worker access key
```

Copy the deployed URL, e.g. `https://heybuddy-proxy.you.workers.dev`.

## 2a. Run the prebuilt app

Download `HeyBuddy-windows-x64.zip` from the repository's **Releases** (or the
latest *Windows build* Actions run), unzip, run `HeyBuddy.exe`.
Windows SmartScreen may warn because the exe is unsigned: *More info → Run anyway*.

## 2b. Or run from source

```powershell
git clone https://github.com/Hem456anth/HeyBuddy.git
cd HeyBuddy
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-desktop.txt
python -m src.main          # or pythonw -m src.main for no console
```

## 3. First launch

The panel opens and the blue buddy appears next to your mouse.
Click **⚙ Settings**, paste the Worker URL, then:

* **Ping** — checks the Worker and your AssemblyAI key
* **Test microphone** — checks your input level

Hold **Ctrl+Alt**, talk, release.

Settings, logs and (optional) recordings live in `%APPDATA%\HeyBuddy`
(tray menu → *Open Logs and Settings Folder*).

### Tray menu

| Item | Does |
| --- | --- |
| Open | show/hide the panel (chat history, text box) |
| Toggle Companion | mute the hotkey (e.g. during meetings) |
| Show Buddy Cursor | buddy always visible, or only during a turn |
| Clear Conversation | forget the last 10 exchanges |
| Settings | model, hotkey, mic, voice, autostart, Worker access key |
| Quit | exit |

## Build the .exe

```powershell
build.bat
```

Creates a venv, installs `requirements-dev.txt`, runs the tests, builds
`dist\HeyBuddy.exe` with PyInstaller (one file, no console, ~60 MB including
Qt and ffmpeg), then launches it with `--selfcheck` and fails the build if the
self-check fails.

CI does the same on every push (`.github/workflows/windows-build.yml`) and
attaches the zip to a GitHub Release when you push a `v*` tag.

## Self-check (works without API keys)

```powershell
HeyBuddy.exe --selfcheck          # or: python -m src.main --selfcheck
```

Runs the real pipeline against a local mock Worker: monitor enumeration,
multi-screen capture, Claude SSE parsing, POINT → pixel mapping, MP3 decode,
overlay window styles, and two real screen grabs proving the buddy is drawn
on screen but hidden from capture. Results:
`%APPDATA%\HeyBuddy\selfcheck\selfcheck.json` (+ PNG evidence). Exit code 0 = pass.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

31 tests cover the POINT grammar, coordinate mapping, multi-turn
transcripts, the motion model, config robustness, an end-to-end typed turn
against the mock Worker, and (off-Windows) offscreen rendering of every
buddy state.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Worker URL is not set" | Settings → paste your `*.workers.dev` URL |
| Ping: 401 | Worker has `APP_SHARED_SECRET`; paste it into *Worker access key* |
| Ping OK, Claude fails 400 "model" | pick a current model in Settings, or check `ALLOWED_MODELS` on the Worker |
| No voice, text appears | Worker missing `ELEVENLABS_API_KEY` / voice id; "Voice unavailable" banner shows the reason |
| Hotkey does nothing | another app grabs Ctrl+Alt: choose another chord in Settings; check *Toggle Companion* is on |
| Typing AltGr characters starts a turn | it now cancels automatically; or switch the chord to Right Alt / Ctrl+Shift |
| Buddy points at the wrong place | check `DPI awareness: per-monitor-v2` in `heybuddy.log`; send the log's `POINT flight:` lines with an issue |
| Buddy appears in screenshots | Windows older than 2004 can't exclude windows from capture; harmless |
| Two buddies | only one instance runs now; kill stray `pythonw.exe` from older versions |

## Docs

* [docs/REVERSE_ENGINEERING.md](docs/REVERSE_ENGINEERING.md) — how Clicky works and how each part maps to Windows
* [CLAUDE.md](CLAUDE.md) — architecture, threading, file map, conventions (for contributors and coding agents)
* [SECURITY.md](SECURITY.md) — data flow, storage, Worker hardening

## License

MIT — see [LICENSE](LICENSE).
