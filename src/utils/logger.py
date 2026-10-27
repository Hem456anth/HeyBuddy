"""Centralized logging configuration."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .constants import APP_NAME, LOGS_DIR

_configured = False
_latency_logger: logging.Logger | None = None


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

    # The windowed (console=False) .exe has no stdout at all.
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)
        root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOGS_DIR / "heybuddy.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def get_latency_logger() -> logging.Logger:
    """Stage timings only (see core/latency.py): `logs/latency.log`."""
    global _latency_logger
    if _latency_logger is None:
        get_logger()
        logger = logging.getLogger(f"{APP_NAME}.latency")
        logger.setLevel(logging.DEBUG)
        handler = RotatingFileHandler(
            LOGS_DIR / "latency.log", maxBytes=250_000, backupCount=2, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        _latency_logger = logger
    return _latency_logger
