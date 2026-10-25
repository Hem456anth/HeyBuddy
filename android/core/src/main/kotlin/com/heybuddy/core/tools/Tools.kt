package com.heybuddy.core.tools

import com.heybuddy.core.memory.MemoryStore
import com.heybuddy.core.backend.ToolCall
import com.heybuddy.core.backend.ToolOutcome
import com.heybuddy.core.backend.stringArg
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import java.io.File
import java.net.URI

/**
 * Local tools the model may call on Android, with HeyBuddy's guard rails.
 *
 * Same idea as the Windows `core/tools.py`, minus everything Android does
 * not allow or HeyBuddy will not do: there is **no shell**, no access to
 * other apps' data, and files live only in HeyBuddy's private storage.
 * Anything that acts outside the app (typing into another app, reading the
 * clipboard, launching an app, sharing) goes through [ApprovalPolicy].
 */
enum class Decision { ALLOW, ASK, DENY }

/** User-facing policy, set in Settings. */
data class ApprovalPolicy(
    /** typing into other apps, reading the clipboard, launching apps, sharing */
    val actionPolicy: Decision = Decision.ASK,
    /** when true every tool that leaves the app asks, even "safe" ones */
    val alwaysAsk: Boolean = false,
    /** integration (Composio) reads may run without asking; writes/irreversible always ask */
    val autoAllowIntegrationReads: Boolean = true,
) {
    /** Decision for a server-validated integration call, by the server's risk class. */
    fun decideIntegration(risk: String): Decision = when {
        risk == "read" && autoAllowIntegrationReads && !alwaysAsk -> Decision.ALLOW
        else -> Decision.ASK // write, irreversible and anything unknown: always the user's explicit yes
    }

    fun decide(tool: String): Decision {
        val risk = TOOL_RISK[tool] ?: return Decision.DENY
        return when (risk) {
            Risk.SAFE -> if (alwaysAsk && tool in OUTWARD_SAFE_TOOLS) Decision.ASK else Decision.ALLOW
            Risk.SENSITIVE -> if (alwaysAsk && actionPolicy == Decision.ALLOW) Decision.ASK else actionPolicy
        }
    }

    enum class Risk { SAFE, SENSITIVE }

    companion object {
        val TOOL_RISK: Map<String, Risk> = mapOf(
            "open_url" to Risk.SAFE,
            "open_file" to Risk.SAFE,
            "copy_to_clipboard" to Risk.SAFE,
            "web_fetch" to Risk.SAFE,
            "remember" to Risk.SAFE,
            "read_memory" to Risk.SAFE,
            "start_background_job" to Risk.SAFE,
            "list_files" to Risk.SAFE,
            "read_file" to Risk.SAFE,
            "write_file" to Risk.SAFE,
            "report_progress" to Risk.SAFE,
            "add_artifact" to Risk.SAFE,
            "read_clipboard" to Risk.SENSITIVE,
            "insert_text" to Risk.SENSITIVE,
            "launch_app" to Risk.SENSITIVE,
            "share" to Risk.SENSITIVE,
        )
        /** Safe tools that still have a visible effect outside HeyBuddy. */
        val OUTWARD_SAFE_TOOLS = setOf("open_url", "open_file", "copy_to_clipboard")
    }
}

/** What the platform (the Android app, or a fake in tests) provides. */
interface ToolPlatform {
    suspend fun openUrl(url: String): String
    suspend fun openFile(file: File): String
    suspend fun share(text: String?, file: File?): String
    suspend fun copyToClipboard(text: String): String
    suspend fun readClipboard(): String
    suspend fun insertText(text: String): String
    /** Returns the label/package it launched, or throws [ToolError]. */
    suspend fun launchApp(nameOrPackage: String): String
    suspend fun fetchUrl(url: String): Pair<String, String> // (content-type, body)
    /** Blocks until the user answers; false on timeout or denial. */
    suspend fun approve(title: String, detail: String): Boolean
}

class ToolError(message: String) : Exception(message)

class ToolContext(
    /** Folder file tools may use freely (job folder, or the app workspace). */
    val workingDir: File,
    /** Root of all HeyBuddy files; nothing outside it is ever read or written. */
    val appFilesRoot: File,
    val platform: ToolPlatform,
    val policy: ApprovalPolicy,
    val memory: MemoryStore?,
    val allowBackgroundJobs: Boolean = true,
    val startJob: (suspend (String, String) -> String)? = null,
    val reportProgress: ((String) -> Unit)? = null,
    val artifacts: MutableList<String> = mutableListOf(),
)

