"""SQLite persistence: devices (hashed tokens), integration connections, action proposals and logs.

Nothing here stores provider keys, prompts, audio or screenshots. Device tokens are stored only as
HMAC-SHA256(pepper, token). Composio is told only an opaque per-user id; we keep its connected
account ids, never OAuth tokens (those stay with Composio).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, toolkit TEXT NOT NULL,
    connected_account_id TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tool_slug TEXT NOT NULL, toolkit TEXT NOT NULL,
    args_hash TEXT NOT NULL, risk TEXT NOT NULL, created_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS action_log (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tool_slug TEXT NOT NULL, risk TEXT NOT NULL,
    status TEXT NOT NULL, summary TEXT NOT NULL, created_at REAL NOT NULL
);
"""

PROPOSAL_TTL_SECONDS = 15 * 60


def args_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Device:
    id: str
    user_id: str
    name: str


class Store:
    def __init__(self, path: str, pepper: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._pepper = (pepper or "dev-pepper").encode()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _hash_token(self, token: str) -> str:
        return hmac.new(self._pepper, token.encode(), hashlib.sha256).hexdigest()

    def _exec(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._db.execute(sql, params)
            rows = cursor.fetchall()
            self._db.commit()
            return rows

    # ----- devices -----
    def create_device(self, name: str, user_id: str | None = None) -> tuple[Device, str]:
        token = "hb_" + secrets.token_urlsafe(32)
        device = Device(id=uuid.uuid4().hex, user_id=user_id or "u_" + uuid.uuid4().hex[:16], name=name[:60] or "phone")
        self._exec("INSERT INTO devices (id, user_id, token_hash, name, created_at) VALUES (?, ?, ?, ?, ?)",
                   (device.id, device.user_id, self._hash_token(token), device.name, time.time()))
        return device, token

    def device_for_token(self, token: str) -> Device | None:
        rows = self._exec("SELECT id, user_id, name FROM devices WHERE token_hash = ? AND revoked = 0",
                          (self._hash_token(token),))
        if not rows:
            return None
        row = rows[0]
        self._exec("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time(), row["id"]))
        return Device(row["id"], row["user_id"], row["name"])

    def revoke_device(self, device_id: str) -> None:
        self._exec("UPDATE devices SET revoked = 1 WHERE id = ?", (device_id,))

    # ----- connections -----
    def add_connection(self, user_id: str, toolkit: str, connected_account_id: str) -> str:
        connection_id = uuid.uuid4().hex
        self._exec("INSERT INTO connections (id, user_id, toolkit, connected_account_id, created_at) VALUES (?,?,?,?,?)",
                   (connection_id, user_id, toolkit, connected_account_id, time.time()))
        return connection_id

    def connections(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._exec("SELECT id, toolkit, connected_account_id, created_at FROM connections WHERE user_id = ? "
                          "ORDER BY created_at", (user_id,))
        return [dict(row) for row in rows]

    def connection_for(self, user_id: str, toolkit: str) -> dict[str, Any] | None:
        rows = self._exec("SELECT id, toolkit, connected_account_id FROM connections WHERE user_id = ? AND toolkit = ? "
                          "ORDER BY created_at DESC LIMIT 1", (user_id, toolkit))
        return dict(rows[0]) if rows else None

    def remove_connection(self, user_id: str, connection_id: str) -> dict[str, Any] | None:
        rows = self._exec("SELECT id, toolkit, connected_account_id FROM connections WHERE user_id = ? AND id = ?",
                          (user_id, connection_id))
        if not rows:
            return None
        self._exec("DELETE FROM connections WHERE id = ?", (connection_id,))
        return dict(rows[0])

    # ----- proposals (model-proposed integration calls awaiting execution) -----
    def add_proposal(self, user_id: str, tool_slug: str, toolkit: str, arguments: dict[str, Any], risk: str) -> str:
        proposal_id = "p_" + uuid.uuid4().hex
        self._exec("INSERT INTO proposals (id, user_id, tool_slug, toolkit, args_hash, risk, created_at) "
                   "VALUES (?,?,?,?,?,?,?)", (proposal_id, user_id, tool_slug, toolkit, args_hash(arguments), risk, time.time()))
        return proposal_id

    def take_proposal(self, user_id: str, proposal_id: str) -> dict[str, Any] | None:
        rows = self._exec("SELECT * FROM proposals WHERE id = ? AND user_id = ? AND used = 0", (proposal_id, user_id))
        if not rows:
            return None
        row = dict(rows[0])
        if time.time() - row["created_at"] > PROPOSAL_TTL_SECONDS:
            return None
        return row

    def mark_proposal_used(self, proposal_id: str) -> None:
        self._exec("UPDATE proposals SET used = 1 WHERE id = ?", (proposal_id,))

    # ----- action log (redacted summaries only) -----
    def log_action(self, user_id: str, tool_slug: str, risk: str, status: str, summary: str) -> str:
        log_id = uuid.uuid4().hex
        self._exec("INSERT INTO action_log (id, user_id, tool_slug, risk, status, summary, created_at) VALUES (?,?,?,?,?,?,?)",
                   (log_id, user_id, tool_slug, risk, status, summary[:500], time.time()))
        return log_id

    def logs(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._exec("SELECT id, tool_slug, risk, status, summary, created_at FROM action_log WHERE user_id = ? "
                          "ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        return [dict(row) for row in rows]

    def delete_logs(self, user_id: str, log_id: str | None = None) -> int:
        if log_id:
            before = len(self._exec("SELECT id FROM action_log WHERE user_id = ? AND id = ?", (user_id, log_id)))
            self._exec("DELETE FROM action_log WHERE user_id = ? AND id = ?", (user_id, log_id))
            return before
        before = len(self._exec("SELECT id FROM action_log WHERE user_id = ?", (user_id,)))
        self._exec("DELETE FROM action_log WHERE user_id = ?", (user_id,))
        return before
