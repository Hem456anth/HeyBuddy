"""Tool rounds through the real proxy/client against the mock Worker, and jobs."""
import time

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.api.claude_client import ClaudeClient, run_tool_loop  # noqa: E402
from src.api.cloudflare_proxy import CloudflareProxy  # noqa: E402
from src.core.jobs import JobManager, parse_artifacts  # noqa: E402
from src.tools.selfcheck import _MockWorker, _has_tool_result  # noqa: E402


def test_tool_loop_executes_and_feeds_back_results():
    def reply(payload):
        if not _has_tool_result(payload):
            return {"text": '```buddy\nsay("checking")\n```', "tool_use": {"id": "t1", "name": "echo", "input": {"v": 41}}}
        result = payload["messages"][-1]["content"][0]
        return f'```buddy\nsay("got {result["content"]}")\n```'

    worker = _MockWorker(reply, b"")
    try:
        client = ClaudeClient(CloudflareProxy(worker.url), "test-model")
        calls, rounds = [], []
        texts = run_tool_loop(
            client, {"model": "m", "max_tokens": 10, "tools": [{"name": "echo"}],
                     "messages": [{"role": "user", "content": "hi"}]},
            execute=lambda call: (calls.append(call) or str(call.input["v"] + 1), False),
            on_round_text=lambda text, more: rounds.append(more),
        )
        assert [c.name for c in calls] == ["echo"] and calls[0].input == {"v": 41}
        assert rounds == [True, False]
        assert "got 42" in texts[-1]
        second = worker.chat_requests[1]["messages"]
        assert second[1]["role"] == "assistant" and second[1]["content"][-1]["type"] == "tool_use"
        assert second[2]["content"][0]["tool_use_id"] == "t1"
    finally:
        worker.close()


def test_background_job_writes_artifact(tmp_path):
    app = QApplication.instance() or QApplication([])

    def reply(payload):
        system = payload["system"]
        folder = system.split("your working folder: ", 1)[1].strip().splitlines()[0]
        if not _has_tool_result(payload):
            return {"tool_use": {"id": "w", "name": "write_file", "input": {"path": "output/r.txt", "content": "ok"}}}
        return f"done.\nARTIFACTS:\n{folder}/output/r.txt"

    worker = _MockWorker(reply, b"")
    try:
        client = ClaudeClient(CloudflareProxy(worker.url), "test-model")
        jobs = JobManager(client, approve=lambda *_: False, policies=lambda: ("deny", "deny"),
                          jobs_dir=tmp_path / "jobs", index_file=tmp_path / "jobs.json")
        job_id = jobs.start("Report", "write it")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and jobs.get(job_id).is_active:
            app.processEvents()
            time.sleep(0.02)
        job = jobs.get(job_id)
        assert job.status == "done", job.error
        assert job.reply == "done."
        assert job.artifacts and job.artifacts[0].endswith("r.txt")
        assert (tmp_path / "jobs.json").exists()
        reloaded = JobManager(client, approve=lambda *_: False, policies=lambda: ("deny", "deny"),
                              jobs_dir=tmp_path / "jobs", index_file=tmp_path / "jobs.json")
        assert reloaded.get(job_id).status == "done"
    finally:
        worker.close()


def test_parse_artifacts_ignores_missing(tmp_path):
    real = tmp_path / "a.pdf"
    real.write_text("x")
    assert parse_artifacts(f"done\nARTIFACTS:\n- {real}\n- {tmp_path / 'missing.pdf'}", tmp_path) == [str(real)]
    assert parse_artifacts("ARTIFACTS: none", tmp_path) == []
