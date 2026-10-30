"""Mock home-services provider implementing the brief's fixed tool contract.

It is deliberately a *stateful vendor*, not a stub: its own SQLite file, holds that
expire, bookings that persist across agent restarts, and idempotency keys honoured on
its side. Failure behaviour follows the brief exactly:

  book:    70% ok · 15% slot_taken · 15% timeout, of which half actually booked.
           hold_expired if past expiry.
  cancel:  first call per booking fails with transient 503; retry succeeds.
  hold:    slot_unavailable; quoted price may differ from search price (configurable).
  reschedule: slot_conflict on configured slots.

Randomness is keyed by (seed, tool, call number) so outcomes are reproducible even
across restarts. Faults can also be *armed* for the next matching call, which is how
evals and the demo force a specific failure.
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import timedelta

from ..clock import Clock
from ..util import parse_iso, sha256
from .world import ALL_AREAS, PROVIDERS, free_slots, reviews_for, seed_bookings

SERVICES = sorted({p[2] for p in PROVIDERS})


class ProviderError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ProviderTimeout(Exception):
    """The call did not answer in time. The outcome is unknown to the caller."""


@dataclass
class MockConfig:
    seed: int = 7
    mode: str = "realistic"                 # realistic: 70/15/15 · calm: book always ok unless a fault is armed
    book_ok: float = 0.70
    book_slot_taken: float = 0.15           # the rest times out
    timeout_booked_fraction: float = 0.5
    price_drift: str = "off"                # off | random
    drift_probability: float = 0.3
    cancel_first_503: bool = True
    reschedule_conflicts: tuple = ()        # ((provider_id, iso_start), ...)
    hold_ttl_min: int = 10
    timeout_seconds: float = 3.0
    latency: dict = field(default_factory=lambda: {
        "search_providers": 0.35, "get_provider": 0.25, "list_bookings": 0.2, "hold_slot": 0.3,
        "book": 0.8, "get_booking_status": 0.15, "cancel": 0.4, "reschedule": 0.6,
    })


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS providers (id TEXT PRIMARY KEY, name TEXT, service TEXT, areas TEXT,
    price INTEGER, rating REAL, jobs INTEGER, attributes TEXT);
CREATE TABLE IF NOT EXISTS reviews (provider_id TEXT, idx INTEGER, stars INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS slots (provider_id TEXT, start TEXT, PRIMARY KEY (provider_id, start));
CREATE TABLE IF NOT EXISTS holds (hold_id TEXT PRIMARY KEY, provider_id TEXT, start TEXT,
    quoted_price INTEGER, expires_at TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS bookings (booking_id TEXT PRIMARY KEY, user_id TEXT, provider_id TEXT,
    service TEXT, area TEXT, start TEXT, price INTEGER, status TEXT, idem_key TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, op TEXT, request_hash TEXT,
    ok INTEGER, response TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE IF NOT EXISTS faults (id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT, outcome TEXT,
    consumed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cancel_attempts (booking_id TEXT PRIMARY KEY, attempts INTEGER);
CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, tool TEXT,
    args TEXT, outcome TEXT);
"""


