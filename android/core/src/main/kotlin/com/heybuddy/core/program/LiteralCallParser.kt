package com.heybuddy.core.program

/**
 * Parses exactly one `name(arg, arg, key=value, {key: value})` call whose
 * arguments are literals only. Nothing is ever evaluated: this is a tiny
 * hand-written tokenizer + recursive-descent parser that understands
 * numbers, strings, booleans, null, lists and a trailing options object.
 * Anything else (attribute access, nested calls, operators, identifiers
 * used as values) is rejected with [ProgramSyntaxError].
 *
 * Mirrors the Windows port's `ast`-based `parse_statement` (literal-only,
 * no eval) and accepts the same JS habits: `true/false/null/undefined`,
 * Python `True/False/None`, a trailing `{straight: true}` object, a final
 * `;`, single, double or back-tick quotes.
 *
 * Value types produced: [Long] (integers), [Double] (decimals), [String],
 * [Boolean], `null`, [List] of values.
 */
class ProgramSyntaxError(message: String) : IllegalArgumentException(message)

data class ParsedCall(
    val name: String,
    val args: List<Any?>,
    val kwargs: Map<String, Any?>,
)

object LiteralCallParser {

    private sealed class Token {
        data class Ident(val text: String) : Token()
        data class Num(val value: Any) : Token()
        data class Str(val value: String) : Token()
        data class Punct(val char: Char) : Token()
        object End : Token()
    }

    private const val MAX_STATEMENT_CHARS = 20_000
    private const val MAX_LIST_DEPTH = 4

    fun parse(statement: String): ParsedCall {
        val trimmed = statement.trim().trimEnd(';').trim()
        if (trimmed.length > MAX_STATEMENT_CHARS) throw ProgramSyntaxError("statement too long")
        val tokens = tokenize(trimmed)
        return Parser(tokens, trimmed).parseCall()
    }

    // ------------------------------------------------------------------
    // Tokenizer
    // ------------------------------------------------------------------

    private fun tokenize(source: String): List<Token> {
        val tokens = ArrayList<Token>()
        var index = 0
        while (index < source.length) {
            val char = source[index]
            when {
                char.isWhitespace() -> index++
                char == '"' || char == '\'' || char == '`' -> {
                    val (value, next) = readString(source, index)
                    tokens += Token.Str(value)
                    index = next
                }
                char.isDigit() || (char == '.' && index + 1 < source.length && source[index + 1].isDigit()) -> {
                    val (value, next) = readNumber(source, index)
                    tokens += Token.Num(value)
                    index = next
                }
                char.isLetter() || char == '_' || char == '$' -> {
                    var end = index + 1
                    while (end < source.length && (source[end].isLetterOrDigit() || source[end] == '_' || source[end] == '$')) end++
                    tokens += Token.Ident(source.substring(index, end))
                    index = end
                }
                char in "()[]{},=:-+." -> {
                    tokens += Token.Punct(char)
                    index++
                }
                else -> throw ProgramSyntaxError("unexpected character '$char'")
            }
        }
        tokens += Token.End
        return tokens
    }

    private fun readString(source: String, start: Int): Pair<String, Int> {
        val quote = source[start]
        val builder = StringBuilder()
        var index = start + 1
        while (index < source.length) {
            val char = source[index]
            if (char == '\\' && index + 1 < source.length) {
                val escaped = source[index + 1]
                when (escaped) {
                    'n' -> builder.append('\n')
                    't' -> builder.append('\t')
                    'r' -> builder.append('\r')
                    '0' -> builder.append('\u0000')
                    'u' -> {
                        if (index + 6 <= source.length) {
                            val hex = source.substring(index + 2, index + 6)
                            val code = hex.toIntOrNull(16) ?: throw ProgramSyntaxError("bad \\u escape")
                            builder.append(code.toChar())
                            index += 6
                            continue
                        } else {
                            throw ProgramSyntaxError("bad \\u escape")
                        }
                    }
                    else -> builder.append(escaped) // \" \' \\ \` and unknown escapes keep the char
                }
                index += 2
                continue
            }
            if (char == quote) return builder.toString() to index + 1
            builder.append(char)
            index++
        }
        throw ProgramSyntaxError("unterminated string")
    }

    private fun readNumber(source: String, start: Int): Pair<Any, Int> {
        var index = start
        var sawDot = false
        var sawExponent = false
        while (index < source.length) {
            val char = source[index]
            when {
                char.isDigit() || char == '_' -> index++
                char == '.' && !sawDot && !sawExponent -> { sawDot = true; index++ }
                (char == 'e' || char == 'E') && !sawExponent -> {
                    sawExponent = true
                    index++
                    if (index < source.length && (source[index] == '+' || source[index] == '-')) index++
                }
                else -> break
            }
        }
        val text = source.substring(start, index).replace("_", "")
        val value: Any = if (sawDot || sawExponent) {
            text.toDoubleOrNull() ?: throw ProgramSyntaxError("bad number $text")
        } else {
            text.toLongOrNull() ?: text.toDoubleOrNull() ?: throw ProgramSyntaxError("bad number $text")
        }
        return value to index
    }

