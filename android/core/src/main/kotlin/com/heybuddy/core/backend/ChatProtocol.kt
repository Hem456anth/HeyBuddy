package com.heybuddy.core.backend

import com.heybuddy.core.geometry.CapturedFrame
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.util.Base64

/** One server-sent event. */
data class SseEvent(val event: String, val data: String)

/** Line-oriented SSE parser (`event:` / `data:` / blank-line dispatch). */
class SseParser {
    private var eventName = ""
    private val dataLines = ArrayList<String>()

    fun feedLine(rawLine: String): SseEvent? {
        val line = rawLine.trimEnd('\r')
        if (line.isEmpty()) return dispatch()
        if (line.startsWith(":")) return null
        val colon = line.indexOf(':')
        val field = if (colon == -1) line else line.substring(0, colon)
        var value = if (colon == -1) "" else line.substring(colon + 1)
        if (value.startsWith(" ")) value = value.substring(1)
        when (field) {
            "event" -> eventName = value
            "data" -> dataLines += value
        }
        return null
    }

    fun finish(): SseEvent? = dispatch()

    private fun dispatch(): SseEvent? {
        if (dataLines.isEmpty() && eventName.isEmpty()) return null
        val event = SseEvent(eventName.ifEmpty { "message" }, dataLines.joinToString("\n"))
        eventName = ""
        dataLines.clear()
        return event
    }
}

enum class Role(val wire: String) { USER("user"), ASSISTANT("assistant") }

/** A completed conversation message (plain text; old screenshots are never resent). */
data class ChatMessage(val role: Role, val content: String)

/**
 * Builds `/v1/chat` requests. Messages use the OpenAI Chat Completions
 * shape the backend forwards to Vercel AI Gateway: the newest user message
 * carries the screenshot as an inline `image_url` data URL plus its label.
 */
class ChatPayloadBuilder(
    var model: String?,
    var systemPrompt: String,
    var maxTokens: Int = 2048,
    var historyMaxExchanges: Int = HISTORY_MAX_EXCHANGES,
    var integrations: Boolean = true,
) {
    fun build(history: List<ChatMessage>, frames: List<CapturedFrame> = emptyList(), tools: JsonArray? = null): JsonObject {
        var conversation = history.takeLast(historyMaxExchanges * 2 + 1)
        while (conversation.isNotEmpty() && conversation.first().role != Role.USER) conversation = conversation.drop(1)
        val messages = conversation.mapIndexed { index, message ->
            val isLast = index == conversation.lastIndex
            if (isLast && message.role == Role.USER && frames.isNotEmpty()) {
                buildJsonObject {
                    put("role", "user")
                    put("content", buildJsonArray {
                        for (frame in frames) {
                            add(buildJsonObject {
                                put("type", "image_url")
                                put("image_url", buildJsonObject {
                                    put("url", "data:${frame.mediaType};base64," + Base64.getEncoder().encodeToString(frame.jpeg))
                                })
                            })
                            add(buildJsonObject { put("type", "text"); put("text", frame.label) })
                        }
                        add(buildJsonObject { put("type", "text"); put("text", message.content) })
                    })
                }
            } else {
                buildJsonObject { put("role", message.role.wire); put("content", message.content) }
            }
        }
        return buildJsonObject {
            model?.takeIf { it.isNotBlank() }?.let { put("model", it) }
            put("system", systemPrompt)
            put("messages", JsonArray(messages))
            put("max_tokens", maxTokens)
            put("integrations", integrations)
            if (tools != null && tools.isNotEmpty()) put("tools", tools)
        }
    }

    companion object {
        const val HISTORY_MAX_EXCHANGES = 10
    }
}

/**
 * A tool call the model made. `kind` comes from the backend:
 *  - local:       one of the app's own tools (ToolRunner + approval policy)
 *  - integration: a Composio tool the server validated; needs the user's
 *                 confirmation unless it is a read and reads are auto-allowed
 *  - rejected:    the server refused it (unknown tool / invalid arguments)
 */
data class ToolCall(
    val id: String,
    val name: String,
    val input: JsonObject,
    val kind: String = "local",
    val risk: String = "",
    val proposalId: String = "",
    val toolkit: String = "",
    val title: String = "",
    val preview: String = "",
    val error: String = "",
)

data class RoundResult(val text: String, val toolCalls: List<ToolCall>, val finishReason: String)

class BackendStreamException(message: String) : RuntimeException(message)

