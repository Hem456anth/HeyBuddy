package com.heybuddy.android

import android.content.Intent
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import com.heybuddy.android.settings.AppSettings
import com.heybuddy.android.ui.CompanionActivity
import com.heybuddy.android.ui.MainActivity
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.util.regex.Pattern

/** Onboarding can be walked through with every permission denied or skipped, and nothing crashes. */
@RunWith(AndroidJUnit4::class)
class OnboardingAndPermissionsTest {
    @get:Rule val compose = createEmptyComposeRule()
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val device = UiDevice.getInstance(instrumentation)
    private lateinit var saved: AppSettings

    @Before fun setUp() = runBlocking {
        saved = context.graph.settings.current()
        context.graph.settings.update { AppSettings() } // fresh-install state
    }

    @After fun tearDown() = runBlocking { context.graph.settings.update { saved } }

    /** Deny a runtime-permission dialog if Android shows one (it may auto-deny after earlier denials). */
    private fun denyPermissionDialogIfShown() {
        val deny = device.wait(Until.findObject(By.res(Pattern.compile(".*:id/permission_deny(_and_dont_ask_again)?_button"))), 8_000)
        deny?.click()
        device.waitForIdle()
    }

    private fun waitForTag(tag: String, timeoutMs: Long = 10_000) {
        compose.waitUntil(timeoutMs) { compose.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
    }

    /** Advance a step: its skip/later/not-now button, or "Next" when the step is already satisfied. */
    private fun advance() {
        compose.waitForIdle()
        if (compose.onAllNodesWithTag("onboarding_skip").fetchSemanticsNodes().isNotEmpty()) {
            compose.onNodeWithTag("onboarding_skip").performClick()
        } else {
            compose.onAllNodesWithText("Next")[0].performClick()
        }
    }

    @Test fun walkThroughWithEverythingDenied() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            waitForTag("onboarding_welcome")
            compose.onNodeWithTag("onboarding_next").performClick()
            waitForTag("onboarding_server")
            advance() // "Later": no server yet
            waitForTag("onboarding_mic")
            if (compose.onAllNodesWithTag("grant_mic").fetchSemanticsNodes().isNotEmpty()) {
                compose.onNodeWithTag("grant_mic").performClick()
                denyPermissionDialogIfShown()
            } else {
                advance()
            }
            waitForTag("onboarding_notifications")
            if (compose.onAllNodesWithTag("grant_notifications").fetchSemanticsNodes().isNotEmpty()) {
                compose.onNodeWithTag("grant_notifications").performClick()
                denyPermissionDialogIfShown()
            } else {
                advance()
            }
            waitForTag("onboarding_assistant")
            advance()
            waitForTag("onboarding_accessibility")
            advance()
            waitForTag("onboarding_overlay")
            advance()
            waitForTag("onboarding_done")
            compose.onNodeWithTag("onboarding_finish").performClick()
            waitForTag("tab_Home")
            assertEquals(Lifecycle.State.RESUMED, scenario.state)
        }
    }

    @Test fun companionWithMicDeniedFallsBackToTyping() {
        val intent = CompanionActivity.talkIntent(context, source = "test").setFlags(0)
        ActivityScenario.launch<CompanionActivity>(intent).use { scenario ->
            denyPermissionDialogIfShown()
            waitForTag("companion_input", 15_000)
            assertEquals(Lifecycle.State.RESUMED, scenario.state)
        }
    }

    @Test fun companionTypeShortcutOpensWithoutServer() {
        val intent = Intent(CompanionActivity.ACTION_TALK).setClass(context, CompanionActivity::class.java)
            .putExtra(CompanionActivity.EXTRA_MODE, "type")
        ActivityScenario.launch<CompanionActivity>(intent).use { scenario ->
            waitForTag("companion_sheet", 15_000)
            waitForTag("companion_input")
            assertEquals(Lifecycle.State.RESUMED, scenario.state)
        }
    }
}
