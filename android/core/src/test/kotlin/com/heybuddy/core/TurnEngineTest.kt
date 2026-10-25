package com.heybuddy.core

import com.heybuddy.core.geometry.CapturedFrame
import com.heybuddy.core.memory.MemoryStore
import com.heybuddy.core.program.Beat
import com.heybuddy.core.program.BuddyProgram
import com.heybuddy.core.program.Mark
import com.heybuddy.core.geometry.ResolvedGeometry
import com.heybuddy.core.speech.BeatSpeaker
import com.heybuddy.core.speech.VoiceOutput
import com.heybuddy.core.tools.ApprovalPolicy
import com.heybuddy.core.tools.ToolContext
import com.heybuddy.core.turn.Conversation
import com.heybuddy.core.turn.TurnEngine
import com.heybuddy.core.turn.TurnListener
import com.heybuddy.core.backend.ChatPayloadBuilder
import com.heybuddy.core.backend.BackendClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.Collections

/** End-to-end turn against a mock backend: program -> beats -> TTS -> marks, tools, memory, targets. */
class TurnEngineTest {
    @get:Rule val temp = TemporaryFolder()
    private val worker = MockBackend()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @After fun tearDown() = worker.shutdown()

    private val frame = CapturedFrame(byteArrayOf(9, 9), "screen 1", 1, 576, 1280, 0, 0, 1080, 2400)

    private class RecordingVoice(val worker: BackendClient, val failSynthesis: Boolean = false) : VoiceOutput {
        val events: MutableList<String> = Collections.synchronizedList(ArrayList())
        override suspend fun synthesize(text: String): ByteArray {
            if (failSynthesis) throw java.io.IOException("tts down")
            return worker.tts(text)
        }
        override suspend fun play(audio: ByteArray, onStarted: () -> Unit) { onStarted(); events += "play:${audio.size}"; delay(20) }
        override suspend fun speakFallback(text: String, onStarted: () -> Unit): Boolean { onStarted(); events += "device:$text"; return true }
    }

    private fun engine(memory: MemoryStore?, platform: FakePlatform = FakePlatform()): TurnEngine {
        val client = BackendClient(worker.url, worker.token)
        return TurnEngine(
            client, ChatPayloadBuilder("anthropic/claude-sonnet-5.5", ""), memory, toolsEnabled = true,
            toolContext = { ToolContext(temp.newFolder(), temp.root, platform, ApprovalPolicy(), memory) },
        )
    }

    @Test fun programTurnSpeaksBeatsInOrderAndRevealsMarks() = runBlocking {
        worker.chatReplies += MockBackend.sseText(
            "```buddy\nsay(\"the wifi switch is here\")\nring(48, 200, 480, 60, \"wifi\")\nnoted(\"Uses a Pixel\")\nsay(\"tap it\")\ntarget(48, 200, 480, 60, \"Wi-Fi\")\n```",
        )
        val memory = MemoryStore(File(temp.root, "memory.md"))
        val voice = RecordingVoice(BackendClient(worker.url, worker.token))
        val started = Collections.synchronizedList(ArrayList<Beat>())
        var done = false
        val speaker = BeatSpeaker(scope, voice, useServerVoice = true, onBeatStarted = { started += it }, onAudioStarted = {}, onAllDone = { done = true }, onError = {})
        val partials = ArrayList<String>()
        var programGeometry: Map<Mark, ResolvedGeometry> = emptyMap()
        val result = engine(memory).run("how do I turn on wifi", listOf(frame), Conversation().snapshot(), speaker, object : TurnListener {
            override fun onPartialText(text: String) { partials += text }
            override fun onProgram(program: BuddyProgram, geometry: Map<Mark, ResolvedGeometry>) { programGeometry = geometry }
        })
        speaker.close()
        withTimeout(5000) { speaker.join() }
        assertTrue(done)
        assertEquals(listOf("the wifi switch is here", "tap it"), started.map { it.text })
        assertEquals(listOf("ring"), started[0].marks.map { it.kind })
        assertEquals("Wi-Fi", result.targets.single().label)
        assertEquals(2, programGeometry.size)
        assertEquals(2, voice.events.count { it.startsWith("play:") })
        assertTrue(partials.none { it.contains("```") || it.contains("ring(") })
        assertTrue(memory.facts().single().endsWith("Uses a Pixel"))
        val body = Json.parseToJsonElement(worker.chatBodies().single()).jsonObject
        assertTrue(body["system"]!!.jsonPrimitive.content.contains("android"))
        val blocks = body["messages"]!!.jsonArray.last().jsonObject["content"]!!.jsonArray
        assertEquals("image_url", blocks[0].jsonObject["type"]!!.jsonPrimitive.content)
        assertTrue(body.containsKey("tools"))
    }

