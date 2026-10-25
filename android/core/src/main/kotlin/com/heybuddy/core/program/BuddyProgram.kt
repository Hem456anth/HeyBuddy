package com.heybuddy.core.program

/**
 * The "buddy program" reply format: spoken lines that own on-screen marks.
 *
 * Kotlin port of the Windows port's `src/core/scene_program.py` (same
 * behaviour, same tests). The model answers with one fenced block (or a
 * bare program starting with `say(` / `step(` / `task(`):
 *
 * ```buddy
 * say("tap the gear up here")
 * ring(980, 60, 80, 80, "settings")
 * say("then pick display")
 * target(40, 900, 1000, 120, "Display")
 * ```
 *
 * Nothing is executed. Every statement goes through [LiteralCallParser],
 * which only accepts whitelisted call names with literal arguments.
 * Coordinates are pixels of the screenshot image the model saw.
 */
class Mark(
    val kind: String,
    val params: MutableMap<String, Any?>,
    var screenIndex: Int? = null,
) {
    // Identity equality on purpose: marks are used as keys for their
    // resolved geometry, and params may be mutated after creation.
    val label: String
        get() = (params["label"] ?: params["text"] ?: "").toString()

    override fun toString(): String = "Mark($kind, $params, screen=$screenIndex)"
}

data class Beat(
    val text: String,
    val marks: MutableList<Mark> = mutableListOf(),
    val clearBefore: Boolean = false,
)

data class BuddyProgram(
    val beats: MutableList<Beat>,
    val explicitlyNoPoint: Boolean = false,
    val parseErrors: MutableList<String> = mutableListOf(),
    val notedFacts: MutableList<String> = mutableListOf(),
    val seenNotes: MutableList<String> = mutableListOf(),
    var taskTitle: String = "",
    var planText: String = "",
) {
    val marks: List<Mark> get() = beats.flatMap { it.marks }
    val targets: List<Mark> get() = marks.filter { it.kind == "target" }
    val spokenText: String get() = beats.map { it.text }.filter { it.isNotEmpty() }.joinToString(" ").trim()
}

object BuddyProgramParser {

    /** kind -> (positional parameter names, how many leading params must be numbers). */
    val MARK_SIGNATURES: Map<String, Pair<List<String>, Int>> = mapOf(
        "point" to (listOf("x", "y", "label") to 2),
        "ring" to (listOf("x", "y", "w", "h", "label") to 4),
        "frame" to (listOf("x", "y", "w", "h", "label") to 4),
        "highlight" to (listOf("x", "y", "w", "h") to 4),
        "underline" to (listOf("x", "y", "w") to 3),
        "arrow" to (listOf("x1", "y1", "x2", "y2", "label") to 4),
        "num" to (listOf("x", "y", "n") to 2),
        "write" to (listOf("x", "y", "text") to 2),
        // Outlines a control and waits for the user's tap inside it; the tap
        // starts an automatic follow-up turn (guided walkthroughs).
        "target" to (listOf("x", "y", "w", "h", "label") to 4),
        // Free polyline through [[x, y], ...]; straight=true means no smoothing.
        "trace" to (listOf("points") to 1),
    )

    /** Calls that change beats or memory but draw nothing. */
    val CONTROL_CALLS: Set<String> = setOf("say", "clear", "startOver", "erase", "step", "noted", "seen", "task", "plan")

    const val MAX_STATEMENTS = 80
    const val MAX_TEXT_CHARS = 600

    private val FENCE_RE = Regex("```(?:buddy|heybuddy|clicky|js|javascript)?[ \\t]*\\n(.*?)(?:```|\\z)", RegexOption.DOT_MATCHES_ALL)
    private val BARE_PROGRAM_RE = Regex("^\\s*(?:step|say|task)\\s*\\(")
    private val WHITESPACE_RE = Regex("[ \\t]{2,}")
    private val SAY_LINE_RE = Regex("say\\(\\s*([\"'])(.*?)(?<!\\\\)\\1", RegexOption.DOT_MATCHES_ALL)

    fun clean(text: String): String = WHITESPACE_RE.replace(text, " ").trim()

