package com.heybuddy.android.audio

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import com.heybuddy.core.backend.BackendClient
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/** One push-to-talk listening session. */
interface SpeechInput {
    suspend fun start()
    /** Stop listening and return the final transcript ("" if nothing was heard). */
    suspend fun finish(): String
    fun cancel()
}

/**
 * Optional server speech-to-text: records a push-to-talk clip (16 kHz mono
 * PCM16, max 60 s) and uploads it as WAV to the backend's /v1/stt (Gradium).
 * Only offered when the server says STT is configured. No partial results.
 */
class ServerSpeechInput(
    private val backend: BackendClient,
    private val onLevel: (Float) -> Unit,
) : SpeechInput {
    private val pcm = java.io.ByteArrayOutputStream()
    private val mic = MicStreamer(onChunk = { bytes, length ->
        synchronized(pcm) { if (pcm.size() < MAX_BYTES) pcm.write(bytes, 0, length) }
    }, onLevel = onLevel)

    override suspend fun start() = mic.start()

    override suspend fun finish(): String {
        kotlinx.coroutines.delay(150) // keep the last syllable
        mic.stop()
        val audio = synchronized(pcm) { pcm.toByteArray() }
        if (audio.size < MicStreamer.CHUNK_BYTES) return ""
        return backend.stt(Wav.fromPcm16(audio, MicStreamer.SAMPLE_RATE))
    }

    override fun cancel() { mic.stop(); synchronized(pcm) { pcm.reset() } }

    companion object { const val MAX_BYTES = MicStreamer.SAMPLE_RATE * 2 * 60 }
}

/** Minimal WAV (RIFF) writer for mono 16-bit PCM. */
object Wav {
    fun fromPcm16(pcm: ByteArray, sampleRate: Int): ByteArray {
        val header = java.nio.ByteBuffer.allocate(44).order(java.nio.ByteOrder.LITTLE_ENDIAN)
        header.put("RIFF".toByteArray()).putInt(36 + pcm.size).put("WAVE".toByteArray())
        header.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(sampleRate).putInt(sampleRate * 2).putShort(2).putShort(16)
        header.put("data".toByteArray()).putInt(pcm.size)
        return header.array() + pcm
    }
}

/** Fallback: Android's on-device/system SpeechRecognizer (the default; no server needed). */
class DeviceSpeechInput(
    private val context: Context,
    private val onPartial: (String) -> Unit,
    private val onLevel: (Float) -> Unit,
) : SpeechInput {
    private val main = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null
    private val result = CompletableDeferred<String>()
    @Volatile private var latest = ""

    override suspend fun start() = withContext(Dispatchers.Main) {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) throw IllegalStateException("No speech recognizer on this device")
        val instance = if (android.os.Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(context)) {
            SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
        } else {
            SpeechRecognizer.createSpeechRecognizer(context)
        }
        recognizer = instance
        instance.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {}
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) { onLevel(((rmsdB + 2f) / 12f).coerceIn(0f, 1f)) }
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}
            override fun onError(error: Int) { result.complete(latest) }
            override fun onResults(results: Bundle?) {
                val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull() ?: latest
                result.complete(text)
            }
            override fun onPartialResults(partialResults: Bundle?) {
                partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.let {
                    latest = it; onPartial(it)
                }
            }
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            .putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 4000L)
        instance.startListening(intent)
    }

    override suspend fun finish(): String {
        withContext(Dispatchers.Main) { recognizer?.stopListening() }
        val text = withTimeoutOrNull(4000) { result.await() } ?: latest
        main.post { recognizer?.destroy(); recognizer = null }
        return text
    }

    override fun cancel() {
        main.post { recognizer?.cancel(); recognizer?.destroy(); recognizer = null }
        result.complete("")
    }
}
