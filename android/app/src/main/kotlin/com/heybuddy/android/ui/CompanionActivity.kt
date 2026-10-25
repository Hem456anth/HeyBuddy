package com.heybuddy.android.ui

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.view.Gravity
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.heybuddy.android.a11y.BuddyAccessibilityService
import com.heybuddy.android.capture.FrameEncoder
import com.heybuddy.android.capture.MediaProjectionCapture
import com.heybuddy.android.companion.FrameSource
import com.heybuddy.android.graph
import com.heybuddy.android.service.CompanionService
import com.heybuddy.core.geometry.CapturedFrame
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * The companion sheet as a small bottom window over whatever app is open.
 * Every one-tap activation lands here (Quick Settings tile, widget, launcher
 * shortcut, notification action, accessibility button, ACTION_ASSIST).
 * The window only covers the sheet (FLAG_NOT_TOUCH_MODAL), so the app above
 * it stays usable for guided taps.
 */
class CompanionActivity : ComponentActivity() {

    private var sheetVisible by mutableStateOf(false)
    private var typeMode by mutableStateOf(false)
    private var projectionResult: CompletableDeferred<Pair<Int, Intent?>>? = null

    private val micPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) graph.turns.startListening(pendingFrames) else typeMode = true
    }
    private val projectionConsent = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        projectionResult?.complete(result.resultCode to result.data)
    }
    private var pendingFrames: List<CapturedFrame> = emptyList()

    private val activityFrameSource = FrameSource {
        BuddyAccessibilityService.instance?.capture()?.let { FrameEncoder.encode(this, it, "accessibility screenshot") }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setFinishOnTouchOutside(false)
        window.setGravity(Gravity.BOTTOM)
        window.setLayout(WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.WRAP_CONTENT)
        window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL)
        setContent {
            HeyBuddyTheme {
                if (sheetVisible) {
                    CompanionSheet(graph = graph, onClose = { finish() }, startInTypeMode = typeMode, fillScreen = false)
                }
            }
        }
        handle(intent, fresh = savedInstanceState == null)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent, fresh = true)
    }

    override fun onStart() {
        super.onStart()
        graph.approvals.visibleSurfaces++
        graph.turns.setFrameSource(activityFrameSource)
    }

    override fun onStop() {
        graph.approvals.visibleSurfaces = (graph.approvals.visibleSurfaces - 1).coerceAtLeast(0)
        super.onStop()
    }

    private fun handle(intent: Intent?, fresh: Boolean) {
        if (!fresh) { sheetVisible = true; return }
        typeMode = intent?.getStringExtra(EXTRA_MODE) == "type"
        sheetVisible = false
        lifecycleScope.launch {
            val frames = captureForActivation()
            pendingFrames = frames
            sheetVisible = true
            if (typeMode) return@launch
            if (ContextCompat.checkSelfPermission(this@CompanionActivity, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                graph.turns.startListening(frames)
            } else {
                micPermission.launch(Manifest.permission.RECORD_AUDIO)
            }
        }
    }

    /** Screenshot of the app below, taken before the sheet appears. */
    private suspend fun captureForActivation(): List<CapturedFrame> {
        val settings = graph.settings.current()
        if (!settings.screenContext) return emptyList()
        BuddyAccessibilityService.instance?.let { service ->
            delay(SHADE_SETTLE_MS) // let the notification shade / launcher animation finish
            return listOfNotNull(service.capture()?.let { FrameEncoder.encode(this, it, "accessibility screenshot") })
        }
        if (!settings.mediaProjectionFallback) return emptyList()
        // MediaProjection: user consents each time (Android 14+), one frame, then stop.
        val manager = getSystemService(MediaProjectionManager::class.java)
        val result = CompletableDeferred<Pair<Int, Intent?>>()
        projectionResult = result
        projectionConsent.launch(manager.createScreenCaptureIntent())
        val (code, data) = result.await()
        if (code != Activity.RESULT_OK || data == null) return emptyList()
        if (!CompanionService.startProjection(this)) return emptyList()
        return try {
            val projection = manager.getMediaProjection(code, data) ?: return emptyList()
            delay(250) // consent dialog fade-out
            listOfNotNull(MediaProjectionCapture.grabOneFrame(this, projection)?.let { FrameEncoder.encode(this, it, "screen capture") })
        } catch (_: SecurityException) {
            emptyList()
        } finally {
            CompanionService.endTurn(this)
        }
    }

    companion object {
        const val ACTION_TALK = "com.heybuddy.android.action.TALK"
        const val EXTRA_MODE = "mode"
        const val EXTRA_SOURCE = "source"
        const val SHADE_SETTLE_MS = 350L

        fun talkIntent(context: Context, source: String, type: Boolean = false): Intent =
            Intent(context, CompanionActivity::class.java)
                .setAction(ACTION_TALK)
                .putExtra(EXTRA_SOURCE, source)
                .apply { if (type) putExtra(EXTRA_MODE, "type") }
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }
}
