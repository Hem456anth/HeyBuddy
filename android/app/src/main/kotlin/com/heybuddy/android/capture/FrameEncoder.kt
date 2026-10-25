package com.heybuddy.android.capture

import android.content.Context
import android.graphics.Bitmap
import android.hardware.display.DisplayManager
import android.view.Display
import com.heybuddy.core.geometry.CapturedFrame
import java.io.ByteArrayOutputStream

/**
 * Turns a full-display bitmap into what the model sees: longest edge
 * <= 1280 px, JPEG q80, labeled with its pixel size (the coordinate space
 * the model must answer in). Region = the whole display in physical px at
 * the current rotation.
 */
object FrameEncoder {
    const val JPEG_QUALITY = 80

    fun displayInfo(context: Context): Triple<Int, Int, Pair<Int, Float>> {
        val display = context.getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
        val metrics = android.util.DisplayMetrics()
        @Suppress("DEPRECATION")
        display.getRealMetrics(metrics)
        return Triple(metrics.widthPixels, metrics.heightPixels, display.rotation to metrics.density)
    }

    fun encode(context: Context, bitmap: Bitmap, source: String): CapturedFrame {
        val (displayWidth, displayHeight, rotationAndDensity) = displayInfo(context)
        val software = if (bitmap.config == Bitmap.Config.HARDWARE) bitmap.copy(Bitmap.Config.ARGB_8888, false) else bitmap
        val (width, height) = CapturedFrame.scaledSize(software.width, software.height)
        val scaled = if (width != software.width || height != software.height) Bitmap.createScaledBitmap(software, width, height, true) else software
        val bytes = ByteArrayOutputStream().use { stream ->
            scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, stream)
            stream.toByteArray()
        }
        // Captures normally cover the whole display. If the aspect ratio differs (some
        // assist screenshots leave out a bar), assume it is display-px from the top-left.
        val sameAspect = displayHeight > 0 && software.height > 0 &&
            kotlin.math.abs(software.width.toDouble() / software.height - displayWidth.toDouble() / displayHeight) < 0.02
        val regionWidth = if (sameAspect) displayWidth else software.width
        val regionHeight = if (sameAspect) displayHeight else software.height
        if (scaled !== software) scaled.recycle()
        if (software !== bitmap) software.recycle()
        return CapturedFrame(
            jpeg = bytes,
            label = CapturedFrame.labelFor(1, width, height, source),
            screenNumber = 1,
            imageWidth = width,
            imageHeight = height,
            regionLeft = 0,
            regionTop = 0,
            regionWidth = regionWidth,
            regionHeight = regionHeight,
            rotation = rotationAndDensity.first,
            density = rotationAndDensity.second,
        )
    }
}
