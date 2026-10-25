# HeyBuddy for Android: testing

All results on this page come from GitHub Actions
(`.github/workflows/android-build.yml`, branch `android-v1`). Nothing below was
run on a real phone.

## Layers

| Layer | Where | Command | What it covers |
|---|---|---|---|
| Backend unit/integration | `backend/tests` (pytest + respx) | `ruff check . && pytest` | pairing, token hashing, rate limits, model allowlist, SSE chat relay, tool-call classification, proposal/execute gate (single-use, arg-hash, confirmation), TTS/STT, redaction. Providers are mocked. |
| Core logic | `android/core/src/test` (JUnit on the JVM) | `./gradlew :core:test` | buddy-program parser, literal-call safety, screen/overlay mapping, ink geometry, backend protocol against MockWebServer, end-to-end `TurnEngine` turns (beats, marks, tools, memory, targets), tool policy and sandbox, integration gate, memory and jobs. |
| Static analysis | Android Lint | `./gradlew :app:lintDebug` (+ `lintVitalRelease` during the release build) | errors fail the build; no baseline file is used. |
| Build | AGP | `:app:assembleDebug :app:assembleRelease :app:assembleDebugAndroidTest` | compile, R8 minification + resource shrinking of the release APK, APK signature (`apksigner verify`). |
| Instrumented self-check | `android/app/src/androidTest` on emulators (reactivecircus/android-emulator-runner, KVM, x86_64 `google_apis`) | `./gradlew :app:connectedDebugAndroidTest` | see below |

### Instrumented tests (19)

| Class | Test | Checks |
|---|---|---|
| `ManifestComponentsTest` | `assistantServiceIsBoundOnlyBySystem` | VoiceInteractionService resolvable via PackageManager, protected by `BIND_VOICE_INTERACTION`, has `android.voice_interaction` meta-data; session service protected too |
| | `recognitionServiceDeclared` | RecognitionService resolvable + protected |
| | `quickSettingsTileResolvable` | TileService resolvable via `ACTION_QS_TILE`, protected by `BIND_QUICK_SETTINGS_TILE` |
| | `accessibilityServiceDeclaredAndProtected` | accessibility service resolvable, protected, has its config meta-data |
| | `widgetProviderInstalled` | `AppWidgetManager` lists the Glance widget provider |
| | `shortcutsAndAssistIntentsResolve` | `ACTION_ASSIST` and the TALK action resolve to `CompanionActivity`; `ShortcutManager.manifestShortcuts` contains `talk` and `type` and each resolves |
| | `internalComponentsAreNotExported` | approval receiver and foreground service are not exported |
| `InkOverlayRenderTest` | `ringAndArrowRender` | the ink view hosted in Compose draws nothing before marks, draws blue ink pixels after a ring + arrow, clears |
| | `marksFromAnotherRotationAreSkipped` | marks captured in another rotation are not drawn |
| `TurnEndToEndTest` (MockWebServer on the device) | `pairingStoresTokenAndRejectsWrongSecret` | wrong secret -> "refused", no token; right secret -> token stored in Keystore-backed store |
| | `typedTurnParsesProgramAndFinishes` | real `TurnController` turn: settings + token -> SSE chat -> parser -> beats -> IDLE with the spoken text; bearer token and screenshot `image_url` sent; program code not shown |
| | `revokedTokenShowsErrorWithoutCrashing` | 401 -> ERROR state, no crash |
| | `unpairedPhoneAsksToConnect` | no token -> "Connect this phone…" without any network call |
| `OnboardingAndPermissionsTest` | `walkThroughWithEverythingDenied` | full onboarding with mic/notification permission dialogs **denied** (UiAutomator) and every optional step skipped, ends on the Home tab |
| | `companionWithMicDeniedFallsBackToTyping` | talk activation with the mic permission denied falls back to the text field |
| | `companionTypeShortcutOpensWithoutServer` | the "Type" shortcut intent opens the sheet with no server configured |
| `SecretStoreTest` | `roundTripIsEncryptedAtRest` | Keystore AES-GCM round trip, new instance reads it, plaintext not in prefs, empty deletes |
| | `corruptValueReadsAsUnset` | tampered value reads as unset instead of crashing |
| | `settingsRoundTrip` | DataStore settings round trip, volume clamped |

## Actual CI results