    /** Split on newlines and top-level semicolons, never inside a string. */
    fun splitStatements(code: String): List<String> {
        val statements = ArrayList<String>()
        val current = StringBuilder()
        var quote: Char? = null
        var escaped = false
        for (char in code) {
            if (quote != null) {
                current.append(char)
                if (escaped) escaped = false
                else if (char == '\\') escaped = true
                else if (char == quote) quote = null
                continue
            }
            when (char) {
                '"', '\'', '`' -> { quote = char; current.append(char) }
                ';', '\n' -> { statements += current.toString(); current.setLength(0) }
                else -> current.append(char)
            }
        }
        statements += current.toString()
        return statements.map { it.trim() }.filter { it.isNotEmpty() && !it.startsWith("//") && !it.startsWith("#") }
    }

    /** Turn a full model reply into beats. Never throws. */
    fun parse(reply: String?): BuddyProgram {
        var text = reply ?: ""
        if (!text.contains("```") && BARE_PROGRAM_RE.containsMatchIn(text)) {
            text = "```buddy\n$text\n```"
        }
        val fences = FENCE_RE.findAll(text).toList()
        if (fences.isEmpty()) return parseLegacy(text)

        val outside = StringBuilder()
        var cursor = 0
        for (fence in fences) {
            outside.append(text, cursor, fence.range.first).append(' ')
            cursor = fence.range.last + 1
        }
        if (cursor < text.length) outside.append(text.substring(cursor))
        val prose = clean(PointTags.strip(outside.toString()))
        val code = fences.joinToString("\n") { it.groupValues[1] }

        val beats = mutableListOf<Beat>()
        if (prose.isNotEmpty()) beats += Beat(text = prose.take(MAX_TEXT_CHARS * 2))
        val program = BuddyProgram(beats = beats)
        var pendingClear = false
        for (statement in splitStatements(code).take(MAX_STATEMENTS)) {
            try {
                val call = LiteralCallParser.parse(statement)
                val firstRaw = call.args.firstOrNull() ?: call.kwargs["text"] ?: ""
                val firstText = clean(literalToText(firstRaw)).take(MAX_TEXT_CHARS)
                when (call.name) {
                    "say" -> if (firstText.isNotEmpty()) {
                        beats += Beat(text = firstText, clearBefore = pendingClear)
                        pendingClear = false
                    }
                    "clear", "startOver", "erase" -> pendingClear = true
                    "step" -> Unit
                    "noted" -> if (firstText.isNotEmpty()) program.notedFacts += firstText.take(300)
                    "seen" -> if (firstText.isNotEmpty()) program.seenNotes += firstText.take(300)
                    "task" -> program.taskTitle = firstText.take(80)
                    "plan" -> program.planText = firstText
                    else -> {
                        val mark = buildMark(call)
                        if (beats.isEmpty()) {
                            beats += Beat(text = "", clearBefore = pendingClear)
                            pendingClear = false
                        }
                        beats.last().marks += mark
                    }
                }
            } catch (error: IllegalArgumentException) {
                program.parseErrors += (error.message ?: "bad statement")
            } catch (error: IndexOutOfBoundsException) {
                program.parseErrors += (error.message ?: "bad statement")
            }
        }
        if (beats.isEmpty()) beats += Beat(text = "")
        return program
    }

    /** Python's str() of a literal, close enough for spoken text. */
    private fun literalToText(value: Any?): String = when (value) {
        null -> "None"
        is Boolean -> if (value) "True" else "False"
        is Double -> if (value == Math.floor(value) && !value.isInfinite()) value.toString() else value.toString()
        else -> value.toString()
    }

    private fun isNumber(value: Any?): Boolean = value is Long || value is Double || value is Int

