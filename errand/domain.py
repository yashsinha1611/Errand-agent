"""Typed core of the agent: intents, state, approvals, results."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .util import sha256, truncate

SERVICES: dict[str, str] = {
    "plumber": "plumber",
    "electrician": "electrician",
    "ac_servicing": "AC servicing",
    "cleaning": "home cleaning",
    "carpenter": "carpenter",
}
AREAS = ["Indiranagar", "Koramangala", "HSR Layout", "Whitefield"]


# --------------------------------------------------------------------------- untrusted text
class UntrustedText:
    """Free text that came from a tool (reviews, descriptions).

    It deliberately has no ``__str__`` that returns the content, so it cannot be
    concatenated into a prompt or a reply by accident. Code that needs the text must
    call ``for_display`` (sanitised, truncated) or ``raw`` (screening only).
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = str(value)

    def __str__(self) -> str:
        return "[untrusted text]"

    __repr__ = __str__

    def raw(self) -> str:
        return self._value

    def for_display(self, limit: int = 280) -> str:
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", self._value)  # drop link targets
        text = re.sub(r"https?://\S+", "[link removed]", text)
        text = re.sub(r"[\x00-\x1f\x7f`*_#>]", " ", text)
        return truncate(re.sub(r"\s+", " ", text).strip(), limit)

    def digest(self) -> str:
        return sha256(self._value)[:12]


def contains_untrusted(obj: Any) -> bool:
    """Deep scan used by prompt builders to refuse untrusted text."""
    if isinstance(obj, UntrustedText):
        return True
    if isinstance(obj, dict):
        return any(contains_untrusted(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(contains_untrusted(v) for v in obj)
    return False


# --------------------------------------------------------------------------- user turn provenance
_TURN_TOKEN = object()


@dataclass(frozen=True)
class UserTurnRef:
    """Proof that a write was caused by a user message.

    Only ``AgentService`` creates these (it holds the private token). Memory writes and
    approval grants require one, so a tool result has no path to either.
    """

    session_id: str
    turn: int
    _token: object = field(repr=False, compare=False, default=None)

    @classmethod
    def issue(cls, session_id: str, turn: int, token: object) -> "UserTurnRef":
        if token is not _TURN_TOKEN:
            raise PermissionError("UserTurnRef can only be issued by the agent service")
        return cls(session_id, turn, token)

    def is_valid(self) -> bool:
        return self._token is _TURN_TOKEN


def turn_token() -> object:
    return _TURN_TOKEN


# --------------------------------------------------------------------------- intents
INTENT_KINDS = (
    "start_task", "provide_details", "select", "confirm", "decline", "memory",
    "ask_reviews", "list_bookings", "greeting", "abort", "unclear",
)
DATE_EXPR = re.compile(r"^(today|tomorrow|day_after_tomorrow|this_week|next_week|weekend|"
                       r"weekday:(mon|tue|wed|thu|fri|sat|sun)|date:\d{4}-\d{2}-\d{2})$")
TIME_EXPR = re.compile(r"^(morning|afternoon|evening|any|after:\d{2}:\d{2}|before:\d{2}:\d{2}|at:\d{2}:\d{2})$")
MEMORY_KEYS = ("home_area", "earliest_time", "preferred_technician")

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(INTENT_KINDS),
                 "description": "What the user is doing with this message."},
        "task": {"type": ["string", "null"], "enum": ["book", "reschedule", "cancel", None]},
        "service": {"type": ["string", "null"], "enum": [*SERVICES.keys(), None]},
        "area": {"type": ["string", "null"], "enum": [*AREAS, None]},
        "date_expr": {"type": ["string", "null"],
                      "description": "today | tomorrow | day_after_tomorrow | this_week | next_week | weekend | "
                                     "weekday:sat | date:YYYY-MM-DD"},
        "time_expr": {"type": ["string", "null"],
                      "description": "morning | afternoon | evening | any | after:HH:MM | before:HH:MM | at:HH:MM"},
        "budget_inr": {"type": ["integer", "null"], "minimum": 0},
        "no_budget": {"type": "boolean"},
        "provider_ref": {"type": ["string", "null"],
                         "description": "Provider name as the user said it, 'last_time' for 'same as last time', "
                                        "or 'that' for 'them / that one'."},
        "option_index": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
        "booking_ref": {"type": ["string", "null"], "description": "Booking id like B-1990, if the user gave one."},
        "memory_op": {"type": ["string", "null"], "enum": ["remember", "forget", "view", "correct", None]},
        "memory_key": {"type": ["string", "null"], "enum": [*MEMORY_KEYS, None]},
        "memory_value": {"type": ["string", "null"]},
        "is_correction": {"type": "boolean"},
    },
    "required": ["kind"],
}


class IntentError(ValueError):
    pass


@dataclass
class Intent:
    kind: str
    task: str | None = None
    service: str | None = None
    area: str | None = None
    date_expr: str | None = None
    time_expr: str | None = None
    budget_inr: int | None = None
    no_budget: bool = False
    provider_ref: str | None = None
    option_index: int | None = None
    booking_ref: str | None = None
    memory_op: str | None = None
    memory_key: str | None = None
    memory_value: str | None = None
    is_correction: bool = False

    def has_details(self) -> bool:
        return any(v is not None for v in (self.service, self.area, self.date_expr, self.time_expr,
                                           self.budget_inr, self.provider_ref)) or self.no_budget

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, False)}


