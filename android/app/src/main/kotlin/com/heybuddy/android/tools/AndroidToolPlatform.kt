package com.heybuddy.android.tools

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.webkit.MimeTypeMap
import androidx.core.content.FileProvider
import com.heybuddy.android.a11y.BuddyAccessibilityService
import com.heybuddy.android.graph
import com.heybuddy.core.tools.ToolError
import com.heybuddy.core.tools.ToolPlatform
import com.heybuddy.core.tools.ToolSafety
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Android implementations of the tools. Every call here has already passed
 * [com.heybuddy.core.tools.ToolRunner]'s validation and approval gate.
 */
class AndroidToolPlatform(private val context: Context) : ToolPlatform {
    private val http = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS).readTimeout(20, TimeUnit.SECONDS)
        .followRedirects(true).build()

    private fun start(intent: Intent) {
        try {
            context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (_: ActivityNotFoundException) {
            throw ToolError("no app on this phone can open that")
        } catch (_: SecurityException) {
            throw ToolError("Android blocked opening that")
        }
    }

    override suspend fun openUrl(url: String): String = withContext(Dispatchers.Main) {
        start(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE))
        "opened $url"
    }

    private fun uriFor(file: File): Uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)

    private fun mimeOf(file: File): String =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(file.extension.lowercase()) ?: "text/plain"

    override suspend fun openFile(file: File): String = withContext(Dispatchers.Main) {
        val intent = Intent(Intent.ACTION_VIEW).setDataAndType(uriFor(file), mimeOf(file)).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        start(Intent.createChooser(intent, file.name))
        "opened ${file.name}"
    }

    override suspend fun share(text: String?, file: File?): String = withContext(Dispatchers.Main) {
        val send = Intent(Intent.ACTION_SEND)
        if (file != null) {
            send.type = mimeOf(file)
            send.putExtra(Intent.EXTRA_STREAM, uriFor(file))
            send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        } else {
            send.type = "text/plain"
            send.putExtra(Intent.EXTRA_TEXT, text)
        }
        start(Intent.createChooser(send, "Share"))
        "opened the share sheet"
    }

    override suspend fun copyToClipboard(text: String): String = withContext(Dispatchers.Main) {
        context.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("HeyBuddy", text))
        "copied ${text.length} characters"
    }

    override suspend fun readClipboard(): String = withContext(Dispatchers.Main) {
        // Android 10+: only readable while HeyBuddy has focus (the sheet or main screen).
        val clip = context.getSystemService(ClipboardManager::class.java).primaryClip
            ?: throw ToolError("Android only lets HeyBuddy read the clipboard while HeyBuddy is on screen, or the clipboard is empty")
        val text = (0 until clip.itemCount).joinToString("\n") { clip.getItemAt(it).coerceToText(context).toString() }
        text.ifEmpty { throw ToolError("the clipboard has no text") }
    }

    override suspend fun insertText(text: String): String = withContext(Dispatchers.Main) {
        val service = BuddyAccessibilityService.instance
            ?: throw ToolError("turn on the HeyBuddy helper in Accessibility settings so I can type for you")
        try {
            service.insertText(text)
        } catch (error: IllegalStateException) {
            throw ToolError(error.message ?: "couldn't type there")
        }
    }

    override suspend fun launchApp(nameOrPackage: String): String = withContext(Dispatchers.Main) {
        val pm = context.packageManager
        pm.getLaunchIntentForPackage(nameOrPackage)?.let { start(it); return@withContext "opened $nameOrPackage" }
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val apps = pm.queryIntentActivities(launcher, PackageManager.MATCH_ALL)
        val wanted = nameOrPackage.trim().lowercase()
        val match = apps.firstOrNull { it.loadLabel(pm).toString().lowercase() == wanted }
            ?: apps.firstOrNull { it.loadLabel(pm).toString().lowercase().startsWith(wanted) }
            ?: apps.firstOrNull { it.loadLabel(pm).toString().lowercase().contains(wanted) }
            ?: throw ToolError("I couldn't find an app called $nameOrPackage")
        val intent = pm.getLaunchIntentForPackage(match.activityInfo.packageName) ?: throw ToolError("that app can't be opened")
        start(intent)
        "opened ${match.loadLabel(pm)}"
    }

    override suspend fun fetchUrl(url: String): Pair<String, String> = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(url).header("User-Agent", "HeyBuddy-Android/0.1 (+companion)").build()
        try {
            http.newCall(request).execute().use { response ->
                // Redirects may land on a private host: refuse that too.
                if (ToolSafety.isPrivateHost(response.request.url.toString())) throw ToolError("private network addresses are not fetched")
                val source = response.body.source()
                source.request(ToolSafety.MAX_FETCH_BYTES.toLong())
                val bytes = source.buffer.readByteArray(minOf(source.buffer.size, ToolSafety.MAX_FETCH_BYTES.toLong()))
                (response.header("content-type") ?: "") to String(bytes, Charsets.UTF_8)
            }
        } catch (error: ToolError) {
            throw error
        } catch (error: Exception) {
            throw ToolError("fetch failed: ${error.message}")
        }
    }

    override suspend fun approve(title: String, detail: String): Boolean = context.graph.approvals.approve(title, detail)
}