    @Test fun toolRoundSpeaksFirstThenAnswers() = runBlocking {
        worker.chatReplies += MockBackend.sseToolCall("```buddy\nsay(\"let me open it\")\n```", "t1", "open_url", "{\"url\":\"https://maps.example.org\"}")
        worker.chatReplies += MockBackend.sseText("```buddy\nsay(\"it's open\")\n```")
        val platform = FakePlatform()
        val voice = RecordingVoice(BackendClient(worker.url, worker.token), failSynthesis = true)
        val started = Collections.synchronizedList(ArrayList<String>())
        val errors = Collections.synchronizedList(ArrayList<String>())
        val speaker = BeatSpeaker(scope, voice, true, onBeatStarted = { started += it.text }, onAudioStarted = {}, onAllDone = {}, onError = { errors += it })
        val result = engine(null, platform).run("open maps", emptyList(), emptyList(), speaker, object : TurnListener {})
        speaker.close(); withTimeout(5000) { speaker.join() }
        assertEquals(listOf("opened https://maps.example.org"), platform.actions)
        assertEquals(listOf("let me open it", "it's open"), started)
        // server voice failed -> device voice spoke both lines
        assertEquals(listOf("device:let me open it", "device:it's open"), voice.events)
        assertTrue(errors.single().startsWith("Voice unavailable"))
        assertEquals("let me open it it's open", result.spokenText)
    }

    @Test fun silentPacingWithoutVoice() = runBlocking {
        val started = Collections.synchronizedList(ArrayList<Long>())
        val speaker = BeatSpeaker(scope, null, useServerVoice = false, onBeatStarted = { started += System.nanoTime() }, onAudioStarted = {}, onAllDone = {}, onError = {})
        speaker.add(Beat("one two three four five six", mutableListOf()))
        speaker.add(Beat("next", mutableListOf()))
        speaker.close(); withTimeout(5000) { speaker.join() }
        val gapMs = (started[1] - started[0]) / 1_000_000
        assertTrue("gap $gapMs", gapMs in 2000..3500)
    }

    @Test fun cancelStopsSpeech() = runBlocking {
        val voice = RecordingVoice(BackendClient(worker.url, worker.token))
        var allDone = false
        val speaker = BeatSpeaker(scope, voice, true, onBeatStarted = {}, onAudioStarted = {}, onAllDone = { allDone = true }, onError = {})
        speaker.cancel()
        speaker.add(Beat("ignored", mutableListOf()))
        delay(100)
        assertTrue(voice.events.isEmpty())
        assertTrue(!allDone)
    }

    @Test fun conversationKeepsTenExchangesAndFollowUpWording() {
        val conversation = Conversation()
        repeat(15) { conversation.addExchange("q$it", "a$it") }
        val snapshot = conversation.snapshot()
        assertEquals(20, snapshot.size)
        assertEquals("q5", snapshot.first().content)
        assertTrue(TurnEngine.targetFollowUp("Wi-Fi").startsWith("[app] the user tapped \"Wi-Fi\""))
    }
}
