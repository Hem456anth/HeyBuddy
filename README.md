# HeyClicky

A Windows port of [farzaa/clicky](https://github.com/farzaa/clicky) — an AI
companion that lives in your system tray, can see your screen, hears your
voice (push to talk), and animates a blue cursor toward things on screen to
help you learn.

> Credits: this project is a Windows reimplementation of @farzaa's macOS
> [Clicky](https://github.com/farzaa/clicky). The Cloudflare Worker proxy
> (`/chat`, `/tts`, `/transcribe-token`) is reused unchanged.

## Status

**All three phases complete.** The full pipeline is wired:

- pystray tray icon → floating PyQt6 panel
- Push-to-talk via a Win32 low-level keyboard hook (preset chords)
- `sounddevice` PCM16 16 kHz mono recorder, streamed live into the STT WS
- AssemblyAI realtime v3 websocket transcription (token from `/transcribe-token`)
- Claude `/chat` SSE streaming with screenshot attachment + POINT-protocol
  system prompt
- `[POINT:x,y:label:screenN]` tag parsing + DPI-correct, bezier-arc cursor
  flight across any monitor
- ElevenLabs `/tts` synthesis + sounddevice playback
- Transient cursor mode (panel auto-hides during turns)
- Settings: model, hotkey preset, transient mode, autostart
- Diagnostics: Worker reachability ping + microphone level test
- Windows autostart via registry
- PyInstaller one-file build (`build.bat`)

---

## Setup checklist

### 1. Deploy the Cloudflare Worker

HeyClicky reuses the upstream Worker. From the upstream repo:

```bash
git clone https://github.com/farzaa/clicky
cd clicky/worker
npm install

npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put ASSEMBLYAI_API_KEY
npx wrangler secret put ELEVENLABS_API_KEY
npx wrangler secret put ELEVENLABS_VOICE_ID   # or set as a var; ID of the voice you want

npx wrangler deploy
```

Note the deployed URL (e.g. `https://heyclicky.your-account.workers.dev`).

### 2. Install HeyClicky (dev / from source)

```powershell
git clone <this repo>
cd heyclicky
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-desktop.txt
```

### 3. Point HeyClicky at the Worker

Edit `config/settings.json`:

```json
{
  "worker_url": "https://heyclicky.your-account.workers.dev",
  ...
}
```

(Bare hostnames like `name.workers.dev` are also accepted; the client adds
`https://` automatically.)

### 4. Smoke-test the input path (no network)

```powershell
python -m src.tools.smoke_test
```

Hold Ctrl+Alt, speak, release. A `.wav` lands in `./recordings/`. If this
works the audio + hotkey plumbing is healthy.

### 5. Launch the app

```powershell
python -m src.main
```

A blue dot appears in your system tray. Click it → floating panel opens.
Hold Ctrl+Alt anywhere in Windows to talk to Claude.

Open **Settings** in the panel to:
- Change the model (Sonnet 4.6 default, Opus 4.7/4.8 available)
- Switch the push-to-talk chord (Ctrl+Alt, Ctrl+Shift, Alt+Shift, Right Alt, Alt+Space)
- Toggle transient cursor mode (panel hides during turns; just the cursor speaks)
- Enable Windows autostart
- **Ping** the Worker and **Test microphone** — run these first if anything misbehaves

### 6. (Optional) Build a standalone .exe

```powershell
pip install pyinstaller
build.bat
```

Produces `dist/HeyClicky.exe` — a single file you can copy to other Windows
machines that don't have Python installed.

---

## How it works

The `CompanionManager` state machine runs:

```
IDLE → LISTENING → PROCESSING → RESPONDING → IDLE
```

On hotkey press it opens an AssemblyAI realtime websocket and starts the
mic; PCM blocks stream into the WS as they're captured. On release it
finalizes the transcript, grabs a screenshot of the cursor's monitor, asks
Claude over `/chat` SSE for a reply, then plays the response via TTS while
flying the blue cursor to any `[POINT:x,y:label:screenN]` markers Claude
embedded.

See [`CLAUDE.md`](CLAUDE.md) for the architecture in detail (threading
model, Worker contract, file layout, conventions, POINT protocol).

---

## Troubleshooting

| Symptom | Likely fix |
| --- | --- |
| "Worker unreachable" on Ping | URL typo, Worker not deployed, or running behind a VPN that blocks `*.workers.dev` |
| Worker reachable but Claude requests 401/403 | Worker is missing `ANTHROPIC_API_KEY` secret |
| Worker reachable but no token from `/transcribe-token` | Worker is missing `ASSEMBLYAI_API_KEY` secret |
| TTS silent but text appears | Worker is missing `ELEVENLABS_API_KEY` or `ELEVENLABS_VOICE_ID` |
| Hotkey doesn't fire | Another app's global hook may be eating it; pick a different preset in Settings |
| Mic test reports "silent" | Windows Sound settings → check input device + volume; ensure no other app has exclusive access |
| Cursor flies to wrong spot on HiDPI display | Open Settings → Save once; this re-applies per-monitor DPI awareness |
| `python -m src.main` shows a black console window | Use `pythonw -m src.main` (or just run the built `.exe`) |

Logs land in `logs/heyclicky.log` (rotated, 1 MB × 3 backups).

---

## Project layout

See [`CLAUDE.md`](CLAUDE.md) for the canonical file map and conventions.

---

## License

MIT — same spirit as upstream Clicky.
