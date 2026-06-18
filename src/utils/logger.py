"""Centralized logging configuration."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .constants import APP_NAME, LOGS_DIR

_configured = False


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger; configures handlers on first call."""
    global _configured
    if not _configured:
        _configure_root()
        _configured = True
    return logging.getLogger(name or APP_NAME)


def _configure_root() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger(APP_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOGS_DIR / "heyclicky.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
