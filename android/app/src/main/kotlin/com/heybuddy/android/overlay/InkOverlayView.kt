package com.heybuddy.android.overlay

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PathMeasure
import android.graphics.Typeface
import android.os.SystemClock
import android.view.View
import android.view.animation.DecelerateInterpolator
import com.heybuddy.core.geometry.InkGeometry
import com.heybuddy.core.geometry.InkShape
import com.heybuddy.core.geometry.OverlayMapper
import com.heybuddy.core.geometry.ResolvedGeometry
import com.heybuddy.core.program.Mark

/**
 * Draws HeyBuddy's buddy pointer and ink marks (original style: blue round
 * pen, translucent marker for highlights, dashed frames, numbered badges).
 * Never takes touches: it lives in a FLAG_NOT_TOUCHABLE window (or a host
 * region the session excludes from its touchable area).
 *
 * Coordinates arrive in display px and are mapped to this view with its
 * on-screen location, so status-bar/cutout offsets and a moved window are
 * handled. Marks captured in another rotation are skipped.
 */
class InkOverlayView(context: Context) : View(context) {

    private class LiveMark(val mark: Mark, val geometry: ResolvedGeometry, val bornAt: Long) {
        var progress = 0f
        var shape: InkShape? = null
    }

    private val density = resources.displayMetrics.density
    private val marks = ArrayList<LiveMark>()
    private var fade = 1f
    private var buddyX = Float.NaN
    private var buddyY = Float.NaN
    private var buddyLabel = ""
    private var buddyAnimator: ValueAnimator? = null
    private var fadeAnimator: ValueAnimator? = null

    /** Rotation of the display right now (Surface.ROTATION_*), set by the controller. */
    var currentRotation: Int = 0

