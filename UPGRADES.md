# HeyBuddy changelog

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