object ToolSpecs {
    private fun schema(required: List<String>, vararg properties: Pair<String, String>): JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            for ((name, description) in properties) putJsonObject(name) {
                put("type", "string")
                if (description.isNotEmpty()) put("description", description)
            }
        }
        put("required", buildJsonArray { required.forEach { add(kotlinx.serialization.json.JsonPrimitive(it)) } })
    }

    val SPECS: Map<String, Pair<String, JsonObject>> = linkedMapOf(
        "open_url" to ("Open an http(s) link in the user's browser or the app that handles it." to schema(listOf("url"), "url" to "")),
        "open_file" to ("Open a file HeyBuddy made (a job artifact or a file in its workspace) with the app that handles it." to schema(listOf("path"), "path" to "")),
        "share" to ("Open the Android share sheet with text or with a file HeyBuddy made. The user picks where it goes." to schema(emptyList(), "text" to "", "path" to "a HeyBuddy file to share instead of text")),
        "copy_to_clipboard" to ("Put text on the clipboard." to schema(listOf("text"), "text" to "")),
        "read_clipboard" to ("Read the text on the clipboard. Android only allows this while HeyBuddy is on screen; the user is asked first." to schema(emptyList())),
        "insert_text" to ("Type text into the field the user is focused on in the app they are using (needs HeyBuddy's accessibility service; the user is asked first). Only when they asked you to write something there." to schema(listOf("text"), "text" to "")),
        "launch_app" to ("Open an installed app by its name (for example \"Calendar\") or package name. The user is asked first." to schema(listOf("app"), "app" to "")),
        "web_fetch" to ("Fetch a public web page and return its readable text." to schema(listOf("url"), "url" to "")),
        "remember" to ("Save one lasting fact about the user (a preference, a name, a goal) for next time. Never secrets." to schema(listOf("fact"), "fact" to "")),
        "read_memory" to ("Read everything the user asked HeyBuddy to remember." to schema(emptyList())),
        "start_background_job" to ("Hand longer work (research, writing a document, multi-step tasks over a few seconds) to a background helper that reports back when done. Write a complete brief: goal, deliverable, format." to schema(listOf("title", "brief"), "title" to "two or three words", "brief" to "")),
        "list_files" to ("List files in your working folder (or a subfolder of it)." to schema(emptyList(), "path" to "folder inside the working folder; empty for its root")),
        "read_file" to ("Read a UTF-8 text file in your working folder (first 200 KB)." to schema(listOf("path"), "path" to "")),
        "write_file" to ("Create or overwrite a text file in your working folder." to schema(listOf("path", "content"), "path" to "", "content" to "")),
        "report_progress" to ("Send the user a one-line progress update at a real milestone." to schema(listOf("message"), "message" to "")),
        "add_artifact" to ("Register a finished deliverable file so HeyBuddy shows it to the user." to schema(listOf("path"), "path" to "")),
    )

    val VOICE_TOOLS = listOf(
        "open_url", "open_file", "share", "copy_to_clipboard", "read_clipboard", "insert_text", "launch_app",
        "web_fetch", "remember", "read_memory", "start_background_job",
    )
    val JOB_TOOLS = listOf("web_fetch", "list_files", "read_file", "write_file", "report_progress", "add_artifact", "read_memory")

    fun definitions(names: List<String>): JsonArray = buildJsonArray {
        for (name in names) {
            val (description, inputSchema) = SPECS.getValue(name)
            // OpenAI Chat Completions function format (what the backend forwards to Vercel AI Gateway).
            add(buildJsonObject {
                put("type", "function")
                put("function", buildJsonObject { put("name", name); put("description", description); put("parameters", inputSchema) })
            })
        }
    }
}

object ToolSafety {
    const val MAX_OUTPUT_CHARS = 12_000
    const val MAX_READ_BYTES = 200_000
    const val MAX_FETCH_BYTES = 2_000_000

    private val SENSITIVE_NAME = Regex("(^|[\\\\/])(\\.env(\\..*)?|id_rsa.*|id_ed25519.*|.*\\.pem|.*\\.p12|.*\\.pfx|.*\\.kdbx|auth\\.json|auth\\.bin|.*secret.*\\.xml|.*\\.preferences_pb)$", RegexOption.IGNORE_CASE)

