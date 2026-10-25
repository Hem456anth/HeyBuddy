package com.heybuddy.core.jobs

import com.heybuddy.core.prompt.Prompts
import com.heybuddy.core.tools.ToolContext
import com.heybuddy.core.tools.ToolRunner
import com.heybuddy.core.tools.ToolSpecs
import com.heybuddy.core.backend.ToolLoop
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.File
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Background jobs ("helpers"): longer work handed off from a voice turn.
 * Same shape as the Windows `core/jobs.py`: one folder per job with
 * `output/`, `tmp/` and `AGENTS.md`, an index file that survives restarts,
 * progress lines, a final reply with an `ARTIFACTS:` block. On Android the
 * runner is scheduled by WorkManager and the job may only use HeyBuddy's
 * private storage and the web (no shell).
 */
@Serializable
data class Job(
    val id: String,
    val title: String,
    val brief: String,
    val folder: String,
    var status: String = "queued", // queued | running | done | failed | cancelled
    val createdAt: Long = System.currentTimeMillis(),
    var finishedAt: Long? = null,
    var progress: List<String> = emptyList(),
    var reply: String = "",
    var artifacts: List<String> = emptyList(),
    var error: String = "",
    var seen: Boolean = false,
) {
    val isActive: Boolean get() = status == "queued" || status == "running"

    val summaryLine: String
        get() = when (status) {
            "done" -> reply.trim().lineSequence().firstOrNull().orEmpty().take(160)
            "failed" -> "Couldn't finish: ${error.take(120)}"
            "cancelled" -> "Stopped."
            else -> progress.lastOrNull() ?: "Working on it..."
        }
}

object JobText {
    private val ARTIFACTS_RE = Regex("(?is)ARTIFACTS:\\s*(.+?)(?:\\n\\s*\\n|\\z)")

    fun slugify(title: String): String =
        title.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').ifEmpty { "job" }.take(32)

    fun parseArtifacts(reply: String, jobFolder: File): List<String> {
        val match = ARTIFACTS_RE.find(reply) ?: return emptyList()
        val paths = ArrayList<String>()
        for (line in match.groupValues[1].lines()) {
            val candidate = line.trim().trimStart('-', '*', '•', ' ').trim().trim('`', '"', '\'')
            if (candidate.isEmpty() || candidate.equals("none", ignoreCase = true)) continue
            var file = File(candidate)
            if (!file.isAbsolute) file = File(jobFolder, candidate)
            if (file.exists()) paths += file.path
        }
        return paths
    }

    fun stripArtifacts(reply: String): String = ARTIFACTS_RE.replace(reply, "").trim()
}

/** Persistent list of jobs (`jobs.json`) and their folders. Thread-safe. */
class JobStore(private val jobsDir: File, private val indexFile: File) {
    private val json = Json { ignoreUnknownKeys = true; prettyPrint = true; encodeDefaults = true }
    private val jobs = LinkedHashMap<String, Job>()
    private val listeners = ArrayList<(Job) -> Unit>()

    init { load() }

    @Synchronized
    private fun load() {
        if (!indexFile.exists()) return
        try {
            for (job in json.decodeFromString<List<Job>>(indexFile.readText())) {
                if (job.isActive) { job.status = "failed"; job.error = "HeyBuddy was closed while this ran" }
                jobs[job.id] = job
            }
        } catch (_: Exception) {
            // unreadable index: start empty rather than crash
        }
    }

    @Synchronized
    fun save() {
        val kept = jobs.values.sortedBy { it.createdAt }.takeLast(MAX_JOBS_KEPT)
        indexFile.parentFile?.mkdirs()
        val temp = File(indexFile.path + ".tmp")
        temp.writeText(json.encodeToString(kept))
        if (!temp.renameTo(indexFile)) { indexFile.writeText(temp.readText()); temp.delete() }
    }

    fun addListener(listener: (Job) -> Unit) = synchronized(listeners) { listeners += listener }
    fun removeListener(listener: (Job) -> Unit) = synchronized(listeners) { listeners -= listener }
    fun notifyChanged(job: Job) { synchronized(listeners) { listeners.toList() }.forEach { it(job) } }

    @Synchronized
    fun all(): List<Job> = jobs.values.sortedByDescending { it.createdAt }

    @Synchronized
    fun get(id: String): Job? = jobs[id]

    fun create(title: String, brief: String): Job {
        val id = UUID.randomUUID().toString().replace("-", "").take(10)
        val folder = File(jobsDir, "${JobText.slugify(title)}-$id")
        File(folder, "output").mkdirs()
        File(folder, "tmp").mkdirs()
        val started = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm"))
        File(folder, "AGENTS.md").writeText("# $title\n\nStarted $started by HeyBuddy.\n\n## Brief\n\n$brief\n\n## Notes\n\n")
        val job = Job(id = id, title = title.trim().ifEmpty { "Job" }, brief = brief, folder = folder.path)
        synchronized(this) { jobs[id] = job }
        save()
        notifyChanged(job)
        return job
    }

    fun update(job: Job) {
        synchronized(this) { jobs[job.id] = job }
        save()
        notifyChanged(job)
    }

    fun remove(id: String) {
        synchronized(this) { jobs[id]?.takeIf { !it.isActive }?.let { jobs.remove(id) } }
        save()
    }

    companion object { const val MAX_JOBS_KEPT = 50 }
}

/** Runs one job's tool loop through the HeyBuddy backend. Cancellation = coroutine cancellation. */
class JobRunner(
    private val loop: ToolLoop,
    private val store: JobStore,
    private val model: String?,
) {
    suspend fun run(job: Job, contextFactory: (Job, (String) -> Unit) -> ToolContext): Job {
        job.status = "running"
        store.update(job)
        val folder = File(job.folder)
        val context = contextFactory(job) { line ->
            job.progress = (job.progress + line).takeLast(30)
            store.update(job)
        }
        val runner = ToolRunner(context)
        val payload: JsonObject = buildJsonObject {
            model?.takeIf { it.isNotBlank() }?.let { put("model", it) }
            put("integrations", false)
            put("system", Prompts.JOB_SYSTEM_PROMPT + "\nyour working folder: ${folder.path}\n")
            put("max_tokens", 8192)
            put("tools", ToolSpecs.definitions(ToolSpecs.JOB_TOOLS))
            put("messages", buildJsonArray { add(buildJsonObject { put("role", "user"); put("content", job.brief) }) })
        }
        try {
            val texts = loop.run(payload, execute = { call -> runner.execute(call, ToolSpecs.JOB_TOOLS) }, maxRounds = MAX_JOB_ROUNDS)
            val final = texts.lastOrNull().orEmpty()
            job.artifacts = (context.artifacts + JobText.parseArtifacts(final, folder)).distinct()
            job.reply = JobText.stripArtifacts(final).ifEmpty { "Done." }
            job.status = "done"
        } catch (cancel: kotlinx.coroutines.CancellationException) {
            job.status = "cancelled"
            job.finishedAt = System.currentTimeMillis()
            store.update(job)
            throw cancel
        } catch (error: Exception) {
            job.status = "failed"
            job.error = error.message ?: error.toString()
        }
        job.finishedAt = System.currentTimeMillis()
        store.update(job)
        return job
    }

    companion object { const val MAX_JOB_ROUNDS = 30 }
}
