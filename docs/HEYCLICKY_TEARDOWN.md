# HeyClicky for Windows: reverse-engineering report

What HeyClicky for Windows actually is, how it works, and what HeyBuddy
0.3 rebuilt from it. Written from static analysis of the shipped binaries
and from one user's own HeyClicky data folder, following the REA
(Reverse Engineer Anything, github.com/morluto/rea) workflow:
observe → understand → recreate → verify.

Tags: **[O]** observed in the binaries or data, **[I]** inferred,
**[U]** unknown (not shipped in the client).

> Clean-room rule. HeyBuddy contains **no** HeyClicky code, prompts,
> scripts, sounds, fonts, images, videos or skills. Decompiled sources were
> read to learn *behaviour and interfaces* and were never copied. Every
> HeyBuddy file is original. Names such as "HeyClicky" appear only to
> describe what was studied. No license check, login, paywall or DRM was
> bypassed, and no HeyClicky server was called.

---

## 1. Artifacts examined

| Artifact | What it is | How it was examined |
|---|---|---|
| `HeyClicky-Setup.exe` (4,136,232 bytes, SHA-256 `34d8050a…9293`) | .NET 9 **NativeAOT** bootstrapper, Authenticode-signed (Microsoft ID Verified CS). Checks Windows/CPU support, downloads the real installer from `github.com/heyclicky/clicky-releases-windows/releases/latest/download`, verifies the download is signed by the same publisher, then runs it. | UTF-16/ASCII string extraction of the PE |
| `HeyClickyApp-1.1.3-win-x64-full.nupkg` (414 MB) and `releases.win-x64.json` | **Velopack** package (self-updating). `lib/app/` holds 747 files, ~905 MB unpacked. | Unzipped; `HeyClicky*.dll` decompiled with ILSpy 9.1 (2,014 C# files) |
| `HeyClicky.zip` (user's `%LOCALAPPDATA%\HeyClicky`) | Runtime data: settings, job folders, brain round logs, agent config | Non-secret files only. `auth.bin`, `codex-home/auth.json` and the session tokens embedded in logs were **not** opened or used |

REA's CLI (`npx rea-agents`, v6.7.0) was installed and used for artifact
triage; .NET assemblies were decompiled with `ilspycmd` (what REA's managed
route wraps). The full working notes with file/class citations live outside
the repo; this document is the public summary.

## 2. Technology stack [O]

- **App:** C# / .NET 9, **WinUI 3** (Windows App SDK 2.5), MVVM Toolkit, Win2D, Vortice (Direct2D/3D11/DirectComposition/XAudio2), WebView2, CsWin32 interop.
- **Scripting:** Microsoft **ClearScript V8** runs the on-screen drawing engine (JavaScript) and a three.js 3D engine.
- **Projects:** `HeyClicky` (UI), `HeyClicky.Core` (voice, brain, ink, billing, onboarding…), `HeyClicky.Agents` (background jobs), `HeyClicky.Platform` (Win32: hooks, capture, audio, overlays), `HeyClicky.Drawing`, `HeyClicky.Drawing3D`, `HeyClicky.Contracts`.
- **Bundled runtimes:** OpenAI **Codex CLI 0.152.1** (`codex.exe app-server`, Apache-2.0 upstream), ripgrep, a proprietary agent harness (`clicky-harness.exe`), a Chrome **MV3 extension** plus native-messaging host, ONNX Runtime/DirectML.
- **Services:** HeyClicky's backend (`api.heyclicky.com`) brokers every model call and mints short-lived credentials; Supabase (Google sign-in); **Deepgram** (speech-to-text); **OpenAI Realtime** (used only as text-to-speech); PostHog analytics; Sentry crash reports. Updates via Velopack, checked hourly.

## 3. How a voice turn works [O]

```
hold Ctrl+Alt ──► mic (WASAPI 24 kHz mono PCM16) ──► Deepgram nova-3 stream (interim text in the notch)
release (≥0.5 s hold, +150 ms tail) ──► capture every monitor (DXGI dup, 1280 px JPEG q80, cursor screen first)
      ──► POST /brain/turn (SSE) on HeyClicky's server: their prompt, their tools, their model routing
      ◄── a JavaScript-like "program":  say("...")  ring(...)  target(...)  + tool calls
program ──► V8 drawing engine (marks timed to speech) ──► click-through overlay
each say() line ──► OpenAI Realtime response.create "read verbatim" ──► 24 kHz audio
```

- **States:** Idle → Listening → Processing → Responding → Idle. A new press, Esc or a typed submit interrupts and cancels the speech queue.
- **No wake word, no voice activity detection.** Push-to-talk only.
- **Hotkeys are modifier-only chords** via a low-level keyboard hook: hold **Ctrl+Alt** to talk, **double-tap Ctrl** for a text box (tap ≤ 400 ms, gap ≤ 500 ms), hold **Ctrl+Win** to dictate, double-tap Ctrl+Win for hands-free dictation, **Esc** cancels. Win chords suppress the Start menu.
- **Screen context:** every monitor; the cursor's monitor is the main image. Browser page text (up to 60k characters) is read through UI Automation. No full UI Automation tree is sent; UIA is also used to snap a model's box onto the real control.
- **Coordinates:** pixels of the cursor-screen screenshot, origin top-left, mapped back with the monitor's bounds.
- **History:** a session thread rolls over after 2 h idle and is compacted server-side.

### The reply "program" (the core idea) [O]

The brain does not answer with prose plus `[POINT:x,y]` tags (that was the
original macOS Clicky). It writes a short program. Real examples from the
user's own round log (`OneBrain/<account>/<session>.jsonl`), paraphrased
only where personal:

```js
step(1);
say("I can see your Gmail draft, with plenty of room to think.");
ring(174, 135, 85, 25);
step(2);
say("I can trace the space you're working in, right on your screen.");
trace([[164,165],[1155,165],[1155,587],[164,587]], {straight: true});
step(3);
say("And point to tiny controls, like the attachment button down here.");
point(335, 657);
```

```js
say("Start with Introduction to LangChain.");
target(771, 359, 432, 27, "Open the introduction");
noted("Prefers small, hands-on examples.");
```

When the user clicks inside a `target(...)` box, the app starts the next
turn by itself with a note like *"[App] The user clicked "Open the
introduction", the step your drawing asked for, and the screen changed."*
That is how guided, step-by-step walkthroughs work.

Observed vocabulary: `say`, `step`, `point`, `ring`, `frame`, `highlight`,
`underline`, `trace`, `target`, `hover`, `label`, `connect`, `erase`,
`startOver`, `typeText`, `copyText`, `openPage`, `handOff`, `noted` (memory),
`seen` (context), `task` (names a walkthrough), `plan`, plus cards
(`sticky`, `paper`, `photo`, `button`) and 3D figures. Strokes are written
over `length / 600` s (clamped 0.14–0.7 s) in the cursor colour with a glow;
a clear fades over 0.45 s; marks pointing at the screen are dropped when
the screen changes.

### Tools the brain can call [O]

`open_url`, `copy_to_clipboard`, `insert_text`, `shell_exec` (PowerShell),
`file_read` / `file_write` / `file_list`, `save_memory`, Google Calendar
find/create, Spotify, `sessions_spawn` / `sessions_send` /
`sessions_interrupt` … (background jobs), artifacts, routines, and
computer-use ("Drive"). They run on the client **without an approval
step**, and there is no file or command allow/deny list.

## 4. Background jobs ("Clickys") [O]

- The brain calls `sessions_spawn` with a brief. The app creates
  `%LOCALAPPDATA%\HeyClicky\Clickys\jobs\<slug>-<id>\` with `output\`,
  `tmp\` and an `AGENTS.md` for the helper's notes, and runs Codex
  (`app-server`) or their own harness there with **full access and approvals
  off**.
- The helper's final message ends with an ARTIFACTS list; the app shows the
  reply in its dock, opens the finished file, and can read the first
  sentence aloud. Job records (`agents/defaults.json` → `clicky.jobs.v1`)
  keep title, prompt, folder, outcome, summary, reply and artifact paths.
- Model traffic for jobs goes through HeyClicky's proxy with a session
  token, and each turn needs a usage lease from their server.

## 5. UI surfaces [O]

- **Cursor buddy:** a filled triangle (16 DIP, #3380FF, glow radius 8) at
  mouse +35/+25 on a spring; listening shows 5 mic bars, thinking 3
  orbiting dots; pointing flies a quadratic Bézier (lift min(0.2 × d, 80),
  d/800 s clamped 0.6–1.4 s, smoothstep, 1.3× mid-flight swell), types a
  label bubble, holds 3 s, flies back.
- **Notch:** pure-black pill at the top centre of the screen under the
  cursor, 60 × 8 idle, widening to an activity strip (~360 × 32): cyan
  listening bars, pink thinking dots, orange speaking bars. A 0.45 s hover
  opens a 532-px panel with Home, Chat, Settings and Document tabs.
- **Theme:** dark (#191A1F background, #ECECEF text), radius 14, no
  Mica/Acrylic. Settings window 900 × 640.
- **Tray:** Open Settings, Pause, Report a Bug, Request a Feature, What's
  New, Check for Updates, Quit.
- **Lifecycle:** per-user mutex plus a named pipe for a second launch;
  launch at login on by default; `clicky://` and `heyclicky://` URL schemes;
  data in `%LOCALAPPDATA%\HeyClicky`.

## 6. What depends on HeyClicky's private backend [O]

The brain's system prompt and tool schemas; model routing (GPT, Claude and
DeepSeek options behind one endpoint); credential minting for Deepgram and
OpenAI Realtime; metering, plans and paywall; server-side memory,
compaction and history sync; calendar, Spotify and search proxies;
Composio account linking; proactive suggestions. **The client has no
bring-your-own-key option.** A compatible clone therefore cannot talk to
HeyClicky's services and must supply its own prompt, tools and keys.

## 7. Privacy observations [O]

- About 140 PostHog usage events with no in-app opt-out; AI tracing events
  can include prompt and response content.
- Sentry crash reporting with personal data enabled and no UI opt-out.
- Background agents run with no sandbox and no approvals.

## 8. What HeyBuddy 0.3 reproduces, and how

| HeyClicky behaviour | HeyBuddy 0.3 | Notes |
|---|---|---|
| Hold Ctrl+Alt to talk, 0.5 s minimum hold, 150 ms tail, interrupt | ✅ same | `core/hotkey_monitor.py`, `core/companion_manager.py` |
| Double-tap Ctrl text box | ✅ quick-ask box under the notch | `ui/quick_ask.py` |
| Hold Ctrl+Win dictation, Start menu suppressed | ✅ typed at the caret via clipboard paste, clipboard restored | hands-free double-tap variant not built |
| Esc cancels and clears drawings | ✅ | never swallowed from other apps |
| All monitors, 1280 px JPEG q80, cursor screen first | ✅ (since 0.2) | `core/screen_capture.py` |
| Reply program: `say` lines that own marks | ✅ HeyBuddy's own safe parser (Python `ast`, literals only; nothing executed) | `core/scene_program.py`; accepts HeyClicky-style bare programs too |
| Marks: point, ring, frame, highlight, underline, arrow, num, write, trace, target, clear/startOver/erase | ✅ drawn by HeyBuddy's Qt painter with the observed timings | `ui/ink_layer.py` |
| Guided walkthroughs (`target` waits for the click, then continues) | ✅ automatic `[app] the user clicked …` follow-up turn | `ui/overlay_window.py` |
| `noted`, `task` | ✅ local memory file; task title in the notch | `core/memory_store.py` |
| Per-line speech, marks timed to lines | ✅ ElevenLabs per line with prefetch; silent pacing at 2.6 words/s | `core/beat_speaker.py` |
| Tools: open URL, copy, insert text, shell, file read/write/list, memory | ✅ plus web fetch, open path, active window | `core/tools.py`, **with approvals and a deny list** |
| Background jobs in `jobs\<slug>-<id>\{output,tmp,AGENTS.md}`, ARTIFACTS, spoken result | ✅ Claude tool loop through your Worker | `core/jobs.py`, `ui/jobs_view.py` |
| Notch with listening/thinking/speaking strip, hover opens panel | ✅ | `ui/notch_window.py` |
| Cursor buddy, flight, bubble, per-monitor overlays hidden from capture | ✅ (since 0.2) | `ui/overlay_window.py`, `core/buddy_motion.py` |
| Installer, Start menu, launch at login, uninstaller | ✅ Inno Setup, per-user, no admin | `installer/heybuddy.iss` |

### Deliberately different

- **Speech services:** AssemblyAI streaming STT and ElevenLabs TTS through
  the user's own Cloudflare Worker (HeyClicky: Deepgram plus OpenAI Realtime
  minted by their backend). The Worker contract is unchanged from 0.2.
