package com.heybuddy.android.service

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.heybuddy.android.HeyBuddyApp
import com.heybuddy.android.R
import com.heybuddy.android.ui.CompanionActivity
import kotlinx.coroutines.CompletableDeferred

/**
 * Foreground service with three uses, each with the Android 14 type that
 * matches it, and never an always-on microphone:
 *  - `microphone` only while the user is talking in a turn;
 *  - `mediaProjection` only for the one frame of a user-approved capture;
 *  - `specialUse` for the optional "Tap to talk" companion notification.
 * When a turn ends and the companion notification is off, it stops itself.
 */
class CompanionService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_TURN -> {
                turnActive = true
                promote(if (intent.getBooleanExtra(EXTRA_MIC, false)) TYPE_MIC else TYPE_SPECIAL)
            }
            ACTION_PROJECTION -> {
                promote(TYPE_PROJECTION)
                projectionReady?.complete(true)
            }
            ACTION_COMPANION_ON -> { companionOn = true; promote(TYPE_SPECIAL) }
            ACTION_COMPANION_OFF -> { companionOn = false; maybeStop() }
            ACTION_TURN_END -> { turnActive = false; maybeStop() }
            else -> maybeStop()
        }
        return START_NOT_STICKY
    }

    private fun maybeStop() {
        if (turnActive) return
        if (companionOn) {
            promote(TYPE_SPECIAL)
        } else {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    private fun promote(kind: Int) {
        val type = when {
            Build.VERSION.SDK_INT < 29 -> 0
            kind == TYPE_MIC -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            kind == TYPE_PROJECTION -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            Build.VERSION.SDK_INT >= 34 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            else -> 0
        }
        try {
            ServiceCompat.startForeground(this, NOTIFICATION_ID, notification(kind), type)
        } catch (error: Exception) {
            // Android refused (e.g. microphone FGS from the background). The turn still runs while
            // HeyBuddy is visible; we just don't hold a foreground service.
            projectionReady?.complete(false)
            stopSelf()
        }
    }

    private fun notification(kind: Int): Notification {
        val talk = PendingIntent.getActivity(
            this, 1, CompanionActivity.talkIntent(this, source = "notification"),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val text = when (kind) {
            TYPE_MIC -> "Listening"
            TYPE_PROJECTION -> "Taking one screenshot"
            else -> if (turnActive) "Thinking" else "Tap to talk"
        }
        return NotificationCompat.Builder(this, HeyBuddyApp.CHANNEL_COMPANION)
            .setSmallIcon(R.drawable.ic_buddy)
            .setContentTitle("HeyBuddy")
            .setContentText(text)
            .setContentIntent(talk)
            .addAction(0, "Talk", talk)
            .setOngoing(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    companion object {
        const val NOTIFICATION_ID = 4001
        private const val ACTION_TURN = "turn"
        private const val ACTION_TURN_END = "turn_end"
        private const val ACTION_PROJECTION = "projection"
        private const val ACTION_COMPANION_ON = "companion_on"
        private const val ACTION_COMPANION_OFF = "companion_off"
        private const val EXTRA_MIC = "mic"
        private const val TYPE_MIC = 1
        private const val TYPE_PROJECTION = 2
        private const val TYPE_SPECIAL = 3

        @Volatile private var turnActive = false
        @Volatile private var companionOn = false
        @Volatile private var projectionReady: CompletableDeferred<Boolean>? = null

        private fun send(context: Context, action: String, extras: Intent.() -> Unit = {}) {
            val intent = Intent(context, CompanionService::class.java).setAction(action).apply(extras)
            try {
                ContextCompat.startForegroundService(context, intent)
            } catch (_: Exception) {
                // ForegroundServiceStartNotAllowedException (app in background): skip.
            }
        }

        fun startTurn(context: Context, microphone: Boolean) = send(context, ACTION_TURN) { putExtra(EXTRA_MIC, microphone) }

        fun endTurn(context: Context) {
            if (!turnActive && !companionOn) return
            turnActive = false
            try { context.startService(Intent(context, CompanionService::class.java).setAction(ACTION_TURN_END)) } catch (_: Exception) {}
        }

        /** Promote to a mediaProjection FGS (call after the user consented). Returns false if refused. */
        suspend fun startProjection(context: Context): Boolean {
            val ready = CompletableDeferred<Boolean>()
            projectionReady = ready
            send(context, ACTION_PROJECTION)
            return kotlinx.coroutines.withTimeoutOrNull(3000) { ready.await() } ?: false
        }

        fun setCompanionNotification(context: Context, on: Boolean) =
            if (on) send(context, ACTION_COMPANION_ON) else try {
                context.startService(Intent(context, CompanionService::class.java).setAction(ACTION_COMPANION_OFF)); Unit
            } catch (_: Exception) {}
    }
}
