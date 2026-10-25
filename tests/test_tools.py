"""Tool safety rules and behaviour (no Windows needed)."""
from pathlib import Path

import pytest

from src.core.memory_store import MemoryStore
from src.core.tools import ToolContext, ToolError, execute_tool, is_sensitive_path, tool_definitions, VOICE_TOOLS
from tests import fake_win32


def _ctx(tmp_path, answer=True, **kwargs):
    asked = []

    def approve(title, detail):
        asked.append(title)
        return answer

    context = ToolContext(working_dir=tmp_path, approve=approve, memory=MemoryStore(tmp_path / "memory.md"), **kwargs)
    return context, asked


def test_definitions_are_valid_anthropic_tools():
    for tool in tool_definitions(VOICE_TOOLS):
        assert set(tool) == {"name", "description", "input_schema"}
        assert tool["input_schema"]["type"] == "object"


def test_sensitive_paths_are_refused(tmp_path):
    assert is_sensitive_path(Path("C:/Users/a/.ssh/id_ed25519"))
    assert is_sensitive_path(Path("C:/Users/a/AppData/Local/Google/Chrome/User Data/Default/Login Data"))
    assert is_sensitive_path(Path("/x/project/.env"))
    assert not is_sensitive_path(Path("C:/Users/a/Documents/report.docx"))
    context, _ = _ctx(tmp_path)
    secret = tmp_path / ".env"
    secret.write_text("KEY=1")
    with pytest.raises(ToolError):
        execute_tool("read_file", {"path": str(secret)}, context)


def test_write_inside_working_dir_needs_no_approval(tmp_path):
    context, asked = _ctx(tmp_path)
    execute_tool("write_file", {"path": "out/a.txt", "content": "hi"}, context)
    assert (tmp_path / "out" / "a.txt").read_text() == "hi" and asked == []


def test_write_outside_asks_and_respects_no(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    context, asked = _ctx(tmp_path / "work", answer=False)
    (tmp_path / "work").mkdir()
    with pytest.raises(ToolError):
        execute_tool("write_file", {"path": str(outside), "content": "x"}, context)
    assert asked == ["Create a file?"] and not outside.exists()


def test_command_policy(tmp_path):
    context, asked = _ctx(tmp_path, command_policy="deny")
    with pytest.raises(ToolError):
        execute_tool("run_command", {"command": "echo hi"}, context)
    assert asked == []
    context, asked = _ctx(tmp_path, command_policy="ask", answer=True)
    output = execute_tool("run_command", {"command": "echo hi"}, context)
    assert "exit code 0" in output and "hi" in output and asked == ["Run a command?"]


def test_urls_and_private_hosts(tmp_path):
    context, _ = _ctx(tmp_path)
    with pytest.raises(ToolError):
        execute_tool("open_url", {"url": "file:///C:/Windows/System32/calc.exe"}, context)
    with pytest.raises(ToolError):
        execute_tool("web_fetch", {"url": "http://127.0.0.1:8080/admin"}, context)


def test_clipboard_insert_and_window(tmp_path):
    context, _ = _ctx(tmp_path)
    execute_tool("copy_to_clipboard", {"text": "abc"}, context)
    assert fake_win32.FAKE_CLIPBOARD[0] == "abc"
    execute_tool("insert_text", {"text": "typed"}, context)
    assert fake_win32.FAKE_PASTED[-1] == "typed"
    assert "notepad.exe" in execute_tool("get_active_window", {}, context)


def test_memory_refuses_secrets(tmp_path):
    context, _ = _ctx(tmp_path)
    assert execute_tool("remember", {"fact": "Prefers short answers"}, context) == "remembered"
    with pytest.raises(ToolError):
        execute_tool("remember", {"fact": "my password: hunter22"}, context)
    assert context.memory.facts()[0].endswith("Prefers short answers")
    assert "Prefers short answers" in context.memory.prompt_block()
