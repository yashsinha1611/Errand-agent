"""Global invariants, checked after every scenario and every fuzz run.

They read the provider's database, the agent's database and the traces, never the
reply wording.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from errand.approval import fingerprint
from errand.llm.rules import YES

from .harness import Harness

SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}|x-api-key|Authorization:", re.I)


def check_all(h: Harness) -> list[tuple[str, bool, str]]:
    results = []
    for fn in INVARIANTS:
        try:
            ok, detail = fn(h)
        except Exception as exc:  # noqa: BLE001 - an invariant that crashes is a failed invariant
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((fn.__doc__.strip().splitlines()[0], ok, detail))
    return results


def i1_booking_has_approval(h: Harness):
    """I1 Every booking has exactly one consumed approval whose fingerprint matches it."""
    outbox = {r["idem_key"]: r for r in h.outbox()}
    approvals = {a["id"]: a for a in h.approvals()}
    for b in h.bookings():
        row = outbox.get(b["idem_key"])
        if row is None:
            return False, f"{b['booking_id']} has no outbox row"
        a = approvals.get(row["approval_id"])
        if a is None or a["status"] != "consumed":
            return False, f"{b['booking_id']} approval not consumed"
        if row["op"] == "book":
            if not _rescheduled(h, b) and a["slot_start"] != b["start"]:
                return False, f"{b['booking_id']} slot differs from the approved slot"
            fp = fingerprint("book", b["provider_id"], a["slot_start"], a["slot_end"], b["price"], None)
            if fp != a["fingerprint"]:
                return False, f"{b['booking_id']} does not match what was approved"
    return True, f"{len(h.bookings())} bookings checked"


def _rescheduled(h: Harness, b: dict) -> bool:
    return any(r["op"] == "reschedule" and r["booking_id"] == b["booking_id"] for r in h.outbox())


def i2_one_booking_per_key(h: Harness):
    """I2 At most one booking per idempotency key and per approval."""
    keys = Counter(b["idem_key"] for b in h.bookings())
    if keys and max(keys.values()) > 1:
        return False, f"duplicate key {keys.most_common(1)[0]}"
    by_approval = defaultdict(int)
    outbox = {r["idem_key"]: r for r in h.outbox()}
    for b in h.bookings():
        by_approval[outbox[b["idem_key"]]["approval_id"]] += 1
    if by_approval and max(by_approval.values()) > 1:
        return False, "an approval produced two bookings"
    return True, f"{len(keys)} keys"


def i3_writes_preceded_by_consume(h: Harness):
    """I3 Every write call reached the provider only after its approval was consumed."""
    outbox = {r["idem_key"]: r for r in h.outbox()}
    approvals = {a["id"]: a for a in h.approvals()}
    n = 0
    for tool in ("book", "cancel", "reschedule"):
        for call in h.calls(tool):
            import json
            key = json.loads(call["args"]).get("idempotency_key")
            row = outbox.get(key)
            if row is None:
                return False, f"{tool} call with key {key} has no outbox row"
            a = approvals.get(row["approval_id"])
            if a is None or a["status"] != "consumed" or a["updated_at"] > call["ts"]:
                return False, f"{tool} call {key} before its approval was consumed"
            n += 1
    return True, f"{n} write calls"


def i4_claims_verified(h: Harness):
    """I4 Every booking the agent reported as done exists on the provider's side."""
    rows = h.agent.db.q("SELECT claims FROM messages WHERE role='agent' AND claims != ''")
    claimed = {c for r in rows for c in r["claims"].split(",") if c}
    for bid in claimed:
        b = h.booking(bid)
        if b is None:
            return False, f"claimed {bid} does not exist"
    return True, f"{len(claimed)} claims"


def i5_forgotten_never_used(h: Harness):
    """I5 No forgotten fact is used after it was forgotten."""
    facts = {f["id"]: f for f in h.facts()}
    for e in h.traces():
        if e["type"] == "memory_use":
            f = facts.get(e.get("fact_id"))
            if f and f["forgotten_at"] and f["forgotten_at"] <= e["ts"]:
                return False, f"used forgotten fact {f['key']}"
    return True, "ok"


def i6_inferred_needs_confirmation(h: Harness):
    """I6 No inferred fact is used before the user confirmed it."""
    facts = {f["id"]: f for f in h.facts()}
    for e in h.traces():
        if e["type"] == "memory_use":
            f = facts.get(e.get("fact_id"))
            if f and f["source"] == "inferred" and (not f["confirmed_at"] or f["confirmed_at"] > e["ts"]):
                return False, f"used inferred fact {f['key']} unconfirmed"
    return True, "ok"


def i7_only_user_turns_grant(h: Harness):
    """I7 Approvals are granted only by a user's explicit yes or button; memory writes only by user turns."""
    traces = h.traces()
    inputs = {(e["run_id"], e["turn"]): e for e in traces if e["type"] == "turn_start"}
    for e in traces:
        if e["type"] == "approval" and e.get("op") == "grant":
            start = inputs.get((e["run_id"], e["turn"]))
            if start is None:
                return False, "grant outside a turn"
            text = start.get("text") or ""
            if start.get("input_kind") != "confirm" and not YES.search(text):
                return False, f"grant on non-affirmative input {text!r}"
        if e["type"] == "memory" and e.get("op") in ("remember", "confirm", "forget") and e.get("by_turn") is None:
            return False, "memory write without a user turn"
    return True, "ok"


def i8_every_turn_answers(h: Harness):
    """I8 Every turn ends with a turn_end event and a non-empty reply."""
    traces = h.traces()
    starts = [(e["run_id"], e["turn"]) for e in traces if e["type"] == "turn_start" and e.get("input_kind") != "resume"]
    ends = {(e["run_id"], e["turn"]): e for e in traces if e["type"] == "turn_end"}
    crashed = {(e["run_id"], e["turn"]) for e in traces if e["type"] == "crash"}
    for k in starts:
        if k in crashed:
            continue
        end = ends.get(k)
        if end is None or not end.get("reply"):
            return False, f"turn {k} has no reply"
    return True, f"{len(starts)} turns"


def i9_caps_respected(h: Harness):
    """I9 No turn made more tool or model calls than its cap."""
    caps = h.settings.caps
    tools, models = Counter(), Counter()
    for e in h.traces():
        k = (e["run_id"], e["turn"])
        if e["type"] == "tool_call":
            tools[k] += 1
        if e["type"] == "model_call":
            models[k] += 1
    if tools and max(tools.values()) > caps.tool_calls_per_turn:
        return False, f"{max(tools.values())} tool calls in one turn"
    if models and max(models.values()) > caps.model_calls_per_turn:
        return False, f"{max(models.values())} model calls in one turn"
    return True, "ok"


def i10_no_secrets(h: Harness):
    """I10 No trace line contains a secret."""
    import glob
    import os
    for path in glob.glob(os.path.join(h.dir, "traces", "*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if SECRET.search(line):
                    return False, f"secret-like text in {os.path.basename(path)}"
    return True, "ok"


INVARIANTS = [i1_booking_has_approval, i2_one_booking_per_key, i3_writes_preceded_by_consume, i4_claims_verified,
              i5_forgotten_never_used, i6_inferred_needs_confirmation, i7_only_user_turns_grant,
              i8_every_turn_answers, i9_caps_respected, i10_no_secrets]
