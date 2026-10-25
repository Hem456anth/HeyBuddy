package com.heybuddy.core

import com.heybuddy.core.memory.MemoryStore
import com.heybuddy.core.tools.ApprovalPolicy
import com.heybuddy.core.tools.Decision
import com.heybuddy.core.tools.ToolContext
import com.heybuddy.core.tools.ToolRunner
import com.heybuddy.core.tools.ToolSafety
import com.heybuddy.core.tools.ToolSpecs
import com.heybuddy.core.backend.ToolCall
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/** Tool approval policy and safety (parity with tests/test_tools.py where Android allows). */
class ToolsTest {
    @get:Rule val temp = TemporaryFolder()

    private fun call(name: String, vararg args: Pair<String, String>) =
        ToolCall("id", name, JsonObject(args.associate { it.first to JsonPrimitive(it.second) }))

    private fun setup(policy: ApprovalPolicy = ApprovalPolicy(), answer: Boolean = true): Triple<ToolRunner, FakePlatform, ToolContext> {
        val root = temp.root
        val work = File(root, "workspace").apply { mkdirs() }
        val platform = FakePlatform(answer)
        val context = ToolContext(work, root, platform, policy, MemoryStore(File(root, "memory.md")), startJob = { title, _ -> "job-$title" })
        return Triple(ToolRunner(context), platform, context)
    }

    private val all = ToolSpecs.SPECS.keys.toList()

    @Test fun definitionsAreValidAnthropicTools() {
        for (tool in ToolSpecs.definitions(ToolSpecs.VOICE_TOOLS + ToolSpecs.JOB_TOOLS)) {
            assertEquals(setOf("type", "function"), tool.jsonObject.keys)
            val function = tool.jsonObject["function"]!!.jsonObject
            assertEquals(setOf("name", "description", "parameters"), function.keys)
            assertEquals("object", function["parameters"]!!.jsonObject["type"]!!.jsonPrimitive.content)
        }
        assertFalse("no shell on Android", ToolSpecs.SPECS.keys.any { it.contains("command") || it.contains("shell") })
    }

    @Test fun policyMatrix() {
        val default = ApprovalPolicy()
        assertEquals(Decision.ASK, default.decide("insert_text"))
        assertEquals(Decision.ASK, default.decide("read_clipboard"))
        assertEquals(Decision.ASK, default.decide("launch_app"))
        assertEquals(Decision.ASK, default.decide("share"))
        assertEquals(Decision.ALLOW, default.decide("open_url"))
        assertEquals(Decision.ALLOW, default.decide("remember"))
        assertEquals(Decision.DENY, default.decide("run_command"))
        assertEquals(Decision.DENY, ApprovalPolicy(actionPolicy = Decision.DENY).decide("insert_text"))
        assertEquals(Decision.ALLOW, ApprovalPolicy(actionPolicy = Decision.ALLOW).decide("insert_text"))
        val careful = ApprovalPolicy(actionPolicy = Decision.ALLOW, alwaysAsk = true)
        assertEquals(Decision.ASK, careful.decide("insert_text"))
        assertEquals(Decision.ASK, careful.decide("open_url"))
        assertEquals(Decision.ALLOW, careful.decide("web_fetch"))
        assertEquals(Decision.ALLOW, default.decideIntegration("read"))
        assertEquals(Decision.ASK, default.decideIntegration("write"))
        assertEquals(Decision.ASK, default.decideIntegration("irreversible"))
        assertEquals(Decision.ASK, default.decideIntegration("anything-else"))
        assertEquals(Decision.ASK, ApprovalPolicy(autoAllowIntegrationReads = false).decideIntegration("read"))
    }

    @Test fun sensitiveActionsAskAndRespectNo() = runBlocking {
        val (runner, platform, _) = setup(answer = false)
        val outcome = runner.execute(call("insert_text", "text" to "hello"), all)
        assertTrue(outcome.isError)
        assertEquals("the user declined", outcome.output)
        assertEquals(listOf("Type into the app you're using?"), platform.asked)
        assertTrue(platform.actions.isEmpty())
    }

