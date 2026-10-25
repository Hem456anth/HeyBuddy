# How HeyBuddy can be summoned on Android (findings)

Goal: summon HeyBuddy as quickly as HeyClicky's desktop hotkey, using only
public Android mechanisms, and **never interfering with the power button's
system functions: the power menu, Emergency SOS, emergency calling, or the
screen on/off key.**

Sources were fetched on 2026-10-25 (copies in the build workspace, not in
the repo). Links are to the official pages.

## Summary

| Route | Works on | Needs | Status in HeyBuddy |
|---|---|---|---|
| **Default digital assistant** (VoiceInteractionService + assistant role) -> the phone's own assistant gesture (long-press power/side key where the OEM offers it, corner swipe, home long-press on 3-button nav) | Android 8.0+ (role API 29+); gesture availability is OEM/setting dependent | User picks HeyBuddy in Settings > Apps > Default apps > Digital assistant app | Implemented; declaration verified in emulator CI; **not** verified on a real phone's power key |
| Quick Settings tile | Android 7.0+ (tile add prompt 13+) | User adds the tile | Implemented; resolvable in CI |
| Home-screen widget (Glance) | All supported versions | User places it | Implemented; provider listed in CI |
| Launcher shortcuts ("Talk", "Type") | 7.1+ | Long-press the app icon | Implemented; resolvable in CI |
| Accessibility button / shortcut | 8.0+ | Accessibility service enabled (restricted setting on 13+ for sideloaded apps) | Implemented; not exercised in CI (cannot enable the service unattended) |
| Persistent "Tap to talk" notification | All | Notification permission (13+), user opt-in | Implemented, off by default |
| Intercepting the physical power key, a 5-second hold, multi-press, or volume keys in the background | **Not possible for apps** | n/a | **Deliberately not attempted** |

## The power button

* Android gives apps **no public API to receive or intercept the power key**
  in the background. `KeyEvent.KEYCODE_POWER` exists as a constant
  (<https://developer.android.com/reference/android/view/KeyEvent>), but key
  presses are only delivered to the focused window, and in AOSP the power key
  is consumed by the system window-manager policy before any app sees it
  (this last point is from the AOSP source, not a doc page; we did not find a
  public API that contradicts it).
* The power key already carries safety functions owned by the system:
  * **Emergency SOS**: "press the power button five times or more"
    (Android 12+), <https://support.google.com/android/answer/9319337>.
  * **Emergency/power menu**: on Android 11 and earlier (and Pixel 5 and
    earlier on 12) "Hold the Power button for 5 seconds", then Emergency; on
    Pixel 6+ power + volume up,
    <https://support.google.com/pixelphone/answer/7055029>.
* Therefore HeyBuddy **does not** try to detect long presses, multi-presses
  or 5-second holds, does not use accessibility key filtering on hardware
  keys, and does not ask for `BIND_DEVICE_ADMIN` or anything that changes
  power-key behaviour.

### The official route: become the digital assistant

* An app becomes eligible by declaring a `VoiceInteractionService`
  (<https://developer.android.com/reference/android/service/voice/VoiceInteractionService>)
  bound with `BIND_VOICE_INTERACTION`, a
  `VoiceInteractionSessionService`, and a `RecognitionService`.
* On API 29+ the assistant is the **`ROLE_ASSISTANT`** role
  ("android.app.role.ASSISTANT",
  <https://developer.android.com/reference/android/app/role/RoleManager>).
  In AOSP this role is not grantable through the in-app request dialog, so
  HeyBuddy tries `createRequestRoleIntent` and falls back to opening
  Settings (`ACTION_VOICE_INPUT_SETTINGS` -> `ACTION_MANAGE_DEFAULT_APPS_SETTINGS`).
  The onboarding text tells the user exactly where to pick it.
* Once HeyBuddy is the assistant, **whatever assistant gesture the phone
  offers** opens it. On Pixel 6+ "press and hold the Power button" is the
  assistant gesture, and Settings > System > Gestures > Press and hold power
  button lets the user choose assistant vs power menu
  (<https://support.google.com/pixelphone/answer/7443425>). Samsung exposes a
  similar choice under Side button settings. Some OEMs only allow their own
  assistant on the side key; then HeyBuddy can still be reached by corner
  swipe / home long-press / tile / widget.
* The power menu and Emergency SOS remain the system's: choosing an assistant
  does not disable SOS (5 presses) or the power + volume-up menu. HeyBuddy
  never changes these settings; it only opens the screen where the **user**
  chooses.
* With the assistant role, the system can hand HeyBuddy an **assist
  screenshot** and the assist structure (visible text) of the current app
  (`VoiceInteractionSession.onHandleScreenshot` / `onHandleAssist`,
  <https://developer.android.com/reference/android/service/voice/VoiceInteractionSession>),
  subject to the user's "Use screenshot" / "Use text from screen" settings.
  On API 26-28 the older `onHandleAssist(Bundle, AssistStructure, AssistContent)`
  overload is used.

## Other surfaces

* **Quick Settings tile**: `TileService` with `BIND_QUICK_SETTINGS_TILE`
  (<https://developer.android.com/develop/ui/views/quicksettings-tiles>). On
  13+ HeyBuddy can ask the system to add it (`requestAddTileService`).
* **Widget**: Jetpack Glance app widget
  (<https://developer.android.com/develop/ui/compose/glance>).
* **Shortcuts**: static `shortcuts.xml`
  (<https://developer.android.com/develop/ui/views/launch/shortcuts/creating-shortcuts>).
* **Accessibility button**: `AccessibilityServiceInfo.FLAG_REQUEST_ACCESSIBILITY_BUTTON`
  (<https://developer.android.com/reference/android/accessibilityservice/AccessibilityServiceInfo>).
  For APKs installed outside an app store, Android 13+ marks accessibility as
  a **restricted setting**; the user must open App info > ⋮ > Allow
  restricted settings first (<https://support.google.com/android/answer/12623953>).
* **Notification**: optional, user-enabled; uses the `specialUse` foreground
  service type only while shown
  (<https://developer.android.com/develop/background-work/services/fgs/service-types>).
  Starting it from the background is subject to
  <https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start>.

## Screen capture without the assistant role

* Accessibility `takeScreenshot` (API 30+,
  <https://developer.android.com/reference/android/accessibilityservice/AccessibilityService>).
* `MediaProjection`, opt-in, one frame, with the system consent dialog
  **every time** (Android 14+ requires consent per session),
  <https://developer.android.com/media/grow/media-projection>.

## What we explicitly do not do

* No power/volume key interception, no fake power menu, no overlay over the
  power menu, the emergency dialer, the lock screen or system dialogs.
* No `SYSTEM_ALERT_WINDOW` window drawn over the emergency/SOS UI (overlay
  windows are non-touchable and only exist during a turn).
* No hotword / always-listening: the microphone is used only during a turn
  the user started.
