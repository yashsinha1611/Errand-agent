"""Approval is a database row bound to a fingerprint of exactly what will happen.

The executor can only write after ``consume`` succeeds: a compare-and-set from
``granted`` to ``consumed`` on a matching fingerprint, in the same transaction that
inserts the outbox row. Nothing a model says, and nothing a tool returns, can create
or grant an approval: ``grant`` requires a ``UserTurnRef``.
"""
from __future__ import annotations

from datetime import timedelta

from .clock import Clock
from .domain import Approval, UserTurnRef
from .storage import AgentDB
from .util import IdFactory, canonical_json, parse_iso, sha256


class ApprovalRequired(Exception):
    """The planned write has no granted approval with a matching fingerprint."""


def fingerprint(action: str, provider_id: str, slot_start: str, slot_end: str, price: int,
                booking_id: str | None) -> str:
    """Hash of everything the user agreed to. hold_id is deliberately excluded: a
    re-hold of the same slot at the same price is the same agreement."""
    return sha256(canonical_json({
        "action": action, "provider_id": provider_id, "slot_start": slot_start, "slot_end": slot_end,
        "price": int(price), "booking_id": booking_id,
    }))


COLUMNS = ["id", "user_id", "session_id", "action", "provider_id", "provider_name", "slot_start", "slot_end",
           "price", "search_price", "budget", "booking_id", "hold_id", "hold_expires", "fingerprint", "status",
           "prompt_text", "proposed_turn", "decided_turn", "void_reason", "created_at", "updated_at"]


def _row_to_approval(row) -> Approval:
    return Approval(**{k: row[k] for k in COLUMNS})


