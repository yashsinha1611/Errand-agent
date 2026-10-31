"""Scenario harness. Every scenario gets a fresh world on disk and a fake clock.

A restart throws away every in-memory object and rebuilds the agent on the same
SQLite files, which is what a process restart does.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable

from errand.agent import InjectedCrash
from errand.clock import FakeClock
from errand.config import Settings
from errand.domain import TurnResult, UserInput
from errand.factory import World, build
from errand.llm.base import Model
from errand.llm.rules import RuleModel
from errand.mock.provider import MockConfig


@dataclass
class Check:
    desc: str
    ok: bool


@dataclass
class Scenario:
    id: str
    title: str
    covers: list[str]
    expected: str
    run: Callable[["Harness"], str]
    mock: dict = field(default_factory=dict)
    caps: dict = field(default_factory=dict)
    model: Callable[[], Model] | None = None


class Harness:
    def __init__(self, scenario_id: str, mock: MockConfig | None = None, caps: dict | None = None,
                 model: Model | None = None, seed: int = 11) -> None:
        self.dir = tempfile.mkdtemp(prefix=f"errand-{scenario_id}-")
        self.clock = FakeClock()
        settings = Settings(data_dir=self.dir)
        self.settings = settings.with_caps(**caps) if caps else settings
        self.mock_config = mock or MockConfig(mode="calm")
        self.model = model or RuleModel()
        self.seed = seed
        self.session = "sess-1"
        self._sessions = 1
        self.results: list[TurnResult] = []
        self.events: list[dict] = []
        self.checks: list[Check] = []
        self.crashes = 0
        self.world: World = self._build()

    # ------------------------------------------------------------------ lifecycle
    def _build(self) -> World:
        return build(self.settings, clock=self.clock, mock_config=self.mock_config, model=self.model,
                     seed=self.seed)

    @property
    def agent(self):
        return self.world.agent

    @property
    def mock(self):
        return self.world.provider

    def restart(self) -> None:
        self.world.close()
        self.world = self._build()

    def new_session(self) -> None:
        self._sessions += 1
        self.session = f"sess-{self._sessions}"

    def cleanup(self) -> None:
        try:
            self.world.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------------ talking
    def say(self, text: str) -> TurnResult:
        r = self.agent.handle(self.session, UserInput(text=text), on_event=self.events.append)
        self.results.append(r)
        return r

    def press(self, kind: str = "confirm") -> TurnResult:
        appr = self.last_approval_card()
        assert appr is not None, "no approval card to press"
        label = "Book" if kind == "confirm" else "Don't book"
        r = self.agent.handle(self.session, UserInput(kind=kind, text=label, approval_id=appr["id"]),
                              on_event=self.events.append)
        self.results.append(r)
        return r

    def resume(self) -> TurnResult:
        r = self.agent.handle(self.session, UserInput(kind="resume"), on_event=self.events.append)
        self.results.append(r)
        return r

    def crash_on(self, point: str, action: Callable[[], Any]) -> bool:
        """Arm a crash at `point`, run `action`, and restart if the process 'died'."""
        self.mock.arm("agent", f"crash:{point}")
        try:
            action()
            crashed = False
        except InjectedCrash:
            crashed = True
            self.crashes += 1
        if not crashed:  # the action never reached the crash point: disarm so it can't fire later
            while self.mock.take_fault("agent"):
                pass
        self.restart()
        return crashed

    def last_approval_card(self) -> dict | None:
        for r in reversed(self.results):
            if r.approval:
                return r.approval
        return None

    # ------------------------------------------------------------------ assertions
    def expect(self, ok: bool, desc: str) -> bool:
        self.checks.append(Check(desc, bool(ok)))
        return bool(ok)

    # ------------------------------------------------------------------ inspection
    def bookings(self) -> list[dict]:
        """Bookings the agent created (seeded history has no idempotency key)."""
        return [b for b in self.mock.all_bookings() if b["idem_key"]]

    def booking(self, booking_id: str) -> dict | None:
        return next((b for b in self.mock.all_bookings() if b["booking_id"] == booking_id), None)

    def calls(self, tool: str) -> list[dict]:
        return self.mock.calls(tool)

    def approvals(self) -> list[dict]:
        return [dict(r) for r in self.agent.db.q("SELECT * FROM approvals ORDER BY created_at")]

    def outbox(self) -> list[dict]:
        return [dict(r) for r in self.agent.db.q("SELECT * FROM outbox ORDER BY created_at")]

    def facts(self, status: str | None = None) -> list[dict]:
        rows = self.agent.db.q("SELECT * FROM memory_facts ORDER BY created_at")
        return [dict(r) for r in rows if status is None or r["status"] == status]

    def state(self):
        return self.agent.db.load_state(self.session, self.settings.user_id)

    def traces(self) -> list[dict]:
        out = []
        for path in sorted(glob.glob(os.path.join(self.dir, "traces", "*.jsonl"))):
            with open(path, encoding="utf-8") as fh:
                out.extend(json.loads(line) for line in fh if line.strip())
        return out

    def trace_file(self, session: str | None = None) -> str:
        return os.path.join(self.dir, "traces", f"{session or self.session}.jsonl")

    def tool_calls(self, name: str) -> list[dict]:
        return [e for e in self.traces() if e["type"] == "tool_call" and e.get("name") == name]