    private fun buildMark(call: ParsedCall): Mark {
        val (names, required) = MARK_SIGNATURES.getValue(call.name)
        val kwargs = LinkedHashMap(call.kwargs)
        var screenValue = kwargs.remove("screen")
        val params = LinkedHashMap<String, Any?>()
        call.args.take(names.size).forEachIndexed { index, value -> params[names[index]] = value }
        // A trailing extra positional like point(x, y, "label", 2) is the screen number.
        if (call.args.size > names.size && screenValue == null && isNumber(call.args[names.size])) {
            screenValue = call.args[names.size]
        }
        for ((key, value) in kwargs) if (key in names) params[key] = value
        val screenIndex = (screenValue as? Number)?.toInt()

        if (call.name == "trace") {
            val points = params["points"]
            val valid = points is List<*> && points.size >= 2 && points.all { point ->
                point is List<*> && point.size >= 2 && isNumber(point[0]) && isNumber(point[1])
            }
            if (!valid) throw ProgramSyntaxError("trace() needs [[x, y], ...] with at least two points")
            params["points"] = (points as List<*>).take(64).map { point ->
                val pair = point as List<*>
                listOf((pair[0] as Number).toDouble(), (pair[1] as Number).toDouble())
            }
            params["straight"] = kwargs["straight"] == true
            params["closed"] = kwargs["closed"] == true
            return Mark(kind = call.name, params = params, screenIndex = screenIndex)
        }
        for (key in names.take(required)) {
            if (!isNumber(params[key])) throw ProgramSyntaxError("${call.name}() needs numeric $key")
        }
        for (key in listOf("label", "text")) {
            if (params.containsKey(key)) params[key] = literalToText(params[key]).take(120)
        }
        if (params.containsKey("n")) params["n"] = literalToText(params["n"]).take(4)
        return Mark(kind = call.name, params = params, screenIndex = screenIndex)
    }

    private fun parseLegacy(text: String): BuddyProgram {
        val marks = mutableListOf<Mark>()
        var explicitlyNoPoint = false
        for (tag in PointTags.find(text)) {
            if (tag == null) { explicitlyNoPoint = true; continue }
            marks += Mark(
                kind = "point",
                params = linkedMapOf("x" to tag.x.toLong(), "y" to tag.y.toLong(), "label" to tag.label),
                screenIndex = tag.screen,
            )
        }
        val spoken = clean(PointTags.strip(text))
        return BuddyProgram(beats = mutableListOf(Beat(text = spoken, marks = marks)), explicitlyNoPoint = explicitlyNoPoint)
    }

    /**
     * What the companion sheet shows while a reply streams: prose plus the
     * say() lines seen so far, never raw code or half-streamed tags.
     */
    fun displayTextForPartial(partial: String?): String {
        val text = partial ?: ""
        val fenceStart = text.indexOf("```")
        if (fenceStart == -1) {
            var withoutTags = PointTags.strip(text)
            val bracket = withoutTags.lastIndexOf('[')
            if (bracket != -1 && !withoutTags.substring(bracket).contains(']')) {
                val fragment = withoutTags.substring(bracket)
                if ("[POINT:".startsWith(fragment.take(7)) || fragment.startsWith("[POINT")) {
                    withoutTags = withoutTags.substring(0, bracket)
                }
            }
            return clean(withoutTags)
        }
        val prose = clean(PointTags.strip(text.substring(0, fenceStart)))
        val spoken = SAY_LINE_RE.findAll(text.substring(fenceStart)).joinToString(" ") { clean(it.groupValues[2]) }
        return clean("$prose $spoken")
    }
}

/** Clicky-style legacy `[POINT:x,y:label:screenN]` tags. */
object PointTags {
    data class Tag(val x: Int, val y: Int, val label: String, val screen: Int?)

    val PATTERN = Regex(
        "\\[POINT:(?:none|(\\d+)\\s*,\\s*(\\d+)" +
            "(?::(?!screen\\d+\\])([^\\]:]+?))?" +
            "(?::screen(\\d+))?)\\]",
    )

    /** Every tag in order; `null` entries are `[POINT:none]`. */
    fun find(text: String): List<Tag?> = PATTERN.findAll(text).map { match ->
        val x = match.groups[1]?.value
        val y = match.groups[2]?.value
        if (x == null || y == null) null
        else Tag(x.toInt(), y.toInt(), (match.groups[3]?.value ?: "").trim(), match.groups[4]?.value?.toInt())
    }.toList()

    fun strip(text: String): String = PATTERN.replace(text, "")
}
