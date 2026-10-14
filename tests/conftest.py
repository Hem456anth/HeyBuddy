"""Test bootstrap.

On Windows the real `src.utils.win32` is used. Everywhere else (Linux CI,
dev sandboxes) a small fake with the same surface is installed so the pure
logic - parsing, coordinate math, motion, the state machine - is testable
without a Windows desktop. The Windows-only behaviour is covered by the
`--selfcheck` run on a real Windows runner.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HEYBUDDY_HOME", tempfile.mkdtemp(prefix="heybuddy-tests-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The mock Worker listens on 127.0.0.1; never route that through a proxy.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

if sys.platform != "win32":
    from tests import fake_win32
    sys.modules["src.utils.win32"] = fake_win32
