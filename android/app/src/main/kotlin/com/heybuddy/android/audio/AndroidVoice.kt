package com.heybuddy.android.audio

import android.content.Context
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import com.heybuddy.core.speech.VoiceOutput
import com.heybuddy.core.backend.BackendClient
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.util.Locale
import java.util.UUID
import kotlin.coroutines.resume

/**
 * Voice output: Gradium WAV from the backend's /v1/tts played with
 * MediaPlayer; Android TextToSpeech as the fallback (and the only voice
 * when the user picks "device voice" or the server has no TTS).
 */
class AndroidVoice(
    private val context: Context,
    private val backend: BackendClient?,
    /** 0..1, applied to both the server voice and the device voice. */
    private val volume: Float = 1f,
) : VoiceOutput {
    private var tts: TextToSpeech? = null
    private var ttsReady: CompletableDeferred<Boolean>? = null
    @Volatile private var currentPlayer: MediaPlayer? = null

    override suspend fun synthesize(text: String): ByteArray {
        val client = backend ?: throw IllegalStateException("server voice is off")
        return client.tts(text)
    }

    override suspend fun play(audio: ByteArray, onStarted: () -> Unit) {
        val file = File(context.cacheDir, "beat-${UUID.randomUUID()}.wav")
        withContext(Dispatchers.IO) { file.writeBytes(audio) }
        try {
            withContext(Dispatchers.Main) {
                suspendCancellableCoroutine { continuation ->
                    val player = MediaPlayer()
                    currentPlayer = player
                    continuation.invokeOnCancellation { runCatching { player.stop() }; player.release() }
                    player.setAudioAttributes(
                        AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build(),
                    )
                    player.setDataSource(file.path)
                    player.setVolume(volume, volume)
                    player.setOnPreparedListener { it.start(); onStarted() }
                    player.setOnCompletionListener { it.release(); if (continuation.isActive) continuation.resume(Unit) }
                    player.setOnErrorListener { mp, _, _ -> mp.release(); if (continuation.isActive) continuation.resume(Unit); true }
                    player.prepareAsync()
                }
            }
        } finally {
            currentPlayer = null
            file.delete()
        }
    }

    private suspend fun ensureTts(): Boolean {
        val existing = ttsReady
        if (existing != null) return existing.await()
        val ready = CompletableDeferred<Boolean>()
        ttsReady = ready
        withContext(Dispatchers.Main) {
            tts = TextToSpeech(context.applicationContext) { status ->
                if (status == TextToSpeech.SUCCESS) tts?.language = Locale.getDefault()
                ready.complete(status == TextToSpeech.SUCCESS)
            }
        }
        return withTimeoutOrNull(5000) { ready.await() } ?: false
    }

    override suspend fun speakFallback(text: String, onStarted: () -> Unit): Boolean {
        if (!ensureTts()) return false
        val engine = tts ?: return false
        val id = UUID.randomUUID().toString()
        val done = CompletableDeferred<Unit>()
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) { if (utteranceId == id) onStarted() }
            override fun onDone(utteranceId: String?) { if (utteranceId == id) done.complete(Unit) }
            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) { if (utteranceId == id) done.complete(Unit) }
            override fun onError(utteranceId: String?, errorCode: Int) { if (utteranceId == id) done.complete(Unit) }
        })
        val params = android.os.Bundle().apply { putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, volume) }
        if (engine.speak(text, TextToSpeech.QUEUE_ADD, params, id) != TextToSpeech.SUCCESS) return false
        try {
            withTimeoutOrNull(60_000) { done.await() }
        } catch (cancel: kotlinx.coroutines.CancellationException) {
            engine.stop()
            throw cancel
        }
        return true
    }

    fun stopAll() {
        runCatching { currentPlayer?.stop() }
        tts?.stop()
    }

    fun shutdown() {
        stopAll()
        tts?.shutdown()
        tts = null
        ttsReady = null
    }
}
