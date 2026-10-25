# Feature comparison: HeyClicky (Windows) vs HeyBuddy (Windows) vs HeyBuddy (Android)

HeyClicky facts come from the clean-room study in
[`HEYCLICKY_TEARDOWN.md`](HEYCLICKY_TEARDOWN.md). HeyBuddy is an independent
implementation: no HeyClicky code, prompts, assets or trademarked text are
used.

Legend: ✅ implemented · ◐ partial / different mechanism · ❌ not available ·
**CI** = verified only in CI (unit/emulator), **unverified** = built but not
exercised by any automated test or real device.

| Capability | HeyClicky for Windows | HeyBuddy Windows 0.3 | HeyBuddy Android 0.1 preview | Can't reproduce exactly on Android, and why |
|---|---|---|---|---|
| Summon | hold Ctrl+Alt (low-level keyboard hook) | hold Ctrl+Alt | ◐ assistant gesture (if HeyBuddy is the default digital assistant), QS tile, widget, launcher shortcuts, accessibility button, optional notification | **No global key hooks on Android.** Apps cannot intercept the power key or a 5 s power hold (the system owns the power menu and Emergency SOS). Long-press power reaches HeyBuddy **only** where the OEM maps it to "Digital assistant" and the user picked HeyBuddy. Volume keys are not interceptable in the background. An "Action Button" exists only on recent iPhones; Android OEM keys (e.g. Samsung side key) are configured by the OEM, not by apps. |
| Push-to-talk feel (hold to talk, release to send) | ✅ | ✅ | ◐ tap to start, tap to send (sheet button); assistant sessions start listening immediately | A hardware chord can't be held; touch is the input. |
| Quick text box | double-tap Ctrl | double-tap Ctrl | ✅ "Type" shortcut / Type button in the sheet | — |
| Dictation into the focused field | hold Ctrl+Win | hold Ctrl+Win | ◐ `insert_text` tool via accessibility, after approval | Needs the accessibility service; never types into password fields. No system-wide dictation hotkey. |
| Screen context | every monitor, 1280 px JPEG, page text via UI Automation | every monitor | ◐ one frame of the phone screen: assist screenshot (assistant role), accessibility `takeScreenshot` (API 30+), or MediaProjection (opt-in) | **MediaProjection asks for consent each time** (Android 14+) and shows a system indicator; accessibility screenshots need the user to enable the service (a restricted setting for sideloaded apps on 13+). Secure windows (banking, DRM) are black. |
| Visible text of the current app | UI Automation | UI Automation | ◐ assist structure text (assistant role only) | Only delivered to the active assistant, and only if the user leaves "Use text from screen" on. |
| Reply "program": spoken lines own drawings | ✅ (V8 engine) | ✅ safe literal parser | ✅ safe literal parser in `:core` (**CI**) | — |
| Ink marks over other apps (ring, frame, arrow, highlight, underline, number, note, trace, point) | ✅ click-through overlay | ✅ | ◐ accessibility overlay, or "Display over other apps", else a marked-up copy of the screenshot inside the sheet. Rendering **CI**-tested; drawing over real third-party apps **unverified** | Drawing over other apps requires accessibility or `SYSTEM_ALERT_WINDOW`; neither can be granted silently. Marks are dropped after a rotation. |
| Guided walkthrough (`target`, waits for your click) | ✅ | ✅ | ◐ waits for a tap via accessibility events, or "I did it, next step" button | Detecting the tap needs the accessibility service; without it the user confirms manually. Taps are never injected. |
| Voice out | OpenAI Realtime via their backend | ElevenLabs via your Worker | ✅ Gradium TTS via your backend, Android TTS fallback (fallback path **CI** at the logic level; real audio **unverified**) | — |
| Speech in | Deepgram streaming via their backend | AssemblyAI via your Worker | ✅ Android `SpeechRecognizer` (default) or record -> Gradium STT (opt-in) (**unverified** on device) | On-device recognizer quality and availability vary by OEM. |
| Local tools | open URL, copy, insert text, PowerShell, files, memory, calendar, Spotify… **no approvals** | open URL/file, copy, insert text, PowerShell, files, web fetch, memory, **with approvals + deny list** | ◐ open URL/file, share, clipboard, insert text, launch app, web fetch, sandboxed files, memory; sensitive tools ask | No shell on Android (no PowerShell equivalent for apps, and it would be unsafe). Clipboard reads in the background are blocked by Android 10+, so `read_clipboard` works only while HeyBuddy is in front. |
| Connected apps (Gmail, Calendar…) | Composio via their backend | ❌ | ✅ Composio via your backend, server-validated proposals, writes always confirmed (**CI** with mocks; live Composio **unverified**) | — |
| Background jobs | Codex with full access, approvals off | Claude tool loop in a job folder | ◐ WorkManager job with the same sandboxed tools | Android limits background execution (Doze, OEM battery killers); long jobs may be delayed. No shell. |
| Memory | server-side | local `memory.md` | ✅ local `memory.md`, viewable/deletable in the app (**CI** in `:core`) | — |
| Notch / cursor buddy | ✅ | ✅ | ◐ the companion sheet + a buddy pointer drawn in the overlay | There is no mouse cursor to follow on a phone. |
| Always-on wake word | ❌ (push-to-talk) | ❌ | ❌ by design | Would need continuous mic + a hotword model; privacy and battery cost. |
| Computer use / taking over the screen | ✅ "Drive" | ❌ | ❌ | Would require accessibility gesture injection on the user's behalf; deliberately not built. |
| Accounts, plans, analytics | account + paywall, PostHog, Sentry | none | none: no analytics, no crash reporter | — |
| Keys | on their servers | your Cloudflare Worker | your HeyBuddy backend (FastAPI) | — |
| Distribution | installer | Inno Setup installer | sideload APK from GitHub pre-release (CI-signed preview key, **not** a Play key) | Not on Google Play; Play policy review of accessibility/assistant use not attempted. |

## Notes

* "Assistant gesture" availability: Pixel 6+ maps press-and-hold power to the
  assistant by default and lets the user switch it back to the power menu;
  Samsung offers a side-key setting; other OEMs vary. HeyBuddy cannot
  change these settings itself. See [`ANDROID_ACTIVATION.md`](ANDROID_ACTIVATION.md).
* Everything marked **unverified** compiles and is wired up, but has not been
  observed working on real hardware. See [`ANDROID_TESTING.md`](ANDROID_TESTING.md).
