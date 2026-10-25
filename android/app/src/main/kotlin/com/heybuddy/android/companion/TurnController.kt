package com.heybuddy.android.companion

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.core.content.ContextCompat
import com.heybuddy.android.AppGraph
import com.heybuddy.android.audio.AndroidVoice
import com.heybuddy.android.audio.ServerSpeechInput
import com.heybuddy.android.audio.DeviceSpeechInput
import com.heybuddy.android.audio.SpeechInput
import com.heybuddy.android.service.CompanionService
import com.heybuddy.android.settings.AppSettings
import com.heybuddy.android.settings.SecretStore
import com.heybuddy.android.settings.SpeechInputMode
import com.heybuddy.android.settings.VoiceMode
import com.heybuddy.android.tools.AndroidToolPlatform
import com.heybuddy.core.geometry.CapturedFrame
import com.heybuddy.core.geometry.OverlayMapper
import com.heybuddy.core.geometry.ResolvedGeometry
import com.heybuddy.core.jobs.Job
import com.heybuddy.core.program.Beat
import com.heybuddy.core.program.BuddyProgram
import com.heybuddy.core.program.Mark
import com.heybuddy.core.speech.BeatSpeaker
import com.heybuddy.core.tools.ToolContext
import com.heybuddy.core.turn.TurnEngine
import com.heybuddy.core.turn.TurnListener
import com.heybuddy.core.backend.ChatPayloadBuilder
import com.heybuddy.core.backend.BackendClient
import com.heybuddy.core.backend.BackendUrlPolicy
import com.heybuddy.core.turn.ActionConfirmer
import com.heybuddy.core.turn.IntegrationGate
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.concurrent.atomic.AtomicInteger

enum class Phase { IDLE, LISTENING, THINKING, SPEAKING, WAITING_FOR_TAP, ERROR }

data class CompanionUiState(
    val phase: Phase = Phase.IDLE,
    val heard: String = "",
    val reply: String = "",
    val level: Float = 0f,
    val error: String = "",
    val taskTitle: String = "",
    val targetLabel: String = "",
    val toolRunning: String = "",
    /** Screenshot + marks shown inside the sheet when no overlay surface is available. */
    val preview: Bitmap? = null,
    val previewMarks: List<Pair<Mark, ResolvedGeometry>> = emptyList(),
    val previewFrame: CapturedFrame? = null,
    val hasScreenshot: Boolean = false,
)

/** Supplies a screenshot for a turn (assist screenshot, accessibility capture, or one-frame projection). */
fun interface FrameSource {
    suspend fun capture(): CapturedFrame?
}

/**
 * The companion state machine (Windows `CompanionManager`, Android edition):
 *
 * ```
 * IDLE -> LISTENING (tap/hold) -> THINKING (release / send) -> SPEAKING -> IDLE
 * SPEAKING -> WAITING_FOR_TAP (a target() is on screen and guided steps are on)
 * WAITING_FOR_TAP -> THINKING (user tapped the target: follow-up turn with a fresh frame)
 * any -> IDLE (cancel)   any -> ERROR -> IDLE
 * ```
 * Every turn has a generation number; results from a superseded turn are dropped.
 */
class TurnController(private val context: Context, private val graph: AppGraph) {
    private val _state = MutableStateFlow(CompanionUiState())
    val state: StateFlow<CompanionUiState> = _state

    private val generation = AtomicInteger(0)
    private var turnJob: kotlinx.coroutines.Job? = null
    private var speechInput: SpeechInput? = null
    private var speaker: BeatSpeaker? = null
    private var voice: AndroidVoice? = null
    private var frameSource: FrameSource? = null
    private var pendingFrames: List<CapturedFrame> = emptyList()
    private var activeTargets: List<Pair<Mark, ResolvedGeometry>> = emptyList()
    private var lastFrame: CapturedFrame? = null
    /** Visible text from the assist structure (assistant sessions only); used once, for the next turn. */
    @Volatile var screenText: String = ""

    private suspend fun settings(): AppSettings = graph.settings.current()

    private fun backend(settings: AppSettings): BackendClient? {
        if (BackendUrlPolicy.check(settings.serverUrl) is BackendUrlPolicy.Verdict.Rejected) return null
        val token = graph.secrets.get(SecretStore.DEVICE_TOKEN)
        if (token.isBlank()) return null
        return BackendClient(settings.serverUrl, token)
    }

    private var lastUserText: String = ""
    private var lastFrames: List<CapturedFrame> = emptyList()

