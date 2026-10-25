package com.heybuddy.android.ui

import android.graphics.Rect
import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.heybuddy.android.AppGraph
import com.heybuddy.android.companion.Phase
import com.heybuddy.android.overlay.OverlayController

/**
 * The companion sheet (HeyBuddy's own design): a dark rounded card at the
 * bottom with a status strip, what was heard, the reply as it streams, and
 * a big talk button. Shared by the assistant session and CompanionActivity.
 */
@Composable
fun CompanionSheet(
    graph: AppGraph,
    onClose: () -> Unit,
    onSheetBounds: (Rect) -> Unit = {},
    startInTypeMode: Boolean = false,
    fillScreen: Boolean = true,
) {
    val state by graph.turns.state.collectAsState()
    val pending by graph.approvals.pending.collectAsState()
    var typing by remember(startInTypeMode) { mutableStateOf(startInTypeMode) }
    var typed by remember { mutableStateOf("") }

    Box(if (fillScreen) Modifier.fillMaxSize() else Modifier.fillMaxWidth(), contentAlignment = Alignment.BottomCenter) {
        Column(
            Modifier
                .fillMaxWidth()
                .navigationBarsPadding()
                .imePadding()
                .padding(10.dp)
                .clip(RoundedCornerShape(28.dp))
                .background(Palette.sheet)
                .onGloballyPositioned { coordinates ->
                    val b = coordinates.boundsInWindow()
                    onSheetBounds(Rect(b.left.toInt(), b.top.toInt(), b.right.toInt(), b.bottom.toInt()))
                }
                .padding(18.dp)
                .animateContentSize()
                .testTag("companion_sheet"),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(state.phase)
                Spacer(Modifier.width(10.dp))
                Text(
                    statusLabel(state.phase, state.toolRunning, state.hasScreenshot),
                    color = Palette.muted, fontSize = 13.sp, modifier = Modifier.weight(1f).testTag("companion_status"),
                )
                if (state.taskTitle.isNotEmpty()) {
                    Text(state.taskTitle, color = Palette.accent, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Spacer(Modifier.width(8.dp))
                }
                val muted by graph.settings.flow.collectAsState(initial = null)
                val isMuted = muted?.muted == true
                TextButton(onClick = { graph.turns.setMuted(!isMuted) }, modifier = Modifier.testTag("mute_button")) {
                    Text(if (isMuted) "Unmute" else "Mute", color = Palette.muted)
                }
                TextButton(onClick = { graph.turns.cancel(); onClose() }) { Text("Close", color = Palette.muted) }
            }
            if (state.heard.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Text("“${state.heard}”", color = Palette.muted, fontSize = 15.sp, maxLines = 3, overflow = TextOverflow.Ellipsis)
            }
            if (state.reply.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Column(Modifier.heightIn(max = 180.dp).verticalScroll(rememberScrollState())) {
                    Text(state.reply, color = Color.White, fontSize = 18.sp, lineHeight = 24.sp, modifier = Modifier.testTag("companion_reply"))
                }
            }
            if (state.error.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Text(state.error, color = Palette.warn, fontSize = 14.sp, modifier = Modifier.testTag("companion_error"))
                if (state.phase == Phase.ERROR || state.phase == Phase.IDLE) {
                    TextButton(onClick = { graph.turns.dismissError(); graph.turns.retry() }, modifier = Modifier.testTag("retry_button")) { Text("Try again") }
                }
            }
            val overlayMissing = graph.overlay.bestAvailableMode() == OverlayController.Mode.NONE
            if (state.preview != null && state.previewMarks.isNotEmpty() && overlayMissing) {
                Spacer(Modifier.height(10.dp))
                InkPreview(state)
            }
            if (state.phase == Phase.WAITING_FOR_TAP) {
                Spacer(Modifier.height(10.dp))
                Text("Tap “${state.targetLabel}” and I'll take it from there.", color = Palette.accent, fontSize = 14.sp)
                TextButton(onClick = { graph.turns.continueAfterTarget() }) { Text("I did it, next step") }
            }
            Spacer(Modifier.height(14.dp))
            if (typing) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(
                        value = typed, onValueChange = { typed = it }, modifier = Modifier.weight(1f).testTag("companion_input"),
                        placeholder = { Text("Ask about your screen...") }, singleLine = false, maxLines = 4,
                    )
                    Spacer(Modifier.width(8.dp))
                    Button(onClick = { graph.turns.submitText(typed); typed = "" }, enabled = typed.isNotBlank()) { Text("Send") }
                }
                TextButton(onClick = { typing = false }) { Text("Use voice", color = Palette.muted) }
            } else {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth()) {
                    TextButton(onClick = { typing = true }) { Text("Type", color = Palette.muted) }
                    TalkButton(state.phase, state.level,
                        onTap = {
                            when (state.phase) {
                                Phase.LISTENING -> graph.turns.finishListening()
                                Phase.THINKING -> graph.turns.cancel()
                                // Barge-in: talking over HeyBuddy stops its voice and listens right away.
                                Phase.SPEAKING, Phase.WAITING_FOR_TAP -> graph.turns.bargeIn()
                                else -> graph.turns.startListening(emptyList())
                            }
                        },
                    )
                    Spacer(Modifier.width(56.dp))
                }
            }
        }
    }

    pending?.let { request ->
        AlertDialog(
            onDismissRequest = { graph.approvals.answer(request.id, false) },
            title = { Text(request.title) },
            text = { Text(request.detail, maxLines = 12, overflow = TextOverflow.Ellipsis) },
            confirmButton = { Button(onClick = { graph.approvals.answer(request.id, true) }, modifier = Modifier.testTag("approve_allow")) { Text("Allow") } },
            dismissButton = { TextButton(onClick = { graph.approvals.answer(request.id, false) }, modifier = Modifier.testTag("approve_deny")) { Text("Don't allow") } },
        )
    }
}

