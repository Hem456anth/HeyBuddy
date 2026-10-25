"""Local tools the model may call, with HeyBuddy's own safety rules.

HeyClicky's brain can call client-side tools (open a URL, copy, insert
text, run a shell command, read/write files, remember a fact, start a
background job...). HeyClicky runs them with *no* approval step and no
allow/deny list (docs/HEYCLICKY_TEARDOWN.md, "Agents"). HeyBuddy keeps the
capability but adds guard rails:

* `run_command` and file writes outside HeyBuddy's own workspace ask the
  user first (configurable: ask / allow / deny);
* credential stores, SSH/cloud keys and browser profiles are never read
  or written;
* URLs must be http(s); output sent back to the model is size-capped.

Tool definitions use the Anthropic Messages API `tools` schema.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..utils.constants import JOBS_DIR, USER_DATA_DIR
from ..utils.logger import get_logger
from ..utils import win32
from .memory_store import MemoryStore

log = get_logger(__name__)

MAX_TOOL_OUTPUT_CHARS = 12_000
MAX_READ_BYTES = 200_000
COMMAND_TIMEOUT_SECONDS = 90
WEB_FETCH_TIMEOUT_SECONDS = 20

# Never touched, whatever the model asks (case-insensitive path fragments).
SENSITIVE_PATH_FRAGMENTS = (
    "/.ssh", "/.aws", "/.azure", "/.gnupg", "/.kube", "/.docker/config.json",
    "/.git-credentials", "/.netrc", "/.npmrc", "/.pypirc",
    "/appdata/roaming/microsoft/credentials", "/appdata/local/microsoft/credentials",
    "/appdata/roaming/microsoft/protect", "/appdata/roaming/microsoft/crypto",
    "/appdata/local/google/chrome/user data", "/appdata/local/microsoft/edge/user data",
    "/appdata/roaming/mozilla/firefox/profiles", "/appdata/local/bravesoftware",
    "/appdata/roaming/opera software",
    "/heybuddy/config/settings.json",
    "/windows/system32/config",
)
SENSITIVE_NAME_RE = re.compile(r"(^|[\\/])(\.env(\..*)?|id_rsa.*|id_ed25519.*|.*\.pem|.*\.pfx|.*\.p12|.*\.kdbx|auth\.json|auth\.bin)$", re.I)

ApprovalFn = Callable[[str, str], bool]   # (title, detail) -> approved?


@dataclass
class ToolContext:
    """What a tool call may touch. Voice turns and jobs get different ones."""
    working_dir: Path
    approve: ApprovalFn
    command_policy: str = "ask"        # ask | allow | deny
    write_policy: str = "ask"          # ask | allow | deny (outside working_dir / workspace)
    allow_background_jobs: bool = True
    start_job: Callable[[str, str], str] | None = None
    report_progress: Callable[[str], None] | None = None
    memory: MemoryStore | None = None
    artifacts: list[str] = field(default_factory=list)


class ToolError(Exception):
    pass


def _cap(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3:]
    return f"{head}\n... [{len(text) - limit} characters cut] ...\n{tail}"


def normalize_path(raw: str, base: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(raw or "").strip().strip('"')))
    path = Path(expanded)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def is_sensitive_path(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    if any(fragment in normalized for fragment in SENSITIVE_PATH_FRAGMENTS):
        return True
    return bool(SENSITIVE_NAME_RE.search(normalized))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def html_to_text(raw_html: str) -> str:
    raw_html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw_html)
    raw_html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", raw_html)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw_html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Tool definitions (schemas the model sees)
# ---------------------------------------------------------------------------

def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "open_url": {
        "description": "Open an http(s) URL in the user's default browser.",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    },
    "open_path": {
        "description": "Open an existing file or folder with its default Windows app (File Explorer for folders).",
        "input_schema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    "copy_to_clipboard": {
        "description": "Put text on the user's clipboard.",
        "input_schema": _schema({"text": {"type": "string"}}, ["text"]),
    },
    "insert_text": {
        "description": "Type text into the app the user is focused on, at their caret (pasted via the clipboard). Only when they asked you to write something there.",
        "input_schema": _schema({"text": {"type": "string"}}, ["text"]),
    },
    "get_active_window": {
        "description": "Title and process name of the window the user is working in.",
        "input_schema": _schema({}, []),
    },
    "run_command": {
        "description": "Run a PowerShell command on the user's PC and get its output. The user may be asked to approve it. Prefer read-only commands; never delete or overwrite data they didn't ask about.",
        "input_schema": _schema({
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "Working folder (optional)."},
        }, ["command"]),
    },
    "list_directory": {
        "description": "List a folder's entries (name, type, size).",
        "input_schema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    "read_file": {
        "description": "Read a UTF-8 text file (first 200 KB).",
        "input_schema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    "write_file": {
        "description": "Create or overwrite a text file. Writing outside the job folder asks the user first.",
        "input_schema": _schema({
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, ["path", "content"]),
    },
    "web_fetch": {
        "description": "Fetch a public web page and return its readable text.",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    },
    "remember": {
        "description": "Save one durable fact about the user (a preference, a name, a goal) so you know it next time. Never secrets.",
        "input_schema": _schema({"fact": {"type": "string"}}, ["fact"]),
    },
    "start_background_job": {
        "description": "Hand longer work (research, building a file, multi-step tasks over a few seconds) to a background helper. It works in its own folder and reports back when done. Write a complete brief: the goal, what to deliver, the format.",
        "input_schema": _schema({
            "title": {"type": "string", "description": "Two or three words."},
            "brief": {"type": "string"},
        }, ["title", "brief"]),
    },
    "report_progress": {
        "description": "Send the user a one-line progress update at a real milestone.",
        "input_schema": _schema({"message": {"type": "string"}}, ["message"]),
    },
    "add_artifact": {
        "description": "Register a finished deliverable file so HeyBuddy shows it to the user.",
        "input_schema": _schema({"path": {"type": "string"}}, ["path"]),
    },
}

VOICE_TOOLS = (
    "open_url", "open_path", "copy_to_clipboard", "insert_text", "get_active_window",
    "run_command", "list_directory", "read_file", "write_file", "web_fetch", "remember",
    "start_background_job",
)
JOB_TOOLS = (
    "run_command", "list_directory", "read_file", "write_file", "web_fetch", "open_url",
    "report_progress", "add_artifact",
)


def tool_definitions(names: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{"name": name, **TOOL_SPECS[name]} for name in names]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_tool(name: str, arguments: dict[str, Any], context: ToolContext) -> str:
    """Run one tool call; returns text for the model. Raises ToolError for
    a refusal or failure the model should see as `is_error`."""
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool {name}")
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    log.info("Tool %s(%s)", name, ", ".join(f"{k}={str(v)[:80]!r}" for k, v in arguments.items()))
    return _cap(handler(arguments, context))


def _require(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"missing {key}")
    return value


def _tool_open_url(arguments: dict[str, Any], context: ToolContext) -> str:
    url = _require(arguments, "url").strip()
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ToolError("only http and https URLs can be opened")
    webbrowser.open(url, new=2)
    return f"opened {url}"


def _tool_open_path(arguments: dict[str, Any], context: ToolContext) -> str:
    path = normalize_path(_require(arguments, "path"), context.working_dir)
    if not path.exists():
        raise ToolError(f"{path} does not exist")
    if is_sensitive_path(path):
        raise ToolError("that location is off limits")
    if path.is_file() and path.suffix.lower() in (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".scr", ".lnk", ".js", ".hta"):
        if not context.approve("Open a program?", f"HeyBuddy wants to launch:\n{path}"):
            raise ToolError("the user declined")
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:  # dev hosts
        webbrowser.open(path.as_uri())
    return f"opened {path}"


def _tool_copy(arguments: dict[str, Any], context: ToolContext) -> str:
    text = str(arguments.get("text", ""))
    if not win32.set_clipboard_text(text):
        raise ToolError("clipboard is busy")
    return f"copied {len(text)} characters"


def _tool_insert(arguments: dict[str, Any], context: ToolContext) -> str:
    text = str(arguments.get("text", ""))
    if not text:
        raise ToolError("nothing to insert")
    if not win32.paste_text_into_focused_window(text):
        raise ToolError("could not type into the focused window")
    return f"inserted {len(text)} characters"


def _tool_active_window(arguments: dict[str, Any], context: ToolContext) -> str:
    return json.dumps(win32.get_foreground_window_info())


def _policy_gate(policy: str, title: str, detail: str, context: ToolContext) -> None:
    if policy == "allow":
        return
    if policy == "deny":
        raise ToolError("this action is turned off in HeyBuddy settings")
    if not context.approve(title, detail):
        raise ToolError("the user declined")


def _tool_run_command(arguments: dict[str, Any], context: ToolContext) -> str:
    command = _require(arguments, "command")
    cwd = normalize_path(arguments.get("cwd") or str(context.working_dir), context.working_dir)
    if not cwd.is_dir():
        cwd = context.working_dir
    if is_sensitive_path(cwd) or any(fragment.strip("/") in command.lower().replace("\\", "/") for fragment in (".ssh", ".aws/", "credentials", "user data/")):
        raise ToolError("that location is off limits")
    _policy_gate(context.command_policy, "Run a command?", f"Folder: {cwd}\n\n{command}", context)
    if sys.platform == "win32":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
        creationflags = 0x08000000  # CREATE_NO_WINDOW
    else:
        argv = ["/bin/sh", "-c", command]
        creationflags = 0
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS,
            creationflags=creationflags, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"timed out after {COMMAND_TIMEOUT_SECONDS} s")
    output = completed.stdout.decode("utf-8", "replace")
    errors = completed.stderr.decode("utf-8", "replace")
    result = f"exit code {completed.returncode}\n{output}"
    if errors.strip():
        result += f"\n[stderr]\n{errors}"
    return result


def _tool_list_directory(arguments: dict[str, Any], context: ToolContext) -> str:
    path = normalize_path(_require(arguments, "path"), context.working_dir)
    if is_sensitive_path(path):
        raise ToolError("that location is off limits")
    if not path.is_dir():
        raise ToolError(f"{path} is not a folder")
    rows = []
    for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))[:500]:
        try:
            size = entry.stat().st_size if entry.is_file() else 0
        except OSError:
            size = 0
        rows.append(f"{'dir ' if entry.is_dir() else 'file'}\t{size}\t{entry.name}")
    return f"{path}\n" + "\n".join(rows)


def _tool_read_file(arguments: dict[str, Any], context: ToolContext) -> str:
    path = normalize_path(_require(arguments, "path"), context.working_dir)
    if is_sensitive_path(path):
        raise ToolError("that file is off limits")
    if not path.is_file():
        raise ToolError(f"{path} is not a file")
    with path.open("rb") as handle:
        data = handle.read(MAX_READ_BYTES)
    return data.decode("utf-8", "replace")


def _tool_write_file(arguments: dict[str, Any], context: ToolContext) -> str:
    path = normalize_path(_require(arguments, "path"), context.working_dir)
    content = str(arguments.get("content", ""))
    if is_sensitive_path(path):
        raise ToolError("that location is off limits")
    in_own_space = _inside(path, context.working_dir) or _inside(path, JOBS_DIR)
    if not in_own_space:
        verb = "Overwrite" if path.exists() else "Create"
        _policy_gate(context.write_policy, f"{verb} a file?", f"{path}\n({len(content)} characters)", context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} characters to {path}"


def _tool_web_fetch(arguments: dict[str, Any], context: ToolContext) -> str:
    url = _require(arguments, "url").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("only http and https URLs")
    host = (parsed.hostname or "").lower()
    if host in ("localhost",) or host.startswith(("127.", "10.", "192.168.", "169.254.")) or host.endswith(".local"):
        raise ToolError("private network addresses are not fetched")
    request = urllib.request.Request(url, headers={"User-Agent": "HeyBuddy/0.3 (+desktop companion)"})
    try:
        with urllib.request.urlopen(request, timeout=WEB_FETCH_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("content-type", "")
            body = response.read(2_000_000).decode("utf-8", "replace")
    except Exception as error:
        raise ToolError(f"fetch failed: {error}")
    return html_to_text(body) if "html" in content_type or body.lstrip().startswith("<") else body


def _tool_remember(arguments: dict[str, Any], context: ToolContext) -> str:
    fact = _require(arguments, "fact")
    if context.memory is None:
        raise ToolError("memory is turned off")
    if not context.memory.add(fact):
        raise ToolError("that looks like a secret, so it was not saved")
    return "remembered"


def _tool_start_job(arguments: dict[str, Any], context: ToolContext) -> str:
    if not context.allow_background_jobs or context.start_job is None:
        raise ToolError("background jobs are not available here")
    job_id = context.start_job(_require(arguments, "title")[:60], _require(arguments, "brief"))
    return f"started background job {job_id}; tell the user you're on it"


def _tool_report_progress(arguments: dict[str, Any], context: ToolContext) -> str:
    if context.report_progress is not None:
        context.report_progress(_require(arguments, "message")[:200])
    return "ok"


def _tool_add_artifact(arguments: dict[str, Any], context: ToolContext) -> str:
    path = normalize_path(_require(arguments, "path"), context.working_dir)
    if not path.exists():
        raise ToolError(f"{path} does not exist")
    context.artifacts.append(str(path))
    return "registered"


_HANDLERS: dict[str, Callable[[dict[str, Any], ToolContext], str]] = {
    "open_url": _tool_open_url,
    "open_path": _tool_open_path,
    "copy_to_clipboard": _tool_copy,
    "insert_text": _tool_insert,
    "get_active_window": _tool_active_window,
    "run_command": _tool_run_command,
    "list_directory": _tool_list_directory,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "web_fetch": _tool_web_fetch,
    "remember": _tool_remember,
    "start_background_job": _tool_start_job,
    "report_progress": _tool_report_progress,
    "add_artifact": _tool_add_artifact,
}


def default_working_dir() -> Path:
    workspace = USER_DATA_DIR / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace
