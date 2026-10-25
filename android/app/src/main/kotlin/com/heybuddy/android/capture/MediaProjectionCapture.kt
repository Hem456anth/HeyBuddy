package com.heybuddy.android.capture

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.Looper
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull

/**
 * One frame from a MediaProjection, then stop. The user consents every time
 * (Android 14+ requires a fresh consent per capture session), the projection
 * runs inside the companion foreground service (type mediaProjection), and
 * it is stopped as soon as the first frame arrives.
 */
object MediaProjectionCapture {
    suspend fun grabOneFrame(context: Context, projection: MediaProjection, timeoutMs: Long = 3000): Bitmap? {
        val (width, height, rotationAndDensity) = FrameEncoder.displayInfo(context)
        val densityDpi = (rotationAndDensity.second * 160).toInt()
        val handler = Handler(Looper.getMainLooper())
        val reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        val result = CompletableDeferred<Bitmap?>()
        var display: VirtualDisplay? = null
        projection.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { result.complete(null) }
        }, handler)
        reader.setOnImageAvailableListener({ imageReader ->
            val image = imageReader.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                val plane = image.planes[0]
                val rowPadding = plane.rowStride - plane.pixelStride * width
                val padded = Bitmap.createBitmap(width + rowPadding / plane.pixelStride, height, Bitmap.Config.ARGB_8888)
                padded.copyPixelsFromBuffer(plane.buffer)
                val exact = Bitmap.createBitmap(padded, 0, 0, width, height)
                if (exact !== padded) padded.recycle()
                result.complete(exact)
            } finally {
                image.close()
            }
        }, handler)
        display = projection.createVirtualDisplay(
            "heybuddy-frame", width, height, densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader.surface, null, handler,
        )
        val bitmap = withTimeoutOrNull(timeoutMs) { result.await() }
        display?.release()
        reader.close()
        projection.stop()
        return bitmap
    }
}
