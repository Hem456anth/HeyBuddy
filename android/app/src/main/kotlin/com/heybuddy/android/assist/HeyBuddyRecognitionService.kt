package com.heybuddy.android.assist

import android.content.Intent
import android.speech.RecognitionService
import android.speech.SpeechRecognizer

/**
 * The assistant role requires a RecognitionService in the voice-interaction
 * metadata. HeyBuddy does its recognition inside a turn (the system recognizer, or
 * optionally the HeyBuddy server), so this service is a stub that
 * politely reports "not available" to any other app that binds it.
 */
class HeyBuddyRecognitionService : RecognitionService() {
    override fun onStartListening(recognizerIntent: Intent?, listener: Callback?) {
        try { listener?.error(SpeechRecognizer.ERROR_CLIENT) } catch (_: Exception) {}
    }
    override fun onCancel(listener: Callback?) = Unit
    override fun onStopListening(listener: Callback?) = Unit
}