    private val pen = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND; strokeJoin = Paint.Join.ROUND; color = INK_BLUE
    }
    private val marker = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL; color = MARKER_YELLOW }
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL; color = INK_BLUE }
    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; textSize = 14f * density; typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
    }
    private val labelBackground = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = INK_BLUE; style = Paint.Style.FILL }
    private val notePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = INK_BLUE; textSize = 20f * density; typeface = Typeface.create("casual", Typeface.BOLD)
    }
    private val dash = DashPathEffect(floatArrayOf(10f * density, 7f * density), 0f)
    private val path = Path()
    private val partial = Path()
    private val measure = PathMeasure()

    init {
        isClickable = false
        isFocusable = false
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
    }

    private fun mapper(): OverlayMapper {
        val location = IntArray(2)
        getLocationOnScreen(location)
        return OverlayMapper(location[0], location[1], currentRotation)
    }

    val markCount: Int get() = marks.size

    /** Reveal a beat's marks (clearing earlier ones first if the beat asks). */
    fun showMarks(newMarks: List<Pair<Mark, ResolvedGeometry>>, clearFirst: Boolean) {
        fadeAnimator?.cancel()
        fade = 1f
        if (clearFirst) marks.clear()
        val now = SystemClock.uptimeMillis()
        for ((mark, geometry) in newMarks) {
            val live = LiveMark(mark, geometry, now)
            marks += live
            if (mark.kind == "point") flyBuddyTo(geometry, mark.label)
            ValueAnimator.ofFloat(0f, 1f).apply {
                duration = if (mark.kind == "highlight" || mark.kind == "num" || mark.kind == "write") 280 else 520
                interpolator = DecelerateInterpolator()
                addUpdateListener { live.progress = it.animatedValue as Float; invalidate() }
                start()
            }
        }
        invalidate()
    }

    /** Draw immediately at full progress (tests, previews). */
    fun showMarksImmediately(newMarks: List<Pair<Mark, ResolvedGeometry>>) {
        val now = SystemClock.uptimeMillis()
        for ((mark, geometry) in newMarks) {
            marks += LiveMark(mark, geometry, now).apply { progress = 1f }
            if (mark.kind == "point") { val p = mapper().toOverlay(geometry.x, geometry.y, geometry.rotation); if (p != null) { buddyX = p.first; buddyY = p.second; buddyLabel = mark.label } }
        }
        invalidate()
    }

    fun fadeOutAfter(delayMs: Long) {
        fadeAnimator?.cancel()
        fadeAnimator = ValueAnimator.ofFloat(1f, 0f).apply {
            startDelay = delayMs
            duration = 600
            addUpdateListener { fade = it.animatedValue as Float; invalidate() }
            addListener(object : android.animation.AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: android.animation.Animator) {
                    if (fade <= 0.01f) clearAll()
                }
            })
            start()
        }
    }

    fun clearAll() {
        marks.clear()
        buddyX = Float.NaN
        buddyLabel = ""
        fade = 1f
        invalidate()
    }

    private fun flyBuddyTo(geometry: ResolvedGeometry, label: String) {
        val target = mapper().toOverlay(geometry.x, geometry.y, geometry.rotation) ?: return
        val startX = if (buddyX.isNaN()) width * 0.5f else buddyX
        val startY = if (buddyY.isNaN()) height * 0.85f else buddyY
        buddyAnimator?.cancel()
        buddyLabel = label
        buddyAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 650
            interpolator = DecelerateInterpolator(1.6f)
            addUpdateListener {
                val t = it.animatedValue as Float
                // arc upward while flying
                val lift = (4 * t * (1 - t)) * 60f * density
                buddyX = startX + (target.first - startX) * t
                buddyY = startY + (target.second - startY) * t - lift
                invalidate()
            }
            start()
        }
    }

    private fun shapeFor(live: LiveMark): InkShape? {
        live.shape?.let { return it }
        val map = mapper()
        val g = live.geometry
        val origin = map.toOverlay(g.x, g.y, g.rotation) ?: return null
        val (x, y) = origin
        val label = live.mark.label
        val shape = when (live.mark.kind) {
            "ring" -> InkGeometry.ring(x, y, g.w.toFloat(), g.h.toFloat(), density, label)
            "frame" -> InkGeometry.frame(x, y, g.w.toFloat(), g.h.toFloat(), density, label)
            "target" -> InkGeometry.target(x, y, g.w.toFloat(), g.h.toFloat(), density, label)
            "highlight" -> InkGeometry.highlight(x, y, g.w.toFloat(), g.h.toFloat(), density)
            "underline" -> InkGeometry.underline(x, y, g.w.toFloat(), density)
            "arrow" -> {
                val end = map.toOverlay(g.x2, g.y2, g.rotation) ?: return null
                InkGeometry.arrow(x, y, end.first, end.second, density, label)
            }
            "num" -> InkGeometry.badge(x, y, density, (live.mark.params["n"] ?: "1").toString())
            "write" -> InkGeometry.note(x, y, density, (live.mark.params["text"] ?: "").toString())
            "trace" -> {
                val points = FloatArray(g.points.size)
                for (i in g.points.indices step 2) {
                    val p = map.toOverlay(g.points[i], g.points[i + 1], g.rotation) ?: return null
                    points[i] = p.first; points[i + 1] = p.second
                }
                InkGeometry.trace(points, live.mark.params["straight"] == true, live.mark.params["closed"] == true, density)
            }
            "point" -> null
            else -> null
        }
        live.shape = shape
        return shape
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val alpha = (fade * 255).toInt().coerceIn(0, 255)
        for (live in marks) {
            val shape = shapeFor(live) ?: continue
            for (stroke in shape.strokes) {
                path.reset()
                val points = stroke.points
                if (points.size < 2) continue
                path.moveTo(points[0], points[1])
                for (i in 2 until points.size step 2) path.lineTo(points[i], points[i + 1])
                if (stroke.closed) path.close()
                when {
                    stroke.fill && stroke.highlighter -> {
                        marker.alpha = (alpha * 0.38f * live.progress).toInt()
                        canvas.drawPath(path, marker)
                    }
                    stroke.fill -> {
                        fill.alpha = (alpha * live.progress).toInt()
                        canvas.drawPath(path, fill)
                    }
                    else -> {
                        pen.strokeWidth = stroke.widthDp * density
                        pen.alpha = alpha
                        pen.pathEffect = if (stroke.dashed) dash else null
                        if (live.progress >= 0.999f) {
                            canvas.drawPath(path, pen)
                        } else {
                            measure.setPath(path, false)
                            partial.reset()
                            measure.getSegment(0f, measure.length * live.progress, partial, true)
                            canvas.drawPath(partial, pen)
                        }
                    }
                }
            }
            if (shape.badgeText.isNotEmpty()) {
                labelPaint.alpha = alpha
                val textWidth = labelPaint.measureText(shape.badgeText)
                canvas.drawText(shape.badgeText, shape.labelX - textWidth / 2, shape.labelY + labelPaint.textSize / 3, labelPaint)
            } else if (live.mark.kind == "write" && shape.label.isNotEmpty()) {
                notePaint.alpha = (alpha * live.progress).toInt()
                canvas.drawText(shape.label, shape.labelX, shape.labelY, notePaint)
            } else if (shape.label.isNotEmpty() && live.progress > 0.6f) {
                drawLabel(canvas, shape.label, shape.labelX, shape.labelY, alpha)
            }
        }
        if (!buddyX.isNaN()) drawBuddy(canvas, buddyX, buddyY, alpha)
    }

    private fun drawLabel(canvas: Canvas, text: String, x: Float, y: Float, alpha: Int) {
        val padding = 6f * density
        val width = labelPaint.measureText(text)
        val left = x.coerceIn(padding, maxOf(padding, this.width - width - padding * 3))
        val top = (y - labelPaint.textSize - padding * 2).coerceAtLeast(padding)
        labelBackground.alpha = (alpha * 0.92f).toInt()
        canvas.drawRoundRect(left, top, left + width + padding * 2, top + labelPaint.textSize + padding * 1.6f, 8f * density, 8f * density, labelBackground)
        labelPaint.alpha = alpha
        canvas.drawText(text, left + padding, top + labelPaint.textSize + padding * 0.3f, labelPaint)
    }

    /** HeyBuddy's pointer: a small rounded arrowhead with a white edge. */
    private fun drawBuddy(canvas: Canvas, x: Float, y: Float, alpha: Int) {
        val size = 22f * density
        path.reset()
        path.moveTo(x, y)
        path.lineTo(x + size * 0.32f, y + size)
        path.lineTo(x + size * 0.48f, y + size * 0.58f)
        path.lineTo(x + size * 0.92f, y + size * 0.5f)
        path.close()
        fill.alpha = alpha
        canvas.drawPath(path, fill)
        pen.pathEffect = null
        pen.color = Color.WHITE
        pen.strokeWidth = 2f * density
        pen.alpha = alpha
        canvas.drawPath(path, pen)
        pen.color = INK_BLUE
        if (buddyLabel.isNotEmpty()) drawLabel(canvas, buddyLabel, x + size, y + size * 1.6f, alpha)
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        marks.forEach { it.shape = null } // remap after a resize / window move
    }

    companion object {
        val INK_BLUE = Color.rgb(0x3D, 0x7B, 0xFF)
        val MARKER_YELLOW = Color.rgb(0xFF, 0xD5, 0x4F)
    }
}
