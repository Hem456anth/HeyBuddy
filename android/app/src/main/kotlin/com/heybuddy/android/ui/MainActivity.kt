package com.heybuddy.android.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
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
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.heybuddy.android.AppGraph
import com.heybuddy.android.BuildConfig
import com.heybuddy.android.activation.Activation
import com.heybuddy.android.graph
import com.heybuddy.android.service.CompanionService
import com.heybuddy.android.settings.AppSettings
import com.heybuddy.android.settings.SecretStore
import com.heybuddy.android.settings.SpeechInputMode
import com.heybuddy.android.settings.VoiceMode
import com.heybuddy.core.jobs.Job
import com.heybuddy.core.tools.Decision
import com.heybuddy.core.backend.BackendUrlPolicy
import kotlinx.coroutines.launch
import java.io.File

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        val startTab = intent?.getStringExtra(EXTRA_TAB)
        setContent { HeyBuddyTheme { MainScreen(graph, startTab) } }
    }

    override fun onStart() { super.onStart(); graph.approvals.visibleSurfaces++ }
    override fun onStop() { graph.approvals.visibleSurfaces = (graph.approvals.visibleSurfaces - 1).coerceAtLeast(0); super.onStop() }

    companion object { const val EXTRA_TAB = "tab" }
}

@Composable
fun MainScreen(graph: AppGraph, startTab: String? = null) {
    val settings by graph.settings.flow.collectAsState(initial = null)
    val current = settings ?: return
    if (!current.onboardingDone) {
        OnboardingFlow(graph)
        return
    }
    val tabs = listOf("Home", "Apps", "Jobs", "Memory", "Settings")
    var selected by remember { mutableIntStateOf(tabs.indexOfFirst { it.equals(startTab, true) }.coerceAtLeast(0)) }
    Column(Modifier.fillMaxSize().background(Palette.background).safeDrawingPadding()) {
        TabRow(selectedTabIndex = selected, containerColor = Palette.background) {
            tabs.forEachIndexed { index, title ->
                Tab(selected = selected == index, onClick = { selected = index }, text = { Text(title) }, modifier = Modifier.testTag("tab_$title"))
            }
        }
        when (selected) {
            0 -> HomeTab(graph, current)
            1 -> IntegrationsTab(graph, current)
            2 -> JobsTab(graph)
            3 -> MemoryTab(graph)
            else -> SettingsTab(graph, current)
        }
    }
}

// ---------------------------------------------------------------------------
// Home: talk from inside the app + activation status
// ---------------------------------------------------------------------------

@Composable
private fun HomeTab(graph: AppGraph, settings: AppSettings) {
    val context = LocalContext.current
    var refresh by remember { mutableIntStateOf(0) }
    RefreshOnResume { refresh++ }
    val isAssistant = remember(refresh) { Activation.isAssistant(context) }
    val a11y = remember(refresh) { Activation.isAccessibilityOn(context) }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        Text("HeyBuddy", fontSize = 28.sp, fontWeight = FontWeight.Bold, color = Color.White)
        Text("Ask about anything on your screen. I can point, draw and walk you through it.", color = Palette.muted)
        Spacer(Modifier.height(16.dp))
        Button(onClick = { context.startActivity(CompanionActivity.talkIntent(context, "home")) }, modifier = Modifier.fillMaxWidth().testTag("home_talk")) {
            Text("Talk to HeyBuddy")
        }
        Spacer(Modifier.height(20.dp))
        Text("Ways to call me", fontWeight = FontWeight.SemiBold, color = Color.White)
        StatusRow("Long-press power (digital assistant)", isAssistant, "Set up") { openAssistantSetup(context) }
        StatusRow("Accessibility button & on-screen drawing", a11y, "Turn on") { context.startActivity(Activation.accessibilitySettingsIntent()) }
        StatusRow("Quick Settings tile", null, "Add tile") { Activation.requestAddTile(context) {} }
        Text("Also: the home-screen widget, and long-press the app icon for shortcuts.", color = Palette.muted, fontSize = 13.sp)
        if (!graph.secrets.has(SecretStore.DEVICE_TOKEN)) {
            Spacer(Modifier.height(16.dp))
            Text("Connect this phone to your HeyBuddy server in Settings so I can think and speak.", color = Palette.warn)
        }
    }
}

