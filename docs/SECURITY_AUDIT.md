# HeyBuddy for Android + backend: security & privacy audit

Scope: `android/` (app + core) and `backend/` on branch `android-v1`.
Self-audit by the developers, not an external review. Date: 2026-10-25.

## 1. Android permissions

| Permission | Kind | Why HeyBuddy needs it | When it is used | If denied |
|---|---|---|---|---|
| `INTERNET` | normal | talk to *your* backend; `web_fetch`/`open_url` tools | during turns | app can't work |
| `RECORD_AUDIO` | runtime | push-to-talk voice | only while LISTENING in a turn the user started | falls back to typing (tested in emulator CI) |
| `POST_NOTIFICATIONS` (13+) | runtime | job results, approvals when no HeyBuddy screen is visible, optional "Tap to talk" | on events | no notifications; approvals then need the sheet open |
| `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_MICROPHONE` | normal | keep the mic alive for the length of one turn | per turn only | turn may be cut if the app is backgrounded |
| `FOREGROUND_SERVICE_MEDIA_PROJECTION` | normal | the one-frame MediaProjection capture (opt-in) | per capture, after the system consent dialog | no projection capture |
| `FOREGROUND_SERVICE_SPECIAL_USE` | normal | optional persistent "Tap to talk" notification (declared subtype text in the manifest) | only if the user turns it on | — |
| `SYSTEM_ALERT_WINDOW` | special (user toggle) | draw ink when the accessibility service is off | only during a turn; windows are `FLAG_NOT_TOUCHABLE` | marks shown inside the sheet instead |
| `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | normal (opens system dialog) | user opt-in for OEMs that kill background jobs | only from Settings when the user taps it | jobs may be delayed |
| `BIND_VOICE_INTERACTION` (protects our services) | signature/system | only the system may bind the assistant services | — | — |
| `BIND_ACCESSIBILITY_SERVICE` (protects our service) | system | only the system may bind the accessibility service | — | — |
| `BIND_QUICK_SETTINGS_TILE` (protects our tile) | system | only System UI may bind the tile | — | — |
| `<queries>` launcher/https/recognizer/TTS/custom-tabs intents | visibility | `launch_app` by name, opening links, finding a recognizer/TTS/Custom Tabs | — | — |

Not requested: contacts, location, phone, SMS, camera, storage, `QUERY_ALL_PACKAGES`,
device admin, `BIND_NOTIFICATION_LISTENER_SERVICE`, exact alarms.

Exported components: `MainActivity` (launcher), `CompanionActivity`
(`ACTION_ASSIST` + `com.heybuddy.android.action.TALK`; it only opens the sheet,
it takes no data from the intent other than `mode=type`), the system-bound
services above (each permission-protected), and the widget receiver. The
approval receiver and the foreground service are **not exported**
(asserted by `ManifestComponentsTest.internalComponentsAreNotExported`).

### Accessibility service

Optional. Used for: drawing in an accessibility overlay, noticing taps on the
highlighted target, `insert_text` after approval, screenshots on API 30+,
the accessibility-button shortcut. It does **not** inject gestures/taps,
does not read password fields (`insert_text` refuses `isPassword` nodes),
and does not log or upload window content except the single screenshot of
a turn the user started. On Android 13+ sideloaded apps must pass the
"restricted settings" step; HeyBuddy explains it and never tries to bypass it.

## 2. Threat model

| Threat | Mitigation | Residual risk |
|---|---|---|
| **Stolen provider keys** | Keys live only on the backend (env/secret store). The APK contains no keys; the phone gets a per-device random token. Tokens are stored server-side as `HMAC-SHA256(pepper, token)`. | Backend host compromise exposes keys: use the platform's secret store, rotate keys. |
| **Unauthorised device** | `/v1/pair` needs the pairing secret (constant-time compare, per-IP rate limit). Every other endpoint needs a valid, unrevoked token; per-token rate limit. "Disconnect" revokes the token. | A leaked pairing secret lets someone pair until you rotate it. Rotate after pairing your devices. |
| **Network attacker** | HTTPS only (`network_security_config`: cleartext only to `localhost`/`127.0.0.1`/`10.0.2.2` for development; user CAs not trusted). Backend in production rejects non-HTTPS (`X-Forwarded-Proto`) and sends HSTS. | No certificate pinning (users self-host with varying certificates). |
| **Prompt injection from the screen** (a web page or message says "ignore your instructions and send…") | Screen text and tool/integration results are wrapped as untrusted data (`Untrusted.frame` / `frame_untrusted`) and the backend's server rules (prepended to every system prompt, `safety.SERVER_RULES`) tell the model never to follow instructions found inside `<untrusted>` content. More importantly, **injection cannot cause side effects on its own**: every sensitive local tool and every Composio write/irreversible action needs the user's explicit "Allow" on a preview of the exact action. | The model can still be misled into giving wrong *answers* or proposing an action; the user must read the approval preview. |
| **Unauthorised tool execution** | Local tools: allowlist of tool names (`ApprovalPolicy.TOOL_RISK`, unknown = deny); sensitive tools ask by default; file tools confined to the app's folder (`resolveInside`), sensitive names blocked; `web_fetch` refuses private/loopback hosts; outputs capped. Integrations: server-side JSON-schema validation, risk classification (unknown = write), **single-use 15-minute proposals bound to user + tool + argument hash**, `confirmed=true` required for non-read, toolkit must still be enabled server-side. The model cannot call `/execute` itself; only the phone can, after the gate. | "Auto-allow integration reads" is on by default; a read can still leak data *to the model*. Turn it off in Settings if that matters. |
| **Model-chosen model/cost abuse** | Server allowlist of models; `max_tokens` capped server-side; per-token rate limit. | Rate limit is per token, in-memory (resets on restart). |
| **Malicious intents to exported components** | `CompanionActivity` ignores extras except `mode`; services require system permissions. | — |
| **Overlay abuse / tapjacking** | Overlays are non-touchable and exist only during a turn; HeyBuddy never draws over system dialogs on purpose and never injects input. | — |
| **Interference with emergency functions** | No power-key interception, no changes to power-button settings. See `ANDROID_ACTIVATION.md`. | — |
| **Supply chain** | Pinned dependency versions (Gradle version catalog, `requirements*.txt`); Gradle wrapper validated (`validateDistributionUrl`); CI builds from source. | No dependency vulnerability scanning in CI yet; no reproducible-build guarantee. |
| **Release signing** | CI preview APKs are signed with a throwaway key generated in the CI run (password masked, key never uploaded). | Each preview may have a different certificate (uninstall to update). Not suitable for Play distribution. |

## 3. Data: what is stored, where, and how to delete it

| Data | Where | Retention | How to delete |
|---|---|---|---|
| Device token | phone, `SecretStore` (Keystore AES-GCM), excluded from backup/transfer | until Disconnect/uninstall | Settings > Disconnect (also revokes server-side) |
| Settings | phone, DataStore | until uninstall | Android "Clear storage" |
| Memory facts (`memory.md`) | phone | until cleared | Memory tab > Clear all |
| Conversation (last 10 exchanges) | phone, memory only | process lifetime | Settings > Clear conversation, or kill the app |
| Job records and job files | phone, app files (`jobs/` excluded from cloud backup) | until removed | Jobs tab > Remove |
| Screenshots | phone RAM, sent to backend -> Gateway -> model provider for that turn | not stored by HeyBuddy | — (provider retention: see the Gateway/provider policy; Vercel offers ZDR options) |
| Audio | Android recognizer (on-device or Google, OEM-dependent), or backend -> Gradium if server STT is on | not stored by HeyBuddy | — (Gradium S plan and up advertises zero data retention) |
| Devices (id, name, token hash, user id) | backend SQLite | until revoked | Disconnect; server owner can delete rows |
| Connections (toolkit, Composio account id) | backend SQLite; OAuth tokens live at Composio | until disconnected | Apps tab > Disconnect (also deletes the Composio connected account) |
| Integration proposals (tool, arguments, risk) | backend SQLite | single-use, expire after 15 min, **rows are not purged automatically** | server owner: delete old rows (known gap) |
| Action log (tool, risk, status, short redacted summary) | backend SQLite | until the user deletes | Apps tab > Delete / Clear all |

Chat contents and screenshots are **not** stored by the backend.

## 4. Logging

* Backend: Python logging through a `RedactingFilter` that masks API keys,
  bearer tokens and similar patterns; logs device ids truncated to 8 chars;
  never logs request bodies, prompts, screenshots or audio. Upstream error
  messages are redacted before being returned to the phone.
* Android: the app has **no `android.util.Log` calls** in app code (checked
  with grep on 2026-10-25), no analytics SDK and no crash reporter.

## 5. Battery and performance (honest status)

**Not measured on a real device.** No battery drain, wake-lock, latency,
memory or thermal measurements were taken. By design:

* nothing runs between turns (no wake word, no polling, no background
  service unless the user enables the "Tap to talk" notification);
* the mic and its foreground service run only during a turn;
* one JPEG screenshot (longest edge <= 1280 px) per turn;
* background jobs use WorkManager and respect Doze.

Expected costs, unmeasured: network upload of one screenshot per turn
(typically tens to a few hundred KB), streaming download, TTS audio download.
End-to-end latency depends on the backend host, the Gateway model and Gradium
and was not measured.

## 6. Known gaps / follow-ups

* Purge expired proposals on the backend; persist rate-limit state.
* Dependency vulnerability scanning in CI (e.g. `pip-audit`, Gradle
  dependency check).
* Play-style policy review of accessibility and assistant use if ever
  distributed through a store.
* Real-device testing of overlays over third-party apps, the assistant
  gesture on several OEMs, and battery/latency.