    fun cap(text: String, limit: Int = MAX_OUTPUT_CHARS): String {
        if (text.length <= limit) return text
        val head = text.take(limit * 2 / 3)
        val tail = text.takeLast(limit / 3)
        return "$head\n... [${text.length - limit} characters cut] ...\n$tail"
    }

    fun isSensitiveName(path: String): Boolean = SENSITIVE_NAME.containsMatchIn(path.replace('\\', '/'))

    /** Resolve [raw] inside [root]; null if it escapes the root (.., absolute paths elsewhere, symlinks). */
    fun resolveInside(root: File, raw: String): File? {
        val trimmed = raw.trim().trim('"')
        val candidate = if (File(trimmed).isAbsolute) File(trimmed) else File(root, trimmed)
        val canonicalRoot = root.canonicalFile
        val canonical = candidate.canonicalFile
        return if (canonical == canonicalRoot || canonical.path.startsWith(canonicalRoot.path + File.separator)) canonical else null
    }

    fun isHttpUrl(url: String): Boolean = try {
        val scheme = URI(url.trim()).scheme?.lowercase()
        scheme == "http" || scheme == "https"
    } catch (_: Exception) { false }

    /** Private, loopback and link-local hosts are never fetched (SSRF guard). */
    fun isPrivateHost(url: String): Boolean {
        val host = try { URI(url.trim()).host?.lowercase() } catch (_: Exception) { null } ?: return true
        if (host == "localhost" || host.endsWith(".local") || host.endsWith(".internal")) return true
        if (host.startsWith("127.") || host.startsWith("10.") || host.startsWith("192.168.") || host.startsWith("169.254.") || host == "0.0.0.0") return true
        val match = Regex("^172\\.(\\d+)\\.").find(host)
        if (match != null && match.groupValues[1].toInt() in 16..31) return true
        if (host.startsWith("[") || host.contains(":")) return host.contains("::1") || host.startsWith("[fe80") || host.startsWith("[fc") || host.startsWith("[fd")
        return false
    }

    fun htmlToText(rawHtml: String): String {
        var html = rawHtml.replace(Regex("(?is)<(script|style|noscript|svg)[^>]*>.*?</\\1>"), " ")
        html = html.replace(Regex("(?i)<br\\s*/?>|</(p|div|li|h[1-6]|tr)>"), "\n")
        var text = html.replace(Regex("<[^>]+>"), " ")
        text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", "\"").replace("&#39;", "'")
        text = text.replace(Regex("[ \\t\\r\\f\\u000B]+"), " ").replace(Regex(" *\\n *"), "\n")
        return text.replace(Regex("\\n\\s*\\n+"), "\n\n").trim()
    }
}

/** Executes tool calls: validation, policy, approval, then the platform. */
class ToolRunner(private val context: ToolContext) {

    suspend fun execute(call: ToolCall, allowed: List<String>): ToolOutcome {
        if (call.name !in allowed) return ToolOutcome("tool ${call.name} isn't available here", true)
        return try {
            ToolOutcome(ToolSafety.cap(run(call.name, call.input)), false)
        } catch (refusal: ToolError) {
            ToolOutcome(refusal.message ?: "refused", true)
        }
    }

    private fun require(input: JsonObject, key: String): String {
        val value = input.stringArg(key)
        if (value.isNullOrBlank()) throw ToolError("missing $key")
        return value
    }

    private suspend fun gate(tool: String, title: String, detail: String) {
        when (context.policy.decide(tool)) {
            Decision.ALLOW -> Unit
            Decision.DENY -> throw ToolError("this action is turned off in HeyBuddy settings")
            Decision.ASK -> if (!context.platform.approve(title, detail)) throw ToolError("the user declined")
        }
    }

    private fun appFile(raw: String, mustExist: Boolean): File {
        val file = ToolSafety.resolveInside(context.appFilesRoot, raw)
            ?: ToolSafety.resolveInside(context.workingDir, raw)
            ?: throw ToolError("only files HeyBuddy made can be used")
        if (ToolSafety.isSensitiveName(file.path)) throw ToolError("that file is off limits")
        if (mustExist && !file.exists()) throw ToolError("${file.name} does not exist")
        return file
    }

    private fun workFile(raw: String): File {
        val file = ToolSafety.resolveInside(context.workingDir, raw.ifBlank { "." })
            ?: throw ToolError("stay inside your working folder")
        if (ToolSafety.isSensitiveName(file.path)) throw ToolError("that file is off limits")
        return file
    }

