package com.heybuddy.android.companion

import android.Manifest
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.heybuddy.android.HeyBuddyApp
import com.heybuddy.android.R
import com.heybuddy.android.graph
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withTimeoutOrNull
import java.util.concurrent.atomic.AtomicInteger

data class PendingApproval(val id: Int, val title: String, val detail: String)

/**
 * Approval prompts for tools. A visible HeyBuddy surface (main screen,
 * companion sheet or assistant session) renders [pending] as a dialog.
 * When none is visible, a high-priority notification with Allow / Don't
 * allow actions is posted instead. No answer in 120 s means no.
 */
class ApprovalBroker(private val context: Context) {
    private val nextId = AtomicInteger(1)
    private val _pending = MutableStateFlow<PendingApproval?>(null)
    val pending: StateFlow<PendingApproval?> = _pending
    private var answer: CompletableDeferred<Boolean>? = null
    @Volatile var visibleSurfaces: Int = 0

    suspend fun approve(title: String, detail: String): Boolean {
        val request = PendingApproval(nextId.getAndIncrement(), title, detail)
        val deferred = CompletableDeferred<Boolean>()
        synchronized(this) {
            answer?.complete(false)
            answer = deferred
            _pending.value = request
        }
        if (visibleSurfaces <= 0) postNotification(request)
        val result = withTimeoutOrNull(TIMEOUT_MS) { deferred.await() } ?: false
        synchronized(this) {
            if (_pending.value?.id == request.id) _pending.value = null
            if (answer === deferred) answer = null
        }
        NotificationManagerCompat.from(context).cancel(NOTIFICATION_ID)
        return result
    }

    fun answer(id: Int, allowed: Boolean) {
        synchronized(this) {
            if (_pending.value?.id != id) return
            answer?.complete(allowed)
        }
    }

    private fun postNotification(request: PendingApproval) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED &&
            android.os.Build.VERSION.SDK_INT >= 33
        ) return
        fun action(allowed: Boolean) = PendingIntent.getBroadcast(
            context, request.id * 2 + if (allowed) 1 else 0,
            Intent(context, ApprovalReceiver::class.java).putExtra(EXTRA_ID, request.id).putExtra(EXTRA_ALLOWED, allowed),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification = NotificationCompat.Builder(context, HeyBuddyApp.CHANNEL_APPROVALS)
            .setSmallIcon(R.drawable.ic_buddy)
            .setContentTitle(request.title)
            .setContentText(request.detail.take(200))
            .setStyle(NotificationCompat.BigTextStyle().bigText(request.detail.take(1000)))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setAutoCancel(true)
            .setTimeoutAfter(TIMEOUT_MS)
            .addAction(0, "Allow", action(true))
            .addAction(0, "Don't allow", action(false))
            .build()
        try {
            NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, notification)
        } catch (_: SecurityException) {
            // notifications denied: the request simply times out as "no"
        }
    }

    companion object {
        const val TIMEOUT_MS = 120_000L
        const val NOTIFICATION_ID = 4101
        const val EXTRA_ID = "approval_id"
        const val EXTRA_ALLOWED = "approval_allowed"
    }
}

class ApprovalReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val id = intent.getIntExtra(ApprovalBroker.EXTRA_ID, -1)
        context.graph.approvals.answer(id, intent.getBooleanExtra(ApprovalBroker.EXTRA_ALLOWED, false))
    }
}
