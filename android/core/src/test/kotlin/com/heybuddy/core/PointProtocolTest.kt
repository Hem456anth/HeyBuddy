package com.heybuddy.core

import com.heybuddy.core.geometry.CapturedFrame
import com.heybuddy.core.program.BuddyProgramParser
import com.heybuddy.core.program.PointTags
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Parity with tests/test_point_protocol.py (legacy [POINT:] tags and image->display mapping). */
class PointProtocolTest {
    @Test fun pointWithLabelAndScreen() {
        val program = BuddyProgramParser.parse("that's on your other monitor. [POINT:400,300:terminal:screen2]")
        assertEquals("that's on your other monitor.", program.beats[0].text)
        val tag = PointTags.find("x [POINT:400,300:terminal:screen2]").single()!!
        assertEquals(PointTags.Tag(400, 300, "terminal", 2), tag)
    }

    @Test fun pointWithoutScreenMeansDefault() {
        val tag = PointTags.find("click the color inspector. [POINT:1100,42:color inspector]").single()!!
        assertNull(tag.screen)
        assertEquals("color inspector", tag.label)
    }

    @Test fun pointNoneIsNotSpoken() {
        val program = BuddyProgramParser.parse("html is the skeleton of a web page. [POINT:none]")
        assertEquals("html is the skeleton of a web page.", program.spokenText)
        assertTrue(program.marks.isEmpty())
        assertTrue(program.explicitlyNoPoint)
    }

    @Test fun labelOnlyScreenSuffixAndNoLabel() {
        assertEquals(PointTags.Tag(5, 6, "", 2), PointTags.find("over there [POINT:5,6:screen2]").single())
        assertEquals(PointTags.Tag(12, 34, "", null), PointTags.find("here. [POINT:12 , 34]").single())
    }

    @Test fun multiplePointsInOrder() {
        val program = BuddyProgramParser.parse("first this [POINT:1,2:file menu] then that [POINT:3,4:save]")
        assertEquals(listOf("file menu", "save"), program.marks.map { it.label })
        assertEquals("first this then that", program.spokenText)
    }

    @Test fun partialTagHiddenWhileStreaming() {
        assertEquals("click save.", BuddyProgramParser.displayTextForPartial("click save. [POI"))
        assertEquals("click save.", BuddyProgramParser.displayTextForPartial("click save. [POINT:12,3"))
        assertEquals("array [0] is first", BuddyProgramParser.displayTextForPartial("array [0] is first"))
    }

    @Test fun imagePointMapsToDisplayPixels() {
        // 2880x1800 panel shown as 1280x800.
        val frame = CapturedFrame(ByteArray(0), "s", 1, 1280, 800, 0, 0, 2880, 1800)
        assertEquals(1440 to 900, frame.imageToDisplay(640.0, 400.0))
        assertEquals(0 to 0, frame.imageToDisplay(0.0, 0.0))
        assertEquals(2879 to 0, frame.imageToDisplay(5000.0, -20.0))
    }
}