- **Brain:** Anthropic Claude with HeyBuddy's own system prompt
  (`utils/constants.py`) and Anthropic tool use, instead of HeyClicky's
  server-side prompt.
- **Safety:** commands and file writes outside HeyBuddy's folders ask
  first (configurable); credential stores, SSH/cloud keys and browser
  profiles are off limits; private-network URLs are not fetched.
- **Privacy:** no analytics, no crash reporting, nothing sent anywhere but
  the user's Worker. Memory is a local, editable `memory.md`.

### Not reproduced (and why)

| Feature | Reason |
|---|---|
| HeyClicky account, plans, paywall, usage leases | Their proprietary service; HeyBuddy is bring-your-own-keys |
| Chrome extension + native host that drives signed-in tabs through the debugger API | Large separate product with real safety risk; HeyBuddy opens links, fetches pages and lets jobs use the shell instead |
| Computer use / screen take-over ("Drive") | Needs a computer-use model loop; out of scope for 0.3 |
| Cards (sticky, photo, paper, button), 3D figures, documents/spreadsheet viewers | Extras outside the core "explain and point" experience |
| Connectors (Gmail, Notion… via Composio), Google Calendar, Spotify | Need OAuth apps or a broker; can be added later as MCP servers |
| Proactive suggestions, routines, onboarding videos, referral, voice picker | Server-driven or marketing features |
| Velopack auto-update | Updates come from GitHub releases; no background updater yet |
| UIA page-text reading and control snapping | Planned; screenshots only for now |
| Hands-free dictation (double-tap Ctrl+Win), Deepgram multi-language | AssemblyAI English streaming model is used |
