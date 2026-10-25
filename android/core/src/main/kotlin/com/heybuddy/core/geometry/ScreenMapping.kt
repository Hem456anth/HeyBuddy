package com.heybuddy.core.geometry

import com.heybuddy.core.program.BuddyProgram
import com.heybuddy.core.program.Mark
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * One screenshot sent to the model, and where it came from.
 *
 * On Android there is one display, but the captured region is not always
 * the whole panel: the assist screenshot, MediaProjection and the
 * accessibility screenshot all return the full logical display in
 * physical pixels for the *rotation at capture time*. We keep that
 * rotation so marks are never drawn on a display that has since rotated.
 *
 * Coordinates:
 *  - image px: what the model sees (downscaled, longest edge <= 1280)
 *  - display px: physical pixels of the display in its current rotation,
 *    origin top-left of the full display (including status/nav bar areas)
 *  - overlay px: pixels inside our overlay window; equals display px minus
 *    the overlay window's on-screen origin (non-zero when the system keeps
 *    the window out of a cutout or bar inset)
 */
data class CapturedFrame(
    val jpeg: ByteArray,
    val label: String,
    val screenNumber: Int,
    val imageWidth: Int,
    val imageHeight: Int,
    /** Region of the display the screenshot covers, in display px. */
    val regionLeft: Int,
    val regionTop: Int,
    val regionWidth: Int,
    val regionHeight: Int,
    /** Display rotation (0..3, Surface.ROTATION_*) when the frame was taken. */
    val rotation: Int = 0,
    /** Display density (px per dp) at capture, for length conversions. */
    val density: Float = 1f,
    val mediaType: String = "image/jpeg",
) {
    /** Map an image coordinate to display px, clamped inside the captured region. */
    fun imageToDisplay(imageX: Double, imageY: Double): Pair<Int, Int> {
        val clampedX = imageX.coerceIn(0.0, imageWidth.toDouble())
        val clampedY = imageY.coerceIn(0.0, imageHeight.toDouble())
        val scaleX = regionWidth / max(1, imageWidth).toDouble()
        val scaleY = regionHeight / max(1, imageHeight).toDouble()
        val x = regionLeft + (clampedX * scaleX).roundToInt()
        val y = regionTop + (clampedY * scaleY).roundToInt()
        return min(x, regionLeft + regionWidth - 1) to min(y, regionTop + regionHeight - 1)
    }

    override fun equals(other: Any?): Boolean = this === other
    override fun hashCode(): Int = System.identityHashCode(this)

    companion object {
        const val MAX_DIMENSION = 1280

        /** Size an image must be scaled to so its longest edge is <= [maxDimension]. */
        fun scaledSize(width: Int, height: Int, maxDimension: Int = MAX_DIMENSION): Pair<Int, Int> {
            val longest = max(width, height)
            if (longest <= maxDimension || longest <= 0) return width to height
            val scale = maxDimension.toDouble() / longest
            return max(1, (width * scale).roundToInt()) to max(1, (height * scale).roundToInt())
        }

        fun labelFor(screenNumber: Int, width: Int, height: Int, source: String): String =
            "screen $screenNumber of 1 ($source, the user's phone screen) - image is ${width}x$height pixels; use these pixels for coordinates"
    }
}

/** Where marks resolve to, in display px. */
data class ResolvedGeometry(
    val x: Double,
    val y: Double,
    val w: Double = 0.0,
    val h: Double = 0.0,
    val x2: Double = 0.0,
    val y2: Double = 0.0,
    /** Flattened [x0, y0, x1, y1, ...] for traces. */
    val points: List<Double> = emptyList(),
    val rotation: Int = 0,
    val density: Float = 1f,
)

class ResolvedMark(val mark: Mark, val geometry: ResolvedGeometry)

object MarkResolver {

    /** Fill display-px geometry for every mark. Without frames all marks are dropped. */
    fun resolve(program: BuddyProgram, frames: List<CapturedFrame>): Map<Mark, ResolvedGeometry> {
        val result = LinkedHashMap<Mark, ResolvedGeometry>()
        if (frames.isEmpty()) {
            program.beats.forEach { it.marks.clear() }
            return result
        }
        for (beat in program.beats) {
            for (mark in beat.marks) {
                val frame = pickFrame(mark, frames)
                mark.screenIndex = frame.screenNumber
                result[mark] = resolveOne(mark, frame)
            }
        }
        return result
    }

