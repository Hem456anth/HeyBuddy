package com.heybuddy.core

import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue

/** A fake HeyBuddy backend speaking the real /v1 wire protocol (see backend/heybuddy_backend/app.py). */
class MockBackend(val token: String = "hb_test_token") {
    val server = MockWebServer()
    val chatReplies = LinkedBlockingQueue<String>()
    val executeReplies = LinkedBlockingQueue<MockResponse>()
    val requests = CopyOnWriteArrayList<Triple<String, String, String>>() // method, path, body
    var ttsStatus = 200

    init {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val body = request.body.readUtf8()
                val path = request.path ?: ""
                requests += Triple(request.method ?: "", path, body)
                if (path == "/v1/pair") {
                    return if (body.contains("\"pairing_secret\":\"right\"")) json("{\"token\":\"$token\",\"device_id\":\"d1\"}")
                    else MockResponse().setResponseCode(403).setBody("{\"detail\":\"wrong pairing secret\"}")
                }
                if (request.getHeader("Authorization") != "Bearer $token") {
                    return MockResponse().setResponseCode(401).setBody("{\"detail\":\"unknown or revoked device token\"}")
                }
                return when {
                    path == "/v1/chat" -> MockResponse().setHeader("content-type", "text/event-stream")
                        .setBody(chatReplies.poll() ?: sseText("(no scripted reply)"))
                    path == "/v1/tts" -> if (ttsStatus == 200) MockResponse().setHeader("content-type", "audio/wav").setBody(okio.Buffer().write(FAKE_WAV))
                        else MockResponse().setResponseCode(ttsStatus).setBody("{\"detail\":\"server text-to-speech is not configured; use the device voice\"}")
                    path == "/v1/stt" -> json("{\"text\":\"where is bluetooth\"}")
                    path == "/v1/config" -> json("{\"llm\":true,\"models\":[\"anthropic/claude-sonnet-5.5\"],\"default_model\":\"anthropic/claude-sonnet-5.5\",\"tts\":true,\"stt\":false,\"integrations\":true,\"toolkits\":[\"gmail\"]}")
                    path == "/v1/integrations/execute" -> executeReplies.poll() ?: json("{\"successful\":true,\"data\":\"{\\\"id\\\":\\\"m1\\\"}\",\"error\":null}")
                    path == "/v1/integrations/connect" -> json("{\"redirect_url\":\"https://connect.composio.dev/link/x\",\"connection_id\":\"c1\"}")
                    path == "/v1/integrations" && request.method == "GET" -> json("{\"enabled\":true,\"toolkits\":[\"gmail\"],\"connections\":[{\"id\":\"c1\",\"toolkit\":\"gmail\",\"status\":\"ACTIVE\"}]}")
                    path.startsWith("/v1/integrations/logs") && request.method == "GET" -> json("{\"logs\":[{\"id\":\"l1\",\"tool_slug\":\"GMAIL_SEND_EMAIL\",\"risk\":\"irreversible\",\"status\":\"ok\",\"summary\":\"GMAIL_SEND_EMAIL ok\",\"created_at\":1.5}]}")
                    request.method == "DELETE" -> json("{\"deleted\":1}")
                    else -> MockResponse().setResponseCode(404).setBody("{\"detail\":\"Not Found\"}")
                }
            }
        }
        server.start()
    }

    private fun json(body: String) = MockResponse().setHeader("content-type", "application/json").setBody(body)

    val url: String get() = server.url("/").toString().trimEnd('/')
    fun chatBodies(): List<String> = requests.filter { it.second == "/v1/chat" }.map { it.third }
    fun bodiesFor(path: String): List<String> = requests.filter { it.second == path }.map { it.third }
    fun shutdown() = server.shutdown()

    companion object {
        val FAKE_WAV = "RIFF\u0024\u0000\u0000\u0000WAVEfmt ".toByteArray() + ByteArray(24)

        private fun esc(text: String) = text.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")

        fun sseText(text: String, finish: String = "stop"): String {
            val builder = StringBuilder()
            for (chunk in text.chunked(maxOf(1, text.length / 3 + 1))) {
                builder.append("event: text\ndata: {\"text\":\"${esc(chunk)}\"}\n\n")
            }
            builder.append("event: done\ndata: {\"finish_reason\":\"$finish\"}\n\n")
            return builder.toString()
        }

        fun sseToolCall(text: String, id: String, name: String, argumentsJson: String, kind: String = "local", extra: String = ""): String {
            val builder = StringBuilder()
            if (text.isNotEmpty()) builder.append("event: text\ndata: {\"text\":\"${esc(text)}\"}\n\n")
            builder.append("event: tool_call\ndata: {\"id\":\"$id\",\"name\":\"$name\",\"arguments\":$argumentsJson,\"kind\":\"$kind\"$extra}\n\n")
            builder.append("event: done\ndata: {\"finish_reason\":\"tool_calls\"}\n\n")
            return builder.toString()
        }
    }
}
