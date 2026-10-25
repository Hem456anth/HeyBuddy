// HeyBuddy for Android: root build. Modules:
//   :core  pure Kotlin/JVM logic (parser, mapping, Worker protocol, tools policy,
//          memory, jobs, beats). Unit-tested on any JVM, no Android SDK needed.
//   :app   the Android app (Compose UI, assistant/accessibility/overlay/capture/audio).
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.kotlin.serialization) apply false
}
