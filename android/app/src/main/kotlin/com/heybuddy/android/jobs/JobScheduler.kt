package com.heybuddy.android.jobs

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.heybuddy.android.AppGraph
import com.heybuddy.android.HeyBuddyApp
import com.heybuddy.android.R
import com.heybuddy.android.graph
import com.heybuddy.android.tools.AndroidToolPlatform
import com.heybuddy.android.ui.MainActivity
import com.heybuddy.core.jobs.Job
import com.heybuddy.core.jobs.JobRunner
import com.heybuddy.core.tools.ToolContext
import com.heybuddy.core.backend.ToolLoop
import com.heybuddy.core.backend.BackendClient
import java.io.File

/** Schedules background jobs with WorkManager (they survive the app going to the background). */
class JobScheduler(private val context: Context, private val graph: AppGraph) {

    fun start(title: String, brief: String): String {
        val job = graph.jobStore.create(title, brief)
        val request = OneTimeWorkRequestBuilder<JobWorker>()
            .setInputData(workDataOf(JobWorker.KEY_JOB_ID to job.id))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .addTag(TAG)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(uniqueName(job.id), ExistingWorkPolicy.KEEP, request)
        return job.id
    }

    fun cancel(jobId: String) {
        WorkManager.getInstance(context).cancelUniqueWork(uniqueName(jobId))
        graph.jobStore.get(jobId)?.takeIf { it.status == "queued" }?.let {
            it.status = "cancelled"; graph.jobStore.update(it)
        }
    }

    companion object {
        const val TAG = "heybuddy-job"
        fun uniqueName(jobId: String) = "job-$jobId"
    }
}

class JobWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val graph = applicationContext.graph
        val jobId = inputData.getString(KEY_JOB_ID) ?: return Result.failure()
        val job = graph.jobStore.get(jobId) ?: return Result.failure()
        if (job.status == "cancelled") return Result.success()
        val settings = graph.settings.current()
        val token = graph.secrets.get(com.heybuddy.android.settings.SecretStore.DEVICE_TOKEN)
        if (settings.serverUrl.isBlank() || token.isBlank()) {
            job.status = "failed"; job.error = "this phone isn't connected to a HeyBuddy server"; graph.jobStore.update(job)
            notifyFinished(job)
            return Result.failure()
        }
        val backend = BackendClient(settings.serverUrl, token)
        val runner = JobRunner(ToolLoop(backend), graph.jobStore, settings.model.ifBlank { null })
        val platform = AndroidToolPlatform(applicationContext)
        val finished = runner.run(job) { running, progress ->
            ToolContext(
                workingDir = File(running.folder),
                appFilesRoot = applicationContext.filesDir,
                platform = platform,
                policy = settings.policy,
                memory = if (settings.memoryEnabled) graph.memory else null,
                allowBackgroundJobs = false,
                reportProgress = progress,
            )
        }
        notifyFinished(finished)
        if (finished.status == "done" && settings.speakJobResults) graph.turns.announceJob(finished)
        return Result.success()
    }

    private fun notifyFinished(job: Job) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(applicationContext, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) return
        val open = PendingIntent.getActivity(
            applicationContext, job.id.hashCode(),
            Intent(applicationContext, MainActivity::class.java).putExtra(MainActivity.EXTRA_TAB, "jobs"),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification = NotificationCompat.Builder(applicationContext, HeyBuddyApp.CHANNEL_JOBS)
            .setSmallIcon(R.drawable.ic_buddy)
            .setContentTitle(if (job.status == "done") "${job.title} is ready" else "${job.title} didn't finish")
            .setContentText(job.summaryLine)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        try { NotificationManagerCompat.from(applicationContext).notify(job.id.hashCode(), notification) } catch (_: SecurityException) {}
    }

    companion object { const val KEY_JOB_ID = "job_id" }
}