class ApprovalGate:
    def __init__(self, db: AgentDB, clock: Clock, ids: IdFactory, ttl_min: int = 15) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids
        self.ttl = timedelta(minutes=ttl_min)

    # ---------------------------------------------------------------- reads
    def get(self, approval_id: str) -> Approval | None:
        row = self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return _row_to_approval(row) if row else None

    def pending(self, session_id: str) -> Approval | None:
        row = self.db.one("SELECT * FROM approvals WHERE session_id=? AND status='pending' "
                          "ORDER BY created_at DESC LIMIT 1", (session_id,))
        return _row_to_approval(row) if row else None

    def latest_consumed(self, session_id: str) -> Approval | None:
        row = self.db.one("SELECT * FROM approvals WHERE session_id=? AND status='consumed' "
                          "ORDER BY updated_at DESC LIMIT 1", (session_id,))
        return _row_to_approval(row) if row else None

    # ---------------------------------------------------------------- writes
    def propose(self, trace, *, user_id: str, session_id: str, action: str, provider_id: str, provider_name: str,
                slot_start: str, slot_end: str, price: int, turn: int, search_price: int | None = None,
                budget: int | None = None, booking_id: str | None = None, hold_id: str | None = None,
                hold_expires: str | None = None, prompt_text: str = "") -> Approval:
        now = self.clock.now().isoformat()
        fp = fingerprint(action, provider_id, slot_start, slot_end, price, booking_id)
        appr = Approval(id=self.ids.new("ap"), user_id=user_id, session_id=session_id, action=action,
                        provider_id=provider_id, provider_name=provider_name, slot_start=slot_start,
                        slot_end=slot_end, price=int(price), fingerprint=fp, status="pending",
                        search_price=search_price, budget=budget, booking_id=booking_id, hold_id=hold_id,
                        hold_expires=hold_expires, prompt_text=prompt_text, proposed_turn=turn,
                        created_at=now, updated_at=now)
        with self.db.tx():
            for row in self.db.q("SELECT id FROM approvals WHERE session_id=? AND status IN ('pending','granted')",
                                 (session_id,)):
                self._set_status(trace, row["id"], "void", "superseded by a newer proposal")
            self.db.run(f"INSERT INTO approvals ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                        tuple(getattr(appr, k) for k in COLUMNS))
        trace.emit("approval", op="propose", id=appr.id, fingerprint=fp[:12], action=action, price=appr.price)
        return appr

    def set_prompt(self, approval_id: str, text: str) -> None:
        self.db.run("UPDATE approvals SET prompt_text=? WHERE id=?", (text, approval_id))

    def is_expired(self, appr: Approval) -> bool:
        return self.clock.now() - parse_iso(appr.created_at) > self.ttl

    def grant(self, trace, approval_id: str, by: UserTurnRef) -> Approval:
        if not isinstance(by, UserTurnRef) or not by.is_valid():
            raise PermissionError("only a user turn can grant an approval")
        now = self.clock.now().isoformat()
        n = self.db.run("UPDATE approvals SET status='granted', decided_turn=?, updated_at=? "
                        "WHERE id=? AND status='pending' AND session_id=?",
                        (by.turn, now, approval_id, by.session_id))
        if n != 1:
            raise ApprovalRequired(f"approval {approval_id} is not pending in this session")
        trace.emit("approval", op="grant", id=approval_id, by_turn=by.turn)
        return self.get(approval_id)  # type: ignore[return-value]

    def update_hold(self, approval_id: str, hold_id: str, hold_expires: str) -> None:
        """A re-hold of the same slot at the same price keeps the agreement."""
        self.db.run("UPDATE approvals SET hold_id=?, hold_expires=? WHERE id=?", (hold_id, hold_expires, approval_id))

    def consume_with_outbox(self, trace, approval: Approval, *, planned_fingerprint: str, idem_key: str, op: str,
                            request_json: str) -> dict:
        """Atomic: granted -> consumed on a matching fingerprint, plus the outbox row."""
        now = self.clock.now().isoformat()
        with self.db.tx():
            existing = self.db.one("SELECT * FROM outbox WHERE idem_key=?", (idem_key,))
            if existing is not None:
                if existing["approval_id"] != approval.id:
                    raise ApprovalRequired("idempotency key belongs to another approval")
                return dict(existing)
            n = self.db.run("UPDATE approvals SET status='consumed', updated_at=? "
                            "WHERE id=? AND status='granted' AND fingerprint=?",
                            (now, approval.id, planned_fingerprint))
            if n != 1:
                raise ApprovalRequired(f"no granted approval matches the planned {op}")
            self.db.run("INSERT INTO outbox (idem_key, approval_id, session_id, user_id, op, request_json, status, "
                        "attempts, created_at, updated_at) VALUES (?,?,?,?,?,?, 'SENT', 0, ?, ?)",
                        (idem_key, approval.id, approval.session_id, approval.user_id, op, request_json, now, now))
        trace.emit("approval", op="consume", id=approval.id, fingerprint=planned_fingerprint[:12])
        trace.emit("outbox", key=idem_key, op=op, to="SENT", approval_id=approval.id)
        return dict(self.db.one("SELECT * FROM outbox WHERE idem_key=?", (idem_key,)))

    def void(self, trace, approval_id: str, reason: str) -> None:
        with self.db.tx():
            self._set_status(trace, approval_id, "void", reason)

    def decline(self, trace, approval_id: str, by: UserTurnRef) -> None:
        if not by.is_valid():
            raise PermissionError("only a user turn can decline")
        self.db.run("UPDATE approvals SET status='declined', decided_turn=?, updated_at=? WHERE id=? AND status='pending'",
                    (by.turn, self.clock.now().isoformat(), approval_id))
        trace.emit("approval", op="decline", id=approval_id, by_turn=by.turn)

    def _set_status(self, trace, approval_id: str, status: str, reason: str) -> None:
        n = self.db.run("UPDATE approvals SET status=?, void_reason=?, updated_at=? "
                        "WHERE id=? AND status IN ('pending','granted')",
                        (status, reason, self.clock.now().isoformat(), approval_id))
        if n:
            trace.emit("approval", op=status, id=approval_id, reason=reason)

    # ---------------------------------------------------------------- outbox
    def outbox_rows(self, session_id: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            rows = self.db.q(f"SELECT * FROM outbox WHERE session_id=? AND status IN ({marks}) ORDER BY created_at",
                             (session_id, *statuses))
        else:
            rows = self.db.q("SELECT * FROM outbox WHERE session_id=? ORDER BY created_at", (session_id,))
        return [dict(r) for r in rows]

    def outbox_set(self, trace, key: str, status: str, booking_id: str | None = None, error: str | None = None,
                   attempts: int | None = None) -> None:
        row = self.db.one("SELECT status FROM outbox WHERE idem_key=?", (key,))
        prev = row["status"] if row else None
        self.db.run("UPDATE outbox SET status=?, booking_id=COALESCE(?, booking_id), last_error=?, "
                    "attempts=COALESCE(?, attempts), updated_at=? WHERE idem_key=?",
                    (status, booking_id, error, attempts, self.clock.now().isoformat(), key))
        if prev != status:
            trace.emit("outbox", key=key, **{"from": prev}, to=status, booking_id=booking_id)