fun openAssistantSetup(context: android.content.Context) {
    context.startActivity(Activation.assistantSettingsIntent(context))
}

@Composable
private fun StatusRow(label: String, on: Boolean?, action: String, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(
            when (on) { true -> "●"; false -> "○"; null -> "·" },
            color = if (on == true) Palette.accent else Palette.muted, modifier = Modifier.width(22.dp),
        )
        Text(label, color = Color.White, modifier = Modifier.weight(1f))
        if (on != true) TextButton(onClick = onClick) { Text(action) }
    }
}

@Composable
fun RefreshOnResume(onResume: () -> Unit) {
    val owner = LocalLifecycleOwner.current
    DisposableEffect(owner) {
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_RESUME) onResume() }
        owner.lifecycle.addObserver(observer)
        onDispose { owner.lifecycle.removeObserver(observer) }
    }
}

// ---------------------------------------------------------------------------
// Onboarding: one permission at a time, each explained, all skippable
// ---------------------------------------------------------------------------

@Composable
fun OnboardingFlow(graph: AppGraph) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var step by remember { mutableIntStateOf(0) }
    var refresh by remember { mutableIntStateOf(0) }
    RefreshOnResume { refresh++ }
    var url by remember { mutableStateOf("") }
    var secret by remember { mutableStateOf("") }
    var pairError by remember { mutableStateOf("") }
    var pairing by remember { mutableStateOf(false) }
    var roleNote by remember { mutableStateOf("") }
    val micLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { step++ }
    val notifLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { step++ }
    val roleLauncher = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        if (!Activation.isAssistant(context)) {
            roleNote = "Android asks you to choose the assistant in Settings. Pick HeyBuddy under Digital assistant app."
            openAssistantSetup(context)
        }
    }

    val steps = listOf("welcome", "server", "mic", "notifications", "assistant", "accessibility", "overlay", "done")
    val name = steps[step.coerceAtMost(steps.lastIndex)]
    fun next() { step = (step + 1).coerceAtMost(steps.lastIndex) }

    Column(
        Modifier.fillMaxSize().background(Palette.background).safeDrawingPadding().padding(24.dp).verticalScroll(rememberScrollState()).testTag("onboarding_$name"),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text("Step ${step + 1} of ${steps.size}", color = Palette.muted, fontSize = 13.sp)
        when (name) {
            "welcome" -> {
                Title("Hi, I'm HeyBuddy")
                Body("Call me from anywhere, ask out loud, and I'll answer about what's on your screen. I can point at things, draw on the screen and walk you through tasks one tap at a time.")
                Body("I only listen and look when you call me. Nothing runs in the background.")
                Button(onClick = ::next, modifier = Modifier.testTag("onboarding_next")) { Text("Get started") }
            }
            "server" -> {
                Title("Connect to your HeyBuddy server")
                Body("HeyBuddy thinks and speaks through a small server you run (see the setup guide). All AI keys live on that server; this phone only gets its own device token.")
                OutlinedTextField(url, { url = it }, label = { Text("Server URL (https://...)") }, singleLine = true, modifier = Modifier.fillMaxWidth().testTag("server_url"))
                OutlinedTextField(secret, { secret = it }, label = { Text("Pairing secret") }, singleLine = true,
                    visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth().testTag("pairing_secret"))
                when (val verdict = BackendUrlPolicy.check(url)) {
                    is BackendUrlPolicy.Verdict.Warning -> Text(verdict.message, color = Palette.warn, fontSize = 13.sp)
                    is BackendUrlPolicy.Verdict.Rejected -> if (url.isNotBlank()) Text(verdict.message, color = Palette.warn, fontSize = 13.sp)
                    else -> Unit
                }
                if (pairError.isNotEmpty()) Text(pairError, color = Palette.warn, fontSize = 13.sp, modifier = Modifier.testTag("pair_error"))
                Row {
                    Button(enabled = !pairing && secret.isNotBlank() && BackendUrlPolicy.check(url) !is BackendUrlPolicy.Verdict.Rejected, onClick = {
                        pairing = true; pairError = ""
                        scope.launch {
                            val result = pairDevice(graph, url, secret)
                            pairing = false
                            if (result == null) { secret = ""; next() } else pairError = result
                        }
                    }, modifier = Modifier.testTag("pair_button")) { Text(if (pairing) "Connecting..." else "Connect") }
                    Spacer(Modifier.width(8.dp))
                    TextButton(onClick = ::next, modifier = Modifier.testTag("onboarding_skip")) { Text("Later") }
                }
            }
            "mic" -> {
                Title("Microphone")
                Body("So you can talk to me. I only use it while you're talking to me, then it's off.")
                val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
                if (granted) { Body("Already allowed."); Button(onClick = ::next) { Text("Next") } }
                else Row {
                    Button(onClick = { micLauncher.launch(Manifest.permission.RECORD_AUDIO) }, modifier = Modifier.testTag("grant_mic")) { Text("Allow microphone") }
                    Spacer(Modifier.width(8.dp)); TextButton(onClick = ::next, modifier = Modifier.testTag("onboarding_skip")) { Text("Not now") }
                }
            }
            "notifications" -> {
                Title("Notifications")
                Body("For job results, approvals when I'm not on screen, and the optional \"Tap to talk\" notification.")
                if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    Row {
                        Button(onClick = { notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) }, modifier = Modifier.testTag("grant_notifications")) { Text("Allow notifications") }
                        Spacer(Modifier.width(8.dp)); TextButton(onClick = ::next, modifier = Modifier.testTag("onboarding_skip")) { Text("Not now") }
                    }
                } else { Body("Nothing to do here."); Button(onClick = ::next) { Text("Next") } }
            }
            "assistant" -> {
                Title("Long-press power to call me")
                Body("Make HeyBuddy your digital assistant. Then, on phones that let the power button open the assistant (Pixel: Settings > System > Gestures > Press and hold power button; Samsung: Settings > Advanced features > Side button), a long press calls me with a screenshot, no extra prompt.")
                Body("Apps can't read the power button directly, so this is the official way.")
                val held = remember(refresh) { Activation.isAssistant(context) }
                if (held) Body("HeyBuddy is your assistant.") else Row {
                    Button(onClick = {
                        val request = Activation.roleRequestIntent(context)
                        if (request != null) roleLauncher.launch(request) else openAssistantSetup(context)
                    }, modifier = Modifier.testTag("request_assistant")) { Text("Set as assistant") }
                    Spacer(Modifier.width(8.dp)); TextButton(onClick = ::next, modifier = Modifier.testTag("onboarding_skip")) { Text("Skip") }
                }
                if (roleNote.isNotEmpty()) Text(roleNote, color = Palette.muted, fontSize = 13.sp)
                if (held) Button(onClick = ::next) { Text("Next") }
            }
            "accessibility" -> {
                Title("Drawing and guided taps (optional)")
                Body("Turn on the HeyBuddy helper in Accessibility to let me draw over other apps, notice when you tap what I highlighted, type text you approve, and take screenshots without asking each time. It also adds an accessibility button shortcut.")
                Body("Installed the APK yourself? Android 13+ may grey this out. Open App info, tap ⋮ > Allow restricted settings, then come back.")
                val on = remember(refresh) { Activation.isAccessibilityOn(context) }
                if (on) { Body("The helper is on."); Button(onClick = ::next) { Text("Next") } } else {
                    Row {
                        Button(onClick = { context.startActivity(Activation.accessibilitySettingsIntent()) }) { Text("Open Accessibility") }
                        Spacer(Modifier.width(8.dp)); TextButton(onClick = ::next, modifier = Modifier.testTag("onboarding_skip")) { Text("Skip") }
                    }
                    OutlinedButton(onClick = { context.startActivity(Activation.appInfoIntent(context)) }) { Text("Open App info") }
                }
            }
            "overlay" -> {
                Title("Draw without accessibility (optional)")
                Body("If you'd rather not use accessibility, \"Display over other apps\" also lets me draw on screen (taps still pass through). Without either, I show my marks on a small copy of your screen inside my card.")
                val on = remember(refresh) { graph.overlay.canDrawAppOverlay() }
                if (on) { Body("Allowed."); Button(onClick = ::next) { Text("Next") } } else Row {
                    Button(onClick = { context.startActivity(Activation.overlaySettingsIntent(context)) }) { Text("Allow") }
                    Spacer(Modifier.width(8.dp)); TextButton(onClick = ::next, modifier = Modifier.testTag("onboarding_skip")) { Text("Skip") }
                }
            }
            else -> {
                Title("All set")
                Body("Long-press power (if you set me as assistant), use the Quick Settings tile, the widget or the app shortcut, and ask away.")
                Button(onClick = { scope.launch { graph.settings.update { it.copy(onboardingDone = true) } } }, modifier = Modifier.testTag("onboarding_finish")) { Text("Done") }
            }
        }
    }
}

