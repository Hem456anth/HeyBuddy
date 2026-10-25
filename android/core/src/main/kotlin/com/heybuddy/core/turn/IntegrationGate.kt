package com.heybuddy.core.turn

import com.heybuddy.core.backend.BackendClient
import com.heybuddy.core.backend.ToolCall
import com.heybuddy.core.backend.ToolOutcome
import com.heybuddy.core.tools.ApprovalPolicy
import com.heybuddy.core.tools.Decision
import java.io.IOException

/** Asks the user to confirm an integration action; returns true only on an explicit yes. */
fun interface ActionConfirmer {
    suspend fun confirm(title: String, detail: String, risk: String): Boolean
}

/**
 * The approval gate for Composio calls the model proposed and the server
 * validated. Reads may run automatically (user setting); writes and
 * irreversible actions (send, delete, pay, post, modify) always show the
 * action preview and need an explicit yes. Results come back as untrusted
 * data for the model, never as instructions.
 */
class IntegrationGate(
    private val backend: BackendClient,
    private val policy: ApprovalPolicy,
    private val confirmer: ActionConfirmer,
) {
    suspend fun run(call: ToolCall): ToolOutcome {
        if (call.proposalId.isBlank()) return ToolOutcome("the server did not approve this action", true)
        val confirmed = when (policy.decideIntegration(call.risk)) {
            Decision.ALLOW -> false
            Decision.DENY -> return ToolOutcome("integrations are turned off in HeyBuddy settings", true)
            Decision.ASK -> {
                if (!confirmer.confirm(titleFor(call), call.preview.ifBlank { call.input.toString().take(600) }, call.risk)) {
                    return ToolOutcome("the user declined", true)
                }
                true
            }
        }
        return try {
            val result = backend.executeIntegration(call.proposalId, call.input, confirmed)
            if (result.successful) ToolOutcome(result.data.ifBlank { "done" }, false)
            else ToolOutcome("the ${call.toolkit.ifBlank { "integration" }} action failed: ${result.error ?: "unknown error"}. you may offer to try again.", true)
        } catch (error: BackendClient.BackendHttpException) {
            ToolOutcome(BackendClient.BackendHttpException.describe(error.status, error.body), true)
        } catch (error: IOException) {
            ToolOutcome("couldn't reach the HeyBuddy server (${error.message}); nothing was done. you may offer to try again.", true)
        }
    }

    companion object {
        fun titleFor(call: ToolCall): String {
            val what = call.title.ifBlank { call.name.removePrefix("cx_").replace('_', ' ').lowercase() }
            val where = call.toolkit.ifBlank { "your account" }
            return when (call.risk) {
                "irreversible" -> "$what with $where? This can't be undone."
                "read" -> "Let HeyBuddy $what from $where?"
                else -> "$what in $where?"
            }
        }
    }
}

/** Wraps outside content (screen text, documents) so the model treats it as data, not instructions. */
object Untrusted {
    fun frame(source: String, content: String, limit: Int = 6000): String {
        val safe = content.replace("</untrusted>", "</ untrusted>").let { if (it.length > limit) it.take(limit) + "\n... [cut]" else it }
        return "<untrusted source=\"$source\">\n$safe\n</untrusted>"
    }
}
