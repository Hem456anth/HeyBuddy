package com.heybuddy.android.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlin.concurrent.thread
import kotlin.math.sqrt

/**
 * 16 kHz mono PCM16 microphone reader for push-to-talk. Started at the
 * beginning of a turn and stopped at its end: HeyBuddy never keeps the mic
 * open in the background. Chunks are 100 ms (3200 bytes), what AssemblyAI
 * expects (50-1000 ms frames).
 */
class MicStreamer(
    private val onChunk: (ByteArray, Int) -> Unit,
    private val onLevel: (Float) -> Unit = {},
) {
    @Volatile private var running = false
    private var record: AudioRecord? = null
    private var worker: Thread? = null

    /** Caller must hold RECORD_AUDIO; throws IllegalStateException if the mic can't open. */
    @SuppressLint("MissingPermission")
    fun start() {
        val minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val bufferSize = maxOf(minBuffer, CHUNK_BYTES * 4)
        val recorder = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufferSize)
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            recorder.release()
            throw IllegalStateException("The microphone is busy or unavailable")
        }
        record = recorder
        running = true
        recorder.startRecording()
        worker = thread(name = "HeyBuddyMic", isDaemon = true) {
            val buffer = ByteArray(CHUNK_BYTES)
            while (running) {
                val read = recorder.read(buffer, 0, buffer.size)
                if (read > 0) {
                    onChunk(buffer, read)
                    onLevel(rms(buffer, read))
                } else if (read < 0) {
                    break
                }
            }
        }
    }

    fun stop() {
        running = false
        try { record?.stop() } catch (_: IllegalStateException) {}
        worker?.join(500)
        record?.release()
        record = null
    }

    private fun rms(buffer: ByteArray, length: Int): Float {
        var sum = 0.0
        var count = 0
        var i = 0
        while (i + 1 < length) {
            val sample = (buffer[i].toInt() and 0xFF) or (buffer[i + 1].toInt() shl 8)
            sum += sample.toDouble() * sample
            count++
            i += 2
        }
        return if (count == 0) 0f else (sqrt(sum / count) / 32768.0).toFloat().coerceIn(0f, 1f)
    }

    companion object {
        const val SAMPLE_RATE = 16_000
        const val CHUNK_BYTES = 3200
    }
}
