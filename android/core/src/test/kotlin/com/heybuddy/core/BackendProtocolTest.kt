package com.heybuddy.core

import com.heybuddy.core.backend.BackendClient
import com.heybuddy.core.backend.BackendStreamException
import com.heybuddy.core.backend.BackendUrlPolicy
import com.heybuddy.core.backend.ChatMessage
import com.heybuddy.core.backend.ChatPayloadBuilder
import com.heybuddy.core.backend.Role
import com.heybuddy.core.backend.SseEvent
import com.heybuddy.core.backend.SseParser
import com.heybuddy.core.backend.StreamAccumulator
import com.heybuddy.core.backend.ToolLoop
import com.heybuddy.core.backend.ToolOutcome
import com.heybuddy.core.geometry.CapturedFrame
import com.heybuddy.core.tools.ToolSpecs
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class BackendProtocolTest {
    private val backend = MockBackend()

    @After fun tearDown() = backend.shutdown()

    private fun client(token: String = backend.token) = BackendClient(backend.url, token)

    @Test fun pairingReturnsAToken() = runBlocking {
        assertEquals(backend.token, BackendClient(backend.url).pair("right", "Pixel 9"))
        try { BackendClient(backend.url).pair("wrong", "x"); fail("expected 403") } catch (error: BackendClient.BackendHttpException) {
            assertEquals(403, error.status)
            assertTrue(error.message!!.contains("wrong pairing secret"))
        }
    }

    @Test fun bearerTokenIsSentAndBadTokenExplains() = runBlocking {
        val config = client().config()
        assertTrue(config.llm && config.tts && config.integrations)
        assertEquals("anthropic/claude-sonnet-5.5", config.defaultModel)
        try { client("nope").config(); fail() } catch (error: BackendClient.BackendHttpException) {
            assertEquals(401, error.status)
            assertTrue(error.message!!.contains("pair this phone again"))
        }
    }

    @Test fun ttsAndSttRoundTrip() = runBlocking {
        assertArrayEquals(MockBackend.FAKE_WAV, client().tts("hello"))
        assertEquals("hello", Json.parseToJsonElement(backend.bodiesFor("/v1/tts").single()).jsonObject["text"]!!.jsonPrimitive.content)
        assertEquals("where is bluetooth", client().stt(byteArrayOf(1, 2, 3)))
        backend.ttsStatus = 503
        try { client().tts("x"); fail() } catch (error: BackendClient.BackendHttpException) { assertEquals(503, error.status) }
    }

    @Test fun chatStreamsHeyBuddyEvents() = runBlocking {
        backend.chatReplies += MockBackend.sseText("hello from the server")
        val accumulator = StreamAccumulator()
        val pieces = ArrayList<String>()
        client().chatStream(JsonObject(mapOf())) { event -> accumulator.feed(event).takeIf { it.isNotEmpty() }?.let(pieces::add) }
        assertEquals("hello from the server", pieces.joinToString(""))
        assertEquals("stop", accumulator.result().finishReason)
    }

    @Test fun sseParserAndErrors() {
        val parser = SseParser()
        val events = listOf(": comment", "event: x", "data: a", "data: b", "", "data: c", "").mapNotNull(parser::feedLine)
        assertEquals(listOf("x" to "a\nb", "message" to "c"), events.map { it.event to it.data })
        try {
            StreamAccumulator().feed(SseEvent("error", "{\"message\":\"bad key\"}"))
            fail()
        } catch (error: BackendStreamException) { assertTrue(error.message!!.contains("bad key")) }
    }

    @Test fun payloadUsesOpenAiShapeWithInlineImageAndCapsHistory() {
        val builder = ChatPayloadBuilder("anthropic/claude-sonnet-5.5", "sys", historyMaxExchanges = 2)
        val history = ArrayList<ChatMessage>()
        for (i in 0 until 5) { history += ChatMessage(Role.USER, "q$i"); history += ChatMessage(Role.ASSISTANT, "a$i") }
        history += ChatMessage(Role.USER, "where is the save button?")
        val frames = listOf(CapturedFrame(byteArrayOf(1, 2, 3), "screen 1", 1, 576, 1280, 0, 0, 1080, 2400))
        val payload = builder.build(history, frames, ToolSpecs.definitions(listOf("open_url")))
        val messages = payload["messages"]!!.jsonArray
        assertEquals(5, messages.size)
        assertEquals("q3", messages[0].jsonObject["content"]!!.jsonPrimitive.content)
        val parts = messages.last().jsonObject["content"]!!.jsonArray
        assertEquals(listOf("image_url", "text", "text"), parts.map { it.jsonObject["type"]!!.jsonPrimitive.content })
        assertEquals("data:image/jpeg;base64,AQID", parts[0].jsonObject["image_url"]!!.jsonObject["url"]!!.jsonPrimitive.content)
        val tool = payload["tools"]!!.jsonArray[0].jsonObject
        assertEquals("function", tool["type"]!!.jsonPrimitive.content)
        assertEquals("open_url", tool["function"]!!.jsonObject["name"]!!.jsonPrimitive.content)
        assertEquals("sys", payload["system"]!!.jsonPrimitive.content)
    }

    @Test fun toolLoopSendsAssistantToolCallsAndToolResults() = runBlocking {
        backend.chatReplies += MockBackend.sseToolCall("let me check", "call_1", "web_fetch", "{\"url\":\"https://example.org\"}")
        backend.chatReplies += MockBackend.sseText("done: it says hi")
        val calls = ArrayList<String>()
        val rounds = ArrayList<Boolean>()
        val payload = ChatPayloadBuilder(null, "s").build(listOf(ChatMessage(Role.USER, "fetch it")), tools = ToolSpecs.definitions(listOf("web_fetch")))
        val texts = ToolLoop(client()).run(payload, execute = { call -> calls += call.name; ToolOutcome("hi", false) },
            onRoundText = { _, more -> rounds += more })
        assertEquals(listOf("let me check", "done: it says hi"), texts)
        assertEquals(listOf("web_fetch"), calls)
        assertEquals(listOf(true, false), rounds)
        val second = Json.parseToJsonElement(backend.chatBodies()[1]).jsonObject["messages"]!!.jsonArray
        val assistant = second[second.size - 2].jsonObject
        assertEquals("assistant", assistant["role"]!!.jsonPrimitive.content)
        val fn = assistant["tool_calls"]!!.jsonArray[0].jsonObject["function"]!!.jsonObject
        assertEquals("web_fetch", fn["name"]!!.jsonPrimitive.content)
        assertEquals("{\"url\":\"https://example.org\"}", fn["arguments"]!!.jsonPrimitive.content)
        val toolMessage = second.last().jsonObject
        assertEquals("tool", toolMessage["role"]!!.jsonPrimitive.content)
        assertEquals("call_1", toolMessage["tool_call_id"]!!.jsonPrimitive.content)
    }

    @Test fun rejectedCallsNeverExecuteAndLastRoundHasNoTools() = runBlocking {
        backend.chatReplies += MockBackend.sseToolCall("", "c1", "cx_BANK_TRANSFER", "{}", kind = "rejected", extra = ",\"error\":\"unknown tool\"")
        backend.chatReplies += MockBackend.sseToolCall("", "c2", "read_memory", "{}")
        var executed = 0
        val payload = ChatPayloadBuilder(null, "s").build(listOf(ChatMessage(Role.USER, "x")), tools = ToolSpecs.definitions(listOf("read_memory")))
        ToolLoop(client()).run(payload, execute = { executed++; ToolOutcome("ok", false) }, maxRounds = 2)
        assertEquals(0, executed)
        val bodies = backend.chatBodies().map { Json.parseToJsonElement(it).jsonObject }
        assertTrue(bodies[0].containsKey("tools"))
        assertTrue(!bodies[1].containsKey("tools"))
        assertEquals("false", bodies[1]["integrations"].toString())
        val refused = bodies[1]["messages"]!!.jsonArray.last().jsonObject["content"]!!.jsonPrimitive.content
        assertTrue(refused.contains("refused by the server"))
    }

    @Test fun integrationEndpoints() = runBlocking {
        val state = client().integrations()
        assertEquals("gmail", state.connections.single().toolkit)
        assertTrue(client().connect("gmail").startsWith("https://connect.composio.dev/"))
        assertEquals("GMAIL_SEND_EMAIL", client().actionLogs().single().toolSlug)
        client().deleteLogs("l1"); client().deleteLogs(); client().disconnect("c1")
        assertTrue(backend.requests.any { it.first == "DELETE" && it.second == "/v1/integrations/logs/l1" })
        assertTrue(backend.requests.any { it.first == "DELETE" && it.second == "/v1/integrations/c1" })
    }

    @Test fun backendUrlPolicy() {
        assertEquals("https://buddy.example.onrender.com", BackendUrlPolicy.normalize(" buddy.example.onrender.com/ "))
        assertTrue(BackendUrlPolicy.check("https://heybuddy.fly.dev") is BackendUrlPolicy.Verdict.Ok)
        assertTrue(BackendUrlPolicy.check("http://10.0.2.2:8000") is BackendUrlPolicy.Verdict.Warning)
        assertTrue(BackendUrlPolicy.check("http://localhost:8000") is BackendUrlPolicy.Verdict.Warning)
        assertTrue(BackendUrlPolicy.check("http://192.168.1.20:8000") is BackendUrlPolicy.Verdict.Rejected)
        assertTrue(BackendUrlPolicy.check("http://evil.com") is BackendUrlPolicy.Verdict.Rejected)
        assertTrue(BackendUrlPolicy.check("ftp://x.dev") is BackendUrlPolicy.Verdict.Rejected)
        assertTrue(BackendUrlPolicy.check("") is BackendUrlPolicy.Verdict.Rejected)
        assertTrue(BackendUrlPolicy.check("https://your-server.example.com") is BackendUrlPolicy.Verdict.Rejected)
    }

    @Suppress("unused") private val keep = JsonArray(emptyList())
}
