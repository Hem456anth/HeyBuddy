package com.heybuddy.core.geometry

import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin

/**
 * Pure geometry for HeyBuddy's own ink style (original design): every
 * mark becomes a few polylines the overlay strokes with a round pen, plus
 * an optional filled area and a label anchor. Kept platform-free so it can
 * be unit-tested and so the Android overlay only has to stroke paths.
 *
 * All inputs/outputs are overlay pixels. [dp] converts design lengths.
 */
data class InkStroke(
    val points: FloatArray,
    val dashed: Boolean = false,
    val closed: Boolean = false,
    val fill: Boolean = false,
    val widthDp: Float = 3f,
    val highlighter: Boolean = false,
)

data class InkShape(
    val strokes: List<InkStroke>,
    val labelX: Float,
    val labelY: Float,
    val label: String,
    val badgeText: String = "",
    /** Bounding box (left, top, right, bottom), for hit tests and invalidation. */
    val bounds: FloatArray,
)

object InkGeometry {

    fun ring(x: Float, y: Float, w: Float, h: Float, dp: Float, label: String): InkShape {
        // A loop slightly larger than the box that overshoots its start, like a pen circling.
        val padding = 8f * dp
        val centerX = x + w / 2
        val centerY = y + h / 2
        val radiusX = w / 2 + padding
        val radiusY = h / 2 + padding
        val steps = 48
        val points = FloatArray((steps + 7) * 2)
        for (i in 0 until steps + 7) {
            val t = -PI / 2.4 + 2 * PI * i / steps
            val wobble = 1.0 + 0.035 * sin(3 * t) - i * 0.002
            points[i * 2] = (centerX + radiusX * wobble * cos(t)).toFloat()
            points[i * 2 + 1] = (centerY + radiusY * wobble * sin(t)).toFloat()
        }
        return InkShape(
            listOf(InkStroke(points)), centerX, y - padding - 6f * dp, label,
            bounds = floatArrayOf(centerX - radiusX * 1.1f, centerY - radiusY * 1.1f, centerX + radiusX * 1.1f, centerY + radiusY * 1.1f),
        )
    }

    fun frame(x: Float, y: Float, w: Float, h: Float, dp: Float, label: String): InkShape {
        val padding = 4f * dp
        val left = x - padding; val top = y - padding; val right = x + w + padding; val bottom = y + h + padding
        val points = floatArrayOf(left, top, right, top, right, bottom, left, bottom)
        return InkShape(listOf(InkStroke(points, dashed = true, closed = true, widthDp = 2.5f)), left, top - 6f * dp, label,
            bounds = floatArrayOf(left, top, right, bottom))
    }

    fun target(x: Float, y: Float, w: Float, h: Float, dp: Float, label: String): InkShape {
        val padding = 3f * dp
        val left = x - padding; val top = y - padding; val right = x + w + padding; val bottom = y + h + padding
        val box = floatArrayOf(left, top, right, top, right, bottom, left, bottom)
        return InkShape(
            listOf(
                InkStroke(box, closed = true, fill = true, widthDp = 0f, highlighter = true),
                InkStroke(box, closed = true, widthDp = 3f),
            ),
            left, top - 6f * dp, label, bounds = floatArrayOf(left, top, right, bottom),
        )
    }

    fun highlight(x: Float, y: Float, w: Float, h: Float, dp: Float): InkShape {
        val points = floatArrayOf(x, y, x + w, y, x + w, y + h, x, y + h)
        return InkShape(listOf(InkStroke(points, closed = true, fill = true, widthDp = 0f, highlighter = true)), x, y, "",
            bounds = floatArrayOf(x, y, x + w, y + h))
    }

    fun underline(x: Float, y: Float, w: Float, dp: Float): InkShape {
        val steps = 16
        val points = FloatArray((steps + 1) * 2)
        for (i in 0..steps) {
            points[i * 2] = x + w * i / steps
            points[i * 2 + 1] = y + 2f * dp + (sin(i * 0.9) * 1.2 * dp).toFloat()
        }
        return InkShape(listOf(InkStroke(points)), x, y, "", bounds = floatArrayOf(x, y - 4 * dp, x + w, y + 6 * dp))
    }

