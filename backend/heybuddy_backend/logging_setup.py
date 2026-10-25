"""Minimal, redacted logging: never audio, screenshots, prompts, tokens or keys."""
from __future__ import annotations

import logging
import re

_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|authorization|x-api-key)[\"'=:\s]+)[^\s\"',}]+"),
    re.compile(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+"),
    re.compile(r"\b(?:sk|pk|ghp|gho|xox[abp]|hb)[-_][A-Za-z0-9_\-]{8,}"),
]


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = redact(message)
        record.args = ()
        return True


def configure() -> logging.Logger:
    logger = logging.getLogger("heybuddy")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


log = configure()
