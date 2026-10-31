"""The agent's own database. Separate from the provider's so restarts are honest."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from .domain import ErrandState

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, version INTEGER NOT NULL,
    state_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, action TEXT, provider_id TEXT, provider_name TEXT,
    slot_start TEXT, slot_end TEXT, price INTEGER, search_price INTEGER, budget INTEGER, booking_id TEXT,
    hold_id TEXT, hold_expires TEXT, fingerprint TEXT NOT NULL, status TEXT NOT NULL, prompt_text TEXT,
    proposed_turn INTEGER, decided_turn INTEGER, void_reason TEXT, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS approvals_session ON approvals (session_id, status);
CREATE TABLE IF NOT EXISTS outbox (
    idem_key TEXT PRIMARY KEY, approval_id TEXT NOT NULL, session_id TEXT, user_id TEXT, op TEXT,
    request_json TEXT, status TEXT NOT NULL, booking_id TEXT, attempts INTEGER DEFAULT 0,
    last_error TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS memory_facts (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('stated', 'inferred')),
    status TEXT NOT NULL CHECK (status IN ('active', 'pending_confirmation', 'superseded', 'forgotten')),
    confirmed_at TEXT, forgotten_at TEXT, evidence TEXT NOT NULL, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS memory_tombstones (
    user_id TEXT, key TEXT, forgotten_at TEXT, PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS boots (n INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn INTEGER, role TEXT, text TEXT,
    claims TEXT, created_at TEXT);
"""


class StaleStateError(RuntimeError):
    """Another writer saved this session first (optimistic concurrency)."""


class AgentDB:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE transaction; nested calls join the outer one."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self.conn.execute(sql, args).rowcount

    # ------------------------------------------------------------------ sessions
    def load_state(self, session_id: str, user_id: str) -> ErrandState:
        row = self.one("SELECT state_json, version FROM sessions WHERE session_id=?", (session_id,))
        if row is None:
            return ErrandState(session_id=session_id, user_id=user_id)
        state = ErrandState.from_json(row["state_json"])
        state.version = row["version"]
        return state

    def save_state(self, state: ErrandState, now: str) -> None:
        with self.tx():
            row = self.one("SELECT version FROM sessions WHERE session_id=?", (state.session_id,))
            if row is None:
                state.version = 1
                self.run("INSERT INTO sessions VALUES (?,?,?,?,?)",
                         (state.session_id, state.user_id, state.version, state.to_json(), now))
                return
            if row["version"] != state.version:
                raise StaleStateError(f"session {state.session_id}: expected v{state.version}, found v{row['version']}")
            state.version += 1
            self.run("UPDATE sessions SET version=?, state_json=?, updated_at=? WHERE session_id=? AND version=?",
                     (state.version, state.to_json(), now, state.session_id, state.version - 1))

    def sessions_with_open_writes(self) -> list[str]:
        rows = self.q("SELECT DISTINCT session_id FROM outbox WHERE status IN ('SENT', 'UNRESOLVED')")
        return [r["session_id"] for r in rows]

    def next_boot(self, now: str) -> int:
        """Counts process starts. Mixed into seeded id generation so ids never repeat after a restart."""
        with self._lock:
            return self.conn.execute("INSERT INTO boots (at) VALUES (?)", (now,)).lastrowid

    # ------------------------------------------------------------------ transcript
    def add_message(self, session_id: str, turn: int, role: str, text: str, claims: list[str], now: str) -> None:
        self.run("INSERT INTO messages (session_id, turn, role, text, claims, created_at) VALUES (?,?,?,?,?,?)",
                 (session_id, turn, role, text, ",".join(claims), now))

    def transcript(self, session_id: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))]

    def close(self) -> None:
        self.conn.close()
