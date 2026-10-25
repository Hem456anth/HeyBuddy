"""Background jobs ("helpers"): longer work handed off from a voice turn.

Observed HeyClicky behaviour (decompiled `HeyClicky.Agents.Jobs` plus the
user's own data folder): the voice brain calls `sessions_spawn` with a
brief; the app makes `Clickys\\jobs\\<slug>-<id>\\` with `output/`, `tmp/`
and an `AGENTS.md`, runs a coding agent there (Codex or their own
harness, full access, no approvals), streams progress lines into the dock,
and when the agent finishes shows its reply plus an ARTIFACTS list and
opens the finished file.

HeyBuddy's clean-room version runs the same *shape* of job with Claude's
tool use through the user's own Worker:

* one folder per job under `%APPDATA%\\HeyBuddy\\jobs`;
* tools: PowerShell (approval-gated by Settings), file read/write (free
  inside the job folder, approval-gated outside), web fetch, progress;
* jobs.json keeps the list across restarts; cancel stops the loop;
* when done: chime, notch/panel card, first sentence spoken (optional),
  artifacts can be opened from the Jobs tab.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

from ..api.claude_client import ClaudeClient, ToolCall, run_tool_loop
from ..utils.constants import JOBS_DIR, JOBS_INDEX_FILE
from ..utils.logger import get_logger
from .tools import JOB_TOOLS, ToolContext, ToolError, execute_tool, tool_definitions

log = get_logger(__name__)

MAX_JOB_ROUNDS = 30
MAX_JOBS_KEPT = 50
_ARTIFACTS_RE = re.compile(r"(?is)ARTIFACTS:\s*(.+?)(?:\n\s*\n|\Z)")

JOB_SYSTEM_PROMPT = """you are a background helper started by heybuddy, a desktop companion on the user's windows pc. the user usually asked out loud, and heybuddy wrote you the brief. your final message is shown to them and its first sentence may be read aloud.

where you work:
- your working folder is the job's home. put deliverables in output\\ and scratch files in tmp\\. when you finish, output\\ holds only what they asked for.
- the shell is powershell on windows. paths use backslashes. ~ is $env:USERPROFILE.
- if python packages are missing, install them into tmp\\pylib (python -m pip install --target tmp\\pylib <pkg>) and run with $env:PYTHONPATH="tmp\\pylib".

how to work:
- work out what they actually want, then do it. if the result depends on something only they know, do the part that doesn't and ask one bundled set of specific questions at the end. never invent their facts or use dummy data where theirs belongs.
- take the most direct route: web_fetch for outside facts, the shell for files and conversions.
- their request is your approval for the writes they asked for. never delete, archive, overwrite things they didn't mention, send messages to other people, or spend money.
- never say something happened unless it did. never invent a fact, file, price or citation.
- check your work before saying it's done: open it, recompute numbers.
- send report_progress at real milestones, not every step.
- register every deliverable with add_artifact.