@Composable private fun Title(text: String) = Text(text, fontSize = 26.sp, fontWeight = FontWeight.Bold, color = Color.White)
@Composable private fun Body(text: String) = Text(text, color = Color(0xFFD0D3D9), fontSize = 16.sp, lineHeight = 22.sp)

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

@Composable
private fun JobsTab(graph: AppGraph) {
    val context = LocalContext.current
    val jobs = remember { mutableStateListOf<Job>().apply { addAll(graph.jobStore.all()) } }
    DisposableEffect(Unit) {
        val listener: (Job) -> Unit = { _ ->
            android.os.Handler(android.os.Looper.getMainLooper()).post { jobs.clear(); jobs.addAll(graph.jobStore.all()) }
        }
        graph.jobStore.addListener(listener)
        onDispose { graph.jobStore.removeListener(listener) }
    }
    if (jobs.isEmpty()) {
        Text("No jobs yet. Ask me to research something or write a document and I'll work on it here.", color = Palette.muted, modifier = Modifier.padding(16.dp).testTag("jobs_empty"))
        return
    }
    LazyColumn(Modifier.fillMaxSize().padding(12.dp)) {
        items(jobs, key = { it.id }) { job ->
            Column(Modifier.fillMaxWidth().padding(vertical = 6.dp).clip(RoundedCornerShape(16.dp)).background(Palette.card).padding(14.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(job.title, color = Color.White, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    Text(job.status, color = if (job.status == "done") Palette.accent else Palette.muted, fontSize = 13.sp)
                }
                Text(job.summaryLine, color = Palette.muted, fontSize = 14.sp)
                if (job.status == "done" && job.reply.isNotEmpty()) Text(job.reply, color = Color(0xFFD0D3D9), fontSize = 14.sp, maxLines = 8)
                for (artifact in job.artifacts) {
                    Text("📄 ${File(artifact).name}", color = Palette.accent, modifier = Modifier.padding(top = 6.dp).clickable {
                        val uri = androidx.core.content.FileProvider.getUriForFile(context, "${context.packageName}.files", File(artifact))
                        val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, context.contentResolver.getType(uri) ?: "text/plain")
                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                        runCatching { context.startActivity(Intent.createChooser(view, File(artifact).name).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
                    })
                }
                Row {
                    if (job.isActive) TextButton(onClick = { graph.jobs.cancel(job.id) }) { Text("Stop") }
                    else TextButton(onClick = { graph.jobStore.remove(job.id); jobs.remove(job) }) { Text("Remove") }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Memory editor
// ---------------------------------------------------------------------------

@Composable
private fun MemoryTab(graph: AppGraph) {
    var text by remember { mutableStateOf(graph.memory.readRaw()) }
    var note by remember { mutableStateOf("") }
    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Text("What I remember about you. Edit or delete lines freely; each line starting with \"- \" is one fact.", color = Palette.muted, fontSize = 14.sp)
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(text, { text = it }, modifier = Modifier.fillMaxWidth().weight(1f).testTag("memory_editor"))
        if (note.isNotEmpty()) Text(note, color = Palette.warn, fontSize = 13.sp)
        Row {
            Button(onClick = {
                val rejected = graph.memory.replaceAll(text)
                text = graph.memory.readRaw()
                note = if (rejected.isEmpty()) "Saved." else "Saved. ${rejected.size} line(s) looked like secrets and were left out."
            }, modifier = Modifier.testTag("memory_save")) { Text("Save") }
            Spacer(Modifier.width(8.dp))
            TextButton(onClick = { graph.memory.clear(); text = graph.memory.readRaw(); note = "Cleared." }) { Text("Clear all") }
        }
    }
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

@Composable
private fun SettingsTab(graph: AppGraph, settings: AppSettings) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var url by remember { mutableStateOf(settings.serverUrl) }
    var secret by remember { mutableStateOf("") }
    var model by remember { mutableStateOf(settings.model) }
    var saved by remember { mutableStateOf("") }
    var refresh by remember { mutableIntStateOf(0) }
    RefreshOnResume { refresh++ }
    fun set(transform: (AppSettings) -> AppSettings) = scope.launch { graph.settings.update(transform) }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Section("Server")
        val paired = remember(refresh, saved) { graph.secrets.has(SecretStore.DEVICE_TOKEN) }
        Text(if (paired) "This phone is connected to ${settings.serverUrl}" else "Not connected yet.", color = Palette.muted, fontSize = 13.sp)
        OutlinedTextField(url, { url = it }, label = { Text("Server URL") }, singleLine = true, modifier = Modifier.fillMaxWidth().testTag("settings_server_url"))
        OutlinedTextField(secret, { secret = it }, label = { Text("Pairing secret (only to connect or reconnect)") },
            singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        OutlinedTextField(model, { model = it }, label = { Text("Model (empty = server default)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        when (val verdict = BackendUrlPolicy.check(url)) {
            is BackendUrlPolicy.Verdict.Warning -> Text(verdict.message, color = Palette.warn, fontSize = 13.sp)
            is BackendUrlPolicy.Verdict.Rejected -> Text(verdict.message, color = Palette.warn, fontSize = 13.sp)
            else -> Unit
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Button(enabled = BackendUrlPolicy.check(url) !is BackendUrlPolicy.Verdict.Rejected, onClick = {
                scope.launch {
                    graph.settings.update { it.copy(model = model.trim()) }
                    saved = if (secret.isNotBlank()) (pairDevice(graph, url, secret) ?: "Connected.") else {
                        graph.settings.update { it.copy(serverUrl = BackendUrlPolicy.normalize(url)) }; "Saved."
                    }
                    secret = ""
                }
            }, modifier = Modifier.testTag("settings_save")) { Text("Save") }
            TextButton(onClick = { scope.launch { saved = unpairDevice(graph) } }) { Text("Disconnect") }
        }
        Text(saved, color = Palette.muted, fontSize = 13.sp)

        Section("Voice")
        Choice("Voice", listOf("Server voice" to VoiceMode.SERVER, "Device voice" to VoiceMode.DEVICE, "Off" to VoiceMode.OFF), settings.voiceMode) { v -> set { it.copy(voiceMode = v) } }
        Text("Server voice uses Gradium on your server if it is set up, otherwise the phone's voice.", color = Palette.muted, fontSize = 13.sp)
        Toggle("Mute", settings.muted) { v -> graph.turns.setMuted(v) }
        Text("Volume ${settings.volumePercent}%", color = Color.White)
        androidx.compose.material3.Slider(value = settings.volumePercent / 100f, onValueChange = { v -> set { it.copy(volumePercent = (v * 100).toInt()) } },
            modifier = Modifier.testTag("volume_slider"))
        Choice("Speech recognition", listOf("On this phone" to SpeechInputMode.DEVICE, "Server (if enabled)" to SpeechInputMode.SERVER), settings.speechInput) { v -> set { it.copy(speechInput = v) } }

        Section("Screen")
        Toggle("Send a screenshot with each question", settings.screenContext) { v -> set { it.copy(screenContext = v) } }
        Toggle("Without accessibility, ask to capture the screen (MediaProjection, every time)", settings.mediaProjectionFallback) { v -> set { it.copy(mediaProjectionFallback = v) } }
        Toggle("Draw on screen", settings.annotations) { v -> set { it.copy(annotations = v) } }
        Toggle("Guided steps wait for your tap", settings.guidedSteps) { v -> set { it.copy(guidedSteps = v) } }

        Section("Actions")
        Toggle("Let HeyBuddy use tools", settings.toolsEnabled) { v -> set { it.copy(toolsEnabled = v) } }
        Choice("Typing, clipboard reading, opening apps, sharing", listOf("Ask" to Decision.ASK, "Allow" to Decision.ALLOW, "Never" to Decision.DENY), settings.actionPolicy) { v -> set { it.copy(actionPolicy = v) } }
        Toggle("Always ask before anything leaves HeyBuddy", settings.alwaysAsk) { v -> set { it.copy(alwaysAsk = v) } }
        Toggle("Connected apps (integrations)", settings.integrationsEnabled) { v -> set { it.copy(integrationsEnabled = v) } }
        Toggle("Let connected apps read without asking (sending, posting, deleting, paying always ask)", settings.autoAllowIntegrationReads) { v -> set { it.copy(autoAllowIntegrationReads = v) } }
        Toggle("Background jobs", settings.backgroundJobs) { v -> set { it.copy(backgroundJobs = v) } }
        Toggle("Speak job results", settings.speakJobResults) { v -> set { it.copy(speakJobResults = v) } }
        Toggle("Memory", settings.memoryEnabled) { v -> set { it.copy(memoryEnabled = v) } }

        Section("Background")
        Toggle("\"Tap to talk\" notification", settings.companionNotification) { v ->
            set { it.copy(companionNotification = v) }
            CompanionService.setCompanionNotification(context, v)
        }
        val ignoring = remember(refresh) { Activation.isIgnoringBatteryOptimizations(context) }
        Text("Some phones (Xiaomi, Samsung, OnePlus, Huawei...) stop apps aggressively; see dontkillmyapp.com. HeyBuddy only needs this if you keep the notification on.", color = Palette.muted, fontSize = 13.sp)
        if (!ignoring) OutlinedButton(onClick = { context.startActivity(Activation.batteryOptOutIntent(context)) }) { Text("Let HeyBuddy ignore battery optimization") }

        Section("Activation")
        TextButton(onClick = { openAssistantSetup(context) }) { Text("Digital assistant settings") }
        TextButton(onClick = { context.startActivity(Activation.accessibilitySettingsIntent()) }) { Text("Accessibility settings") }
        TextButton(onClick = { context.startActivity(Activation.overlaySettingsIntent(context)) }) { Text("Display over other apps") }
        TextButton(onClick = { Activation.requestAddTile(context) {} }) { Text("Add Quick Settings tile") }
        TextButton(onClick = { set { it.copy(onboardingDone = false) } }) { Text("Run setup again") }

        Section("About")
        Text("HeyBuddy for Android ${BuildConfig.VERSION_NAME}. No analytics. Memory stays on this phone. Questions, screenshots and voice go only to your own HeyBuddy server and the AI providers it is set up with.", color = Palette.muted, fontSize = 13.sp)
        TextButton(onClick = { graph.conversation.clear(); saved = "Conversation cleared." }) { Text("Clear conversation") }
    }
}

@Composable private fun Section(title: String) {
    Spacer(Modifier.height(10.dp)); Text(title, color = Palette.accent, fontWeight = FontWeight.SemiBold); HorizontalDivider(color = Palette.card)
}

@Composable
private fun Toggle(label: String, value: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = Color.White, modifier = Modifier.weight(1f))
        Switch(checked = value, onCheckedChange = onChange)
    }
}

@Composable
private fun <T> Choice(label: String, options: List<Pair<String, T>>, value: T, onChange: (T) -> Unit) {
    Column(Modifier.padding(vertical = 4.dp)) {
        Text(label, color = Color.White)
        Row {
            for ((name, option) in options) {
                if (option == value) Button(onClick = {}) { Text(name) } else OutlinedButton(onClick = { onChange(option) }) { Text(name) }
                Spacer(Modifier.width(6.dp))
            }
        }
    }
}


/** Pair with the backend: returns null on success or a message to show. The pairing secret is not stored. */
suspend fun pairDevice(graph: AppGraph, rawUrl: String, pairingSecret: String): String? {
    val url = BackendUrlPolicy.normalize(rawUrl)
    return try {
        val existing = graph.secrets.get(SecretStore.DEVICE_TOKEN)
        val token = com.heybuddy.core.backend.BackendClient(url, existing).pair(pairingSecret.trim(), Build.MODEL ?: "Android")
        graph.secrets.put(SecretStore.DEVICE_TOKEN, token)
        graph.settings.update { it.copy(serverUrl = url) }
        null
    } catch (error: com.heybuddy.core.backend.BackendClient.BackendHttpException) {
        if (error.status == 403) "The server refused that pairing secret." else "Couldn't connect: ${error.message}"
    } catch (error: Exception) {
        "Couldn't reach $url (${error.message})"
    }
}

suspend fun unpairDevice(graph: AppGraph): String {
    val settings = graph.settings.current()
    val token = graph.secrets.get(SecretStore.DEVICE_TOKEN)
    if (token.isNotBlank()) runCatching { com.heybuddy.core.backend.BackendClient(settings.serverUrl, token).unpair() }
    graph.secrets.put(SecretStore.DEVICE_TOKEN, "")
    return "Disconnected. This phone's token was revoked."
}