    @Test fun approvedActionRuns() = runBlocking {
        val (runner, platform, _) = setup(answer = true)
        assertFalse(runner.execute(call("launch_app", "app" to "Calendar"), all).isError)
        assertEquals(listOf("launch:Calendar"), platform.actions)
        assertTrue(runner.execute(call("launch_app", "app" to "Missing"), all).isError)
    }

    @Test fun deniedPolicyNeverAsks() = runBlocking {
        val (runner, platform, _) = setup(ApprovalPolicy(actionPolicy = Decision.DENY))
        val outcome = runner.execute(call("read_clipboard"), all)
        assertTrue(outcome.isError)
        assertTrue(platform.asked.isEmpty())
    }

    @Test fun urlsAndPrivateHosts() = runBlocking {
        val (runner, _, _) = setup()
        assertTrue(runner.execute(call("open_url", "url" to "file:///data/data/x"), all).isError)
        assertTrue(runner.execute(call("open_url", "url" to "intent://scan/#Intent;end"), all).isError)
        assertTrue(runner.execute(call("web_fetch", "url" to "http://127.0.0.1:8080/admin"), all).isError)
        assertTrue(runner.execute(call("web_fetch", "url" to "http://192.168.0.1/"), all).isError)
        assertTrue(runner.execute(call("web_fetch", "url" to "http://172.20.0.1/"), all).isError)
        val page = runner.execute(call("web_fetch", "url" to "https://example.org"), all)
        assertFalse(page.isError)
        assertEquals("Hello\nWorld & more", page.output)
    }

    @Test fun filesStayInsideTheWorkingFolder() = runBlocking {
        val (runner, _, context) = setup()
        assertFalse(runner.execute(call("write_file", "path" to "out/a.txt", "content" to "hi"), all).isError)
        assertEquals("hi", File(context.workingDir, "out/a.txt").readText())
        assertTrue(runner.execute(call("write_file", "path" to "../escape.txt", "content" to "x"), all).isError)
        assertTrue(runner.execute(call("read_file", "path" to "/etc/hosts"), all).isError)
        assertTrue(runner.execute(call("read_file", "path" to "/data/data/com.other.app/shared_prefs/auth.json"), all).isError)
        File(context.workingDir, ".env").writeText("KEY=1")
        assertTrue(runner.execute(call("read_file", "path" to ".env"), all).isError)
        assertTrue(runner.execute(call("open_file", "path" to "/sdcard/DCIM/photo.jpg"), all).isError)
        assertFalse(runner.execute(call("open_file", "path" to "workspace/out/a.txt"), all).isError)
        assertTrue(runner.execute(call("list_files"), all).output.contains("out"))
    }

    @Test fun toolsOutsideTheAllowedListAreRefused() = runBlocking {
        val (runner, _, _) = setup()
        assertTrue(runner.execute(call("write_file", "path" to "a", "content" to "b"), ToolSpecs.VOICE_TOOLS).isError)
        assertTrue(runner.execute(call("run_command", "command" to "rm -rf /"), all).isError)
    }

    @Test fun memoryRefusesSecretsAndJobsStart() = runBlocking {
        val (runner, _, context) = setup()
        assertEquals("remembered", runner.execute(call("remember", "fact" to "Prefers short answers"), all).output)
        assertTrue(runner.execute(call("remember", "fact" to "my password: hunter22"), all).isError)
        assertTrue(context.memory!!.facts()[0].endsWith("Prefers short answers"))
        assertTrue(runner.execute(call("read_memory"), all).output.contains("Prefers short answers"))
        assertTrue(runner.execute(call("start_background_job", "title" to "Trip", "brief" to "plan"), all).output.contains("job-Trip"))
    }

    @Test fun clipboardWriteIsFreeReadAsks() = runBlocking {
        val (runner, platform, _) = setup()
        runner.execute(call("copy_to_clipboard", "text" to "abc"), all)
        assertEquals("abc", platform.clipboard)
        assertTrue(platform.asked.isEmpty())
        assertEquals("abc", runner.execute(call("read_clipboard"), all).output)
        assertEquals(listOf("Let HeyBuddy read your clipboard?"), platform.asked)
    }

    @Test fun outputIsCapped() {
        val long = "x".repeat(50_000)
        assertTrue(ToolSafety.cap(long).length < 13_000)
    }
}
