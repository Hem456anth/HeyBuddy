# REA: Reverse Engineering Clicky (farzaa/clicky) for Windows

This is the "Reverse Engineer Anything" teardown that drove the HeyBuddy
Windows port. Each section states what Clicky does (with the Swift file it
lives in), how HeyBuddy reproduces it on Windows, and what was wrong in the
first HeyBuddy port and is fixed now.

Sources inspected: `farzaa/clicky@main` (Swift, macOS 14.2+, ~7.8k lines) and
`Hem456anth/HeyBuddy@main` (Python/PyQt6, ~6.7k lines).

---

## 1. Architecture at a glance

```
            push-to-talk (global hook)
                     │
 mic PCM16 16 kHz ───┼──► AssemblyAI v3 realtime WS ──► transcript
                     │          (token via Worker)
 all monitors ───────┼──► JPEG ≤1280px, labeled "image dimensions: WxH"
                     ▼
              Cloudflare Worker /chat ──► Claude (SSE)
                     │
          reply text + [POINT:x,y:label:screenN]
             │                        │
   Worker /tts ─► ElevenLabs MP3      └─► map image px → monitor px
             │                              │
         speaker                     blue triangle flies there
```

| Concern | Clicky (macOS) | HeyBuddy (Windows) |
| --- | --- | --- |
| App shell | `NSStatusItem`, `LSUIElement` (no Dock icon) — `leanring_buddyApp.swift`, `MenuBarPanelManager.swift` | `pystray` tray icon + frameless PyQt6 panel, `WS_EX_TOOLWINDOW` (no taskbar button) — `src/main.py`, `src/ui/main_window.py` |
| State machine | `CompanionManager.swift` (`idle → listening → processing → responding`) | `src/core/companion_manager.py`, same states + turn generations |
| Hotkey | listen-only `CGEvent` tap, ctrl+option — `GlobalPushToTalkShortcutMonitor.swift` | `WH_KEYBOARD_LL` low-level hook on its own message-pump thread, Ctrl+Alt — `src/utils/win32.py` |
| Mic | `AVAudioEngine` → PCM16 — `BuddyDictationManager.swift` | `sounddevice.InputStream` int16 16 kHz — `src/core/audio_recorder.py` |
| STT | AssemblyAI v3 streaming (OpenAI/Apple fallbacks) — `AssemblyAIStreamingTranscriptionProvider.swift` | `src/api/assemblyai_streaming.py` |
| Screen | ScreenCaptureKit, all displays, own windows excluded — `CompanionScreenCaptureUtility.swift` | `mss` (GDI) + `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` — `src/core/screen_capture.py` |
| LLM | Claude via Worker, SSE — `ClaudeAPI.swift` | `src/api/claude_client.py` + `src/api/cloudflare_proxy.py` |
| TTS | ElevenLabs via Worker, `AVAudioPlayer` — `ElevenLabsTTSClient.swift` | Worker `/tts` → bundled ffmpeg decode → `sounddevice.OutputStream` — `src/core/audio_player.py` |
| Cursor | SwiftUI overlay `NSPanel` per screen — `OverlayWindow.swift` | layered click-through `QWidget` per screen — `src/ui/overlay_window.py` + `src/core/buddy_motion.py` |
| Secrets | Cloudflare Worker holds all keys — `worker/src/index.ts` | same Worker (wire-compatible), optional hardening |

## 2. Screen awareness

**Clicky**: `captureAllScreensAsJPEG()` captures every display, sorts the
cursor's display first, scales each to max 1280 px, JPEG q=0.8, and labels
them `"screen 1 of 2 — cursor is on this screen (primary focus)"` plus
`"(image dimensions: 1280x831 pixels)"`. It excludes its own windows from the
ScreenCaptureKit filter so Claude never sees the buddy.

**Key insight**: Claude answers in *screenshot pixels*, not screen pixels.
Clicky converts `x_screen = x_img / img_w * display_w`.

**First HeyBuddy port (bugs)**: sent one full-resolution PNG of the cursor's
monitor only, never told Claude the image size, and told it to answer in
"logical post-DPI" coordinates, then multiplied by the DPI scale again. A
4K PNG is also over Claude's ~1568 px resize threshold, so the API rescaled
it silently. Result: pointing was off by 1.5–3× on any HiDPI laptop, and
`screenN` on a second monitor pointed at the wrong display (screen1 was the
primary, not the cursor screen).

**Now**: `ScreenCapture.capture_all_screens()` mirrors Clicky exactly
(cursor-first order, ≤1280 px JPEG, identical labels) and every capture is a
`CapturedScreen` that remembers its image size and the monitor's physical
rectangle; `image_point_to_physical()` does the mapping. The process is
per-monitor-DPI-aware v2, so mss, `GetCursorPos` and monitor rectangles all
speak physical pixels. The overlay and panel are excluded from capture with
`WDA_EXCLUDEFROMCAPTURE` (Windows 10 2004+).