def parse_intent(data: Any) -> Intent:
    """Validate model output. Anything off-schema raises IntentError (then repair or 'unclear')."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise IntentError(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IntentError("intent must be an object")
    allowed = {f.name for f in fields(Intent)}
    unknown = set(data) - allowed
    if unknown:
        raise IntentError(f"unknown fields: {sorted(unknown)}")
    kind = data.get("kind")
    if kind not in INTENT_KINDS:
        raise IntentError(f"bad kind: {kind!r}")
    clean: dict[str, Any] = {"kind": kind}

    def opt(name: str, check) -> None:
        v = data.get(name)
        if v is None:
            return
        if not check(v):
            raise IntentError(f"bad {name}: {v!r}")
        clean[name] = v

    opt("task", lambda v: v in ("book", "reschedule", "cancel"))
    opt("service", lambda v: v in SERVICES)
    opt("area", lambda v: v in AREAS)
    opt("date_expr", lambda v: isinstance(v, str) and DATE_EXPR.match(v))
    opt("time_expr", lambda v: isinstance(v, str) and TIME_EXPR.match(v))
    opt("budget_inr", lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1_000_000)
    opt("provider_ref", lambda v: isinstance(v, str) and 0 < len(v) <= 60)
    opt("option_index", lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10)
    opt("booking_ref", lambda v: isinstance(v, str) and re.match(r"^B-\d{3,6}$", v))
    opt("memory_op", lambda v: v in ("remember", "forget", "view", "correct"))
    opt("memory_key", lambda v: v in MEMORY_KEYS)
    opt("memory_value", lambda v: isinstance(v, str) and len(v) <= 60)
    for flag in ("no_budget", "is_correction"):
        v = data.get(flag, False)
        if not isinstance(v, bool):
            raise IntentError(f"bad {flag}: {v!r}")
        clean[flag] = v
    return Intent(**clean)


# --------------------------------------------------------------------------- inputs and results
@dataclass
class UserInput:
    kind: str = "message"            # message | confirm | decline (buttons)
    text: str = ""
    approval_id: str | None = None


@dataclass
class TurnResult:
    text: str
    options: list[dict] = field(default_factory=list)
    approval: dict | None = None
    chips: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)   # booking ids the reply reports as done
    outcome: str = "replied"


# --------------------------------------------------------------------------- approvals
@dataclass
class Approval:
    id: str
    user_id: str
    session_id: str
    action: str                      # book | reschedule | cancel
    provider_id: str
    provider_name: str
    slot_start: str
    slot_end: str
    price: int
    fingerprint: str
    status: str = "pending"          # pending | granted | consumed | void | declined | expired
    search_price: int | None = None
    budget: int | None = None
    booking_id: str | None = None
    hold_id: str | None = None
    hold_expires: str | None = None
    prompt_text: str = ""
    proposed_turn: int = 0
    decided_turn: int | None = None
    void_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""


# --------------------------------------------------------------------------- session state
@dataclass
class ErrandState:
    """Explicit workflow state. Persisted after every turn; the model never holds it."""

    session_id: str
    user_id: str
    version: int = 0
    turn: int = 0
    task: str | None = None              # book | reschedule | cancel
    phase: str = "idle"
    # Slots: each is {"value": ..., "source": "user"|"memory"|"history", "turn": n}
    slots: dict[str, dict] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    search_raw: list[dict] = field(default_factory=list)
    offers: list[dict] = field(default_factory=list)
    selection: dict | None = None
    hold: dict | None = None
    pending_approval_id: str | None = None
    pending_question: dict | None = None     # {"kind": ..., ...}
    target_booking: dict | None = None
    booking_choices: list[dict] = field(default_factory=list)
    slot_choices: list[dict] = field(default_factory=list)
    result_booking_id: str | None = None
    focus_provider: dict | None = None       # last provider discussed, for "them"
    known_providers: list[dict] = field(default_factory=list)   # {"id","name","service"} seen this session
    excluded_slots: list[str] = field(default_factory=list)     # slots that failed this task (taken, conflict)
    unclear_streak: int = 0
    memory_notes: list[str] = field(default_factory=list)  # facts asked about this session
    # Session-level budget counters
    model_calls: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0

    def slot(self, name: str) -> Any:
        s = self.slots.get(name)
        return None if s is None else s.get("value")

    def set_slot(self, name: str, value: Any, source: str, turn: int, **extra: Any) -> None:
        self.slots[name] = {"value": value, "source": source, "turn": turn, **extra}

    def clear_task(self) -> None:
        keep = {"session_id", "user_id", "version", "turn", "model_calls", "tool_calls", "cost_usd",
                "memory_notes", "focus_provider", "known_providers", "result_booking_id"}
        fresh = ErrandState(self.session_id, self.user_id)
        for f in fields(self):
            if f.name not in keep:
                setattr(self, f.name, getattr(fresh, f.name))

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(text: str) -> "ErrandState":
        data = json.loads(text)
        known = {f.name for f in fields(ErrandState)}
        return ErrandState(**{k: v for k, v in data.items() if k in known})
