"""Small helpers shared across the agent."""
from __future__ import annotations

import hashlib
import json
import random
import re
import string
import threading
from datetime import datetime, timedelta
from typing import Any

from .clock import IST

_ALPHABET = string.ascii_uppercase + string.digits


class IdFactory:
    """Readable ids (``ap_7KQ2M9XA``). Seeded in tests so traces are reproducible."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed) if seed is not None else random.SystemRandom()
        self._lock = threading.Lock()

    def new(self, prefix: str, n: int = 8) -> str:
        with self._lock:
            return f"{prefix}_" + "".join(self._rng.choice(_ALPHABET) for _ in range(n))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def fmt_inr(amount: int | float) -> str:
    """₹ with Indian digit grouping: 150000 -> ₹1,50,000."""
    n = int(round(amount))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{sign}₹{s}"


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def fmt_day(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b}"


def fmt_slot(start: str | datetime, hours: int = 1) -> str:
    s = parse_iso(start) if isinstance(start, str) else start
    e = s + timedelta(hours=hours)
    return f"{fmt_day(s)}, {s:%H:%M}–{e:%H:%M}"


def fmt_short_slot(start: str | datetime) -> str:
    s = parse_iso(start) if isinstance(start, str) else start
    return f"{fmt_day(s)}, {s:%H:%M}"


_CONTROL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e]")


def sanitize_label(text: str, limit: int = 48) -> str:
    """Short single-line label (provider names). Strips control and bidi characters."""
    cleaned = _CONTROL.sub("", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
