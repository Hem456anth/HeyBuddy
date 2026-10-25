package com.heybuddy.android

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import com.heybuddy.android.companion.ApprovalBroker
import com.heybuddy.android.companion.TurnController
import com.heybuddy.android.jobs.JobScheduler
import com.heybuddy.android.overlay.OverlayController
import com.heybuddy.android.settings.SecretStore
import com.heybuddy.android.settings.SettingsRepository
import com.heybuddy.core.jobs.JobStore
import com.heybuddy.core.memory.MemoryStore
import com.heybuddy.core.turn.Conversation
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import java.io.File

class HeyBuddyApp : Application() {
    lateinit var graph: AppGraph
        private set

    override fun onCreate() {
        super.onCreate()
        graph = AppGraph(this)
        createNotificationChannels()
    }

    private fun createNotificationChannels() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannels(
            listOf(
                NotificationChannel(CHANNEL_COMPANION, getString(R.string.notification_channel_companion), NotificationManager.IMPORTANCE_LOW),
                NotificationChannel(CHANNEL_JOBS, getString(R.string.notification_channel_jobs), NotificationManager.IMPORTANCE_DEFAULT),
                NotificationChannel(CHANNEL_APPROVALS, getString(R.string.notification_channel_approvals), NotificationManager.IMPORTANCE_HIGH),
            ),
        )
    }

    companion object {
        const val CHANNEL_COMPANION = "companion"
        const val CHANNEL_JOBS = "jobs"
        const val CHANNEL_APPROVALS = "approvals"
    }
}

/** Process-wide singletons (no DI framework needed at this size). */
class AppGraph(val context: Context) {
    val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val secrets = SecretStore(context)
    val settings = SettingsRepository(context)
    val memory = MemoryStore(File(context.filesDir, "memory.md"))
    val jobsDir = File(context.filesDir, "jobs")
    val workspaceDir = File(context.filesDir, "workspace").apply { mkdirs() }
    val jobStore = JobStore(jobsDir, File(context.filesDir, "jobs.json"))
    val conversation = Conversation()
    val approvals = ApprovalBroker(context)
    val overlay = OverlayController(context)
    val jobs = JobScheduler(context, this)
    val turns = TurnController(context, this)
}

val Context.graph: AppGraph get() = (applicationContext as HeyBuddyApp).graph
