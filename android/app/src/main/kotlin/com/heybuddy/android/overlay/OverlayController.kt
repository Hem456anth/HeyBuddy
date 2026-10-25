package com.heybuddy.android.overlay

import android.content.Context
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.input.InputManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Display
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.FrameLayout
import com.heybuddy.core.geometry.ResolvedGeometry
import com.heybuddy.core.program.Mark

/**
 * Where the ink is drawn, in order of preference:
 *
 * 1. **Accessibility overlay** (`TYPE_ACCESSIBILITY_OVERLAY`) when HeyBuddy's
 *    accessibility service is on: trusted for touch pass-through, above apps.
 * 2. **App overlay** (`TYPE_APPLICATION_OVERLAY`) when the user granted
 *    "Display over other apps". Android 12+ blocks touches passing through
 *    untrusted overlays unless the window's alpha is at most the system's
 *    maximum obscuring opacity (0.8 by default), so the window alpha is
 *    capped to keep taps reaching the app below.
 * 3. **Embedded** in a HeyBuddy window that is already full-screen (the
 *    assistant session), which excludes the ink area from its touchable region.
 *
 * Every window is FLAG_NOT_TOUCHABLE | FLAG_NOT_FOCUSABLE and laid out over
 * the whole display, cutouts included.
 */
class OverlayController(private val appContext: Context) {
    enum class Mode { NONE, ACCESSIBILITY, APP_OVERLAY, EMBEDDED }

    private val main = Handler(Looper.getMainLooper())
    private var view: InkOverlayView? = null
    private var windowManager: WindowManager? = null
    private var embeddedParent: ViewGroup? = null
    var mode: Mode = Mode.NONE
        private set

    /** Set by the accessibility service while it is connected. */
    @Volatile var accessibilityContext: Context? = null
    /** Set by the assistant session while it shows. */
    @Volatile var embeddedHost: ViewGroup? = null

    val isShowing: Boolean get() = view != null
    val inkView: InkOverlayView? get() = view

    fun canDrawAppOverlay(): Boolean = Settings.canDrawOverlays(appContext)

    fun bestAvailableMode(): Mode = when {
        accessibilityContext != null -> Mode.ACCESSIBILITY
        embeddedHost != null -> Mode.EMBEDDED
        canDrawAppOverlay() -> Mode.APP_OVERLAY
        else -> Mode.NONE
    }

    private fun currentRotation(context: Context): Int =
        context.getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)?.rotation ?: 0

    /** Make sure an ink surface exists. Returns false when none is available. Main thread. */
    fun ensureAttached(): Boolean {
        val wanted = bestAvailableMode()
        if (view != null && wanted == mode) return true
        detach()
        if (wanted == Mode.NONE) return false
        return try {
            when (wanted) {
                Mode.ACCESSIBILITY -> attachWindow(accessibilityContext!!, WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY, capAlpha = false)
                Mode.APP_OVERLAY -> attachWindow(appContext, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, capAlpha = true)
                Mode.EMBEDDED -> attachEmbedded(embeddedHost!!)
                Mode.NONE -> Unit
            }
            mode = wanted
            true
        } catch (error: Exception) {
            // BadTokenException / SecurityException when the permission was just revoked.
            detach()
            false
        }
    }

    private fun attachWindow(context: Context, type: Int, capAlpha: Boolean) {
        val manager = context.getSystemService(WindowManager::class.java)
        val ink = InkOverlayView(context).apply { currentRotation = currentRotation(context) }
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or
                WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            title = "HeyBuddy ink"
            if (Build.VERSION.SDK_INT >= 30) {
                layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
            } else if (Build.VERSION.SDK_INT >= 28) {
                layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
            }
            if (capAlpha) alpha = maxPassThroughAlpha(context)
            if (Build.VERSION.SDK_INT >= 30) setFitInsetsTypes(0)
        }
        manager.addView(ink, params)
        windowManager = manager
        view = ink
    }

    private fun attachEmbedded(parent: ViewGroup) {
        val ink = InkOverlayView(parent.context).apply { currentRotation = currentRotation(parent.context) }
        parent.addView(ink, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        embeddedParent = parent
        view = ink
    }

    private fun maxPassThroughAlpha(context: Context): Float {
        val limit = if (Build.VERSION.SDK_INT >= 31) {
            context.getSystemService(InputManager::class.java).maximumObscuringOpacityForTouch
        } else 1f
        return minOf(0.8f, limit)
    }

    fun showBeat(marks: List<Pair<Mark, ResolvedGeometry>>, clearFirst: Boolean) = main.post {
        if (marks.isEmpty() && !clearFirst) return@post
        if (!ensureAttached()) return@post
        view?.currentRotation = currentRotation(view!!.context)
        view?.showMarks(marks, clearFirst)
    }

    fun lingerThenFade(seconds: Int) = main.post { view?.fadeOutAfter(seconds * 1000L) }

    fun clear() = main.post { view?.clearAll() }

    fun detach() {
        val current = view ?: run { mode = Mode.NONE; return }
        try {
            when (mode) {
                Mode.EMBEDDED -> embeddedParent?.removeView(current)
                else -> windowManager?.removeViewImmediate(current)
            }
        } catch (_: Exception) {
        }
        view = null
        windowManager = null
        embeddedParent = null
        mode = Mode.NONE
    }

    fun detachOnMain() = main.post { detach() }

    /** Drop the surface if it belongs to a host that is going away. */
    fun onHostGone(context: Context) = main.post {
        if (view?.context === context || (mode == Mode.EMBEDDED && embeddedHost == null)) detach()
    }

    companion object {
        fun isVisible(view: View?) = view != null && view.isAttachedToWindow
    }
}