    // ------------------------------------------------------------------
    // Parser
    // ------------------------------------------------------------------

    private class Parser(private val tokens: List<Token>, private val source: String) {
        private var position = 0

        private fun peek(): Token = tokens[position]
        private fun peekAt(offset: Int): Token = tokens.getOrElse(position + offset) { Token.End }
        private fun next(): Token = tokens[position++]

        private fun expectPunct(char: Char) {
            val token = next()
            if (token !is Token.Punct || token.char != char) {
                throw ProgramSyntaxError("not a call: ${source.take(60)}")
            }
        }

        private fun isPunct(token: Token, char: Char) = token is Token.Punct && token.char == char

        fun parseCall(): ParsedCall {
            val nameToken = next() as? Token.Ident ?: throw ProgramSyntaxError("not a call: ${source.take(60)}")
            if (!isPunct(peek(), '(')) throw ProgramSyntaxError("not a call: ${source.take(60)}")
            val name = nameToken.text
            if (name !in BuddyProgramParser.MARK_SIGNATURES && name !in BuddyProgramParser.CONTROL_CALLS) {
                throw ProgramSyntaxError("unknown call $name()")
            }
            expectPunct('(')
            val args = ArrayList<Any?>()
            val kwargs = LinkedHashMap<String, Any?>()
            while (!isPunct(peek(), ')')) {
                val token = peek()
                when {
                    token is Token.Ident && isPunct(peekAt(1), '=') -> {
                        next(); next()
                        kwargs[token.text] = parseValue(0)
                    }
                    isPunct(token, '{') -> kwargs.putAll(parseObject())
                    else -> args += parseValue(0)
                }
                if (isPunct(peek(), ',')) {
                    next()
                } else if (!isPunct(peek(), ')')) {
                    throw ProgramSyntaxError("only literal numbers and strings are allowed")
                }
            }
            expectPunct(')')
            if (peek() !is Token.End) throw ProgramSyntaxError("not a call: ${source.take(60)}")
            return ParsedCall(name, args, kwargs)
        }

        private fun parseObject(): Map<String, Any?> {
            expectPunct('{')
            val result = LinkedHashMap<String, Any?>()
            while (!isPunct(peek(), '}')) {
                val keyToken = next()
                val key = when (keyToken) {
                    is Token.Ident -> keyToken.text
                    is Token.Str -> keyToken.value
                    else -> throw ProgramSyntaxError("bad options object")
                }
                expectPunct(':')
                val value = parseValue(0)
                if (key.isNotEmpty() && (key[0].isLetter() || key[0] == '_') && key.all { it.isLetterOrDigit() || it == '_' }) {
                    result[key] = value
                }
                if (isPunct(peek(), ',')) next() else if (!isPunct(peek(), '}')) throw ProgramSyntaxError("bad options object")
            }
            expectPunct('}')
            return result
        }

        private fun parseValue(depth: Int): Any? {
            val token = next()
            return when {
                token is Token.Num -> token.value
                token is Token.Str -> token.value
                token is Token.Ident -> when (token.text) {
                    "true", "True" -> true
                    "false", "False" -> false
                    "null", "None", "undefined" -> null
                    else -> throw ProgramSyntaxError("only literal numbers and strings are allowed")
                }
                isPunct(token, '-') || isPunct(token, '+') -> {
                    val inner = next() as? Token.Num ?: throw ProgramSyntaxError("only literal numbers and strings are allowed")
                    val negative = isPunct(token, '-')
                    when (val value = inner.value) {
                        is Long -> if (negative) -value else value
                        is Double -> if (negative) -value else value
                        else -> throw ProgramSyntaxError("only literal numbers and strings are allowed")
                    }
                }
                isPunct(token, '[') || isPunct(token, '(') -> {
                    if (depth >= MAX_LIST_DEPTH) throw ProgramSyntaxError("lists nested too deep")
                    val closing = if (isPunct(token, '[')) ']' else ')'
                    val items = ArrayList<Any?>()
                    while (!isPunct(peek(), closing)) {
                        items += parseValue(depth + 1)
                        if (isPunct(peek(), ',')) next() else if (!isPunct(peek(), closing)) {
                            throw ProgramSyntaxError("only literal numbers and strings are allowed")
                        }
                    }
                    next()
                    items
                }
                else -> throw ProgramSyntaxError("only literal numbers and strings are allowed")
            }
        }
    }
}