Final CI run: **[40172648916](https://github.com/Hem456anth/HeyBuddy/actions/runs/40172648916)**
on commit `129b8d9` (2026-10-25, ubuntu-latest runners). Overall: **success**.

| Job | Result | Details |
|---|---|---|
| Backend (ruff + pytest) | ✅ success | ruff clean; **39 passed** (1 warning) |
| Android (core tests, lint, debug + release APK) | ✅ success | `:core:test` **58/58 passed**; `:app:lintDebug` **0 errors, 13 warnings** (no baseline); `lintVitalRelease` passed; debug + release APK built; release APK signature verified with `apksigner` (signer DN `CN=HeyBuddy CI Preview (not a Play key), O=HeyBuddy`) |
| Emulator API 26 (Android 8.0, google_apis x86_64) | ✅ success | **19/19** instrumented tests passed |
| Emulator API 30 (Android 11) | ✅ success | **19/19** passed |
| Emulator API 34 (Android 14) | ✅ success | **19/19** passed |
| Emulator API 35 (Android 15) | ✅ success | **19/19** passed |
| Publish pre-release | ✅ success | [`android-v0.1.0-preview`](https://github.com/Hem456anth/HeyBuddy/releases/tag/android-v0.1.0-preview) |

Release assets (SHA-256 from `SHA256SUMS.txt`; the release APK hash was
re-checked after downloading it from the release):

| Asset | Size | SHA-256 |
|---|---|---|
| `HeyBuddy-android-0.1.0.apk` (release, R8) | 2,564,953 B | `cc5c6a31c65fcfe4a450bafbfd0bfdcd7b55d56fa63ae224f4bab2810cfb8172` |
| `HeyBuddy-android-0.1.0-debug.apk` | 15,850,536 B | `80d0a7f2d83b0ce0fa97fc77d0d650b4ce675000893f775a0e46114871cf6f8c` |
| `HeyBuddy-android-0.1.0-test-reports.zip` | 730,624 B | `d173113c61e8e19e630836c5f7411e28470c8b5248c1b3a2a96c1d1353af5016` |

Each CI run generates a new throwaway signing key, so a later run's APK has a
different hash and certificate.

### History of fixes on the way to green

| Run | Commit | Outcome | Fix |
|---|---|---|---|
| [40171298092](https://github.com/Hem456anth/HeyBuddy/actions/runs/40171298092) | `5e62e8a` | :app compiled on the first CI attempt; **lint failed** with 3 errors | `NewApi`: `VoiceInteractionSession.AssistState` (API 29) used with minSdk 26 -> annotated `@RequiresApi(29)` and added the API 26-28 `onHandleAssist(Bundle, AssistStructure, AssistContent)` path; `FullBackupContent`: invalid `cache` domain removed from `backup_rules.xml`; `InlinedApi` warning: microphone FGS type only on API 30+. Not baselined. |
| [40171824417](https://github.com/Hem456anth/HeyBuddy/actions/runs/40171824417) | `675c5dd` | build/lint ✅; emulators 26/34/35 ✅ 19/19; **API 30: 18/19** | `companionTypeShortcutOpensWithoutServer` hit `ComposeNotIdleException` ("pending measure/layout" for ~25 s) on the first activity launch of the class, while the system was dex-optimising the test APK. Removed `animateContentSize()` from the sheet (it resizes a wrap-content window every animation frame, a possible layout feedback loop) and made the test's wait tolerate a busy emulator. Pre-release was still published (by design) with the failure reported. |
| [40172648916](https://github.com/Hem456anth/HeyBuddy/actions/runs/40172648916) | `129b8d9` | **all green** | — |

The API 30 failure did not reproduce after the fix, but it happened once, so
treat it as a possible flake (one green run is not proof it is gone).

Bug found by writing the tests: when the mic permission was denied the sheet
was supposed to switch to typing, but `CompanionSheet` captured
`startInTypeMode` only once (`remember { }`), so the switch never happened.
Fixed with `remember(startInTypeMode)`; covered by
`companionWithMicDeniedFallsBackToTyping`.

Lint warnings left (13, none security-related): `UnusedAttribute`
(`canTakeScreenshot` API 30+, widget `targetCell*` API 31+; ignored on older
versions by design), `ObsoleteSdkInt` (`mipmap-anydpi-v26`), `UnusedResources`
(one colour), `UseKtx` style suggestions.

## Not verified (no automated or real-device evidence)

* **Real devices**: no physical phone was used. OEM behaviour (Samsung,
  Xiaomi, etc.), battery optimisation killers and real screen sizes are untested.
* **Power key / assistant gesture**: that a long-press of the power/side key
  opens HeyBuddy after it is chosen as digital assistant. CI only proves the
  assistant service is declared correctly; emulators can't press a physical
  power key in a meaningful way and the role can't be granted unattended.
* **Accessibility service at runtime** (overlay over third-party apps, tap
  detection for guided steps, `insert_text`, accessibility screenshots): the
  service can't be enabled unattended in CI, so only its declaration is tested.
* **MediaProjection capture** (needs the consent dialog).
* **Live providers**: Vercel AI Gateway, Gradium and Composio were never
  called (no keys); all provider traffic in tests is mocked.
* **Voice**: speech recognition quality, TTS audio playback, voice quality
  and end-to-end latency.
* **Battery, memory, thermal, network usage**: not measured.
* **Release signing for distribution**: the CI key is a throwaway preview key.
