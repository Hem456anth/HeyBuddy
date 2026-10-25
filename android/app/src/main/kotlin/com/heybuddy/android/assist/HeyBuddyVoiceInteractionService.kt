package com.heybuddy.android.assist

import android.os.Bundle
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession

/**
 * HeyBuddy as the device's digital assistant. When the user picks HeyBuddy
 * under Settings > Apps > Default apps > Digital assistant app, the system
 * starts this service, and the assistant gesture (long-press power on
 * devices set to "Digital assistant", long-press home, corner swipe, the
 * headset assist key) opens a [HeyBuddySession] with a screenshot of the
 * current screen and its assist structure - no MediaProjection prompt.
 */
class HeyBuddyVoiceInteractionService : VoiceInteractionService() {
    override fun onReady() {
        super.onReady()
        instance = this
    }

    override fun onShutdown() {
        if (instance === this) instance = null
        super.onShutdown()
    }

    /** Ask the system for a new session (with a fresh screenshot), e.g. after a guided tap. */
    fun requestFreshSession(reason: String) {
        showSession(Bundle().apply { putString(ARG_REASON, reason) },
            VoiceInteractionSession.SHOW_WITH_ASSIST or VoiceInteractionSession.SHOW_WITH_SCREENSHOT)
    }

    companion object {
        const val ARG_REASON = "heybuddy_reason"
        @Volatile var instance: HeyBuddyVoiceInteractionService? = null
            private set
    }
}
