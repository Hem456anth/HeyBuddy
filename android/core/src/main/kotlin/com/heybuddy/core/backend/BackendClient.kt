package com.heybuddy.core.backend

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.Call
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.coroutineContext

/** What the backend says it can do (GET /v1/config). */
data class BackendConfig(
    val llm: Boolean,
    val models: List<String>,
    val defaultModel: String,
    val tts: Boolean,
    val stt: Boolean,
    val integrations: Boolean,
    val toolkits: List<String>,
)

data class IntegrationConnection(val id: String, val toolkit: String, val status: String)
data class IntegrationsState(val enabled: Boolean, val toolkits: List<String>, val connections: List<IntegrationConnection>)
data class ActionLogEntry(val id: String, val toolSlug: String, val risk: String, val status: String, val summary: String, val createdAt: Double)
data class ExecutionResult(val successful: Boolean, val data: String, val error: String?)

/**
 * Client for the HeyBuddy FastAPI backend (`backend/` in this repo). The app
 * holds no provider keys: it holds a per-device token obtained once by
 * pairing (`POST /v1/pair` with the server owner's pairing secret), sent as
 * `Authorization: Bearer` on every call.
 */
class BackendClient(
    baseUrl: String,
    private val deviceToken: String = "",
    httpClient: OkHttpClient? = null,
    private val userAgent: String = "HeyBuddy-Android/0.1.0",
) {
    val baseUrl: String = BackendUrlPolicy.normalize(baseUrl)

    val http: OkHttpClient = httpClient ?: OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    class BackendHttpException(val route: String, val status: Int, val body: String) :
        IOException("HeyBuddy server $route failed: HTTP $status ${describe(status, body)}") {
        companion object {
            fun describe(status: Int, body: String): String {
                val message = try {
                    val obj = Json.parseToJsonElement(body).jsonObject
                    (obj["error"] as? JsonPrimitive)?.contentOrNull ?: (obj["detail"] as? JsonPrimitive)?.contentOrNull
                } catch (_: Exception) { null }
                val hint = when (status) {
                    401 -> " (pair this phone again in Settings)"
                    403 -> ""
                    404 -> ""
                    429 -> " (too many requests, wait a moment)"
                    503 -> ""
                    else -> ""
                }
                return ((message ?: body.take(200)).trim() + hint).trim()
            }
        }
    }

    val isPaired: Boolean get() = deviceToken.isNotBlank()

    private fun request(path: String): Request.Builder {
        val builder = Request.Builder().url(baseUrl.trimEnd('/') + path).header("User-Agent", userAgent)
        if (deviceToken.isNotBlank()) builder.header("Authorization", "Bearer $deviceToken")
        return builder
    }

    private suspend fun execute(call: Call): Response = suspendCancellableCoroutine { continuation ->
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : okhttp3.Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWith(Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                if (continuation.isActive) continuation.resumeWith(Result.success(response)) else response.close()
            }
        })
    }

    private suspend fun json(path: String, method: String = "GET", body: JsonElement? = null): JsonObject {
        val requestBody: RequestBody? = body?.toString()?.toRequestBody(JSON_MEDIA)
            ?: if (method == "POST") "{}".toRequestBody(JSON_MEDIA) else null
        val response = execute(http.newCall(request(path).method(method, requestBody).build()))
        return response.use {
            val text = withContext(Dispatchers.IO) { it.body.string() }
            if (!it.isSuccessful) throw BackendHttpException(path, it.code, text)
            if (text.isBlank()) JsonObject(emptyMap()) else Json.parseToJsonElement(text).jsonObject
        }
    }

    /** One-time pairing. Returns the device token to store in the Keystore-backed secret store. */
    suspend fun pair(pairingSecret: String, deviceName: String): String {
        val result = json("/v1/pair", "POST", buildJsonObject { put("pairing_secret", pairingSecret); put("device_name", deviceName.take(60)) })
        return result["token"]?.jsonPrimitive?.contentOrNull ?: throw IOException("server returned no token")
    }

    suspend fun unpair() { json("/v1/device", "DELETE") }

    suspend fun config(): BackendConfig {
        val o = json("/v1/config")
        fun bool(key: String) = (o[key] as? JsonPrimitive)?.booleanOrNull == true
        fun list(key: String) = (o[key] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull } ?: emptyList()
        return BackendConfig(bool("llm"), list("models"), o["default_model"]?.jsonPrimitive?.contentOrNull.orEmpty(),
            bool("tts"), bool("stt"), bool("integrations"), list("toolkits"))
    }

    /**
     * POST /v1/chat and deliver each SSE event to [onEvent]. Cancelling the
     * coroutine cancels the HTTP call; the backend then aborts the model stream.
     */
    suspend fun chatStream(payload: JsonObject, onEvent: (SseEvent) -> Unit) {
        val call = http.newCall(request(CHAT_PATH).post(payload.toString().toRequestBody(JSON_MEDIA)).build())
        val response = execute(call)
        response.use {
            if (!it.isSuccessful) throw BackendHttpException(CHAT_PATH, it.code, it.body.string())
            withContext(Dispatchers.IO) {
                val source = it.body.source()
                val parser = SseParser()
                while (true) {
                    coroutineContext.ensureActive()
                    val line = source.readUtf8Line() ?: break
                    parser.feedLine(line)?.let(onEvent)
                }
                parser.finish()?.let(onEvent)
            }
        }
    }

    /** POST /v1/tts -> WAV bytes (Gradium). Throws [BackendHttpException] 503 when the server has no TTS. */
    suspend fun tts(text: String): ByteArray {
        val body = buildJsonObject { put("text", text.take(MAX_TTS_CHARS)) }.toString().toRequestBody(JSON_MEDIA)
        val response = execute(http.newCall(request(TTS_PATH).post(body).build()))
        return response.use {
            if (!it.isSuccessful) throw BackendHttpException(TTS_PATH, it.code, it.body.string())
            withContext(Dispatchers.IO) { it.body.bytes() }
        }
    }

    /** POST /v1/stt with a WAV file -> transcript (optional server STT). */
    suspend fun stt(wav: ByteArray): String {
        val response = execute(http.newCall(request(STT_PATH).post(wav.toRequestBody(WAV_MEDIA)).build()))
        return response.use {
            val text = withContext(Dispatchers.IO) { it.body.string() }
            if (!it.isSuccessful) throw BackendHttpException(STT_PATH, it.code, text)
            Json.parseToJsonElement(text).jsonObject["text"]?.jsonPrimitive?.contentOrNull.orEmpty()
        }
    }

    // ----- integrations (Composio, server-side) -----
    suspend fun integrations(): IntegrationsState {
        val o = json("/v1/integrations")
        val connections = (o["connections"] as? JsonArray).orEmpty().map { it.jsonObject }.map {
            IntegrationConnection(it.str("id").orEmpty(), it.str("toolkit").orEmpty(), it.str("status").orEmpty())
        }
        val toolkits = (o["toolkits"] as? JsonArray).orEmpty().mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
        return IntegrationsState((o["enabled"] as? JsonPrimitive)?.booleanOrNull == true, toolkits, connections)
    }

    /** Returns the OAuth link to open in a Custom Tab. */
    suspend fun connect(toolkit: String): String =
        json("/v1/integrations/connect", "POST", buildJsonObject { put("toolkit", toolkit) }).str("redirect_url")
            ?: throw IOException("server returned no link")

    suspend fun disconnect(connectionId: String) { json("/v1/integrations/$connectionId", "DELETE") }

    suspend fun executeIntegration(proposalId: String, arguments: JsonObject, confirmed: Boolean): ExecutionResult {
        val o = json("/v1/integrations/execute", "POST", buildJsonObject {
            put("proposal_id", proposalId); put("arguments", arguments); put("confirmed", confirmed)
        })
        return ExecutionResult((o["successful"] as? JsonPrimitive)?.boolean == true, o.str("data").orEmpty(), o.str("error"))
    }

    suspend fun actionLogs(): List<ActionLogEntry> = (json("/v1/integrations/logs")["logs"] as? JsonArray).orEmpty().map { it.jsonObject }.map {
        ActionLogEntry(it.str("id").orEmpty(), it.str("tool_slug").orEmpty(), it.str("risk").orEmpty(), it.str("status").orEmpty(),
            it.str("summary").orEmpty(), (it["created_at"] as? JsonPrimitive)?.contentOrNull?.toDoubleOrNull() ?: 0.0)
    }

    suspend fun deleteLogs(id: String? = null) { json(if (id == null) "/v1/integrations/logs" else "/v1/integrations/logs/$id", "DELETE") }

    companion object {
        const val CHAT_PATH = "/v1/chat"
        const val TTS_PATH = "/v1/tts"
        const val STT_PATH = "/v1/stt"
        const val MAX_TTS_CHARS = 2000
        val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
        val WAV_MEDIA = "audio/wav".toMediaType()
    }
}

