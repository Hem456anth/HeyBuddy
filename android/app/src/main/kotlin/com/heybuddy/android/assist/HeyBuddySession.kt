package com.heybuddy.android.assist

import android.app.assist.AssistStructure
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Rect
import android.graphics.Region
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.ViewCompositionStrategy
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.setViewTreeLifecycleOwner
import androidx.savedstate.SavedStateRegistry
import androidx.savedstate.SavedStateRegistryController
import androidx.savedstate.SavedStateRegistryOwner
import androidx.savedstate.setViewTreeSavedStateRegistryOwner
import android.service.voice.VoiceInteractionSession
import com.heybuddy.android.a11y.BuddyAccessibilityService
import com.heybuddy.android.capture.FrameEncoder
import com.heybuddy.android.companion.FrameSource
import com.heybuddy.android.companion.Phase
import com.heybuddy.android.graph
import com.heybuddy.android.ui.CompanionSheet
import com.heybuddy.android.ui.HeyBuddyTheme
import com.heybuddy.core.geometry.CapturedFrame
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * The assistant session window (shown by the system above the current app).
 * It receives the screenshot and assist structure of what the user was
 * looking at, starts listening right away, shows the companion sheet at
 * the bottom, and draws ink over the rest of the screen. Only the sheet is
 * touchable; taps elsewhere reach the app below.
 */
class HeyBuddySession(context: Context) : VoiceInteractionSession(context), LifecycleOwner, SavedStateRegistryOwner {

    private val lifecycleRegistry = LifecycleRegistry(this)
    private val savedStateController = SavedStateRegistryController.create(this)
    override val lifecycle: Lifecycle get() = lifecycleRegistry
    override val savedStateRegistry: SavedStateRegistry get() = savedStateController.savedStateRegistry

    private var root: FrameLayout? = null
    private var inkLayer: FrameLayout? = null
    private var sheetView: View? = null
    private var screenText: String = ""
    private var started = false
    private var showArgs: Bundle? = null

    override fun onCreate() {
        super.onCreate()
        savedStateController.performRestore(null)
        lifecycleRegistry.currentState = Lifecycle.State.CREATED
    }

    override fun onCreateContentView(): View {
        val container = FrameLayout(context)
        val ink = FrameLayout(context)
        container.addView(ink, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        val compose = ComposeView(context).apply {
            setViewCompositionStrategy(ViewCompositionStrategy.DisposeOnDetachedFromWindow)
            setContent {
                HeyBuddyTheme {
                    CompanionSheet(
                        graph = context.graph,
                        onClose = { hide() },
                        onSheetBounds = { bounds -> sheetBounds.set(bounds) },
                    )
                }
            }
        }
        container.addView(compose, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        container.setViewTreeLifecycleOwner(this)
        container.setViewTreeSavedStateRegistryOwner(this)
        root = container
        inkLayer = ink
        sheetView = compose
        return container
    }

    private val sheetBounds = Rect()

    override fun onComputeInsets(outInsets: Insets) {
        super.onComputeInsets(outInsets)
        outInsets.contentInsets.setEmpty()
        outInsets.touchableInsets = Insets.TOUCHABLE_INSETS_REGION
        outInsets.touchableRegion.set(Region(sheetBounds))
    }

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        showArgs = args
        lifecycleRegistry.currentState = Lifecycle.State.RESUMED
        val graph = context.graph
        graph.approvals.visibleSurfaces++
        graph.overlay.embeddedHost = inkLayer
        graph.turns.setFrameSource(sessionFrameSource)
        started = false
        val wantsScreenshot = showFlags and SHOW_WITH_SCREENSHOT != 0
        // If no screenshot is coming (disabled by the user/policy), start without one.
        graph.appScope.launch {
            kotlinx.coroutines.delay(if (wantsScreenshot) 1200 else 0)
            startTurnOnce(null)
        }
    }

    override fun onHandleScreenshot(screenshot: Bitmap?) {
        val frame = screenshot?.let { FrameEncoder.encode(context, it, "assist screenshot") }
        pendingFresh?.let { deferred -> pendingFresh = null; deferred.complete(frame); return }
        startTurnOnce(frame)
    }

    override fun onHandleAssist(state: AssistState) {
        if (!state.isFocused) return
        val structure = state.assistStructure ?: return
        screenText = collectText(structure)
    }

    private fun startTurnOnce(frame: CapturedFrame?) {
        if (started) return
        started = true
        val graph = context.graph
        graph.appScope.launch {
            val settings = graph.settings.current()
            val frames = if (settings.screenContext) listOfNotNull(frame) else emptyList()
            kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main) {
                if (showArgs?.getString(HeyBuddyVoiceInteractionService.ARG_REASON) == null) {
                    graph.turns.screenText = screenText
                    graph.turns.startListening(frames)
                }
            }
        }
    }

    @Volatile private var pendingFresh: CompletableDeferred<CapturedFrame?>? = null

    /** Follow-up screenshots: accessibility capture if on, else ask the system for a fresh session. */
    private val sessionFrameSource = FrameSource {
        BuddyAccessibilityService.instance?.capture()?.let { return@FrameSource FrameEncoder.encode(context, it, "accessibility screenshot") }
        val service = HeyBuddyVoiceInteractionService.instance ?: return@FrameSource null
        val deferred = CompletableDeferred<CapturedFrame?>()
        pendingFresh = deferred
        kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main) { service.requestFreshSession("follow_up") }
        withTimeoutOrNull(2500) { deferred.await() }
    }

    override fun onHide() {
        super.onHide()
        lifecycleRegistry.currentState = Lifecycle.State.CREATED
        val graph = context.graph
        graph.approvals.visibleSurfaces = (graph.approvals.visibleSurfaces - 1).coerceAtLeast(0)
        if (graph.turns.state.value.phase == Phase.LISTENING) graph.turns.cancel()
        graph.overlay.embeddedHost = null
        graph.overlay.onHostGone(context)
    }

    override fun onDestroy() {
        lifecycleRegistry.currentState = Lifecycle.State.DESTROYED
        super.onDestroy()
    }

    private fun collectText(structure: AssistStructure): String {
        val builder = StringBuilder()
        fun walk(node: AssistStructure.ViewNode) {
            if (builder.length > MAX_SCREEN_TEXT) return
            node.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { builder.append(it.take(200)).append('\n') }
                ?: node.contentDescription?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { builder.append(it.take(120)).append('\n') }
            for (i in 0 until node.childCount) walk(node.getChildAt(i))
        }
        for (i in 0 until structure.windowNodeCount) walk(structure.getWindowNodeAt(i).rootViewNode)
        return builder.toString().take(MAX_SCREEN_TEXT)
    }

    companion object { const val MAX_SCREEN_TEXT = 3000 }
}
