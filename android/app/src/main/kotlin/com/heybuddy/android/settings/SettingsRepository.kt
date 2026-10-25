package com.heybuddy.android.settings

import android.content.Context
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.heybuddy.core.tools.ApprovalPolicy
import com.heybuddy.core.tools.Decision
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "settings")

/** SERVER = Gradium TTS through the HeyBuddy backend (falls back to the device voice). */
enum class VoiceMode { SERVER, DEVICE, OFF }
/** DEVICE = Android SpeechRecognizer (default). SERVER = record a clip and send it to the backend (Gradium STT), only if the server enables it. */
enum class SpeechInputMode { DEVICE, SERVER }

data class AppSettings(
    val serverUrl: String = "",
    /** Empty = the server's default model (the server enforces its allowlist). */
    val model: String = "",
    val voiceMode: VoiceMode = VoiceMode.SERVER,
    val speechInput: SpeechInputMode = SpeechInputMode.DEVICE,
    val muted: Boolean = false,
    val volumePercent: Int = 100,
    val integrationsEnabled: Boolean = true,
    val autoAllowIntegrationReads: Boolean = true,
    val screenContext: Boolean = true,
    val mediaProjectionFallback: Boolean = false,
    val toolsEnabled: Boolean = true,
    val actionPolicy: Decision = Decision.ASK,
    val alwaysAsk: Boolean = false,
    val backgroundJobs: Boolean = true,
    val speakJobResults: Boolean = true,
    val memoryEnabled: Boolean = true,
    val annotations: Boolean = true,
    val annotationLingerSeconds: Int = 12,
    val guidedSteps: Boolean = true,
    val companionNotification: Boolean = false,
    val onboardingDone: Boolean = false,
) {
    val policy: ApprovalPolicy get() = ApprovalPolicy(actionPolicy, alwaysAsk, autoAllowIntegrationReads)
}

class SettingsRepository(private val context: Context) {
    private object Keys {
        val serverUrl = stringPreferencesKey("server_url")
        val muted = booleanPreferencesKey("muted")
        val volume = intPreferencesKey("volume_percent")
        val integrations = booleanPreferencesKey("integrations_enabled")
        val autoReads = booleanPreferencesKey("auto_allow_integration_reads")
        val model = stringPreferencesKey("model")
        val voiceMode = stringPreferencesKey("voice_mode")
        val speechInput = stringPreferencesKey("speech_input")
        val screenContext = booleanPreferencesKey("screen_context")
        val mediaProjection = booleanPreferencesKey("media_projection_fallback")
        val tools = booleanPreferencesKey("tools_enabled")
        val actionPolicy = stringPreferencesKey("action_policy")
        val alwaysAsk = booleanPreferencesKey("always_ask")
        val jobs = booleanPreferencesKey("background_jobs")
        val speakJobs = booleanPreferencesKey("speak_job_results")
        val memory = booleanPreferencesKey("memory_enabled")
        val annotations = booleanPreferencesKey("annotations")
        val linger = intPreferencesKey("annotation_linger_seconds")
        val guided = booleanPreferencesKey("guided_steps")
        val companionNotification = booleanPreferencesKey("companion_notification")
        val onboarding = booleanPreferencesKey("onboarding_done")
    }

    val flow: Flow<AppSettings> = context.dataStore.data.map(::fromPrefs)

    suspend fun current(): AppSettings = flow.first()

    private fun fromPrefs(p: Preferences) = AppSettings(
        serverUrl = p[Keys.serverUrl] ?: "",
        model = p[Keys.model] ?: "",
        voiceMode = p[Keys.voiceMode]?.let { runCatching { VoiceMode.valueOf(it) }.getOrNull() } ?: VoiceMode.SERVER,
        speechInput = p[Keys.speechInput]?.let { runCatching { SpeechInputMode.valueOf(it) }.getOrNull() } ?: SpeechInputMode.DEVICE,
        muted = p[Keys.muted] ?: false,
        volumePercent = p[Keys.volume] ?: 100,
        integrationsEnabled = p[Keys.integrations] ?: true,
        autoAllowIntegrationReads = p[Keys.autoReads] ?: true,
        screenContext = p[Keys.screenContext] ?: true,
        mediaProjectionFallback = p[Keys.mediaProjection] ?: false,
        toolsEnabled = p[Keys.tools] ?: true,
        actionPolicy = p[Keys.actionPolicy]?.let { runCatching { Decision.valueOf(it) }.getOrNull() } ?: Decision.ASK,
        alwaysAsk = p[Keys.alwaysAsk] ?: false,
        backgroundJobs = p[Keys.jobs] ?: true,
        speakJobResults = p[Keys.speakJobs] ?: true,
        memoryEnabled = p[Keys.memory] ?: true,
        annotations = p[Keys.annotations] ?: true,
        annotationLingerSeconds = p[Keys.linger] ?: 12,
        guidedSteps = p[Keys.guided] ?: true,
        companionNotification = p[Keys.companionNotification] ?: false,
        onboardingDone = p[Keys.onboarding] ?: false,
    )

    suspend fun update(transform: (AppSettings) -> AppSettings) {
        context.dataStore.edit { p ->
            val next = transform(fromPrefs(p))
            p[Keys.serverUrl] = next.serverUrl
            p[Keys.muted] = next.muted
            p[Keys.volume] = next.volumePercent.coerceIn(0, 100)
            p[Keys.integrations] = next.integrationsEnabled
            p[Keys.autoReads] = next.autoAllowIntegrationReads
            p[Keys.model] = next.model
            p[Keys.voiceMode] = next.voiceMode.name
            p[Keys.speechInput] = next.speechInput.name
            p[Keys.screenContext] = next.screenContext
            p[Keys.mediaProjection] = next.mediaProjectionFallback
            p[Keys.tools] = next.toolsEnabled
            p[Keys.actionPolicy] = next.actionPolicy.name
            p[Keys.alwaysAsk] = next.alwaysAsk
            p[Keys.jobs] = next.backgroundJobs
            p[Keys.speakJobs] = next.speakJobResults
            p[Keys.memory] = next.memoryEnabled
            p[Keys.annotations] = next.annotations
            p[Keys.linger] = next.annotationLingerSeconds
            p[Keys.guided] = next.guidedSteps
            p[Keys.companionNotification] = next.companionNotification
            p[Keys.onboarding] = next.onboardingDone
        }
    }
}