private fun JsonArray?.orEmpty(): List<JsonElement> = this ?: emptyList()
internal fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.contentOrNull

/**
 * Backend URL rules: HTTPS everywhere. Plain HTTP only for on-device
 * development hosts (localhost, 127.0.0.1, the emulator's 10.0.2.2), which
 * the network security config also allows. LAN IPs cannot be allow-listed
 * in a network security config, so LAN development uses `adb reverse`.
 */
object BackendUrlPolicy {
    sealed class Verdict {
        object Ok : Verdict()
        data class Warning(val message: String) : Verdict()
        data class Rejected(val message: String) : Verdict()
    }

    val DEV_HOSTS = setOf("localhost", "127.0.0.1", "10.0.2.2")

    fun normalize(raw: String): String {
        val trimmed = raw.trim().trimEnd('/')
        if (trimmed.isEmpty()) return ""
        return if ("://" in trimmed) trimmed else "https://$trimmed"
    }

    fun check(raw: String): Verdict {
        val url = normalize(raw)
        if (url.isEmpty()) return Verdict.Rejected("Enter your HeyBuddy server URL")
        val parsed = try { java.net.URI(url) } catch (_: Exception) { return Verdict.Rejected("That isn't a valid URL") }
        val host = parsed.host?.lowercase() ?: return Verdict.Rejected("That URL has no host")
        if (host.contains("your-server") || host == "example.com") return Verdict.Rejected("Replace the placeholder with your server's URL")
        return when (parsed.scheme?.lowercase()) {
            "https" -> Verdict.Ok
            "http" -> if (host in DEV_HOSTS) {
                Verdict.Warning("Plain HTTP to $host is for local development only. What you say and your screenshots travel unencrypted.")
            } else {
                Verdict.Rejected("Use https://. Plain HTTP is only allowed for localhost while developing.")
            }
            else -> Verdict.Rejected("Only https:// URLs are supported")
        }
    }
}
