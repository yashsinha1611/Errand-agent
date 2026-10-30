"""Pure business rules: what is missing, what a change invalidates, which offers fit.

No I/O here, so every rule is unit-testable without a database or a provider.
"""
from __future__ import annotations

from datetime import datetime

from .domain import ErrandState, Intent
from .timeparse import in_window, resolve_date, resolve_time
from .util import parse_iso

REQUIRED_BOOK = ("service", "area", "date_range")
ASK_ONCE = ("time_window", "budget")
SEARCH_KEYS = {"service", "area", "date_range", "time_window", "provider"}


def missing_fields(state: ErrandState) -> list[str]:
    need = [f for f in REQUIRED_BOOK if state.slot(f) is None]
    need += [f for f in ASK_ONCE if state.slot(f) is None and f not in state.asked]
    return need


def detail_values(intent: Intent, now: datetime) -> dict[str, dict]:
    """Translate an intent's details into slot values (resolved against the clock)."""
    out: dict[str, dict] = {}
    if intent.service:
        out["service"] = {"value": intent.service}
    if intent.area:
        out["area"] = {"value": intent.area}
    if intent.date_expr:
        start, end, label = resolve_date(intent.date_expr, now)
        out["date_range"] = {"value": [start, end], "label": label, "expr": intent.date_expr}
    if intent.time_expr:
        h1, h2, label = resolve_time(intent.time_expr)
        out["time_window"] = {"value": [h1, h2], "label": label, "expr": intent.time_expr}
    if intent.budget_inr is not None:
        out["budget"] = {"value": intent.budget_inr}
    elif intent.no_budget:
        out["budget"] = {"value": "ANY"}
    if intent.provider_ref and intent.provider_ref != "that":
        out["provider"] = {"value": None, "ref": intent.provider_ref}
    return out


def apply_details(state: ErrandState, values: dict[str, dict], source: str) -> set[str]:
    """Patch slots. Returns the names that actually changed; everything else is kept."""
    changed = set()
    for name, payload in values.items():
        old = state.slots.get(name)
        same = old is not None and old.get("value") == payload.get("value") and old.get("ref") == payload.get("ref")
        if not same:
            state.set_slot(name, payload.get("value"), source, state.turn,
                           **{k: v for k, v in payload.items() if k != "value"})
            changed.add(name)
    return changed


def invalidations(changed: set[str]) -> set[str]:
    """What a change voids downstream. Budget alone only re-filters offers."""
    if changed & SEARCH_KEYS:
        return {"search", "offers", "selection", "hold", "approval"}
    if "budget" in changed:
        return {"offers", "selection_if_over_budget"}
    return set()


def budget_value(state: ErrandState) -> int | None:
    b = state.slot("budget")
    return b if isinstance(b, int) else None


def slot_key(provider_id: str, slot_start: str) -> str:
    """Excluded slots are per provider: another provider's 09:00 is a different slot."""
    return f"{provider_id}|{slot_start}"


def build_offers(state: ErrandState, now: datetime, prefer_provider_id: str | None = None) -> list[dict]:
    dr = state.slot("date_range")
    hours = state.slot("time_window")
    budget = budget_value(state)
    provider = state.slots.get("provider") or {}
    want_id = (provider.get("value") or {}).get("id") if isinstance(provider.get("value"), dict) else None
    offers = []
    for p in state.search_raw:
        if want_id and p["id"] != want_id:
            continue
        excluded = set(state.excluded_slots)
        slots = [s for s in p["free_slots"]
                 if slot_key(p["id"], s) not in excluded
                 and in_window(parse_iso(s), tuple(dr) if dr else None, tuple(hours) if hours else None, now)]
        if not slots:
            continue
        offers.append({"provider_id": p["id"], "name": p["name"], "rating": p["rating"], "jobs": p.get("jobs", 0),
                       "price": p["price"], "slot_start": slots[0], "slots": slots[:8],
                       "within_budget": budget is None or p["price"] <= budget})
    offers.sort(key=lambda o: (not o["within_budget"], o["provider_id"] != prefer_provider_id, -o["rating"], o["price"]))
    return offers


def pick_slot(offer: dict, intent: Intent, now: datetime) -> tuple[dict, bool]:
    """Apply a time named with the selection ("Kumar at 11"). Returns (offer, honoured)."""
    if not intent.time_expr or not intent.time_expr.startswith("at:"):
        return offer, True
    hour = int(intent.time_expr[3:5])
    day = resolve_date(intent.date_expr, now)[0] if intent.date_expr else None
    for s in offer["slots"]:
        dt = parse_iso(s)
        if dt.hour == hour and (day is None or dt.date().isoformat() == day):
            return {**offer, "slot_start": s}, True
    return offer, False


def match_provider_name(ref: str, known: list[dict]) -> dict | None:
    r = ref.lower().strip()
    for p in known:
        if p["name"].lower() == r:
            return p
    for p in known:
        first = p["name"].lower().split()[0]
        if r.startswith(first) or first in r.split() or r in p["name"].lower():
            return p
    return None