    private fun pickFrame(mark: Mark, frames: List<CapturedFrame>): CapturedFrame {
        val index = mark.screenIndex
        return if (index != null && index in 1..frames.size) frames[index - 1] else frames[0]
    }

    private fun num(value: Any?): Double = (value as? Number)?.toDouble() ?: 0.0

    fun resolveOne(mark: Mark, frame: CapturedFrame): ResolvedGeometry {
        val params = mark.params
        return when (mark.kind) {
            "trace" -> {
                @Suppress("UNCHECKED_CAST")
                val points = params["points"] as List<List<Double>>
                val flat = ArrayList<Double>()
                for ((px, py) in points) {
                    val (x, y) = frame.imageToDisplay(px, py)
                    flat += x.toDouble(); flat += y.toDouble()
                }
                ResolvedGeometry(flat[0], flat[1], points = flat, rotation = frame.rotation, density = frame.density)
            }
            "arrow" -> {
                val (x1, y1) = frame.imageToDisplay(num(params["x1"]), num(params["y1"]))
                val (x2, y2) = frame.imageToDisplay(num(params["x2"]), num(params["y2"]))
                ResolvedGeometry(x1.toDouble(), y1.toDouble(), x2 = x2.toDouble(), y2 = y2.toDouble(), rotation = frame.rotation, density = frame.density)
            }
            else -> {
                val (x, y) = frame.imageToDisplay(num(params["x"]), num(params["y"]))
                var w = 0.0
                var h = 0.0
                if (params.containsKey("w")) {
                    val (right, bottom) = frame.imageToDisplay(num(params["x"]) + num(params["w"]), num(params["y"]) + num(params["h"]))
                    w = max(1.0, (right - x).toDouble())
                    h = if (params.containsKey("h")) max(1.0, (bottom - y).toDouble()) else 0.0
                }
                ResolvedGeometry(x.toDouble(), y.toDouble(), w, h, rotation = frame.rotation, density = frame.density)
            }
        }
    }
}

/**
 * Converts display px to overlay-window px at draw time. Returns null when
 * the display has rotated since capture: the screenshot no longer matches
 * what is on screen, so drawing would point at the wrong thing.
 */
class OverlayMapper(
    private val overlayOriginX: Int,
    private val overlayOriginY: Int,
    private val currentRotation: Int,
) {
    fun toOverlay(displayX: Double, displayY: Double, captureRotation: Int): Pair<Float, Float>? {
        if (captureRotation != currentRotation) return null
        return (displayX - overlayOriginX).toFloat() to (displayY - overlayOriginY).toFloat()
    }

    /** Hit test for target(): is a tap at overlay px inside the target box (with slack in display px)? */
    fun hitsTarget(tapOverlayX: Float, tapOverlayY: Float, geometry: ResolvedGeometry, slackPx: Float): Boolean {
        val origin = toOverlay(geometry.x, geometry.y, geometry.rotation) ?: return false
        val left = origin.first - slackPx
        val top = origin.second - slackPx
        val right = origin.first + geometry.w.toFloat() + slackPx
        val bottom = origin.second + geometry.h.toFloat() + slackPx
        return tapOverlayX in left..right && tapOverlayY in top..bottom
    }

    /** Same hit test for a tap reported in display px (accessibility events give screen bounds). */
    fun displayRectHitsTarget(left: Int, top: Int, right: Int, bottom: Int, geometry: ResolvedGeometry, slackPx: Float): Boolean {
        if (geometry.rotation != currentRotation) return false
        val targetLeft = geometry.x - slackPx
        val targetTop = geometry.y - slackPx
        val targetRight = geometry.x + geometry.w + slackPx
        val targetBottom = geometry.y + geometry.h + slackPx
        val centerX = (left + right) / 2.0
        val centerY = (top + bottom) / 2.0
        return centerX in targetLeft..targetRight && centerY in targetTop..targetBottom
    }
}
