# HeyBuddy for Android: architecture

HeyBuddy for Android is a screen companion: you summon it, ask out loud (or
type), it looks at what is on your screen, answers by voice, draws marks
(rings, arrows, highlights, numbered badges) over the app you are in, can walk
you through a task one tap at a time, and can act through local tools and
connected apps with your approval.

It is two programs:

```
 Android phone                                   Your server (Render/Fly/Railway/any Docker host)
 ┌───────────────────────────────────────┐        ┌───────────────────────────────────────────┐
 │ :app  (Kotlin, Jetpack Compose)       │ HTTPS  │ backend/ (FastAPI, Python 3.12)           │
 │  activation surfaces ─┐               │ Bearer │  /v1/pair  /v1/chat (SSE)  /v1/tts /v1/stt │
 │  CompanionActivity ───┼─► TurnController ────►│  /v1/integrations/* (proposals, execute)   │
 │  VoiceInteraction ────┘   │  ▲        │ token  │  SQLite: devices, connections, proposals,  │
 │  Accessibility overlay ◄──┘  │        │        │          action log                        │
 │ :core (pure Kotlin/JVM)      │        │        │  providers/  llm.py  voice.py  composio.py │
 │  BackendClient · TurnEngine · parser  │        └──────┬──────────────┬──────────────┬───────┘
 │  MarkResolver · ToolRunner · Memory   │               ▼              ▼              ▼
 └───────────────────────────────────────┘        Vercel AI Gateway   Gradium       Composio v3.1
```

The phone holds **no provider keys**. It holds one per-device bearer token,
issued by the backend at pairing and stored encrypted with an Android
Keystore AES-256-GCM key (`SecretStore`). The pairing secret itself is never
stored.

## Modules

### `android/core` (JVM library, no Android SDK)

Unit-tested on any JVM (`./gradlew :core:test`).

| Package | What it does |
|---|---|
| `backend` | `BackendClient` (OkHttp): pair/unpair, config, chat SSE parser, TTS, STT, integrations. `ChatPayloadBuilder` builds OpenAI-style messages with screenshot `image_url` parts. `BackendUrlPolicy` rejects non-HTTPS URLs except dev hosts (`localhost`, `127.0.0.1`, `10.0.2.2`). |
| `program` | **Buddy programs.** The model answers with a fenced `buddy` block of calls such as `say("...")`, `ring(x,y,w,h,"label")`, `arrow(...)`, `target(...)`, `noted("...")`. `LiteralCallParser` accepts only whitelisted call names with literal arguments; nothing is evaluated. The result is a list of **beats** (a spoken line + the marks it owns). |
| `geometry` | `CapturedFrame` (screenshot + mapping from image pixels to display pixels), `MarkResolver` (image px -> display px), `OverlayMapper` (display px -> overlay px, drops marks if the rotation changed), `InkGeometry` (stroke shapes). |
| `turn` | `TurnEngine` runs a turn: send -> stream -> parse -> speak beats -> run tool calls -> loop (max 6 rounds). `IntegrationGate` applies the approval policy to Composio proposals. `Conversation` keeps the last 10 exchanges. `Untrusted` frames screen text / tool output as data. |
| `speech` | `BeatSpeaker`: speaks beats in order, prefetches the next line, falls back to the device voice, paces silently when voice is off, and fires `onBeatStarted` so marks appear with the sentence that mentions them. |
| `tools` | Tool specs, `ApprovalPolicy` (safe vs sensitive tools; integration risk), `ToolRunner` (validation, sandboxed file root, private-host blocking for `web_fetch`, output caps). |
| `memory`, `jobs` | `memory.md` facts (user-viewable/deletable) and background job records. |
| `prompt` | The system prompt (original wording, written for this project). |

### `android/app` (the Android app)