    fun arrow(x1: Float, y1: Float, x2: Float, y2: Float, dp: Float, label: String): InkShape {
        val length = max(1f, hypot(x2 - x1, y2 - y1))
        // Bend the shaft to one side by ~18% of its length (quadratic bezier).
        val normalX = -(y2 - y1) / length
        val normalY = (x2 - x1) / length
        val bend = min(120f * dp, length * 0.18f)
        val controlX = (x1 + x2) / 2 + normalX * bend
        val controlY = (y1 + y2) / 2 + normalY * bend
        val steps = 24
        val shaft = FloatArray((steps + 1) * 2)
        for (i in 0..steps) {
            val t = i / steps.toFloat()
            val a = (1 - t) * (1 - t); val b = 2 * (1 - t) * t; val c = t * t
            shaft[i * 2] = a * x1 + b * controlX + c * x2
            shaft[i * 2 + 1] = a * y1 + b * controlY + c * y2
        }
        val angle = atan2((y2 - controlY).toDouble(), (x2 - controlX).toDouble())
        val head = 14f * dp
        val spread = PI / 7
        val headPoints = floatArrayOf(
            (x2 - head * cos(angle - spread)).toFloat(), (y2 - head * sin(angle - spread)).toFloat(),
            x2, y2,
            (x2 - head * cos(angle + spread)).toFloat(), (y2 - head * sin(angle + spread)).toFloat(),
        )
        return InkShape(listOf(InkStroke(shaft), InkStroke(headPoints)), x1, y1 - 8f * dp, label,
            bounds = floatArrayOf(min(x1, x2) - head, min(y1, y2) - head - bend, max(x1, x2) + head, max(y1, y2) + head + bend))
    }

    fun badge(x: Float, y: Float, dp: Float, text: String): InkShape {
        val radius = 13f * dp
        val steps = 24
        val points = FloatArray(steps * 2)
        for (i in 0 until steps) {
            val t = 2 * PI * i / steps
            points[i * 2] = (x + radius * cos(t)).toFloat()
            points[i * 2 + 1] = (y + radius * sin(t)).toFloat()
        }
        return InkShape(listOf(InkStroke(points, closed = true, fill = true, widthDp = 0f)), x, y, "", badgeText = text,
            bounds = floatArrayOf(x - radius, y - radius, x + radius, y + radius))
    }

    fun note(x: Float, y: Float, dp: Float, text: String): InkShape =
        InkShape(emptyList(), x, y, text, bounds = floatArrayOf(x, y - 24 * dp, x + 260 * dp, y + 8 * dp))

    fun trace(points: FloatArray, straight: Boolean, closed: Boolean, dp: Float): InkShape {
        val path = if (straight || points.size < 6) points else smooth(points)
        var left = Float.MAX_VALUE; var top = Float.MAX_VALUE; var right = -Float.MAX_VALUE; var bottom = -Float.MAX_VALUE
        for (i in path.indices step 2) {
            left = min(left, path[i]); right = max(right, path[i]); top = min(top, path[i + 1]); bottom = max(bottom, path[i + 1])
        }
        return InkShape(listOf(InkStroke(path, closed = closed)), points[0], points[1], "", bounds = floatArrayOf(left, top, right, bottom))
    }

    /** Catmull-Rom through the points (passes through every input point). */
    fun smooth(points: FloatArray, segmentsPerSpan: Int = 8): FloatArray {
        val count = points.size / 2
        val out = ArrayList<Float>()
        fun px(i: Int) = points[min(max(i, 0), count - 1) * 2]
        fun py(i: Int) = points[min(max(i, 0), count - 1) * 2 + 1]
        for (i in 0 until count - 1) {
            for (s in 0 until segmentsPerSpan) {
                val t = s / segmentsPerSpan.toFloat()
                val t2 = t * t; val t3 = t2 * t
                fun cr(p0: Float, p1: Float, p2: Float, p3: Float) =
                    0.5f * (2 * p1 + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2 + (-p0 + 3 * p1 - 3 * p2 + p3) * t3)
                out += cr(px(i - 1), px(i), px(i + 1), px(i + 2))
                out += cr(py(i - 1), py(i), py(i + 1), py(i + 2))
            }
        }
        out += px(count - 1); out += py(count - 1)
        return out.toFloatArray()
    }
}
