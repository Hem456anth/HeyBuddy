package com.heybuddy.core

import com.heybuddy.core.jobs.JobRunner
import com.heybuddy.core.jobs.JobStore
import com.heybuddy.core.jobs.JobText
import com.heybuddy.core.memory.MemoryStore
import com.heybuddy.core.tools.ApprovalPolicy
import com.heybuddy.core.tools.ToolContext
import com.heybuddy.core.backend.ToolLoop
import com.heybuddy.core.backend.BackendClient
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class MemoryAndJobsTest {
    @get:Rule val temp = TemporaryFolder()

    @Test fun memoryIsDatedDedupedAndEditable() {
        val store = MemoryStore(File(temp.root, "memory.md")) { "2026-10-25" }
        assertTrue(store.add("Likes  dark   mode"))
        assertTrue(store.add("likes dark mode"))
        assertEquals(listOf("(2026-10-25) Likes dark mode"), store.facts())
        assertFalse(store.add("api key: sk-abcdefghijklmnop"))
        assertFalse(store.add("card 4111 1111 1111 1111"))
        val rejected = store.replaceAll("# header\n- Name is Sam\n- token = abcdefgh\nnot a bullet\n")
        assertEquals(listOf("Name is Sam"), store.facts())
        assertEquals(1, rejected.size)
        assertTrue(store.promptBlock().contains("- Name is Sam"))
        store.clear()
        assertEquals("", store.promptBlock())
    }

    @Test fun artifactsBlockParsing() {
        val folder = temp.newFolder("job")
        File(folder, "output").mkdirs()
        File(folder, "output/plan.md").writeText("x")
        val reply = "done, it's in output/plan.md\n\nARTIFACTS:\n- output/plan.md\n- output/missing.md\n"
        assertEquals(listOf(File(folder, "output/plan.md").path), JobText.parseArtifacts(reply, folder))
        assertEquals("done, it's in output/plan.md", JobText.stripArtifacts(reply))
        assertEquals("trip-to-goa-2026", JobText.slugify("Trip to Goa 2026!"))
        assertEquals("job", JobText.slugify("!!!"))
    }

    @Test fun jobRunsToolLoopAndPersists() = runBlocking {
        val worker = MockBackend()
        worker.chatReplies += MockBackend.sseToolCall("writing it", "t1", "write_file", "{\"path\":\"output/notes.md\",\"content\":\"# Notes\"}")
        worker.chatReplies += MockBackend.sseToolCall("", "t2", "report_progress", "{\"message\":\"half way\"}")
        worker.chatReplies += MockBackend.sseText("Done, notes are ready.\n\nARTIFACTS:\noutput/notes.md")
        val jobsDir = File(temp.root, "jobs")
        val index = File(temp.root, "jobs.json")
        val store = JobStore(jobsDir, index)
        val job = store.create("Notes", "write notes")
        assertTrue(File(job.folder, "AGENTS.md").readText().contains("write notes"))
        val runner = JobRunner(ToolLoop(BackendClient(worker.url, worker.token)), store, "model-x")
        val finished = runner.run(job) { j, progress ->
            ToolContext(File(j.folder), temp.root, FakePlatform(), ApprovalPolicy(), null, allowBackgroundJobs = false, reportProgress = progress)
        }
        assertEquals("done", finished.status)
        assertEquals("Done, notes are ready.", finished.reply)
        assertEquals(listOf(File(job.folder, "output/notes.md").path), finished.artifacts)
        assertEquals(listOf("half way"), finished.progress)
        // survives a restart; an active job at restart becomes failed
        val reloaded = JobStore(jobsDir, index)
        assertEquals("done", reloaded.get(job.id)!!.status)
        val running = reloaded.create("Other", "x").also { it.status = "running"; reloaded.update(it) }
        assertEquals("failed", JobStore(jobsDir, index).get(running.id)!!.status)
        worker.shutdown()
    }
}
