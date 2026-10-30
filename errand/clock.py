"""Injectable clocks.

Every component reads time through a Clock, never ``datetime.now()``. That is what
makes hold expiry, retry backoff and the wall-clock budget testable without sleeping.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Clock:
    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def advance(self, minutes: float = 0, seconds: float = 0) -> None:  # pragma: no cover
        raise NotImplementedError


class SystemClock(Clock):
    """Wall-clock time in IST plus a demo offset.

    ``latency_scale`` scales simulated provider latency: 1.0 makes the demo feel real,
    0 makes the CLI instant. ``advance`` moves the agent's notion of time forward,
    which is how the demo shows a hold expiring without waiting ten minutes.
    """

    def __init__(self, latency_scale: float = 1.0) -> None:
        self.latency_scale = latency_scale
        self._offset = timedelta(0)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return datetime.now(IST) + self._offset

    def sleep(self, seconds: float) -> None:
        if seconds > 0 and self.latency_scale > 0:
            time.sleep(seconds * self.latency_scale)

    def advance(self, minutes: float = 0, seconds: float = 0) -> None:
        with self._lock:
            self._offset += timedelta(minutes=minutes, seconds=seconds)


class FakeClock(Clock):
    """Deterministic clock for tests: ``sleep`` advances time instantly."""

    DEFAULT_START = datetime(2026, 11, 2, 8, 40, tzinfo=IST)  # a Monday

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or self.DEFAULT_START

    def now(self) -> datetime:
        # Strictly increasing by one microsecond per read, so trace timestamps order events
        # exactly (two reads never tie), while staying fully deterministic.
        self._now += timedelta(microseconds=1)
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += timedelta(seconds=seconds)

    def advance(self, minutes: float = 0, seconds: float = 0) -> None:
        self._now += timedelta(minutes=minutes, seconds=seconds)
