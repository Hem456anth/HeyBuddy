"""A small, user-visible memory file (`memory.md`).

HeyClicky keeps memory on its servers (`save_memory`, `noted(...)`).
HeyBuddy keeps it local, as dated markdown bullets the user can open,
edit or clear from Settings. Anything that looks like a secret is refused.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from ..utils.constants import MEMORY_FILE

MAX_FACTS = 200
MAX_FACT_CHARS = 300
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|pwd|api[_ -]?key|secret|token|otp|one[- ]time code|pin code|cvv|card number)\s*[:=]?\s*\S{4,}"
    r"|\b(sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{10,}"
    r"|\b(?:\d[ -]?){13,19}\b"
)


class MemoryStore:
    def __init__(self, path: Path = MEMORY_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()

    def facts(self) -> list[str]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [line[2:].strip() for line in lines if line.startswith("- ")]

    @staticmethod
    def looks_secret(fact: str) -> bool:
        return bool(_SECRET_RE.search(fact))

    def add(self, fact: str) -> bool:
        fact = " ".join((fact or "").split())[:MAX_FACT_CHARS]
        if not fact or self.looks_secret(fact):
            return False
        with self._lock:
            existing = self.facts()
            bare = [re.sub(r"^\(\d{4}-\d{2}-\d{2}\)\s*", "", item).lower() for item in existing]
            if fact.lower() in bare:
                return True
            existing.append(f"({time.strftime('%Y-%m-%d')}) {fact}")
            existing = existing[-MAX_FACTS:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            body = "# What HeyBuddy remembers about you\n\nEdit or delete lines freely.\n\n"
            body += "\n".join(f"- {item}" for item in existing) + "\n"
            self.path.write_text(body, encoding="utf-8")
        return True

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()

    def prompt_block(self) -> str:
        facts = self.facts()
        if not facts:
            return ""
        return "things the user told you to remember (use them quietly, don't recite them):\n" + "\n".join(
            f"- {fact}" for fact in facts[-60:]
        )
