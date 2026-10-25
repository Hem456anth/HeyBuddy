package com.heybuddy.core

import com.heybuddy.core.backend.BackendClient
import com.heybuddy.core.backend.ChatPayloadBuilder
import com.heybuddy.core.backend.ToolCall
import com.heybuddy.core.tools.ApprovalPolicy
import com.heybuddy.core.tools.ToolContext
import com.heybuddy.core.turn.ActionConfirmer
import com.heybuddy.core.turn.IntegrationGate
import com.heybuddy.core.turn.TurnEngine
import com.heybuddy.core.turn.TurnListener
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Composio actions: preview + explicit confirmation for anything consequential, reads per setting. */
class IntegrationGateTest {
    @get:Rule val temp = TemporaryFolder()
    private val backend = MockBackend()
    @After fun tearDown() = backend.shutdown()

    private val args = JsonObject(mapOf("recipient_email" to JsonPrimitive("sam@example.com"), "body" to JsonPrimitive("late")))
    private fun call(risk: String) = ToolCall("c1", "cx_GMAIL_SEND_EMAIL", args, kind = "integration", risk = risk,
        proposalId = "p_1", toolkit = "gmail", title = "Send email", preview = "send email\nrecipient_email: sam@example.com")

    private class Confirmer(val answer: Boolean) : ActionConfirmer {
        val asked = ArrayList<Triple<String, String, String>>()
        override suspend fun confirm(title: String, detail: String, risk: String): Boolean { asked += Triple(title, detail, risk); return answer }
    }

    private fun gate(confirmer: ActionConfirmer, policy: ApprovalPolicy = ApprovalPolicy()) =
        IntegrationGate(BackendClient(backend.url, backend.token), policy, confirmer)

    @Test fun irreversibleActionShowsPreviewAndDeclineSendsNothing() = runBlocking {
        val confirmer = Confirmer(false)
        val outcome = gate(confirmer).run(call("irreversible"))
        assertTrue(outcome.isError)
        assertEquals("the user declined", outcome.output)
        assertTrue(confirmer.asked.single().first.contains("can't be undone"))
        assertTrue(confirmer.asked.single().second.contains("sam@example.com"))
        assertTrue(backend.bodiesFor("/v1/integrations/execute").isEmpty())
    }

    @Test fun confirmedActionExecutesWithConfirmedFlag() = runBlocking {
        val outcome = gate(Confirmer(true)).run(call("write"))
        assertFalse(outcome.isError)
        val sent = Json.parseToJsonElement(backend.bodiesFor("/v1/integrations/execute").single()).jsonObject
        assertEquals("true", sent["confirmed"].toString())
        assertEquals("p_1", sent["proposal_id"]!!.jsonPrimitive.content)
        assertEquals(args, sent["arguments"])
    }

    @Test fun readsRunWithoutAskingOnlyWhenAllowed() = runBlocking {
        val auto = Confirmer(true)
        gate(auto).run(call("read"))
        assertTrue(auto.asked.isEmpty())
        val strict = Confirmer(true)
        gate(strict, ApprovalPolicy(autoAllowIntegrationReads = false)).run(call("read"))
        assertEquals(1, strict.asked.size)
        val careful = Confirmer(true)
        gate(careful, ApprovalPolicy(alwaysAsk = true)).run(call("read"))
        assertEquals(1, careful.asked.size)
        // unknown risk is treated as consequential
        val unknown = Confirmer(true)
        gate(unknown).run(call(""))
        assertEquals(1, unknown.asked.size)
    }

    @Test fun failuresComeBackAsRecoverableErrors() = runBlocking {
        backend.executeReplies += MockResponse().setBody("{\"successful\":false,\"data\":\"\",\"error\":\"rate limited\"}")
        backend.executeReplies += MockResponse().setResponseCode(404).setBody("{\"detail\":\"this action expired or was already done; ask again\"}")
        val first = gate(Confirmer(true)).run(call("write"))
        assertTrue(first.isError && first.output.contains("rate limited") && first.output.contains("try again"))
        val second = gate(Confirmer(true)).run(call("write"))
        assertTrue(second.isError && second.output.contains("expired"))
    }

    @Test fun turnRoutesIntegrationCallsThroughTheGate() = runBlocking {
        backend.chatReplies += MockBackend.sseToolCall("```buddy\nsay(\"sending it\")\n```", "c9", "cx_GMAIL_SEND_EMAIL",
            args.toString(), kind = "integration", extra = ",\"risk\":\"irreversible\",\"proposal_id\":\"p_9\",\"toolkit\":\"gmail\",\"preview\":\"send email\"")
        backend.chatReplies += MockBackend.sseText("```buddy\nsay(\"ok, I didn't send it\")\n```")
        val confirmer = Confirmer(false)
        val client = BackendClient(backend.url, backend.token)
        val platform = FakePlatform()
        val engine = TurnEngine(client, ChatPayloadBuilder(null, ""), null, true,
            { ToolContext(temp.newFolder(), temp.root, platform, ApprovalPolicy(), null) },
            integrationGate = IntegrationGate(client, ApprovalPolicy(), confirmer))
        val result = engine.run("email sam I'm late", emptyList(), emptyList(), null, object : TurnListener {})
        assertEquals(1, confirmer.asked.size)
        assertTrue(backend.bodiesFor("/v1/integrations/execute").isEmpty())
        assertTrue(platform.actions.isEmpty())
        assertTrue(result.spokenText.contains("didn't send"))
        assertEquals("true", Json.parseToJsonElement(backend.chatBodies()[0]).jsonObject["integrations"].toString())
    }
}
