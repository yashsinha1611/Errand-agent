"""One append-only JSONL trace per session run.

Redaction is an allowlist applied here, not at call sites: each event type declares
which fields may be written and everything else is dropped. Untrusted text is stored as
a short excerpt plus a hash. Model-call fields use OpenTelemetry GenAI names.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Callable

from .clock import Clock
from .domain import UntrustedText
from .util import sha256, truncate

ALLOW: dict[str, set[str]] = {
    "process_start": {"pid", "model"},
    "turn_start": {"input_kind", "text"},
    "turn_end": {"outcome", "reply", "claims", "latency_ms"},
    "state_transition": {"from", "to", "diff"},
    "model_call": {"purpose", "gen_ai.request.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
                   "cost_usd", "latency_ms", "intent", "error", "attempt"},
    "tool_call": {"name", "args", "result", "error", "error_class", "latency_ms", "attempt"},
    "approval": {"op", "id", "fingerprint", "action", "price", "reason", "by_turn"},
    "outbox": {"key", "op", "from", "to", "booking_id", "approval_id"},
    "memory": {"op", "key", "source", "fact_id", "value", "by_turn"},
    "memory_use": {"key", "fact_id", "source"},
    "budget": {"kind", "used", "cap"},
    "injection_suspected": {"provider_id", "review_hash", "rule"},
    "recovery": {"key", "op", "status"},
    "crash": {"point"},
    "error": {"type", "message"},
}
SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{8,}|x-api-key[^,}\]]*|Bearer\s+[A-Za-z0-9._\-]+)", re.I)
CONTENT_FIELDS = {"text", "reply"}


def scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(value, UntrustedText):
        return {"untrusted_excerpt": value.for_display(80), "sha": value.digest()}
    if isinstance(value, str):
        return SECRET.sub("[redacted]", truncate(value, 600))
    if isinstance(value, dict):
        return {str(k): scrub(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in list(value)[:40]]
    return value


def summarize(ev: dict) -> str:
    t = ev["type"]
    if t == "tool_call":
        tail = ev.get("error") or _short(ev.get("result"))
        return f"{ev.get('name')} → {tail}"
    if t == "model_call":
        intent = ev.get("intent") or ev.get("error") or ""
        return (f"{ev.get('purpose')} → {intent} ({ev.get('gen_ai.usage.input_tokens', 0)} in, "
                f"{ev.get('gen_ai.usage.output_tokens', 0)} out tokens)")
    if t == "approval":
        return " ".join(str(x) for x in (ev.get("op"), ev.get("id"), ev.get("reason") or "") if x).strip()
    if t == "outbox":
        return f"{ev.get('key')} {ev.get('from') or '∅'} → {ev.get('to')}"
    if t == "state_transition":
        return f"{ev.get('from')} → {ev.get('to')}"
    if t == "memory":
        return f"{ev.get('op')} {ev.get('key')} ({ev.get('source', '')})"
    if t == "memory_use":
        return f"use {ev.get('key')} ({ev.get('source')})"
    if t == "injection_suspected":
        return f"{ev.get('provider_id')} review {ev.get('review_hash')}, rule {ev.get('rule')}"
    if t == "budget":
        return f"{ev.get('kind')} {ev.get('used')}/{ev.get('cap')}"
    if t == "recovery":
        return f"{ev.get('op')} {ev.get('key')} → {ev.get('status')}"
    if t in ("turn_start", "turn_end"):
        return str(ev.get("outcome") or ev.get("input_kind") or "")
    if t == "error":
        return f"{ev.get('type')}: {ev.get('message')}"
    return ""


def _short(v: Any) -> str:
    if isinstance(v, dict):
        keys = ("status", "booking_id", "hold_id", "quoted_price", "count", "reviews", "providers")
        parts = [f"{k}={v[k]}" for k in keys if k in v]
        return ", ".join(parts) if parts else "ok"
    if isinstance(v, list):
        return f"{len(v)} items"
    return str(v)[:60] if v is not None else "ok"


class Tracer:
    def __init__(self, trace_dir: str, clock: Clock, capture_content: bool = True) -> None:
        self.dir = trace_dir
        self.clock = clock
        self.capture_content = capture_content
        os.makedirs(trace_dir, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._seq: dict[str, int] = {}
        self._guard = threading.Lock()

    def path(self, session_id: str) -> str:
        return os.path.join(self.dir, f"{session_id}.jsonl")

    def run(self, session_id: str, turn: int, on_event: Callable[[dict], None] | None = None) -> "TraceRun":
        with self._guard:
            lock = self._locks.setdefault(session_id, threading.Lock())
            if session_id not in self._seq:
                path = self.path(session_id)
                count = 0
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as fh:
                        count = sum(1 for _ in fh)
                self._seq[session_id] = count
        return TraceRun(self, session_id, turn, lock, on_event)

    def next_seq(self, session_id: str) -> int:
        with self._guard:
            self._seq[session_id] += 1
            return self._seq[session_id]


class TraceRun:
    def __init__(self, tracer: Tracer, session_id: str, turn: int, lock: threading.Lock,
                 on_event: Callable[[dict], None] | None) -> None:
        self.tracer = tracer
        self.session_id = session_id
        self.turn = turn
        self.lock = lock
        self.on_event = on_event

    def emit(self, type_: str, **fields: Any) -> dict:
        allowed = ALLOW.get(type_, set())
        clean: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in CONTENT_FIELDS and not self.tracer.capture_content and isinstance(v, str):
                v = {"sha": sha256(v)[:12], "chars": len(v)}
            clean[k] = scrub(v)
        event = {"run_id": self.session_id, "seq": self.tracer.next_seq(self.session_id),
                 "ts": self.tracer.clock.now().isoformat(), "turn": self.turn, "type": type_, **clean}
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self.lock:
            with open(self.tracer.path(self.session_id), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.on_event:
            level = "error" if type_ in ("error", "injection_suspected", "crash") or event.get("error_class") in (
                "UNKNOWN_OUTCOME", "UNEXPECTED") else None
            self.on_event({"type": "trace", "event": {
                "seq": event["seq"], "type": type_, "summary": summarize(event),
                "ms": event.get("latency_ms"), "level": level}})
        return event