    private fun fail(message: String) {
        _state.update { it.copy(phase = Phase.ERROR, error = message, level = 0f) }
        CompanionService.endTurn(context)
    }

    fun setFrameSource(source: FrameSource?) { frameSource = source }

    /** Start a voice turn. [frames] were captured at activation, before any HeyBuddy UI covered the screen. */
    fun startListening(frames: List<CapturedFrame>) {
        cancelInternal(keepOverlay = false)
        val gen = generation.incrementAndGet()
        pendingFrames = frames
        lastFrame = frames.firstOrNull()
        _state.value = CompanionUiState(phase = Phase.LISTENING, hasScreenshot = frames.isNotEmpty(), preview = frames.firstOrNull()?.let(::decodePreview), previewFrame = frames.firstOrNull())
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            fail("HeyBuddy needs microphone access to hear you. You can still type.")
            return
        }
        CompanionService.startTurn(context, microphone = true)
        turnJob = graph.appScope.launch {
            val settings = settings()
            val client = backend(settings)
            val input: SpeechInput = if (settings.speechInput == SpeechInputMode.SERVER && client != null) {
                ServerSpeechInput(client, onLevel = { level -> _state.update { it.copy(level = level) } })
            } else {
                DeviceSpeechInput(context, onPartial = { text -> if (gen == generation.get()) _state.update { it.copy(heard = text) } },
                    onLevel = { level -> _state.update { it.copy(level = level) } })
            }
            speechInput = input
            try {
                input.start()
            } catch (cancel: CancellationException) {
                throw cancel
            } catch (error: Exception) {
                if (input is ServerSpeechInput) {
                    // Fall back to the on-device recognizer for this turn.
                    val fallback = DeviceSpeechInput(context, onPartial = { text -> _state.update { it.copy(heard = text) } }, onLevel = { level -> _state.update { it.copy(level = level) } })
                    speechInput = fallback
                    try { fallback.start() } catch (second: Exception) {
                        if (gen == generation.get()) fail("Couldn't start listening: ${error.message}")
                    }
                } else if (gen == generation.get()) {
                    fail("Couldn't start listening: ${error.message}")
                }
            }
        }
    }

    /** Release / "send": stop listening and run the turn with what was heard. */
    fun finishListening() {
        val gen = generation.get()
        val input = speechInput ?: return
        speechInput = null
        _state.update { it.copy(phase = Phase.THINKING, level = 0f) }
        graph.appScope.launch {
            turnJob?.join()
            var failure = ""
            val heard = try { input.finish() } catch (error: Exception) { failure = error.message.orEmpty(); "" }
            if (gen != generation.get()) return@launch
            if (heard.isBlank()) {
                val message = if (failure.isNotEmpty()) "Speech recognition failed: $failure" else "I didn't catch that. Tap Talk to try again."
                _state.update { it.copy(phase = Phase.IDLE, error = message) }
                CompanionService.endTurn(context)
                return@launch
            }
            _state.update { it.copy(heard = heard) }
            runTurn(gen, heard, pendingFrames)
        }
    }

    /** Typed question (quick-ask). */
    fun submitText(text: String, frames: List<CapturedFrame>? = null) {
        if (text.isBlank()) return
        cancelInternal(keepOverlay = false)
        val gen = generation.incrementAndGet()
        val useFrames = frames ?: pendingFrames
        lastFrame = useFrames.firstOrNull() ?: lastFrame
        _state.value = CompanionUiState(phase = Phase.THINKING, heard = text, hasScreenshot = useFrames.isNotEmpty(),
            preview = useFrames.firstOrNull()?.let(::decodePreview), previewFrame = useFrames.firstOrNull())
        graph.appScope.launch { runTurn(gen, text, useFrames) }
    }

    private suspend fun runTurn(gen: Int, userText: String, frames: List<CapturedFrame>) {
        val settings = settings()
        val client = backend(settings)
        if (client == null) { fail("Connect this phone to your HeyBuddy server in Settings first."); return }
        lastUserText = userText
        lastFrames = frames
        CompanionService.startTurn(context, microphone = false)
        val silent = settings.voiceMode == VoiceMode.OFF || settings.muted
        val voiceOut = if (silent) null else AndroidVoice(context, if (settings.voiceMode == VoiceMode.SERVER) client else null,
            settings.volumePercent.coerceIn(0, 100) / 100f)
        voice = voiceOut
        val annotationsOn = settings.annotations
        val beats = BeatSpeaker(
            scope = graph.appScope,
            voice = voiceOut,
            useServerVoice = settings.voiceMode == VoiceMode.SERVER,
            useDeviceVoice = !silent,
            onBeatStarted = { beat -> if (gen == generation.get()) revealBeat(beat, annotationsOn) },
            onAudioStarted = { if (gen == generation.get()) _state.update { it.copy(phase = Phase.SPEAKING) } },
            onAllDone = { if (gen == generation.get()) graph.appScope.launch { afterSpeech(gen, settings) } },
            onError = { message -> _state.update { it.copy(error = message) } },
        )
        speaker = beats
        val gate = if (settings.toolsEnabled && settings.integrationsEnabled) IntegrationGate(client, settings.policy, ActionConfirmer { title, detail, risk ->
            val label = when (risk) { "irreversible" -> "Can't be undone"; "read" -> "Reads your data"; else -> "Changes your account" }
            graph.approvals.approve(title, "$label\n\n$detail")
        }) else null
        val engine = TurnEngine(
            backend = client,
            payloadBuilder = ChatPayloadBuilder(settings.model.ifBlank { null }, ""),
            memory = if (settings.memoryEnabled) graph.memory else null,
            toolsEnabled = settings.toolsEnabled,
            toolContext = {
                ToolContext(
                    workingDir = graph.workspaceDir,
                    appFilesRoot = context.filesDir,
                    platform = AndroidToolPlatform(context),
                    policy = settings.policy,
                    memory = if (settings.memoryEnabled) graph.memory else null,
                    allowBackgroundJobs = settings.backgroundJobs,
                    startJob = { title, brief -> graph.jobs.start(title, brief) },
                )
            },
            integrationGate = gate,
        )
        val screen = screenText
        screenText = ""
        val modelText = if (screen.isBlank()) userText else
            "$userText\n\n" + com.heybuddy.core.turn.Untrusted.frame("screen_text", screen)
        try {
            val result = engine.run(modelText, frames, graph.conversation.snapshot(), beats, object : TurnListener {
                override fun onPartialText(text: String) {
                    if (gen == generation.get()) _state.update { it.copy(reply = text, toolRunning = "") }
                }
                override fun onProgram(program: BuddyProgram, geometry: Map<Mark, ResolvedGeometry>) {
                    if (gen != generation.get()) return
                    if (program.taskTitle.isNotEmpty()) _state.update { it.copy(taskTitle = program.taskTitle) }
                }
                override fun onToolStarted(name: String) {
                    if (gen == generation.get()) _state.update { it.copy(toolRunning = name) }
                }
            })
            if (gen != generation.get()) return
            graph.conversation.addExchange(userText, result.replyText)
            activeTargets = result.targets.mapNotNull { mark -> result.geometry[mark]?.let { mark to it } }
            _state.update { it.copy(reply = result.spokenText.ifEmpty { it.reply }, targetLabel = activeTargets.firstOrNull()?.first?.label.orEmpty()) }
            beats.close()
        } catch (cancel: CancellationException) {
            beats.cancel()
            throw cancel
        } catch (error: Exception) {
            beats.cancel()
            if (gen == generation.get()) fail(error.message ?: "Something went wrong")
        }
    }

    private fun revealBeat(beat: Beat, annotationsOn: Boolean) {
        val geometry = beat.marks.mapNotNull { mark -> lookupGeometry(mark)?.let { mark to it } }
        if (!annotationsOn) return
        val overlay = graph.overlay
        if (overlay.bestAvailableMode() != com.heybuddy.android.overlay.OverlayController.Mode.NONE) {
            overlay.showBeat(geometry, beat.clearBefore)
        }
        // Always mirror the marks onto the in-sheet preview (the only place they show without an overlay).
        _state.update {
            val base = if (beat.clearBefore) emptyList() else it.previewMarks
            it.copy(previewMarks = base + geometry)
        }
    }

    // Geometry is resolved by the engine per round; recompute from the frame when needed.
    private fun lookupGeometry(mark: Mark): ResolvedGeometry? {
        val frame = lastFrame ?: return null
        return com.heybuddy.core.geometry.MarkResolver.resolveOne(mark, frame)
    }

    private suspend fun afterSpeech(gen: Int, settings: AppSettings) {
        if (gen != generation.get()) return
        if (activeTargets.isNotEmpty() && settings.guidedSteps) {
            _state.update { it.copy(phase = Phase.WAITING_FOR_TAP) }
            return // marks stay until the tap
        }
        graph.overlay.lingerThenFade(settings.annotationLingerSeconds)
        _state.update { it.copy(phase = Phase.IDLE) }
        CompanionService.endTurn(context)
    }

    /** Accessibility saw a tap on a control at these screen bounds. */
    fun onUserTappedAt(left: Int, top: Int, right: Int, bottom: Int) {
        if (_state.value.phase != Phase.WAITING_FOR_TAP) return
        val rotation = com.heybuddy.android.capture.FrameEncoder.displayInfo(context).third.first
        val mapper = OverlayMapper(0, 0, rotation)
        val hit = activeTargets.firstOrNull { (_, geometry) ->
            mapper.displayRectHitsTarget(left, top, right, bottom, geometry, slackPx = 16f * context.resources.displayMetrics.density)
        } ?: return
        continueAfterTarget(hit.first.label)
    }

    /** "I did it" button, for when no accessibility service can see the tap. */
    fun continueAfterTarget(label: String = _state.value.targetLabel) {
        val gen = generation.incrementAndGet()
        activeTargets = emptyList()
        graph.overlay.clear()
        _state.update { it.copy(phase = Phase.THINKING, previewMarks = emptyList(), targetLabel = "", heard = "") }
        graph.appScope.launch {
            delay(TARGET_SETTLE_MS) // let the tapped screen settle before the fresh screenshot
            val frame = try { frameSource?.capture() } catch (_: Exception) { null }
            if (gen != generation.get()) return@launch
            val frames = listOfNotNull(frame)
            if (frame != null) {
                lastFrame = frame
                _state.update { it.copy(preview = decodePreview(frame), previewFrame = frame, hasScreenshot = true) }
            }
            runTurn(gen, TurnEngine.targetFollowUp(label), frames)
        }
    }

    /** Speak a finished job's first sentence (optional, Settings). */
    fun announceJob(job: Job) {
        if (_state.value.phase != Phase.IDLE && _state.value.phase != Phase.ERROR) return
        graph.appScope.launch {
            val settings = settings()
            if (settings.voiceMode == VoiceMode.OFF) return@launch
            if (settings.muted) return@launch
            val client = backend(settings)
            val out = AndroidVoice(context, if (settings.voiceMode == VoiceMode.SERVER) client else null, settings.volumePercent / 100f)
            val line = "${job.title} is done. " + job.summaryLine.substringBefore(". ").take(160)
            val beats = BeatSpeaker(graph.appScope, out, settings.voiceMode == VoiceMode.SERVER, true, {}, {}, { out.shutdown() }, {})
            beats.add(Beat(line))
            beats.close()
        }
    }

    /** Barge-in: interrupt speech (or a waiting guided step) and listen right away. */
    fun bargeIn() {
        val frames = lastFrame?.let { listOf(it) } ?: emptyList()
        startListening(frames)
    }

    /** Run the last question again (after an error or a bad answer). */
    fun retry() {
        val text = lastUserText
        if (text.isBlank()) return
        submitText(text, lastFrames)
    }

    /** Mute now: stops the current speech; later turns stay silent until unmuted. */
    fun setMuted(muted: Boolean) {
        graph.appScope.launch { graph.settings.update { it.copy(muted = muted) } }
        if (muted) {
            voice?.stopAll()
            speaker?.cancel()
            if (_state.value.phase == Phase.SPEAKING) _state.update { it.copy(phase = Phase.IDLE) }
        }
    }

    fun cancel() {
        cancelInternal(keepOverlay = false)
        generation.incrementAndGet()
        _state.value = CompanionUiState()
        CompanionService.endTurn(context)
    }

    fun dismissError() {
        if (_state.value.phase == Phase.ERROR) _state.update { it.copy(phase = Phase.IDLE) }
        _state.update { it.copy(error = "") }
    }

    private fun cancelInternal(keepOverlay: Boolean) {
        speechInput?.cancel(); speechInput = null
        speaker?.cancel(); speaker = null
        voice?.shutdown(); voice = null
        turnJob?.cancel(); turnJob = null
        activeTargets = emptyList()
        if (!keepOverlay) graph.overlay.clear()
    }

    private fun decodePreview(frame: CapturedFrame): Bitmap? = try {
        BitmapFactory.decodeByteArray(frame.jpeg, 0, frame.jpeg.size)
    } catch (_: Exception) { null }

    /** Capture a frame now from the best non-interactive source (used at activation). */
    suspend fun captureNow(): CapturedFrame? = withContext(Dispatchers.Default) {
        if (!settings().screenContext) return@withContext null
        try { frameSource?.capture() } catch (_: Exception) { null }
    }

    companion object {
        const val TARGET_SETTLE_MS = 700L
    }
}