private fun statusLabel(phase: Phase, tool: String, hasScreenshot: Boolean): String = when (phase) {
    Phase.IDLE -> if (hasScreenshot) "Ready (I can see your screen)" else "Ready"
    Phase.LISTENING -> "Listening... tap when you're done"
    Phase.THINKING -> if (tool.isNotEmpty()) "Working: ${tool.replace('_', ' ')}" else "Thinking"
    Phase.SPEAKING -> "Speaking"
    Phase.WAITING_FOR_TAP -> "Waiting for your tap"
    Phase.ERROR -> "Something went wrong"
}

@Composable
private fun StatusDot(phase: Phase) {
    val color = when (phase) {
        Phase.LISTENING -> Palette.listen
        Phase.THINKING -> Palette.think
        Phase.SPEAKING, Phase.WAITING_FOR_TAP -> Palette.accent
        Phase.ERROR -> Palette.warn
        Phase.IDLE -> Palette.muted
    }
    Box(Modifier.size(10.dp).clip(CircleShape).background(color))
}

@Composable
private fun TalkButton(phase: Phase, level: Float, onTap: () -> Unit) {
    val label = when (phase) {
        Phase.LISTENING -> "Done"
        Phase.THINKING -> "Stop"
        Phase.SPEAKING, Phase.WAITING_FOR_TAP -> "Talk"
        else -> "Talk"
    }
    Box(
        Modifier
            .size(84.dp)
            .clip(CircleShape)
            .background(if (phase == Phase.LISTENING) Palette.listen else Palette.accent)
            .pointerInput(phase) { detectTapGestures(onTap = { onTap() }) }
            .semantics { contentDescription = "$label button" }
            .testTag("talk_button"),
        contentAlignment = Alignment.Center,
    ) {
        if (phase == Phase.LISTENING) {
            Canvas(Modifier.size(84.dp)) {
                drawCircle(Color.White.copy(alpha = 0.35f), radius = size.minDimension / 2 * (0.55f + level * 0.45f), style = Stroke(width = 4.dp.toPx()))
            }
        }
        Text(label, color = Color.White, fontSize = 17.sp)
    }
}

/** Fallback when no overlay surface exists: draw the marks on a thumbnail of the screenshot. */
@Composable
private fun InkPreview(state: com.heybuddy.android.companion.CompanionUiState) {
    val bitmap = state.preview ?: return
    val frame = state.previewFrame ?: return
    Box(
        Modifier
            .fillMaxWidth(0.5f)
            .aspectRatio(bitmap.width.toFloat() / bitmap.height.coerceAtLeast(1))
            .clip(RoundedCornerShape(12.dp))
            .testTag("ink_preview"),
    ) {
        Image(bitmap.asImageBitmap(), contentDescription = "Your screen with HeyBuddy's marks", contentScale = ContentScale.FillBounds, modifier = Modifier.fillMaxSize())
        Canvas(Modifier.fillMaxSize()) {
            val scaleX = size.width / frame.regionWidth
            val scaleY = size.height / frame.regionHeight
            for ((mark, g) in state.previewMarks) {
                val x = (g.x * scaleX).toFloat(); val y = (g.y * scaleY).toFloat()
                when (mark.kind) {
                    "arrow" -> drawLine(Palette.accent, Offset(x, y), Offset((g.x2 * scaleX).toFloat(), (g.y2 * scaleY).toFloat()), strokeWidth = 3.dp.toPx())
                    "point", "num", "write" -> drawCircle(Palette.accent, radius = 6.dp.toPx(), center = Offset(x, y))
                    "trace" -> for (i in 2 until g.points.size step 2) {
                        drawLine(Palette.accent, Offset((g.points[i - 2] * scaleX).toFloat(), (g.points[i - 1] * scaleY).toFloat()),
                            Offset((g.points[i] * scaleX).toFloat(), (g.points[i + 1] * scaleY).toFloat()), strokeWidth = 3.dp.toPx())
                    }
                    else -> drawRect(Palette.accent, topLeft = Offset(x, y),
                        size = androidx.compose.ui.geometry.Size((g.w * scaleX).toFloat().coerceAtLeast(4f), (g.h * scaleY).toFloat().coerceAtLeast(4f)),
                        style = Stroke(width = 2.5.dp.toPx()))
                }
            }
        }
    }
}

object Palette {
    val sheet = Color(0xF2121317)
    val accent = Color(0xFF3D7BFF)
    val listen = Color(0xFFFF5A5F)
    val think = Color(0xFFFFB020)
    val warn = Color(0xFFFF8A65)
    val muted = Color(0xFF9AA0AA)
    val background = Color(0xFF101114)
    val card = Color(0xFF1A1C21)
}

@Composable
fun HeyBuddyTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = androidx.compose.material3.darkColorScheme(
            primary = Palette.accent,
            background = Palette.background,
            surface = Palette.card,
            onPrimary = Color.White,
            onBackground = Color.White,
            onSurface = Color.White,
        ),
        content = content,
    )
}