| Area | Classes | Notes |
|---|---|---|
| Activation | `TalkTileService` (Quick Settings tile), `TalkWidget`/`TalkWidgetReceiver` (Glance widget), `res/xml/shortcuts.xml` (launcher shortcuts "Talk", "Type"), `CompanionActivity` (`ACTION_ASSIST`, `com.heybuddy.android.action.TALK`), optional companion notification, accessibility button | All land in `CompanionActivity`, a translucent bottom sheet over the current app. |
| Assistant role | `HeyBuddyVoiceInteractionService`, `HeyBuddySessionService`, `HeyBuddySession`, `HeyBuddyRecognitionService` | Lets the user pick HeyBuddy as the **default digital assistant**, so the OEM's assistant gesture (long-press power / side key, corner swipe, where offered) opens HeyBuddy with an assist screenshot. See `ANDROID_ACTIVATION.md`. |
| Turn state machine | `companion/TurnController` | `IDLE -> LISTENING -> THINKING -> SPEAKING -> (WAITING_FOR_TAP) -> IDLE`, generation-numbered so a superseded turn's results are dropped. Exposes a `StateFlow<CompanionUiState>` to Compose. |
| Screen context | `a11y/BuddyAccessibilityService.capture()` (API 30+ `takeScreenshot`), `capture/MediaProjectionCapture` (one frame, consent each time, opt-in), assist screenshot from the session, `FrameEncoder` (JPEG, longest edge <= 1280) | Screenshot is taken **before** the sheet appears. If none is available the turn runs text-only and marks are dropped. |
| Drawing | `overlay/InkOverlayView` (Canvas), `overlay/OverlayController` | Draws in an accessibility overlay window (preferred), a `TYPE_APPLICATION_OVERLAY` window if "Display over other apps" is allowed, or as a preview on a copy of the screenshot inside the sheet. Windows are not touchable, so taps pass through. |
| Guided taps | `BuddyAccessibilityService` tap events -> `TurnController.onUserTappedAt` | Needs the accessibility service. Without it, the sheet shows "I did it, next step". |
| Voice | `audio/AndroidVoice` (server TTS via backend, Android TTS fallback), `audio/SpeechInput` (Android `SpeechRecognizer` default, or record -> `/v1/stt`), `MicStreamer` | Mic only during LISTENING; a `microphone` foreground service runs only for that turn. |
| Tools | `tools/AndroidToolPlatform` | open URL/file, share, clipboard, `insert_text` (accessibility, never into password fields), launch app, web fetch. Sensitive tools ask via `ApprovalBroker` (dialog in the sheet, or a high-priority notification when no HeyBuddy surface is visible). |
| Jobs | `jobs/JobScheduler` (WorkManager) | Background jobs run as WorkManager work, results posted as a notification and optionally spoken. |
| Settings & storage | `settings/SettingsRepository` (DataStore), `settings/SecretStore` (Keystore) | Backup rules exclude the secrets file and job folders. |
| UI | `ui/MainActivity` (onboarding, Home, Apps, Jobs, Memory, Settings tabs), `ui/CompanionSheet`, `ui/IntegrationsTab` | Compose + Material 3. |

## Data flows

### A voice turn

1. User activates (tile / widget / shortcut / assistant gesture / accessibility button).
2. `CompanionActivity` (or `HeyBuddySession`) grabs **one** screenshot of the
   app underneath, then shows the sheet.
3. If `RECORD_AUDIO` is granted, `TurnController.startListening` starts the
   recognizer (and the mic foreground service). If it is denied, the sheet
   falls back to typing.
4. On release: `TurnEngine.run` builds the payload (system prompt + memory
   facts + last exchanges + the question + the screenshot as an `image_url`
   part + local tool specs) and `POST /v1/chat`.
5. The backend checks the device token and rate limit, enforces the model
   allowlist and token cap, appends Composio tools for the user's connected
   toolkits, calls the Gateway, and relays an SSE stream.
6. The phone parses the buddy program as it streams; each beat is spoken
   (server TTS -> device TTS fallback) and its marks are revealed in the
   overlay at the same moment.
7. Tool calls: local ones go through `ToolRunner` + `ApprovalPolicy`;
   integration proposals go through `IntegrationGate` and
   `/v1/integrations/execute`. Results go back to the model as untrusted data
   for the next round.
8. `target()` marks put the turn into WAITING_FOR_TAP; a tap inside the box
   (accessibility) or "I did it" starts a follow-up turn with a fresh frame.
9. Marks linger (default 12 s) and fade. Foreground service stops.

### Pairing

`Settings / onboarding -> POST /v1/pair {pairing_secret, device_name}` ->
backend stores `HMAC-SHA256(pepper, token)` only -> phone stores the token in
`SecretStore`. Unpair calls `DELETE /v1/device` and wipes the token.

### Connecting an app

`Apps tab -> POST /v1/integrations/connect {toolkit}` -> Composio hosted link
-> opened in a Custom Tab -> Composio stores the OAuth tokens (never the
phone, never HeyBuddy's backend) -> backend records the connection id.

## Provider interfaces

See `docs/API_INTEGRATION_GUIDE.md` for endpoints and how to swap providers.
In short: `LLMProvider.stream`, `TTSProvider.synthesize`,
`STTProvider.transcribe`, `IntegrationProvider.{create_link, list_accounts,
delete_account, tools, execute}`. The phone only knows the `/v1/*` device API.

## Build & CI

* `.github/workflows/android-build.yml`: backend (ruff + pytest), Android
  (`:core:test`, `:app:lintDebug`, debug + release APK), emulator matrix
  (API 26/30/34/35, `connectedDebugAndroidTest`), and a pre-release with the
  APKs, `SHA256SUMS.txt` and test reports. Results: `docs/ANDROID_TESTING.md`.
* Toolchain: AGP 9.4.1 (built-in Kotlin), Kotlin 2.4.21, Gradle 9.8.2,
  compileSdk 37, targetSdk 36, minSdk 26, JDK 17.
