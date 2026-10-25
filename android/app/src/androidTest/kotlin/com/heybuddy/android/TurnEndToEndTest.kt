package com.heybuddy.android

import android.graphics.Bitmap
import android.graphics.Color
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.heybuddy.android.companion.Phase
import com.heybuddy.android.settings.AppSettings
import com.heybuddy.android.settings.SecretStore
import com.heybuddy.android.settings.VoiceMode
import com.heybuddy.android.ui.pairDevice
import com.heybuddy.core.geometry.CapturedFrame
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue

/**
 * A full app turn on a device: settings + Keystore token -> TurnController ->
 * BackendClient (SSE) -> buddy-program parser -> beats -> UI state, against a
 * MockWebServer speaking the backend's /v1 protocol. No real provider is called.
 */
@RunWith(AndroidJUnit4::class)
class TurnEndToEndTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext
    private val graph get() = context.graph
    private val token = "hb_device_test_token"
    private val server = MockWebServer()
    private val chatReplies = LinkedBlockingQueue<String>()
    private val requests = CopyOnWriteArrayList<Triple<String, String?, String>>() // path, auth, body
    private lateinit var savedSettings: AppSettings
    private var savedToken = ""

    @Before fun setUp() = runBlocking {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path ?: ""
                val body = request.body.readUtf8()
                requests += Triple(path, request.getHeader("Authorization"), body)
                if (path == "/v1/pair") {
                    return if (body.contains("\"pairing_secret\":\"right-secret\"")) json("{\"token\":\"$token\",\"device_id\":\"d1\"}")
                    else MockResponse().setResponseCode(403).setBody("{\"detail\":\"wrong pairing secret\"}")
                }
                if (request.getHeader("Authorization") != "Bearer $token") {
                    return MockResponse().setResponseCode(401).setBody("{\"detail\":\"unknown or revoked device token\"}")
                }
                return when (path) {
                    "/v1/chat" -> MockResponse().setHeader("content-type", "text/event-stream").setBody(chatReplies.poll() ?: sse("no scripted reply"))
                    "/v1/config" -> json("{\"llm\":true,\"models\":[],\"default_model\":\"\",\"tts\":false,\"stt\":false,\"integrations\":false,\"toolkits\":[]}")
                    else -> MockResponse().setResponseCode(404).setBody("{\"detail\":\"Not Found\"}")
                }
            }
        }
        server.start()
        savedSettings = graph.settings.current()
        savedToken = graph.secrets.get(SecretStore.DEVICE_TOKEN)
        graph.settings.update {
            it.copy(
                serverUrl = server.url("/").toString().trimEnd('/'), voiceMode = VoiceMode.OFF, integrationsEnabled = false,
                guidedSteps = false, annotationLingerSeconds = 1, memoryEnabled = false,
            )
        }
    }

    @After fun tearDown() = runBlocking {
        graph.turns.cancel()
        graph.settings.update { savedSettings }
        graph.secrets.put(SecretStore.DEVICE_TOKEN, savedToken)
        server.shutdown()
    }

    private fun json(body: String) = MockResponse().setHeader("content-type", "application/json").setBody(body)

    private fun esc(text: String) = text.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")
    private fun sse(text: String) = "event: text\ndata: {\"text\":\"${esc(text)}\"}\n\nevent: done\ndata: {\"finish_reason\":\"stop\"}\n\n"

    private fun frame(): CapturedFrame {
        val bitmap = Bitmap.createBitmap(108, 240, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.DKGRAY) }
        val jpeg = ByteArrayOutputStream().also { bitmap.compress(Bitmap.CompressFormat.JPEG, 80, it) }.toByteArray()
        return CapturedFrame(jpeg, "screen 1", 1, 108, 240, 0, 0, 1080, 2400)
    }

    @Test fun pairingStoresTokenAndRejectsWrongSecret() = runBlocking {
        graph.secrets.put(SecretStore.DEVICE_TOKEN, "")
        val url = server.url("/").toString()
        val refused = pairDevice(graph, url, "wrong")
        assertTrue("got: $refused", refused!!.contains("refused"))
        assertEquals("", graph.secrets.get(SecretStore.DEVICE_TOKEN))
        assertNull(pairDevice(graph, url, "right-secret"))
        assertEquals(token, graph.secrets.get(SecretStore.DEVICE_TOKEN))
    }

    @Test fun typedTurnParsesProgramAndFinishes() = runBlocking {
        graph.secrets.put(SecretStore.DEVICE_TOKEN, token)
        chatReplies += sse("```buddy\nsay(\"the wifi switch is here\")\nring(10, 40, 80, 20, \"Wi-Fi\")\nsay(\"tap it\")\n```")
        graph.turns.submitText("how do I turn on wifi", listOf(frame()))
        val done = withTimeout(30_000) {
            graph.turns.state.first { (it.phase == Phase.IDLE && it.reply.isNotEmpty()) || it.phase == Phase.ERROR }
        }
        assertEquals("error: ${done.error}", Phase.IDLE, done.phase)
        assertEquals("the wifi switch is here tap it", done.reply)
        assertTrue("program code must not leak into the sheet", !done.reply.contains("ring("))
        val chat = requests.single { it.first == "/v1/chat" }
        assertEquals("Bearer $token", chat.second)
        assertTrue("screenshot must be sent as an image block", chat.third.contains("image_url"))
        assertTrue(chat.third.contains("how do I turn on wifi"))
    }

    @Test fun revokedTokenShowsErrorWithoutCrashing() = runBlocking {
        graph.secrets.put(SecretStore.DEVICE_TOKEN, "hb_revoked")
        graph.turns.submitText("hello", emptyList())
        val state = withTimeout(20_000) {
            graph.turns.state.first { it.phase == Phase.ERROR || (it.phase == Phase.IDLE && it.reply.isNotEmpty()) }
        }
        assertEquals(Phase.ERROR, state.phase)
        assertTrue(state.error.isNotBlank())
    }

    @Test fun unpairedPhoneAsksToConnect() = runBlocking {
        graph.secrets.put(SecretStore.DEVICE_TOKEN, "")
        graph.turns.submitText("hello", emptyList())
        val state = withTimeout(10_000) { graph.turns.state.first { it.phase == Phase.ERROR } }
        assertTrue(state.error.contains("Connect this phone"))
        assertTrue(requests.none { it.first == "/v1/chat" })
    }
}
