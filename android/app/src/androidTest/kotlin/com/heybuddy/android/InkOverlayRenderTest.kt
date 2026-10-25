package com.heybuddy.android

import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toPixelMap
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.viewinterop.AndroidView
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.heybuddy.android.overlay.InkOverlayView
import com.heybuddy.core.geometry.ResolvedGeometry
import com.heybuddy.core.program.Mark
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** The ink layer draws marks where the program asked (hosted in Compose, as the companion preview does). */
@RunWith(AndroidJUnit4::class)
class InkOverlayRenderTest {
    @get:Rule val compose = createComposeRule()

    private fun inkPixels(): Int {
        val map = compose.onNodeWithTag("ink").captureToImage().toPixelMap()
        var count = 0
        for (y in 0 until map.height step 2) for (x in 0 until map.width step 2) {
            val c = map[x, y]
            // HeyBuddy's blue pen: clearly blue, clearly opaque.
            if (c.alpha > 0.5f && c.blue > 0.5f && c.blue > c.red + 0.2f) count++
        }
        return count
    }

    @Test fun ringAndArrowRender() {
        lateinit var view: InkOverlayView
        compose.setContent {
            AndroidView(factory = { InkOverlayView(it).also { v -> view = v } }, modifier = Modifier.fillMaxSize().testTag("ink"))
        }
        compose.waitForIdle()
        assertEquals("nothing drawn before marks", 0, inkPixels())

        compose.runOnIdle {
            val origin = IntArray(2).also { view.getLocationOnScreen(it) }
            val x = origin[0] + view.width * 0.2
            val y = origin[1] + view.height * 0.3
            view.showMarksImmediately(
                listOf(
                    Mark("ring", mutableMapOf("label" to "Wi-Fi")) to ResolvedGeometry(x, y, w = view.width * 0.5, h = 160.0),
                    Mark("arrow", mutableMapOf()) to ResolvedGeometry(x, y + 400, x2 = x + 300, y2 = y + 200),
                ),
            )
        }
        compose.waitForIdle()
        assertEquals(2, view.markCount)
        val drawn = inkPixels()
        assertTrue("expected ink pixels, got $drawn", drawn > 50)

        compose.runOnIdle { view.clearAll() }
        compose.waitForIdle()
        assertEquals(0, view.markCount)
    }

    @Test fun marksFromAnotherRotationAreSkipped() {
        lateinit var view: InkOverlayView
        compose.setContent {
            AndroidView(factory = { InkOverlayView(it).also { v -> view = v } }, modifier = Modifier.fillMaxSize().testTag("ink"))
        }
        compose.runOnIdle {
            view.currentRotation = 0
            view.showMarksImmediately(listOf(Mark("ring", mutableMapOf()) to ResolvedGeometry(100.0, 300.0, w = 400.0, h = 160.0, rotation = 1)))
        }
        compose.waitForIdle()
        assertEquals(0, inkPixels())
    }
}
