package com.heybuddy.core

import com.heybuddy.core.geometry.CapturedFrame
import com.heybuddy.core.geometry.InkGeometry
import com.heybuddy.core.geometry.MarkResolver
import com.heybuddy.core.geometry.OverlayMapper
import com.heybuddy.core.program.BuddyProgramParser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class GeometryTest {
    // A 1080x2400 phone in portrait, screenshot downscaled to 576x1280.
    private val phone = CapturedFrame(ByteArray(0), "phone", 1, 576, 1280, 0, 0, 1080, 2400, rotation = 0, density = 2.75f)

    @Test fun scaledSizeKeepsLongestEdgeAt1280() {
        assertEquals(576 to 1280, CapturedFrame.scaledSize(1080, 2400))
        assertEquals(1280 to 576, CapturedFrame.scaledSize(2400, 1080))
        assertEquals(720 to 1280, CapturedFrame.scaledSize(720, 1280))
        assertEquals(800 to 600, CapturedFrame.scaledSize(800, 600))
    }

    @Test fun phoneScreenshotMapsBackToDisplay() {
        assertEquals(540 to 1200, phone.imageToDisplay(288.0, 640.0))
        assertEquals(1079 to 2399, phone.imageToDisplay(576.0, 1280.0))
    }

    @Test fun overlayOffsetIsSubtracted() {
        // The system placed our overlay below a 120 px status bar / cutout.
        val mapper = OverlayMapper(overlayOriginX = 0, overlayOriginY = 120, currentRotation = 0)
        assertEquals(540f to 1080f, mapper.toOverlay(540.0, 1200.0, captureRotation = 0))
    }

    @Test fun rotationSinceCaptureDropsTheMark() {
        val mapper = OverlayMapper(0, 0, currentRotation = 1)
        assertNull(mapper.toOverlay(10.0, 10.0, captureRotation = 0))
    }

    @Test fun targetHitTestUsesSlack() {
        val program = BuddyProgramParser.parse("```buddy\nsay(\"tap wifi\")\ntarget(48, 200, 480, 60, \"Wi-Fi\")\n```")
        val geometry = MarkResolver.resolve(program, listOf(phone)).values.single()
        // target box in display px: x=90, y=375, w=900, h=113 (approximately)
        val mapper = OverlayMapper(0, 0, 0)
        assertTrue(mapper.hitsTarget(500f, 420f, geometry, slackPx = 12f))
        assertTrue(mapper.hitsTarget(85f, 370f, geometry, slackPx = 12f))
        assertFalse(mapper.hitsTarget(500f, 900f, geometry, slackPx = 12f))
        assertTrue(mapper.displayRectHitsTarget(400, 380, 600, 480, geometry, 12f))
        assertFalse(OverlayMapper(0, 0, 1).displayRectHitsTarget(400, 380, 600, 480, geometry, 12f))
    }

    @Test fun inkShapesHaveSaneBounds() {
        val ring = InkGeometry.ring(100f, 100f, 200f, 80f, 3f, "x")
        assertTrue(ring.bounds[0] < 100f && ring.bounds[2] > 300f)
        val arrow = InkGeometry.arrow(0f, 0f, 300f, 0f, 3f, "")
        val head = arrow.strokes[1].points
        assertEquals(300f, head[2], 0.01f)
        val smooth = InkGeometry.smooth(floatArrayOf(0f, 0f, 10f, 10f, 20f, 0f))
        assertEquals(20f, smooth[smooth.size - 2], 0.001f)
        assertEquals(0f, smooth[0], 0.001f)
    }
}
