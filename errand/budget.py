"""Hard caps on everything that can loop or cost money."""
from __future__ import annotations

from datetime import timedelta

from .clock import Clock
from .config import Caps
from .domain import ErrandState


class BudgetExhausted(Exception):
    def __init__(self, kind: str, used: float, cap: float) -> None:
        super().__init__(f"{kind} budget exhausted ({used}/{cap})")
        self.kind = kind
        self.used = used
        self.cap = cap


LABELS = {
    "model_calls": "model calls for this message",
    "tool_calls": "provider calls for this message",
    "steps": "workflow steps for this message",
    "wall_clock": "time for this message",
    "session_model_calls": "model calls for this conversation",
    "session_tool_calls": "provider calls for this conversation",
    "session_cost": "spend for this conversation",
}


class Budget:
    """One per turn. Also charges the session-level counters kept in state."""

    def __init__(self, caps: Caps, clock: Clock, state: ErrandState, on_charge=None) -> None:
        self.caps = caps
        self.clock = clock
        self.state = state
        self.deadline = clock.now() + timedelta(seconds=caps.wall_seconds_per_turn)
        self.used = {"model_calls": 0, "tool_calls": 0, "steps": 0}
        self.on_charge = on_charge

    def _check(self, kind: str, used: float, cap: float) -> None:
        if used > cap:
            if self.on_charge:
                self.on_charge(kind, used, cap)
            raise BudgetExhausted(kind, used, cap)

    def check_time(self) -> None:
        if self.clock.now() > self.deadline:
            raise BudgetExhausted("wall_clock", self.caps.wall_seconds_per_turn, self.caps.wall_seconds_per_turn)

    def charge_model(self, cost_usd: float = 0.0) -> None:
        self.check_time()
        self.used["model_calls"] += 1
        self.state.model_calls += 1
        self.state.cost_usd = round(self.state.cost_usd + cost_usd, 6)
        self._check("model_calls", self.used["model_calls"], self.caps.model_calls_per_turn)
        self._check("session_model_calls", self.state.model_calls, self.caps.model_calls_per_session)
        self._check("session_cost", self.state.cost_usd, self.caps.cost_usd_per_session)

    def add_cost(self, cost_usd: float) -> None:
        self.state.cost_usd = round(self.state.cost_usd + cost_usd, 6)
        self._check("session_cost", self.state.cost_usd, self.caps.cost_usd_per_session)

    def charge_tool(self) -> None:
        self.check_time()
        self.used["tool_calls"] += 1
        self.state.tool_calls += 1
        self._check("tool_calls", self.used["tool_calls"], self.caps.tool_calls_per_turn)
        self._check("session_tool_calls", self.state.tool_calls, self.caps.tool_calls_per_session)

    def charge_step(self) -> None:
        self.check_time()
        self.used["steps"] += 1
        self._check("steps", self.used["steps"], self.caps.steps_per_turn)