/** Collects one round of `/v1/chat` events (text / tool_call / done / error). */
class StreamAccumulator {
    private val text = StringBuilder()
    private val calls = ArrayList<ToolCall>()
    var finishReason: String = ""
        private set

    /** Consume one event; returns new visible text (may be empty). Throws on `error`. */
    fun feed(event: SseEvent): String {
        val obj = try { Json.parseToJsonElement(event.data) as? JsonObject } catch (_: Exception) { null } ?: return ""
        when (event.event) {
            "text" -> {
                val piece = obj.str("text").orEmpty()
                text.append(piece)
                return piece
            }
            "tool_call" -> calls += ToolCall(
                id = obj.str("id").orEmpty(),
                name = obj.str("name").orEmpty(),
                input = obj["arguments"] as? JsonObject ?: JsonObject(emptyMap()),
                kind = obj.str("kind") ?: "local",
                risk = obj.str("risk").orEmpty(),
                proposalId = obj.str("proposal_id").orEmpty(),
                toolkit = obj.str("toolkit").orEmpty(),
                title = obj.str("title").orEmpty(),
                preview = obj.str("preview").orEmpty(),
                error = obj.str("error").orEmpty(),
            )
            "done" -> finishReason = obj.str("finish_reason").orEmpty()
            "error" -> throw BackendStreamException(obj.str("message") ?: "the server reported an error")
        }
        return ""
    }

    fun result(): RoundResult = RoundResult(text.toString(), calls.toList(), finishReason.ifEmpty { if (calls.isEmpty()) "stop" else "tool_calls" })
}

/** Outcome of a tool call: text for the model, and whether it is an error. */
data class ToolOutcome(val output: String, val isError: Boolean)

/**
 * Runs model rounds until it stops calling tools. Every tool call, local or
 * integration, goes through [execute] (the app's approval gate) before its
 * result is sent back; tool output can never trigger another tool without
 * passing that gate again. The last round is sent without tools.
 */
class ToolLoop(private val backend: BackendClient) {

    suspend fun streamRound(payload: JsonObject, onText: ((String) -> Unit)? = null): RoundResult {
        val accumulator = StreamAccumulator()
        backend.chatStream(payload) { event ->
            val piece = accumulator.feed(event)
            if (piece.isNotEmpty()) onText?.invoke(piece)
        }
        return accumulator.result()
    }

    suspend fun run(
        payload: JsonObject,
        execute: suspend (ToolCall) -> ToolOutcome,
        onRoundText: (suspend (String, Boolean) -> Unit)? = null,
        onText: ((String) -> Unit)? = null,
        maxRounds: Int = 6,
    ): List<String> {
        val messages = ArrayList<JsonElement>((payload["messages"] as JsonArray))
        val texts = ArrayList<String>()
        for (round in 0 until maxRounds) {
            var roundPayload = JsonObject(payload + ("messages" to JsonArray(messages)))
            if (round == maxRounds - 1) {
                roundPayload = JsonObject(roundPayload - "tools" + ("integrations" to JsonPrimitive(false)))
            }
            val result = streamRound(roundPayload, onText)
            // The final round is sent without tools; ignore any tool call it still makes.
            val wantsTools = result.toolCalls.isNotEmpty() && round < maxRounds - 1
            if (result.text.isNotBlank()) {
                texts += result.text
                onRoundText?.invoke(result.text, wantsTools)
            }
            if (!wantsTools) break
            messages += buildJsonObject {
                put("role", "assistant")
                put("content", result.text)
                put("tool_calls", buildJsonArray {
                    for (call in result.toolCalls) add(buildJsonObject {
                        put("id", call.id)
                        put("type", "function")
                        put("function", buildJsonObject { put("name", call.name); put("arguments", call.input.toString()) })
                    })
                })
            }
            for (call in result.toolCalls) {
                val outcome = try {
                    if (call.kind == "rejected") ToolOutcome("refused by the server: ${call.error}", true) else execute(call)
                } catch (cancel: kotlinx.coroutines.CancellationException) {
                    throw cancel
                } catch (error: Exception) {
                    ToolOutcome("tool failed: ${error.message}", true)
                }
                messages += buildJsonObject {
                    put("role", "tool")
                    put("tool_call_id", call.id)
                    put("content", (if (outcome.isError) "[error] " else "") + outcome.output.ifEmpty { "(no output)" })
                }
            }
        }
        return texts
    }
}

/** Helpers for reading tool inputs. */
fun JsonObject.stringArg(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content
fun JsonObject.boolArg(key: String): Boolean? = (this[key] as? JsonPrimitive)?.booleanOrNull
