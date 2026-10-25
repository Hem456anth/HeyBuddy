# HeyBuddy changelog

## 0.3.0 — HeyClicky-class companion (clean-room)

From a reverse-engineering study of HeyClicky for Windows 1.1.3
(docs/HEYCLICKY_TEARDOWN.md):
- **Draws on screen while it talks.** Replies are buddy programs: each
  `say()` line owns rings, frames, highlights, underlines, arrows, numbered
  badges, handwritten notes and free traces, drawn in sync with speech and
  hidden from screenshots.
- **Guided walkthroughs.** `target()` outlines the control to click, waits
  for your click, then continues with a fresh screenshot.
- **Acts on your PC** with Anthropic tool use: open links/files, copy, type
  at the caret, list/read/write files, PowerShell, web fetch, active window.
  Commands and outside writes ask first; secrets locations are off limits.
- **Background jobs** with their own folders (`output/`, `tmp/`,
  `AGENTS.md`), progress, artifacts, a Jobs tab and a spoken result.
- **Notch** at the top of the screen; **double-tap Ctrl** quick-ask box;
  **hold Ctrl+Win** dictation typed at the caret; **Esc** cancels.
- **Memory** in a local `memory.md` (`remember` tool, `noted()`).
- Per-line speech with prefetch; 0.5 s minimum hold and 150 ms release tail.
- **Installer**: per-user Inno Setup package with Start menu shortcut,
  optional launch at login and a clean uninstaller; CI installs it, runs the
  self-check on the installed copy and uninstalls it.
- Self-check now also proves the tool round trip, the program pipeline, ink
  drawn on screen but hidden from capture, target hit-testing, memory and a
  background job producing a file.

## 0.2.0 — Clicky-faithful Windows port

Correctness
- Pointing now lands on target on HiDPI and multi-monitor setups: screenshots
  are ≤1280 px and labeled with their size, coordinates are mapped from
  image pixels to physical pixels, cursor screen is screen 1.
- All monitors are sent to Claude, not just the cursor's.
- `[POINT:none]` is no longer spoken aloud; labels and screen numbers are optional.
- Pausing mid-question no longer drops the start of the sentence (turns are
  assembled by `turn_order`; release sends `ForceEndpoint`).
- First words are no longer lost (mic starts before the STT socket and buffers).
- Non-ASCII replies are no longer garbled (SSE forced to UTF-8).
- Settings/logs survive in the .exe (`%APPDATA%\HeyBuddy`; the one-file bundle
  dir is temporary).
- No console window flashes when speaking; works on Python 3.13 (pydub/audioop removed).
- Autostart from source works (Run key used a relative `-m` path).
- The panel's text box can be typed into.

Clicky parity
- Blue triangle buddy follows the mouse; waveform while listening, spinner
  while thinking; bezier flight with tangent rotation and scale pulse;
  typed label bubble; return flight cancelled by mouse movement; multiple
  points visited in order; transient mode fade.
- Clicky's speech-first system prompt (adapted to Windows); 10-exchange memory.
- Hotkey interrupts an answer in progress; other keys with the chord cancel.
- Own windows excluded from screen capture.

Security / ops
- Recordings off by default. Optional Worker access key, model allowlist,
  token cap and size limits. Single-instance guard.
- `--selfcheck`, 30+ tests, GitHub Actions build that self-checks the exe.
