package com.heybuddy.core.memory

import java.io.File
import java.time.LocalDate

/**
 * A small, user-visible memory file (`memory.md` in app-private storage).
 * Same format and rules as the Windows `memory_store.py`: dated markdown
 * bullets the user can read, edit or clear in the app; anything that looks
 * like a secret is refused.
 */
class MemoryStore(val file: File, private val today: () -> String = { LocalDate.now().toString() }) {

    fun facts(): List<String> {
        if (!file.exists()) return emptyList()
        return file.readLines(Charsets.UTF_8).filter { it.startsWith("- ") }.map { it.substring(2).trim() }
    }

    @Synchronized
    fun add(rawFact: String): Boolean {
        val fact = rawFact.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").take(MAX_FACT_CHARS)
        if (fact.isEmpty() || looksSecret(fact)) return false
        val existing = facts().toMutableList()
        val bare = existing.map { it.replace(Regex("^\\(\\d{4}-\\d{2}-\\d{2}\\)\\s*"), "").lowercase() }
        if (fact.lowercase() in bare) return true
        existing += "(${today()}) $fact"
        write(existing.takeLast(MAX_FACTS))
        return true
    }

    /** Replace the whole file with what the user typed in the editor (secrets still refused line by line). */
    @Synchronized
    fun replaceAll(text: String): List<String> {
        val rejected = ArrayList<String>()
        val kept = text.lines().map { it.trim() }.filter { it.startsWith("- ") }.map { it.substring(2).trim() }.filter {
            val secret = looksSecret(it)
            if (secret) rejected += it
            it.isNotEmpty() && !secret
        }
        if (kept.isEmpty()) clear() else write(kept.takeLast(MAX_FACTS))
        return rejected
    }

    fun readRaw(): String = if (file.exists()) file.readText(Charsets.UTF_8) else HEADER

    @Synchronized
    fun clear() {
        if (file.exists()) file.delete()
    }

    fun promptBlock(): String {
        val all = facts()
        if (all.isEmpty()) return ""
        return "things the user told you to remember (use them quietly, don't recite them):\n" +
            all.takeLast(60).joinToString("\n") { "- $it" }
    }

    private fun write(items: List<String>) {
        file.parentFile?.mkdirs()
        val temp = File(file.path + ".tmp")
        temp.writeText(HEADER + items.joinToString("\n") { "- $it" } + "\n", Charsets.UTF_8)
        if (!temp.renameTo(file)) {
            file.writeText(temp.readText()); temp.delete()
        }
    }

    companion object {
        const val MAX_FACTS = 200
        const val MAX_FACT_CHARS = 300
        const val HEADER = "# What HeyBuddy remembers about you\n\nEdit or delete lines freely.\n\n"
        private val SECRET_RE = Regex(
            "(?i)(password|passwd|pwd|api[_ -]?key|secret|token|otp|one[- ]time code|pin code|cvv|card number)\\s*[:=]?\\s*\\S{4,}" +
                "|\\b(sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{10,}" +
                "|\\b(?:\\d[ -]?){13,19}\\b",
        )

        fun looksSecret(fact: String): Boolean = SECRET_RE.containsMatchIn(fact)
    }
}
