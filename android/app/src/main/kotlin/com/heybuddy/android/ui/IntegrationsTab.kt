package com.heybuddy.android.ui

import android.content.Context
import android.net.Uri
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.heybuddy.android.AppGraph
import com.heybuddy.android.settings.AppSettings
import com.heybuddy.android.settings.SecretStore
import com.heybuddy.core.backend.ActionLogEntry
import com.heybuddy.core.backend.BackendClient
import com.heybuddy.core.backend.IntegrationsState
import kotlinx.coroutines.launch
import java.text.DateFormat
import java.util.Date

/**
 * Connected apps (Composio, through the HeyBuddy server). Connecting opens
 * the provider's OAuth page in a Custom Tab; the server keeps only the
 * connected-account id. The action log lists what HeyBuddy did (redacted)
 * and can be cleared.
 */
@Composable
fun IntegrationsTab(graph: AppGraph, settings: AppSettings) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var state by remember { mutableStateOf<IntegrationsState?>(null) }
    var logs by remember { mutableStateOf<List<ActionLogEntry>>(emptyList()) }
    var message by remember { mutableStateOf("") }
    var refresh by remember { mutableIntStateOf(0) }
    RefreshOnResume { refresh++ }

    fun client(): BackendClient? {
        val token = graph.secrets.get(SecretStore.DEVICE_TOKEN)
        return if (settings.serverUrl.isBlank() || token.isBlank()) null else BackendClient(settings.serverUrl, token)
    }

    LaunchedEffect(refresh, settings.serverUrl) {
        val backend = client() ?: run { message = "Connect this phone to your HeyBuddy server first (Settings)."; return@LaunchedEffect }
        try {
            state = backend.integrations()
            logs = backend.actionLogs()
            message = ""
        } catch (error: Exception) {
            message = "Couldn't load connected apps: ${error.message}"
        }
    }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).testTag("integrations_tab")) {
        Text("Connected apps", fontSize = 22.sp, fontWeight = FontWeight.Bold, color = Color.White)
        Text("Let HeyBuddy use apps like your email or calendar. Reading can be automatic (Settings); sending, posting, deleting, paying or changing anything always shows you the action first.",
            color = Palette.muted, fontSize = 14.sp)
        if (!settings.integrationsEnabled) Text("Connected apps are turned off in Settings.", color = Palette.warn, fontSize = 13.sp)
        if (message.isNotEmpty()) Text(message, color = Palette.warn, fontSize = 13.sp, modifier = Modifier.padding(top = 8.dp))
        val current = state
        if (current != null && !current.enabled) {
            Text("Your server doesn't have integrations set up (COMPOSIO_API_KEY and COMPOSIO_AUTH_CONFIGS).", color = Palette.muted, fontSize = 13.sp)
        }
        if (current != null && current.enabled) {
            Spacer(Modifier.height(12.dp))
            for (toolkit in current.toolkits) {
                val connection = current.connections.lastOrNull { it.toolkit == toolkit }
                Row(Modifier.fillMaxWidth().padding(vertical = 6.dp).clip(RoundedCornerShape(14.dp)).background(Palette.card).padding(12.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(toolkit.replaceFirstChar { it.uppercase() }, color = Color.White, fontWeight = FontWeight.SemiBold)
                        Text(connection?.status?.lowercase()?.replace('_', ' ') ?: "not connected", color = Palette.muted, fontSize = 13.sp)
                    }
                    if (connection == null || connection.status !in setOf("ACTIVE")) {
                        Button(onClick = {
                            scope.launch {
                                try {
                                    val link = client()?.connect(toolkit) ?: return@launch
                                    openCustomTab(context, link)
                                    message = "Finish signing in, then come back here."
                                } catch (error: Exception) { message = "Couldn't start connecting: ${error.message}" }
                            }
                        }, modifier = Modifier.testTag("connect_$toolkit")) { Text(if (connection == null) "Connect" else "Retry") }
                    }
                    if (connection != null) {
                        TextButton(onClick = {
                            scope.launch {
                                try { client()?.disconnect(connection.id); refresh++ } catch (error: Exception) { message = "Couldn't disconnect: ${error.message}" }
                            }
                        }) { Text("Disconnect") }
                    }
                }
            }
        }

        Spacer(Modifier.height(18.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("What HeyBuddy did", color = Color.White, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            if (logs.isNotEmpty()) TextButton(onClick = {
                scope.launch { try { client()?.deleteLogs(); logs = emptyList() } catch (error: Exception) { message = "Couldn't clear: ${error.message}" } }
            }, modifier = Modifier.testTag("clear_logs")) { Text("Clear all") }
        }
        if (logs.isEmpty()) Text("Nothing yet.", color = Palette.muted, fontSize = 13.sp)
        for (entry in logs) {
            Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(entry.summary, color = if (entry.status == "ok") Color.White else Palette.warn, fontSize = 14.sp)
                    Text("${entry.risk} · ${DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT).format(Date((entry.createdAt * 1000).toLong()))}",
                        color = Palette.muted, fontSize = 12.sp)
                }
                TextButton(onClick = {
                    scope.launch { try { client()?.deleteLogs(entry.id); logs = logs - entry } catch (_: Exception) {} }
                }) { Text("Delete") }
            }
        }
    }
}

/** OAuth in a Custom Tab (never a WebView: the app can't see or capture the sign-in). */
fun openCustomTab(context: Context, url: String) {
    val uri = Uri.parse(url)
    if (uri.scheme != "https") return
    CustomTabsIntent.Builder().setShowTitle(true).build().apply {
        intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
    }.launchUrl(context, uri)
}
