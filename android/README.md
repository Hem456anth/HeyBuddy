# HeyBuddy for Android (0.1 preview)

Summon HeyBuddy over any app, ask out loud or type, and it answers about
what's on your screen, draws marks over it, walks you through taps, and can
act through tools and connected apps with your approval.

It needs a **HeyBuddy backend** you run yourself (holds the AI keys). See
[`../backend/DEPLOY.md`](../backend/DEPLOY.md). Architecture:
[`../docs/ANDROID_ARCHITECTURE.md`](../docs/ANDROID_ARCHITECTURE.md).

> Preview status: built and tested in CI (unit tests, lint, emulator tests on
> API 26/30/34/35, see [`../docs/ANDROID_TESTING.md`](../docs/ANDROID_TESTING.md)).
> **Not yet tested on a real phone**, with live AI/voice providers, or for
> battery and latency.

## Requirements

* Android 8.0 (API 26) or newer.
* A running HeyBuddy backend reachable over HTTPS, and its pairing secret.

## 1. Install the APK (sideload)

1. Open the repo's Releases page and download from the pre-release
   **`android-v0.1.0-preview`**:
   * `HeyBuddy-android-0.1.0.apk` (release build, R8-minified), and
   * `SHA256SUMS.txt`.
2. Optional, verify: `sha256sum -c SHA256SUMS.txt --ignore-missing`.
3. On the phone open the APK; allow "Install unknown apps" for your browser
   or file manager when Android asks.
4. The APK is signed with a **throwaway key generated in CI**, not a Play
   Store key. Each preview build may carry a different certificate, so if
   Android refuses to update, uninstall the previous preview first.

The `-debug.apk` is the same app with the debug key and no minification
(useful for `adb` debugging).

## 2. Onboarding

The app walks you through one step at a time; everything except the server is
optional and skippable:

1. **Server**: your backend URL (`https://...`) and pairing secret -> Connect.
2. **Microphone**: needed for voice; deny it and you can still type.
3. **Notifications** (Android 13+): job results and approvals.
4. **Digital assistant** (see below).
5. **Accessibility helper** (see below).
6. **Display over other apps**: alternative way to draw marks.

## 3. Set HeyBuddy as the default assistant

This is the official way to summon an app from a hardware gesture.

* Settings > Apps > Default apps > **Digital assistant app** > HeyBuddy
  (wording varies by phone; the onboarding button opens the right screen).
* Then use your phone's assistant gesture:
  * **Pixel 6 and later:** press and hold the power button (Settings > System >
    Gestures > Press and hold power button > Digital assistant).
  * **Samsung:** Settings > Advanced features > Side button > Press and hold >
    Digital assistant (where offered).
  * Others: swipe up from a bottom corner, or long-press Home on 3-button nav,
    where the phone supports it.
* HeyBuddy never changes the power button itself. The power menu and
  **Emergency SOS** (press power 5 times) keep working exactly as before.
  Apps cannot read a 5-second power hold; see
  [`../docs/ANDROID_ACTIVATION.md`](../docs/ANDROID_ACTIVATION.md).

Other ways to summon: the **Quick Settings tile** (edit tiles, drag "HeyBuddy"),
the **home-screen widget**, **long-press the app icon** for "Talk"/"Type", the
**accessibility button**, or the optional "Tap to talk" notification.

## 4. Accessibility helper on Android 13+ (restricted settings)

Android blocks accessibility for apps installed outside an app store until you
allow it:

1. Settings > Accessibility > HeyBuddy: if the switch is greyed out, go back.
2. Settings > Apps > HeyBuddy > **⋮ (top right) > Allow restricted settings**
   (confirm with your PIN/fingerprint).
3. Return to Settings > Accessibility > HeyBuddy and turn it on.

What it's used for: drawing over other apps, noticing when you tap the
highlighted control, typing text you approved, screenshots without asking each
time (Android 11+), and the accessibility button. It never taps for you and
never types into password fields. More in
[`../docs/SECURITY_AUDIT.md`](../docs/SECURITY_AUDIT.md).

## 5. Pairing details

* Pairing sends the secret once; the server returns a device token that is
  stored encrypted with an Android Keystore key. The secret isn't stored.
* Settings > Server > **Disconnect** revokes the token on the server and
  deletes it from the phone.
* Pairing a second phone: use the same secret (until you rotate it).

## Build from source

```bash
cd android
./gradlew :core:test                 # pure-JVM tests, any machine with JDK 17
./gradlew :app:assembleDebug         # needs the Android SDK (platform 37, build-tools 36)
./gradlew :app:connectedDebugAndroidTest   # with an emulator/device attached
```

CI (`.github/workflows/android-build.yml`) runs all of this plus lint and a
signed release build. For a release build with your own key set
`HEYBUDDY_KEYSTORE_FILE`, `HEYBUDDY_KEYSTORE_PASSWORD`, `HEYBUDDY_KEY_ALIAS`;
without them the release APK is signed with the debug key.

## Troubleshooting

* **"Connect this phone to your HeyBuddy server"**: pair first (Settings > Server).
* **Long-press power opens the power menu**: your phone maps that gesture to the
  power menu or only allows its own assistant; use the tile/widget, or change
  the gesture setting if your phone offers it.
* **No marks drawn over apps**: enable the accessibility helper or "Display over
  other apps"; otherwise marks are shown on a copy of your screen in the card.
* **Jobs never finish on some phones**: allow HeyBuddy to ignore battery
  optimisations (Settings > HeyBuddy > Battery) — see <https://dontkillmyapp.com/>.
