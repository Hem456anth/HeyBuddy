package com.heybuddy.core.turn

import com.heybuddy.core.geometry.CapturedFrame
import com.heybuddy.core.geometry.MarkResolver
import com.heybuddy.core.geometry.ResolvedGeometry
import com.heybuddy.core.memory.MemoryStore
import com.heybuddy.core.program.BuddyProgram
import com.heybuddy.core.program.BuddyProgramParser
import com.heybuddy.core.program.Mark
import com.heybuddy.core.prompt.Prompts
import com.heybuddy.core.speech.BeatSpeaker
import com.heybuddy.core.tools.ToolContext
import com.heybuddy.core.tools.ToolRunner
import com.heybuddy.core.tools.ToolSpecs
import com.heybuddy.core.backend.ChatMessage
import com.heybuddy.core.backend.ChatPayloadBuilder
import com.heybuddy.core.backend.Role
import com.heybuddy.core.backend.ToolLoop
import com.heybuddy.core.backend.ToolOutcome
import com.heybuddy.core.backend.BackendClient

/** Rolling conversation memory: completed exchanges only, last N kept. */
class Conversation(private val maxExchanges: Int = ChatPayloadBuilder.HISTORY_MAX_EXCHANGES) {
    private val messages = ArrayList<ChatMessage>()

    @Synchronized fun snapshot(): List<ChatMessage> = messages.toList()

    @Synchronized
    fun addExchange(user: String, assistant: String) {
        messages += ChatMessage(Role.USER, user)
        messages += ChatMessage(Role.ASSISTANT, assistant.ifBlank { "(no reply)" })
        while (messages.size > maxExchanges * 2) messages.removeAt(0)
    }

    @Synchronized fun clear() = messages.clear()
}

interface TurnListener {
    /** What the companion sheet shows while the reply streams (no code or tags). */
    fun onPartialText(text: String) {}
    /** A round's program, with display-px geometry for every mark it kept. */
    fun onProgram(program: BuddyProgram, geometry: Map<Mark, ResolvedGeometry>) {}
    fun onToolStarted(name: String) {}
}

data class TurnResult(
    val replyText: String,
    val spokenText: String,
    val programs: List<BuddyProgram>,
    val geometry: Map<Mark, ResolvedGeometry>,
) {
    val targets: List<Mark> get() = programs.flatMap { it.targets }.filter { geometry.containsKey(it) }
    val taskTitle: String get() = programs.map { it.taskTitle }.lastOrNull { it.isNotEmpty() }.orEmpty()
}

/**
 * One companion turn: question (+ screenshots) -> the model via the HeyBuddy backend,
 * tool rounds with approvals, buddy programs parsed per round, beats queued
 * on the speaker, facts saved to memory. Platform-free so it is tested on
 * the JVM against a mock backend.
 */
class TurnEngine(
    private val backend: BackendClient,
    private val payloadBuilder: ChatPayloadBuilder,
    private val memory: MemoryStore?,
    private val toolsEnabled: Boolean,
    private val toolContext: () -> ToolContext?,
    private val maxRounds: Int = 6,
    private val integrationGate: IntegrationGate? = null,
) {
    private val loop = ToolLoop(backend)

    fun systemPrompt(): String {
        val memoryBlock = memory?.promptBlock().orEmpty()
        return if (memoryBlock.isEmpty()) Prompts.COMPANION_SYSTEM_PROMPT else Prompts.COMPANION_SYSTEM_PROMPT + "\n" + memoryBlock + "\n"
    }

    suspend fun run(
        userText: String,
        frames: List<CapturedFrame>,
        history: List<ChatMessage>,
        speaker: BeatSpeaker?,
        listener: TurnListener,
    ): TurnResult {
        payloadBuilder.systemPrompt = systemPrompt()
        val context = if (toolsEnabled) toolContext() else null
        val tools = if (context != null) ToolSpecs.definitions(ToolSpecs.VOICE_TOOLS) else null
        payloadBuilder.integrations = integrationGate != null
        val payload = payloadBuilder.build(history + ChatMessage(Role.USER, userText), frames, tools)
        val runner = context?.let { ToolRunner(it) }
        val streamed = StringBuilder()
        val programs = ArrayList<BuddyProgram>()
        val geometry = LinkedHashMap<Mark, ResolvedGeometry>()

        val texts = loop.run(
            payload,
            execute = { call ->
                listener.onToolStarted(call.name)
                when (call.kind) {
                    "integration" -> integrationGate?.run(call) ?: ToolOutcome("integrations are turned off", true)
                    else -> runner?.execute(call, ToolSpecs.VOICE_TOOLS) ?: ToolOutcome("tools are turned off", true)
                }
            },
            onRoundText = { text, _ ->
                val program = BuddyProgramParser.parse(text)
                val resolved = MarkResolver.resolve(program, frames)
                geometry.putAll(resolved)
                for (fact in program.notedFacts) memory?.add(fact)
                programs += program
                listener.onProgram(program, resolved)
                program.beats.forEach { speaker?.add(it) }
                streamed.append("\n")
            },
            onText = { piece ->
                streamed.append(piece)
                listener.onPartialText(BuddyProgramParser.displayTextForPartial(streamed.toString()))
            },
            maxRounds = maxRounds,
        )
        val reply = texts.joinToString("\n").trim()
        val spoken = programs.joinToString(" ") { it.spokenText }.trim()
        return TurnResult(reply.take(MAX_HISTORY_REPLY_CHARS), spoken, programs, geometry)
    }

    companion object {
        const val MAX_HISTORY_REPLY_CHARS = 4000

        /** The follow-up message sent after the user taps a target (Windows: "[app] the user clicked ..."). */
        fun targetFollowUp(label: String): String {
            val name = label.ifBlank { "the highlighted spot" }
            return "[app] the user tapped \"$name\". here is their screen now. continue with the next step, or say it's done."
        }
    }
}
