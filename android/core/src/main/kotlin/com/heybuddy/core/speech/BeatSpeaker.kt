package com.heybuddy.core.speech

import com.heybuddy.core.program.Beat
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout

/** Speech back-ends the app provides (server TTS via the HeyBuddy backend, Android TextToSpeech). */
interface VoiceOutput {
    /** Primary voice: MP3 bytes for [text], or throws. */
    suspend fun synthesize(text: String): ByteArray

    /** Play MP3 bytes; call [onStarted] when sound starts; return when it ends. */
    suspend fun play(audio: ByteArray, onStarted: () -> Unit)

    /** Fallback on-device voice. Return false if unavailable (then beats are paced silently). */
    suspend fun speakFallback(text: String, onStarted: () -> Unit): Boolean
}

/**
 * Speaks beats one after another and reports when each starts, so the
 * overlay can reveal the marks that beat owns (port of the Windows
 * `BeatSpeaker`):
 *
 * - beats can be appended while earlier ones play ("let me check" from the
 *   first tool round, then the answer);
 * - the next line is synthesized while the current one plays;
 * - if the server voice fails, the device voice takes over; with voice off
 *   (or no device voice) beats are paced at ~2.6 words/s so marks still
 *   appear in step with the text.
 */
class BeatSpeaker(
    private val scope: CoroutineScope,
    private val voice: VoiceOutput?,
    private val useServerVoice: Boolean,
    private val useDeviceVoice: Boolean = true,
    private val onBeatStarted: (Beat) -> Unit,
    private val onAudioStarted: () -> Unit,
    private val onAllDone: () -> Unit,
    private val onError: (String) -> Unit,
    private val silentPacing: Boolean = true,
) {
    private class Item(val beat: Beat, val audio: Deferred<ByteArray?>?)

    private val queue = Channel<Item>(Channel.UNLIMITED)
    @Volatile private var serverVoiceFailed = !useServerVoice || voice == null
    @Volatile var cancelled = false
        private set
    private var audioAnnounced = false
    private val runner: Job = scope.launch { run() }

    fun add(beat: Beat) {
        if (cancelled) return
        val prefetch = if (!serverVoiceFailed && beat.text.isNotBlank() && voice != null) {
            scope.async {
                try {
                    voice.synthesize(beat.text)
                } catch (cancel: CancellationException) {
                    throw cancel
                } catch (error: Exception) {
                    if (!serverVoiceFailed) {
                        serverVoiceFailed = true
                        onError("Voice unavailable: ${error.message}")
                    }
                    null
                }
            }
        } else null
        queue.trySend(Item(beat, prefetch))
    }

    /** No more beats will be added; [onAllDone] fires after the last one. */
    fun close() { queue.close() }

    fun cancel() {
        cancelled = true
        queue.close()
        runner.cancel()
    }

    suspend fun join() = runner.join()

    private fun announce() {
        if (!audioAnnounced) { audioAnnounced = true; onAudioStarted() }
    }

    private suspend fun run() {
        try {
            for (item in queue) {
                if (cancelled) break
                speakOne(item)
            }
        } catch (cancel: CancellationException) {
            throw cancel
        } catch (error: Exception) {
            onError("Speech failed: ${error.message}")
        } finally {
            if (!cancelled) onAllDone()
        }
    }

    private suspend fun speakOne(item: Item) {
        val beat = item.beat
        val audio = item.audio?.let { deferred ->
            try { withTimeout(60_000) { deferred.await() } } catch (cancel: CancellationException) {
                if (cancelled) throw cancel
                null
            }
        }
        if (cancelled) return
        var shown = false
        val started = {
            if (!shown) { shown = true; announce(); onBeatStarted(beat) }
        }
        if (audio != null && audio.isNotEmpty() && voice != null) {
            try {
                voice.play(audio, started)
                if (!shown) started() // decode failed: still show the marks
                return
            } catch (cancel: CancellationException) {
                throw cancel
            } catch (error: Exception) {
                onError("Couldn't play the voice: ${error.message}")
            }
        }
        if (beat.text.isNotBlank() && voice != null && useDeviceVoice) {
            val spoke = try { voice.speakFallback(beat.text, started) } catch (cancel: CancellationException) { throw cancel } catch (_: Exception) { false }
            if (spoke) { if (!shown) started(); return }
        }
        // Silent beat: reveal marks now, then hold for a reading-speed pause.
        started()
        if (!silentPacing) return
        val words = beat.text.split(Regex("\\s+")).count { it.isNotEmpty() }
        var holdMs = (words / SILENT_WORDS_PER_SECOND * 1000).toLong()
        if (beat.marks.isNotEmpty()) holdMs = maxOf(holdMs, SILENT_MIN_MS_WITH_MARKS)
        delay(holdMs)
    }

    companion object {
        const val SILENT_WORDS_PER_SECOND = 2.6
        const val SILENT_MIN_MS_WITH_MARKS = 1200L
    }
}
