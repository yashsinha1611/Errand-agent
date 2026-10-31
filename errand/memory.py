"""Stable preferences across sessions, with provenance.

Rules enforced here, not in a prompt:
  * every fact is ``stated`` (the user said it) or ``inferred`` (the agent guessed it);
  * an inferred fact is unusable until the user confirms it;
  * forgetting writes a tombstone, which also blocks re-inference from history;
  * every write needs a ``UserTurnRef`` except inference, which only creates an
    unusable ``pending_confirmation`` row.
"""
from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock
from .domain import UserTurnRef
from .storage import AgentDB
from .util import IdFactory

LABELS = {"home_area": "Home area", "earliest_time": "Earliest time", "latest_time": "Latest time"}


def label_for(key: str) -> str:
    if key.startswith("preferred_technician:"):
        return f"Preferred {key.split(':', 1)[1].replace('_', ' ')}"
    return LABELS.get(key, key.replace("_", " ").capitalize())


@dataclass
class Fact:
    id: str
    key: str
    value: str
    source: str
    status: str
    confirmed_at: str | None
    evidence: str

    @property
    def usable(self) -> bool:
        return self.status == "active" and (self.source == "stated" or self.confirmed_at is not None)


class MemoryStore:
    def __init__(self, db: AgentDB, clock: Clock, ids: IdFactory) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    def _now(self) -> str:
        return self.clock.now().isoformat()

    def _require(self, by: UserTurnRef) -> None:
        if not isinstance(by, UserTurnRef) or not by.is_valid():
            raise PermissionError("memory writes require a user turn")

    @staticmethod
    def _fact(row) -> Fact:
        return Fact(row["id"], row["key"], row["value"], row["source"], row["status"], row["confirmed_at"],
                    row["evidence"])

    # ---------------------------------------------------------------- reads
    def usable(self, user_id: str) -> dict[str, Fact]:
        rows = self.db.q("SELECT * FROM memory_facts WHERE user_id=? AND status='active'", (user_id,))
        return {r["key"]: f for r in rows if (f := self._fact(r)).usable}

    def pending_inferred(self, user_id: str, key: str) -> Fact | None:
        row = self.db.one("SELECT * FROM memory_facts WHERE user_id=? AND key=? AND status='pending_confirmation'",
                          (user_id, key))
        return self._fact(row) if row else None

    def get(self, fact_id: str) -> Fact | None:
        row = self.db.one("SELECT * FROM memory_facts WHERE id=?", (fact_id,))
        return self._fact(row) if row else None

    def view(self, user_id: str) -> list[dict]:
        rows = self.db.q("SELECT * FROM memory_facts WHERE user_id=? AND status IN ('active','pending_confirmation') "
                         "ORDER BY created_at", (user_id,))
        return [{"id": r["id"], "key": label_for(r["key"]), "raw_key": r["key"], "value": r["value"],
                 "source": r["source"], "status": r["status"], "evidence": r["evidence"],
                 "confirmed": r["confirmed_at"] is not None} for r in rows]

    def tombstoned(self, user_id: str, key: str) -> bool:
        return self.db.one("SELECT 1 FROM memory_tombstones WHERE user_id=? AND key=?", (user_id, key)) is not None

    # ---------------------------------------------------------------- writes
    def remember(self, trace, user_id: str, key: str, value: str, by: UserTurnRef, evidence: str) -> Fact:
        self._require(by)
        now = self._now()
        fid = self.ids.new("mf")
        with self.db.tx():
            self.db.run("UPDATE memory_facts SET status='superseded', updated_at=? WHERE user_id=? AND key=? "
                        "AND status IN ('active','pending_confirmation')", (now, user_id, key))
            self.db.run("DELETE FROM memory_tombstones WHERE user_id=? AND key=?", (user_id, key))
            self.db.run("INSERT INTO memory_facts VALUES (?,?,?,?, 'stated', 'active', NULL, NULL, ?, ?, ?)",
                        (fid, user_id, key, value, evidence, now, now))
        trace.emit("memory", op="remember", key=key, source="stated", fact_id=fid, value=value, by_turn=by.turn)
        return self.get(fid)  # type: ignore[return-value]

    def infer(self, trace, user_id: str, key: str, value: str, evidence: str) -> Fact | None:
        """Create an unusable suggestion. Skipped if forgotten before or already known."""
        if self.tombstoned(user_id, key):
            return None
        exists = self.db.one("SELECT 1 FROM memory_facts WHERE user_id=? AND key=? "
                             "AND status IN ('active','pending_confirmation')", (user_id, key))
        if exists:
            return None
        fid = self.ids.new("mf")
        now = self._now()
        self.db.run("INSERT INTO memory_facts VALUES (?,?,?,?, 'inferred', 'pending_confirmation', NULL, NULL, ?, ?, ?)",
                    (fid, user_id, key, value, evidence, now, now))
        trace.emit("memory", op="infer", key=key, source="inferred", fact_id=fid, value=value)
        return self.get(fid)

    def confirm(self, trace, fact_id: str, by: UserTurnRef) -> None:
        self._require(by)
        now = self._now()
        self.db.run("UPDATE memory_facts SET status='active', confirmed_at=?, updated_at=? WHERE id=? "
                    "AND status='pending_confirmation'", (now, now, fact_id))
        f = self.get(fact_id)
        trace.emit("memory", op="confirm", key=f.key if f else None, source="inferred", fact_id=fact_id, by_turn=by.turn)

    def reject(self, trace, fact_id: str, user_id: str, by: UserTurnRef) -> None:
        f = self.get(fact_id)
        if f:
            self.forget(trace, user_id, f.key, by)

    def forget(self, trace, user_id: str, key: str, by: UserTurnRef) -> int:
        """Forget every fact for a key (or a key prefix ending in ':') and tombstone it."""
        self._require(by)
        now = self._now()
        with self.db.tx():
            if key.endswith(":"):
                keys = [r["key"] for r in self.db.q("SELECT DISTINCT key FROM memory_facts WHERE user_id=? AND key LIKE ?",
                                                    (user_id, key + "%"))]
            else:
                keys = [key]
            n = 0
            for k in keys:
                n += self.db.run("UPDATE memory_facts SET status='forgotten', forgotten_at=?, updated_at=? "
                                 "WHERE user_id=? AND key=? AND status IN ('active','pending_confirmation')",
                                 (now, now, user_id, k))
                self.db.run("INSERT OR REPLACE INTO memory_tombstones VALUES (?,?,?)", (user_id, k, now))
                trace.emit("memory", op="forget", key=k, by_turn=by.turn)
        return n
