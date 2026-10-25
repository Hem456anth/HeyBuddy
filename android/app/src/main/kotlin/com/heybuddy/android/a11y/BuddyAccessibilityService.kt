package com.heybuddy.android.a11y

import android.accessibilityservice.AccessibilityButtonController
import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityManager
import android.view.accessibility.AccessibilityNodeInfo
import com.heybuddy.android.graph
import com.heybuddy.android.ui.CompanionActivity
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * Optional accessibility service. It only does what the user turned it on
 * for, and only while HeyBuddy is being used:
 *  - hosts the ink overlay (TYPE_ACCESSIBILITY_OVERLAY, touch-through);
 *  - notices a tap on the control a `target()` outlined (view-clicked events);
 *  - types approved text into the focused field (insert_text tool);
 *  - takes the screenshot for a turn on Android 11+ (no MediaProjection prompt);
 *  - starts HeyBuddy from the accessibility button / shortcut.
 * It never logs or stores what it sees.
 */
class BuddyAccessibilityService : AccessibilityService() {

    private var buttonCallback: AccessibilityButtonController.AccessibilityButtonCallback? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        graph.overlay.accessibilityContext = this
        val callback = object : AccessibilityButtonController.AccessibilityButtonCallback() {
            override fun onClicked(controller: AccessibilityButtonController) {
                startActivity(CompanionActivity.talkIntent(this@BuddyAccessibilityService, source = "accessibility_button"))
            }
        }
        accessibilityButtonController.registerAccessibilityButtonCallback(callback)
        buttonCallback = callback
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        if (event.eventType != AccessibilityEvent.TYPE_VIEW_CLICKED) return
        if (event.packageName == packageName) return
        val source = event.source ?: return
        val bounds = Rect()
        source.getBoundsInScreen(bounds)
        graph.turns.onUserTappedAt(bounds.left, bounds.top, bounds.right, bounds.bottom)
    }

    override fun onInterrupt() = Unit

    override fun onUnbind(intent: Intent?): Boolean {
        disconnect()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        disconnect()
        super.onDestroy()
    }

    private fun disconnect() {
        buttonCallback?.let { accessibilityButtonController.unregisterAccessibilityButtonCallback(it) }
        buttonCallback = null
        if (instance === this) instance = null
        graph.overlay.accessibilityContext = null
        graph.overlay.onHostGone(this)
    }

    /** Android 11+: screenshot of the default display without a MediaProjection prompt. */
    suspend fun capture(): Bitmap? {
        if (Build.VERSION.SDK_INT < 30) return null
        return suspendCancellableCoroutine { continuation ->
            takeScreenshot(Display.DEFAULT_DISPLAY, mainExecutor, object : TakeScreenshotCallback {
                override fun onSuccess(screenshot: ScreenshotResult) {
                    val hardware = Bitmap.wrapHardwareBuffer(screenshot.hardwareBuffer, screenshot.colorSpace)
                    val copy = hardware?.copy(Bitmap.Config.ARGB_8888, false)
                    hardware?.recycle()
                    screenshot.hardwareBuffer.close()
                    if (continuation.isActive) continuation.resume(copy)
                }
                override fun onFailure(errorCode: Int) {
                    if (continuation.isActive) continuation.resume(null)
                }
            })
        }
    }

    /** Type [text] into the focused editable field at its cursor. Returns a status line or throws. */
    fun insertText(text: String): String {
        val focused = rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?: throw IllegalStateException("no text field is focused; tap the field first")
        if (!focused.isEditable) throw IllegalStateException("the focused element isn't a text field")
        if (focused.isPassword) throw IllegalStateException("HeyBuddy never types into password fields")
        val existing = focused.text?.toString().orEmpty()
        // Hint text is reported as text on some fields; don't append to it.
        val current = if (focused.isShowingHintText) "" else existing
        val start = focused.textSelectionStart.takeIf { it in 0..current.length } ?: current.length
        val end = focused.textSelectionEnd.takeIf { it in start..current.length } ?: start
        val updated = current.substring(0, start) + text + current.substring(end)
        val arguments = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, updated)
        }
        if (!focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)) {
            throw IllegalStateException("that app didn't accept typed text")
        }
        val cursor = Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, start + text.length)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, start + text.length)
        }
        focused.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, cursor)
        return "inserted ${text.length} characters"
    }

    companion object {
        @Volatile var instance: BuddyAccessibilityService? = null
            private set

        fun isEnabled(context: Context): Boolean {
            val manager = context.getSystemService(AccessibilityManager::class.java)
            return manager.getEnabledAccessibilityServiceList(android.accessibilityservice.AccessibilityServiceInfo.FEEDBACK_ALL_MASK)
                .any { it.resolveInfo.serviceInfo.packageName == context.packageName }
        }
    }
}
