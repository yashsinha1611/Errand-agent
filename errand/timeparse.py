"""Date and time resolution happens in code, never in the model.

The interpreter returns symbolic expressions (``this_week``, ``weekday:sat``,
``after:10:00``); these functions turn them into concrete ranges using the injected
clock in Asia/Kolkata.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .util import fmt_day

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FIRST_HOUR, LAST_START_HOUR = 8, 18          # slots start 08:00 … 18:00, one hour each


def resolve_date(expr: str, now: datetime) -> tuple[str, str, str]:
    """Return (start_date, end_date, label) as ISO dates, inclusive."""
    today = now.date()
    if expr.startswith("before:"):
        # A deadline ("before Thursday", "Diwali se pehle"): any day from today until the day before it.
        target = date.fromisoformat(resolve_date(expr.split(":", 1)[1], now)[0])
        end = max(today, target - timedelta(days=1))
        t = datetime(target.year, target.month, target.day)
        return today.isoformat(), end.isoformat(), f"before {fmt_day(t)}"
    if expr == "today":
        start = end = today
    elif expr == "tomorrow":
        start = end = today + timedelta(days=1)
    elif expr == "day_after_tomorrow":
        start = end = today + timedelta(days=2)
    elif expr == "this_week":
        start = today
        end = today + timedelta(days=6 - today.weekday()) if today.weekday() < 6 else today + timedelta(days=6)
    elif expr == "next_week":
        start = today + timedelta(days=7 - today.weekday())
        end = start + timedelta(days=6)
    elif expr == "weekend":
        start = today + timedelta(days=(5 - today.weekday()) % 7)
        end = start + timedelta(days=1)
    elif expr.startswith("weekday:"):
        target = WEEKDAYS.index(expr.split(":", 1)[1])
        delta = (target - today.weekday()) % 7
        if delta == 0 and now.hour >= LAST_START_HOUR:
            delta = 7
        start = end = today + timedelta(days=delta)
    elif expr.startswith("date:"):
        start = end = date.fromisoformat(expr.split(":", 1)[1])
    else:
        raise ValueError(f"unknown date expression {expr!r}")
    return start.isoformat(), end.isoformat(), _date_label(start, end)


def _date_label(start: date, end: date) -> str:
    s = datetime(start.year, start.month, start.day)
    e = datetime(end.year, end.month, end.day)
    return fmt_day(s) if start == end else f"{fmt_day(s)} – {fmt_day(e)}"


def resolve_time(expr: str) -> tuple[int, int, str]:
    """Return (first_start_hour, last_start_hour, label) for one-hour slots."""
    if expr in (None, "any"):
        return FIRST_HOUR, LAST_START_HOUR, "any time"
    if expr == "morning":
        return 8, 11, "mornings (08:00–12:00)"
    if expr == "afternoon":
        return 12, 16, "afternoons (12:00–17:00)"
    if expr == "evening":
        return 17, 18, "evenings (17:00–19:00)"
    kind, hhmm = expr.split(":", 1)
    hour = int(hhmm.split(":")[0])
    if kind == "after":
        return max(hour, FIRST_HOUR), LAST_START_HOUR, f"from {hour:02d}:00"
    if kind == "before":
        return FIRST_HOUR, min(hour - 1, LAST_START_HOUR), f"ending by {hour:02d}:00"
    if kind == "at":
        return hour, hour, f"at {hour:02d}:00"
    raise ValueError(f"unknown time expression {expr!r}")


def in_window(slot_start: datetime, date_range: tuple[str, str] | None, hours: tuple[int, int] | None,
              now: datetime) -> bool:
    if slot_start <= now:
        return False
    if date_range:
        d = slot_start.date().isoformat()
        if not (date_range[0] <= d <= date_range[1]):
            return False
    if hours and not (hours[0] <= slot_start.hour <= hours[1]):
        return False
    return True