class MockProvider:
    def __init__(self, path: str, clock: Clock, config: MockConfig | None = None,
                 user_id: str = "u_demo") -> None:
        self.path = path
        self.clock = clock
        self.config = config or MockConfig()
        self.user_id = user_id
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        if self._meta("base_date") is None:
            self._build_world()

    # ------------------------------------------------------------------ world setup
    def _build_world(self) -> None:
        base = self.clock.now().date()
        seed = self.config.seed
        with self._tx():
            for pid, name, service, areas, price, rating, jobs, attrs in PROVIDERS:
                self._db.execute("INSERT INTO providers VALUES (?,?,?,?,?,?,?,?)",
                                 (pid, name, service, json.dumps(areas), price, rating, jobs, json.dumps(attrs)))
                for i, (stars, text) in enumerate(reviews_for(pid, seed)):
                    self._db.execute("INSERT INTO reviews VALUES (?,?,?,?)", (pid, i, stars, text))
                for start in free_slots(pid, base, seed):
                    self._db.execute("INSERT INTO slots VALUES (?,?)", (pid, start))
            for b in seed_bookings(base, self.user_id):
                self._db.execute(
                    "INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (b["booking_id"], self.user_id, b["provider_id"], b["service"], b["area"], b["start"],
                     b["price"], b["status"], None, self.clock.now().isoformat()))
                self._db.execute("DELETE FROM slots WHERE provider_id=? AND start=?", (b["provider_id"], b["start"]))
            self._db.execute("INSERT INTO counters VALUES ('booking', 2040), ('hold', 100)")
            self._db.execute("INSERT INTO meta VALUES ('base_date', ?), ('seed', ?)", (base.isoformat(), str(seed)))

    def reset(self) -> None:
        with self._lock:
            for table in ("meta", "providers", "reviews", "slots", "holds", "bookings", "idempotency",
                          "counters", "faults", "cancel_attempts", "calls"):
                self._db.execute(f"DELETE FROM {table}")
            self._build_world()

    # ------------------------------------------------------------------ helpers
    class _Tx:
        def __init__(self, db: sqlite3.Connection) -> None:
            self.db = db

        def __enter__(self):
            self.db.execute("BEGIN IMMEDIATE")
            return self.db

        def __exit__(self, exc_type, exc, tb):
            self.db.execute("COMMIT" if exc_type is None else "ROLLBACK")
            return False

    def _tx(self) -> "MockProvider._Tx":
        return MockProvider._Tx(self._db)

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _next(self, name: str) -> int:
        self._db.execute("INSERT OR IGNORE INTO counters VALUES (?, 0)", (name,))
        self._db.execute("UPDATE counters SET value = value + 1 WHERE name=?", (name,))
        return self._db.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()["value"]

    def _rng(self, tool: str) -> random.Random:
        n = self._next(f"rng:{tool}")
        return random.Random(f"{self.config.seed}:{tool}:{n}")

    def _log(self, tool: str, args: dict, outcome: str) -> None:
        self._db.execute("INSERT INTO calls (ts, tool, args, outcome) VALUES (?,?,?,?)",
                         (self.clock.now().isoformat(), tool, json.dumps(args, default=str), outcome))

    def _latency(self, tool: str) -> None:
        self.clock.sleep(self.config.latency.get(tool, 0.2))

    # ------------------------------------------------------------------ faults
    def arm(self, tool: str, outcome: str, count: int = 1) -> None:
        with self._lock:
            for _ in range(count):
                self._db.execute("INSERT INTO faults (tool, outcome) VALUES (?, ?)", (tool, outcome))

    def take_fault(self, tool: str, prefix: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, outcome FROM faults WHERE tool=? AND consumed=0 ORDER BY id LIMIT 1", (tool,)).fetchone()
            if row is None or (prefix and not row["outcome"].startswith(prefix)):
                return None
            self._db.execute("UPDATE faults SET consumed=1 WHERE id=?", (row["id"],))
            return row["outcome"]

    def armed_faults(self) -> list[dict]:
        rows = self._db.execute("SELECT id, tool, outcome FROM faults WHERE consumed=0 ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def clear_faults(self) -> None:
        with self._lock:
            self._db.execute("UPDATE faults SET consumed=1 WHERE consumed=0")

    def _unexpected(self, tool: str, fault: str | None) -> None:
        if fault == "error":
            raise KeyError(f"simulated internal error in {tool}")

    def _idem(self, key: str, op: str, request_hash: str):
        row = self._db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["op"] != op or row["request_hash"] != request_hash:
            raise ProviderError("idempotency_mismatch", "key reused with a different request")
        return row

    def _store(self, key: str, op: str, request_hash: str, ok: bool, response: dict) -> None:
        self._db.execute("INSERT OR REPLACE INTO idempotency VALUES (?,?,?,?,?,?)",
                         (key, op, request_hash, 1 if ok else 0, json.dumps(response), self.clock.now().isoformat()))

    def _provider(self, provider_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if row is None:
            raise ProviderError("not_found", f"provider {provider_id}")
        return row

    # ------------------------------------------------------------------ tools
    def search_providers(self, service: str, area: str, date_range: tuple[str, str]) -> list[dict]:
        with self._lock:
            fault = self.take_fault("search_providers")
            self._log("search_providers", {"service": service, "area": area, "date_range": list(date_range)}, fault or "ok")
            self._unexpected("search_providers", fault)
            if service not in SERVICES or area not in ALL_AREAS:
                raise ProviderError("invalid_input", f"unknown service or area: {service}, {area}")
            start, end = date_range
            now = self.clock.now()
            out = []
            for p in self._db.execute("SELECT * FROM providers WHERE service=? ORDER BY id", (service,)):
                if area not in json.loads(p["areas"]):
                    continue
                rows = self._db.execute(
                    "SELECT start FROM slots WHERE provider_id=? AND substr(start,1,10) BETWEEN ? AND ? ORDER BY start",
                    (p["id"], start, end)).fetchall()
                slots = [r["start"] for r in rows if parse_iso(r["start"]) > now]
                out.append({"id": p["id"], "name": p["name"], "price": p["price"], "rating": p["rating"],
                            "jobs": p["jobs"], "attributes": json.loads(p["attributes"]), "free_slots": slots[:40]})
            self._latency("search_providers")
            return out[:8]

    def get_provider(self, provider_id: str) -> dict:
        with self._lock:
            fault = self.take_fault("get_provider")
            self._log("get_provider", {"provider_id": provider_id}, fault or "ok")
            self._unexpected("get_provider", fault)
            p = self._provider(provider_id)
            reviews = [{"stars": r["stars"], "text": r["text"]} for r in
                       self._db.execute("SELECT stars, text FROM reviews WHERE provider_id=? ORDER BY idx", (provider_id,))]
            self._latency("get_provider")
            return {"id": p["id"], "name": p["name"], "service": p["service"], "areas": json.loads(p["areas"]),
                    "price": p["price"], "rating": p["rating"], "jobs": p["jobs"],
                    "attributes": json.loads(p["attributes"]), "reviews": reviews}

    def list_bookings(self, user_id: str) -> list[dict]:
        with self._lock:
            fault = self.take_fault("list_bookings")
            self._log("list_bookings", {"user_id": user_id}, fault or "ok")
            self._unexpected("list_bookings", fault)
            rows = self._db.execute(
                "SELECT b.*, p.name AS provider_name FROM bookings b JOIN providers p ON p.id=b.provider_id "
                "WHERE user_id=? ORDER BY start", (user_id,)).fetchall()
            self._latency("list_bookings")
            out = []
            for r in rows:
                start = parse_iso(r["start"])
                out.append({"booking_id": r["booking_id"], "provider_id": r["provider_id"],
                            "provider_name": r["provider_name"], "service": r["service"], "area": r["area"],
                            "slot_start": r["start"], "slot_end": (start + timedelta(hours=1)).isoformat(),
                            "price": r["price"], "status": r["status"]})
            return out

    def hold_slot(self, provider_id: str, slot: str) -> dict:
        with self._lock:
            fault = self.take_fault("hold_slot")
            self._log("hold_slot", {"provider_id": provider_id, "slot": slot}, fault or "ok")
            self._unexpected("hold_slot", fault)
            p = self._provider(provider_id)
            now = self.clock.now()
            free = self._db.execute("SELECT 1 FROM slots WHERE provider_id=? AND start=?", (provider_id, slot)).fetchone()
            if free is None or fault == "unavailable" or parse_iso(slot) <= now:
                self._latency("hold_slot")
                raise ProviderError("slot_unavailable", f"{provider_id} {slot}")
            # expire stale holds on this slot; an active one is returned as-is (single client)
            for h in self._db.execute("SELECT * FROM holds WHERE provider_id=? AND start=? AND status='active'",
                                      (provider_id, slot)).fetchall():
                if parse_iso(h["expires_at"]) <= now:
                    self._db.execute("UPDATE holds SET status='expired' WHERE hold_id=?", (h["hold_id"],))
                elif fault is None:
                    self._latency("hold_slot")
                    return {"hold_id": h["hold_id"], "expires_at": h["expires_at"], "quoted_price": h["quoted_price"]}
                else:
                    self._db.execute("UPDATE holds SET status='released' WHERE hold_id=?", (h["hold_id"],))
            price = p["price"]
            if fault and fault.startswith("price:"):
                price += int(fault.split(":", 1)[1])
            elif self.config.price_drift == "random":
                rng = self._rng("hold_slot")
                if rng.random() < self.config.drift_probability:
                    price += rng.choice([-100, 100, 150, 200, 300])
            hold_id = f"H-{self._next('hold')}"
            expires = now + timedelta(minutes=self.config.hold_ttl_min)
            self._db.execute("INSERT INTO holds VALUES (?,?,?,?,?, 'active')",
                             (hold_id, provider_id, slot, price, expires.isoformat()))
            self._latency("hold_slot")
            return {"hold_id": hold_id, "expires_at": expires.isoformat(), "quoted_price": price}

    def _decide_book(self, fault: str | None) -> str:
        if fault:
            return fault
        if self.config.mode != "realistic":
            return "ok"
        r = self._rng("book")
        x = r.random()
        if x < self.config.book_ok:
            return "ok"
        if x < self.config.book_ok + self.config.book_slot_taken:
            return "slot_taken"
        return "timeout_booked" if r.random() < self.config.timeout_booked_fraction else "timeout_lost"

    def book(self, hold_id: str, idempotency_key: str) -> dict:
        with self._lock:
            fault = self.take_fault("book")
            self._unexpected("book", fault)
            req = sha256(hold_id)
            prior = self._idem(idempotency_key, "book", req)
            if prior is not None:
                self._log("book", {"hold_id": hold_id, "idempotency_key": idempotency_key}, "replayed")
                self._latency("book")
                resp = json.loads(prior["response"])
                if prior["ok"]:
                    return resp
                raise ProviderError(resp["code"], "replayed")
            hold = self._db.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
            if hold is None:
                self._log("book", {"hold_id": hold_id, "idempotency_key": idempotency_key}, "invalid_input")
                raise ProviderError("invalid_input", "unknown hold")
            now = self.clock.now()
            if hold["status"] != "active" or parse_iso(hold["expires_at"]) <= now or fault == "hold_expired":
                self._db.execute("UPDATE holds SET status='expired' WHERE hold_id=?", (hold_id,))
                self._store(idempotency_key, "book", req, False, {"code": "hold_expired"})
                self._log("book", {"hold_id": hold_id, "idempotency_key": idempotency_key}, "hold_expired")
                self._latency("book")
                raise ProviderError("hold_expired")
            outcome = self._decide_book(fault)
            self._log("book", {"hold_id": hold_id, "idempotency_key": idempotency_key}, outcome)
            if outcome == "slot_taken":
                with self._tx():
                    self._db.execute("DELETE FROM slots WHERE provider_id=? AND start=?", (hold["provider_id"], hold["start"]))
                    self._db.execute("UPDATE holds SET status='lost' WHERE hold_id=?", (hold_id,))
                    self._store(idempotency_key, "book", req, False, {"code": "slot_taken"})
                self._latency("book")
                raise ProviderError("slot_taken")
            if outcome == "timeout_lost":
                self.clock.sleep(self.config.timeout_seconds)
                raise ProviderTimeout("book timed out")
            # ok or timeout_booked: the booking really happens
            p = self._provider(hold["provider_id"])
            booking_id = f"B-{self._next('booking')}"
            with self._tx():
                self._db.execute(
                    "INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (booking_id, self.user_id, hold["provider_id"], p["service"], self._area_for(hold["provider_id"]),
                     hold["start"], hold["quoted_price"], "booked", idempotency_key, now.isoformat()))
                self._db.execute("DELETE FROM slots WHERE provider_id=? AND start=?", (hold["provider_id"], hold["start"]))
                self._db.execute("UPDATE holds SET status='consumed' WHERE hold_id=?", (hold_id,))
                self._store(idempotency_key, "book", req, True, {"booking_id": booking_id})
            if outcome == "timeout_booked":
                self.clock.sleep(self.config.timeout_seconds)
                raise ProviderTimeout("book timed out after committing")
            self._latency("book")
            return {"booking_id": booking_id}

    def _area_for(self, provider_id: str) -> str:
        return json.loads(self._provider(provider_id)["areas"])[0]

    def get_booking_status(self, idempotency_key: str) -> dict:
        with self._lock:
            fault = self.take_fault("get_booking_status")
            self._log("get_booking_status", {"idempotency_key": idempotency_key}, fault or "ok")
            self._unexpected("get_booking_status", fault)
            row = self._db.execute("SELECT * FROM idempotency WHERE key=? AND op='book'", (idempotency_key,)).fetchone()
            self._latency("get_booking_status")
            if row is None:
                return {"status": "not_found"}
            resp = json.loads(row["response"])
            if row["ok"]:
                return {"status": "booked", "booking_id": resp["booking_id"]}
            return {"status": "failed", "reason": resp.get("code")}

    def cancel(self, booking_id: str, idempotency_key: str) -> dict:
        with self._lock:
            fault = self.take_fault("cancel")
            self._unexpected("cancel", fault)
            req = sha256(booking_id)
            prior = self._idem(idempotency_key, "cancel", req)
            if prior is not None:
                self._log("cancel", {"booking_id": booking_id, "idempotency_key": idempotency_key}, "replayed")
                self._latency("cancel")
                return json.loads(prior["response"])
            b = self._db.execute("SELECT * FROM bookings WHERE booking_id=? AND user_id=?",
                                 (booking_id, self.user_id)).fetchone()
            if b is None:
                self._log("cancel", {"booking_id": booking_id}, "not_found")
                raise ProviderError("not_found", booking_id)
            attempts = self._db.execute("SELECT attempts FROM cancel_attempts WHERE booking_id=?", (booking_id,)).fetchone()
            n = attempts["attempts"] if attempts else 0
            self._db.execute("INSERT OR REPLACE INTO cancel_attempts VALUES (?, ?)", (booking_id, n + 1))
            if fault == "503" or (self.config.cancel_first_503 and n == 0):
                self._log("cancel", {"booking_id": booking_id, "idempotency_key": idempotency_key}, "transient_503")
                self._latency("cancel")
                raise ProviderError("transient_503", "service unavailable")
            with self._tx():
                if b["status"] == "booked":
                    self._db.execute("UPDATE bookings SET status='cancelled' WHERE booking_id=?", (booking_id,))
                    self._db.execute("INSERT OR IGNORE INTO slots VALUES (?, ?)", (b["provider_id"], b["start"]))
                resp = {"status": "cancelled", "booking_id": booking_id}
                self._store(idempotency_key, "cancel", req, True, resp)
            self._log("cancel", {"booking_id": booking_id, "idempotency_key": idempotency_key}, "ok")
            self._latency("cancel")
            return resp

    def reschedule(self, booking_id: str, new_slot: str, idempotency_key: str) -> dict:
        with self._lock:
            fault = self.take_fault("reschedule")
            self._unexpected("reschedule", fault)
            req = sha256(f"{booking_id}|{new_slot}")
            args = {"booking_id": booking_id, "new_slot": new_slot, "idempotency_key": idempotency_key}
            prior = self._idem(idempotency_key, "reschedule", req)
            if prior is not None:
                self._log("reschedule", args, "replayed")
                self._latency("reschedule")
                resp = json.loads(prior["response"])
                if prior["ok"]:
                    return resp
                raise ProviderError(resp["code"], "replayed")
            b = self._db.execute("SELECT * FROM bookings WHERE booking_id=? AND user_id=?",
                                 (booking_id, self.user_id)).fetchone()
            if b is None or b["status"] != "booked":
                self._log("reschedule", args, "not_found")
                raise ProviderError("not_found", booking_id)
            free = self._db.execute("SELECT 1 FROM slots WHERE provider_id=? AND start=?",
                                    (b["provider_id"], new_slot)).fetchone()
            conflict = (fault == "conflict" or free is None
                        or (b["provider_id"], new_slot) in set(map(tuple, self.config.reschedule_conflicts)))
            if conflict:
                self._store(idempotency_key, "reschedule", req, False, {"code": "slot_conflict"})
                self._log("reschedule", args, "slot_conflict")
                self._latency("reschedule")
                raise ProviderError("slot_conflict", new_slot)
            with self._tx():
                self._db.execute("INSERT OR IGNORE INTO slots VALUES (?, ?)", (b["provider_id"], b["start"]))
                self._db.execute("DELETE FROM slots WHERE provider_id=? AND start=?", (b["provider_id"], new_slot))
                self._db.execute("UPDATE bookings SET start=? WHERE booking_id=?", (new_slot, booking_id))
                start = parse_iso(new_slot)
                resp = {"booking_id": booking_id, "provider_id": b["provider_id"], "slot_start": new_slot,
                        "slot_end": (start + timedelta(hours=1)).isoformat(), "price": b["price"], "status": "booked"}
                self._store(idempotency_key, "reschedule", req, True, resp)
            self._log("reschedule", args, "ok")
            self._latency("reschedule")
            return resp

    # ------------------------------------------------------------------ inspection (evals only)
    def all_bookings(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM bookings ORDER BY booking_id")]

    def calls(self, tool: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM calls", ()
        if tool:
            q, args = q + " WHERE tool=?", (tool,)
        return [dict(r) for r in self._db.execute(q + " ORDER BY id", args)]

    def idempotency_rows(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM idempotency")]

    def close(self) -> None:
        self._db.close()