    private suspend fun run(name: String, input: JsonObject): String = when (name) {
        "open_url" -> {
            val url = require(input, "url").trim()
            if (!ToolSafety.isHttpUrl(url)) throw ToolError("only http and https links can be opened")
            gate(name, "Open a link?", url)
            context.platform.openUrl(url)
        }
        "open_file" -> {
            val file = appFile(require(input, "path"), mustExist = true)
            gate(name, "Open a file?", file.name)
            context.platform.openFile(file)
        }
        "share" -> {
            val text = input.stringArg("text")
            val file = input.stringArg("path")?.takeIf { it.isNotBlank() }?.let { appFile(it, mustExist = true) }
            if (text.isNullOrBlank() && file == null) throw ToolError("nothing to share")
            gate(name, "Share this?", file?.name ?: text!!.take(400))
            context.platform.share(text, file)
        }
        "copy_to_clipboard" -> {
            val text = input.stringArg("text") ?: ""
            gate(name, "Copy to clipboard?", text.take(400))
            context.platform.copyToClipboard(text)
        }
        "read_clipboard" -> {
            gate(name, "Let HeyBuddy read your clipboard?", "The text you copied will be sent to your HeyBuddy server and model.")
            context.platform.readClipboard()
        }
        "insert_text" -> {
            val text = require(input, "text")
            gate(name, "Type into the app you're using?", text.take(600))
            context.platform.insertText(text)
        }
        "launch_app" -> {
            val app = require(input, "app").trim()
            gate(name, "Open an app?", app)
            context.platform.launchApp(app)
        }
        "web_fetch" -> {
            val url = require(input, "url").trim()
            if (!ToolSafety.isHttpUrl(url)) throw ToolError("only http and https URLs")
            if (ToolSafety.isPrivateHost(url)) throw ToolError("private network addresses are not fetched")
            gate(name, "Fetch a web page?", url)
            val (contentType, body) = context.platform.fetchUrl(url)
            if ("html" in contentType || body.trimStart().startsWith("<")) ToolSafety.htmlToText(body) else body
        }
        "remember" -> {
            val fact = require(input, "fact")
            val memory = context.memory ?: throw ToolError("memory is turned off")
            if (!memory.add(fact)) throw ToolError("that looks like a secret, so it was not saved")
            "remembered"
        }
        "read_memory" -> {
            val memory = context.memory ?: throw ToolError("memory is turned off")
            memory.facts().joinToString("\n").ifEmpty { "(nothing remembered yet)" }
        }
        "start_background_job" -> {
            val start = context.startJob
            if (!context.allowBackgroundJobs || start == null) throw ToolError("background jobs are not available here")
            val id = start(require(input, "title").take(60), require(input, "brief"))
            "started background job $id; tell the user you're on it"
        }
        "list_files" -> {
            val folder = workFile(input.stringArg("path") ?: "")
            if (!folder.isDirectory) throw ToolError("${folder.name} is not a folder")
            val entries = (folder.listFiles() ?: emptyArray()).sortedWith(compareBy({ !it.isDirectory }, { it.name.lowercase() })).take(500)
            folder.path + "\n" + entries.joinToString("\n") { "${if (it.isDirectory) "dir " else "file"}\t${if (it.isFile) it.length() else 0}\t${it.name}" }
        }
        "read_file" -> {
            val file = workFile(require(input, "path"))
            if (!file.isFile) throw ToolError("${file.name} is not a file")
            file.inputStream().use { stream ->
                val bytes = ByteArray(ToolSafety.MAX_READ_BYTES)
                val read = stream.read(bytes).coerceAtLeast(0)
                String(bytes, 0, read, Charsets.UTF_8)
            }
        }
        "write_file" -> {
            val file = workFile(require(input, "path"))
            val content = input.stringArg("content") ?: ""
            file.parentFile?.mkdirs()
            file.writeText(content, Charsets.UTF_8)
            "wrote ${content.length} characters to ${file.path}"
        }
        "report_progress" -> {
            context.reportProgress?.invoke(require(input, "message").take(200))
            "ok"
        }
        "add_artifact" -> {
            val file = workFile(require(input, "path"))
            if (!file.exists()) throw ToolError("${file.name} does not exist")
            context.artifacts += file.path
            "registered"
        }
        else -> throw ToolError("unknown tool $name")
    }
}
