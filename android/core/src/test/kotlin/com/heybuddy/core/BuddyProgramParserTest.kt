package com.heybuddy.core

import com.heybuddy.core.geometry.CapturedFrame
import com.heybuddy.core.geometry.MarkResolver
import com.heybuddy.core.program.BuddyProgramParser
import com.heybuddy.core.program.LiteralCallParser
import com.heybuddy.core.program.ProgramSyntaxError
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Parity with the Windows port's tests/test_scene_program.py (same inputs, same expectations). */
class BuddyProgramParserTest {

    private fun frames() = listOf(
        // A 2560x1600 display shown to the model as 1280x800.
        CapturedFrame(ByteArray(0), "s1", 1, 1280, 800, 0, 0, 2560, 1600),
        CapturedFrame(ByteArray(0), "s2", 2, 1280, 720, 2560, 0, 1920, 1080),
    )

    @Test fun fencedProgramBeatsAndMarks() {
        val reply = "sure.\n```buddy\nsay(\"click export up here\")\nring(1150, 30, 70, 28, \"export\")\nsay(\"then choose pdf\")\nframe(900, 120, 300, 220)\narrow(100, 100, 300, 200, \"drag\", screen=2)\n```"
        val program = BuddyProgramParser.parse(reply)
        assertEquals(listOf("sure.", "click export up here", "then choose pdf"), program.beats.map { it.text })
        assertEquals(listOf("ring"), program.beats[1].marks.map { it.kind })
        assertEquals(listOf("frame", "arrow"), program.beats[2].marks.map { it.kind })
        assertEquals(2, program.beats[2].marks[1].screenIndex)
        assertEquals("sure. click export up here then choose pdf", program.spokenText)
    }

    @Test fun bareProgramWithJsOptions() {
        val reply = "step(1);\nsay(\"I can trace the space.\");\n" +
            "trace([[164, 165], [1155, 165], [1155, 587]], {straight: true});\n" +
            "startOver();\nsay(\"click it\");\ntarget(771, 359, 432, 27, \"Open the introduction\");\n" +
            "noted(\"Low-intermediate Python.\");\ntask(\"Learn LangChain\");\nseen(\"Watching a course.\");"
        val program = BuddyProgramParser.parse(reply)
        assertEquals("trace", program.beats[0].marks[0].kind)
        assertEquals(true, program.beats[0].marks[0].params["straight"])
        assertTrue(program.beats[1].clearBefore)
        assertEquals("Open the introduction", program.targets[0].label)
        assertEquals(listOf("Low-intermediate Python."), program.notedFacts)
        assertEquals("Learn LangChain", program.taskTitle)
        assertEquals(listOf("Watching a course."), program.seenNotes)
        assertTrue(program.parseErrors.isEmpty())
    }

    @Test fun nothingIsExecutedAndBadCallsAreDropped() {
        val reply = "```buddy\n__import__(\"os\").system(\"calc\")\nsay(open(\"x\").read())\nring(1, 2)\nsay(\"ok\")\n```"
        val program = BuddyProgramParser.parse(reply)
        assertEquals(listOf("ok"), program.beats.map { it.text })
        assertEquals(3, program.parseErrors.size)
    }

    @Test fun moreHostileInputIsRejected() {
        for (bad in listOf("say(x)", "say(\"a\" + \"b\")", "say(\"a\").length", "eval(\"1\")", "say(\"a\"); rm()",
            "point(1, 2, label=f())", "say(**{})")) {
            val program = BuddyProgramParser.parse("```buddy\n$bad\n```")
            assertTrue("no error for $bad", program.parseErrors.isNotEmpty())
            assertTrue(program.beats.all { it.text.isEmpty() || it.text == "a" })
        }
        try { LiteralCallParser.parse("system(\"x\")"); throw AssertionError("accepted") } catch (_: ProgramSyntaxError) {}
    }

    @Test fun semicolonsInsideStringsSurvive() {
        val program = BuddyProgramParser.parse("```buddy\nsay(\"first; second\"); point(5, 6, \"x\")\n```")
        assertEquals("first; second", program.beats[0].text)
        assertEquals(mapOf("x" to 5L, "y" to 6L, "label" to "x"), program.beats[0].marks[0].params)
    }

    @Test fun legacyPointTagsStillWork() {
        val program = BuddyProgramParser.parse("over there [POINT:400,300:terminal:screen2]")
        assertEquals("over there", program.beats[0].text)
        val mark = program.beats[0].marks[0]
        assertEquals("point", mark.kind)
        assertEquals(400L, mark.params["x"])
        assertEquals(2, mark.screenIndex)
        assertTrue(BuddyProgramParser.parse("html is markup [POINT:none]").explicitlyNoPoint)
    }

    @Test fun resolutionMapsBoxesToDisplayPixels() {
        val program = BuddyProgramParser.parse("```buddy\nsay(\"a\")\nframe(640, 400, 64, 40)\npoint(640, 360, \"t\", screen=2)\n```")
        val geometry = MarkResolver.resolve(program, frames())
        val (frame, point) = program.beats[0].marks
        val frameGeometry = geometry.getValue(frame)
        assertEquals(listOf(1280.0, 800.0, 128.0, 80.0), listOf(frameGeometry.x, frameGeometry.y, frameGeometry.w, frameGeometry.h))
        val pointGeometry = geometry.getValue(point)
        assertEquals(2560.0 + 960.0, pointGeometry.x, 0.0)
        assertEquals(540.0, pointGeometry.y, 0.0)
    }

    @Test fun marksDroppedWithoutScreenshots() {
        val program = BuddyProgramParser.parse("```buddy\nsay(\"a\")\nring(1,2,3,4)\n```")
        MarkResolver.resolve(program, emptyList())
        assertTrue(program.beats[0].marks.isEmpty())
    }

    @Test fun partialDisplayHidesCode() {
        val partial = "ok ```buddy\nsay(\"click the gear\")\nring(1, 2, 3, 4)\nsay(\"then sa"
        assertEquals("ok click the gear", BuddyProgramParser.displayTextForPartial(partial))
        assertEquals("hello", BuddyProgramParser.displayTextForPartial("hello [POINT:12"))
    }

    @Test fun literalTypesAndOptions() {
        val call = LiteralCallParser.parse("point(-3, 4.5, 'it\\'s', screen=1, {curve: false, label: \"x\"});")
        assertEquals(listOf(-3L, 4.5, "it's"), call.args)
        assertEquals(1L, call.kwargs["screen"])
        assertEquals(false, call.kwargs["curve"])
        val nested = LiteralCallParser.parse("trace([[1, 2], [3, 4]], straight=True)")
        assertEquals(listOf(listOf(1L, 2L), listOf(3L, 4L)), nested.args[0])
        assertEquals(true, nested.kwargs["straight"])
    }

    @Test fun statementLimitAndLongTextAreCapped() {
        val body = (1..200).joinToString("\n") { "say(\"line $it\")" }
        val program = BuddyProgramParser.parse("```buddy\n$body\n```")
        assertEquals(BuddyProgramParser.MAX_STATEMENTS, program.beats.size)
        val long = BuddyProgramParser.parse("```buddy\nsay(\"${"a".repeat(2000)}\")\n```")
        assertEquals(BuddyProgramParser.MAX_TEXT_CHARS, long.beats[0].text.length)
        assertFalse(long.beats[0].text.contains("```"))
    }
}