## 3. Voice input

**Clicky**: hold ctrl+option → `AVAudioEngine` tap → PCM16 → AssemblyAI v3
websocket (`sample_rate=16000&encoding=pcm_s16le&format_turns=true`).
Turns are stored by `turn_order`; on release it sends `ForceEndpoint` and
waits for the final formatted turn, then joins all turns.

**First port (bugs)**: kept only the *last* turn and stopped waiting as soon
as the *first* turn ended, so pausing mid-sentence dropped the start of the
question; sent `Terminate` instead of `ForceEndpoint` (slower); opened the
socket *before* the mic, so the first ~0.5 s of speech was lost; no
`speech_model` (now required by AssemblyAI); Ctrl+Alt+Del or AltGr typing
triggered a turn.

**Now**: mic starts instantly and buffers until the socket is up;
`TurnTranscriptAssembler` stores turns by order and prefers formatted text;
release sends `ForceEndpoint` and waits only for the in-flight turn; a
configurable `speech_model` (default `universal-streaming-english`; Clicky's
`u3-rt-pro` was retired in July 2026); pressing any other key while the chord
is held cancels the recording.

## 4. AI interaction

**Clicky**: Sonnet by default, system prompt tuned for speech ("all
lowercase, casual... write for the ear"), last 10 exchanges as text-only
history, images only on the current turn, SSE streaming, reply parsed with
`\[POINT:(?:none|(\d+)\s*,\s*(\d+)(?::label)?(?::screen(\d+))?)\]`.
A new push-to-talk cancels the in-flight response and TTS.

**First port (bugs)**: `[POINT:none]` wasn't recognised, so the voice read
"point none" aloud; history grew without bound; streamed text showed raw
tags in the panel; SSE text was decoded as ISO-8859-1 (requests' default for
`text/event-stream`), garbling every non-ASCII character; the buddy ignored
the hotkey while busy instead of letting the user interrupt.

**Now**: Clicky's prompt adapted for Windows, the full POINT grammar
(none / no label / screenN), history capped at 10 exchanges, partial tags
hidden while streaming, UTF-8 forced, stream `error` events surfaced, and
interruption via turn generations (stale results are dropped, TTS stops).

## 5. Cursor animation

From `OverlayWindow.swift` (`BlueCursorView`), reproduced number-for-number
in `buddy_motion.py`:

| Behaviour | Value |
| --- | --- |
| Shape | 16 pt equilateral triangle, `#3380FF`, rotated −35°, blue glow |
| Follow offset | cursor + (35, 25) pt, springy catch-up |
| Listening | 5-bar waveform, profile 0.4/0.7/1.0/0.7/0.4, driven by mic level |
| Processing | 14 pt spinner, 252° arc, 0.8 s per turn |
| Flight | quadratic bezier, duration `clamp(dist/800, 0.6, 1.4)` s, smoothstep, arc height `min(dist·0.2, 80)` |
| In flight | rotation follows tangent (+90°), scale 1 + 0.3·sin(πt) |
| Arrival | parks at target + (8, 12) pt, clamped 20 pt inside the screen |
| Bubble | label (or "right here!"…), 30–60 ms per character, hold 3 s, fade 0.5 s |
| Return | flies back to the mouse; moving >100 pt cancels the return |
| Transient mode | fades in for a turn, out 1 s after everything ends |

**First port**: a single overlay spanning the virtual desktop that mixed
physical and Qt-logical coordinates (wrong on any scaled display), a pulsing
dot instead of the triangle, no cursor-following, no listening/thinking
states, no return flight.

**Now**: one overlay per monitor (Qt 6 needs per-screen device-pixel
ratios), positions kept in physical pixels and converted per window, Clicky's
triangle/waveform/spinner/bubble, and only a small dirty rectangle repainted
per 60 fps frame.

## 6. Security model

* No API key ever ships in the client; the Worker injects them.
* New optional Worker hardening: `APP_SHARED_SECRET` (client sends
  `X-HeyBuddy-Key`), `ALLOWED_MODELS`, `MAX_TOKENS_CAP`, 12 MB body limit,
  5,000-char TTS limit. All off by default, so an upstream Worker still works.
* Screenshots and audio go only to the Worker → Anthropic/AssemblyAI; nothing
  is written to disk unless "Keep a .wav of each question" is enabled
  (the first port saved every recording by default).
* HeyBuddy's own windows are excluded from capture.

See `SECURITY.md` for the full threat model.