your final message: lead with the result (the answer, or "done, it's in output\\..."), then only what helps. plain, warm, no filler. end with a line "ARTIFACTS:" followed by one absolute path per line, or "ARTIFACTS: none".
"""


@dataclass
class Job:
    id: str
    title: str
    brief: str
    folder: str
    status: str = "queued"           # queued | running | done | failed | cancelled
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    progress: list[str] = field(default_factory=list)
    reply: str = ""
    artifacts: list[str] = field(default_factory=list)
    error: str = ""
    seen: bool = False

    @property
    def is_active(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def summary_line(self) -> str:
        if self.status == "done":
            return (self.reply.strip().split("\n", 1)[0])[:160]
        if self.status == "failed":
            return f"Couldn't finish: {self.error[:120]}"
        if self.status == "cancelled":
            return "Stopped."
        return self.progress[-1] if self.progress else "Working on it..."


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug or "job")[:32]


def parse_artifacts(reply: str, job_folder: Path) -> list[str]:
    match = _ARTIFACTS_RE.search(reply or "")
    if not match:
        return []
    paths = []
    for line in match.group(1).splitlines():
        candidate = line.strip().lstrip("-*• ").strip().strip("`\"'")
        if not candidate or candidate.lower() == "none":
            continue
        path = Path(candidate)
        if not path.is_absolute():
            path = job_folder / path
        if path.exists():
            paths.append(str(path))
    return paths


def strip_artifacts_block(reply: str) -> str:
    return _ARTIFACTS_RE.sub("", reply or "").strip()


class JobManager(QObject):
    job_changed = pyqtSignal(object)      # Job
    job_finished = pyqtSignal(object)     # Job (done/failed)

    def __init__(
        self,
        claude: ClaudeClient,
        approve: Callable[[str, str], bool],
        policies: Callable[[], tuple[str, str]],
        jobs_dir: Path = JOBS_DIR,
        index_file: Path = JOBS_INDEX_FILE,
    ) -> None:
        super().__init__()
        self.claude = claude
        self.approve = approve
        self.policies = policies
        self.jobs_dir = jobs_dir
        self.index_file = index_file
        self._jobs: dict[str, Job] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._load()

    # ----- persistence -----
    def _load(self) -> None:
        if not self.index_file.exists():
            return
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
            for item in raw if isinstance(raw, list) else []:
                job = Job(**{k: v for k, v in item.items() if k in Job.__dataclass_fields__})
                if job.is_active:   # the app quit while it ran
                    job.status, job.error = "failed", "HeyBuddy was closed while this ran"
                self._jobs[job.id] = job
        except Exception:
            log.exception("jobs.json unreadable; starting with an empty list")

    def _save(self) -> None:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at)[-MAX_JOBS_KEPT:]
            self.index_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self.index_file.with_suffix(".tmp")
            temp.write_text(json.dumps([asdict(j) for j in jobs], indent=2), encoding="utf-8")
            temp.replace(self.index_file)

    # ----- API -----
    def jobs(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start(self, title: str, brief: str) -> str:
        job_id = uuid.uuid4().hex[:10]
        folder = self.jobs_dir / f"{slugify(title)}-{job_id}"
        (folder / "output").mkdir(parents=True, exist_ok=True)
        (folder / "tmp").mkdir(parents=True, exist_ok=True)
        (folder / "AGENTS.md").write_text(
            f"# {title}\n\nStarted {time.strftime('%Y-%m-%d %H:%M')} by HeyBuddy.\n\n## Brief\n\n{brief}\n\n## Notes\n\n",
            encoding="utf-8",
        )
        job = Job(id=job_id, title=title.strip() or "Job", brief=brief, folder=str(folder))
        cancel_flag = threading.Event()
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_flags[job_id] = cancel_flag
        self._save()
        self.job_changed.emit(job)
        threading.Thread(target=self._run, args=(job, cancel_flag), name=f"Job-{job_id}", daemon=True).start()
        log.info("Job %s started: %s", job_id, title)
        return job_id

    def cancel(self, job_id: str) -> None:
        flag = self._cancel_flags.get(job_id)
        if flag:
            flag.set()

    def cancel_all(self) -> None:
        for flag in list(self._cancel_flags.values()):
            flag.set()

    def mark_seen(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and not job.seen:
            job.seen = True
            self._save()

    def remove(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and not job.is_active:
                del self._jobs[job_id]
        self._save()

    # ----- runner -----
    def _progress(self, job: Job, line: str) -> None:
        job.progress.append(line)
        job.progress = job.progress[-30:]
        self.job_changed.emit(job)

    def _run(self, job: Job, cancel_flag: threading.Event) -> None:
        job.status = "running"
        self.job_changed.emit(job)
        folder = Path(job.folder)
        command_policy, write_policy = self.policies()
        context = ToolContext(
            working_dir=folder,
            approve=self.approve,
            command_policy=command_policy,
            write_policy=write_policy,
            allow_background_jobs=False,
            report_progress=lambda message: self._progress(job, message),
        )

        def execute(call: ToolCall) -> tuple[str, bool]:
            if call.name not in JOB_TOOLS:
                return f"tool {call.name} isn't available to background jobs", True
            try:
                return execute_tool(call.name, call.input, context), False
            except ToolError as refusal:
                return str(refusal), True

        payload = {
            "model": self.claude.model,
            "system": JOB_SYSTEM_PROMPT + f"\nyour working folder: {folder}\n",
            "max_tokens": 8192,
            "tools": tool_definitions(JOB_TOOLS),
            "messages": [{"role": "user", "content": job.brief}],
        }
        try:
            texts = run_tool_loop(
                self.claude, payload, execute,
                should_continue=lambda: not cancel_flag.is_set(),
                max_rounds=MAX_JOB_ROUNDS,
            )
            if cancel_flag.is_set():
                job.status = "cancelled"
            else:
                final = texts[-1] if texts else ""
                artifacts = list(dict.fromkeys(context.artifacts + parse_artifacts(final, folder)))
                job.reply = strip_artifacts_block(final) or "Done."
                job.artifacts = artifacts
                job.status = "done"
        except Exception as error:
            log.exception("Job %s failed", job.id)
            job.status, job.error = "failed", str(error)
        job.finished_at = time.time()
        self._cancel_flags.pop(job.id, None)
        self._save()
        self.job_changed.emit(job)
        if job.status in ("done", "failed"):
            self.job_finished.emit(job)
        log.info("Job %s finished: %s", job.id, job.status)
