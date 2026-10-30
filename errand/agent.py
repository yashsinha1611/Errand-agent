"""AgentService: one turn = recover → interpret → dispatch → advance → reply.

Control flow lives here, in code. The model only turns words into an Intent; the
provider is only reached through ProviderClient; every write goes through the
approval gate, the outbox and a status check.
"""
from __future__ import annotations

import json
import random
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Callable

from . import narrate, policy, quarantine
from .approval import ApprovalGate, ApprovalRequired, fingerprint
from .budget import LABELS as BUDGET_LABELS
from .budget import Budget, BudgetExhausted
from .clock import Clock
from .config import Settings
from .domain import AREAS, SERVICES, Approval, ErrandState, Intent, TurnResult, UserInput, UserTurnRef, turn_token
from .interpreter import Interpreter
from .llm.anthropic import AnthropicModel
from .llm.base import Model
from .llm.rules import YES
from .memory import MemoryStore, label_for
from .mock.provider import MockProvider
from .provider_client import ProviderClient, Result
from .storage import AgentDB
from .timeparse import resolve_time
from .tracing import Tracer, scrub
from .util import IdFactory, fmt_day, fmt_inr, fmt_short_slot, fmt_slot, parse_iso, sha256


class InjectedCrash(BaseException):
    """Fault injection: the process 'dies' at a chosen point. BaseException so nothing swallows it."""

    def __init__(self, point: str) -> None:
        super().__init__(point)
        self.point = point


class ProviderFailure(RuntimeError):
    """An unexpected provider error; handled by the top-level exit handler."""


# --------------------------------------------------------------------------- per-turn context
class Step:
    def __init__(self, label: str) -> None:
        self.label = label
        self.status: str | None = None
        self.final: str | None = None
        self.detail: Any = None

    def done(self, label: str | None = None, detail: Any = None) -> None:
        self.status, self.final, self.detail = "done", label, detail

    def warn(self, label: str | None = None, detail: Any = None) -> None:
        self.status, self.final, self.detail = "warn", label, detail

    def fail(self, label: str | None = None, detail: Any = None) -> None:
        self.status, self.final, self.detail = "error", label, detail


class TurnContext:
    def __init__(self, agent: "AgentService", state: ErrandState, trace, budget: Budget, by: UserTurnRef,
                 on_event: Callable[[dict], None] | None) -> None:
        self.agent = agent
        self.state = state
        self.trace = trace
        self.budget = budget
        self.by = by
        self.on_event = on_event
        self.parts: list[str] = []
        self.options: list[dict] = []
        self.approval: dict | None = None
        self.chips: list[str] = []
        self.claims: list[str] = []
        self.outcome = "replied"
        self.flags: set[str] = set()
        self.last_tool_ms = 0
        self._sid = 0
        self._last_view: dict = {}

    def say(self, text: str | None) -> None:
        if text:
            self.parts.append(text)

    def ui(self, event: dict) -> None:
        if self.on_event:
            self.on_event(event)

    @contextmanager
    def step(self, label: str):
        self._sid += 1
        sid = f"s{self._sid}"
        st = Step(label)
        t0 = self.agent.clock.now()
        self.ui({"type": "step", "id": sid, "status": "running", "label": label})
        try:
            yield st
        except BaseException as exc:
            ms = round((self.agent.clock.now() - t0).total_seconds() * 1000)
            stopped = isinstance(exc, InjectedCrash)
            self.ui({"type": "step", "id": sid, "status": "stopped" if stopped else "error", "ms": ms,
                     "label": "Interrupted: the agent process stopped" if stopped else (st.final or f"Failed: {label}")})
            raise
        ms = round((self.agent.clock.now() - t0).total_seconds() * 1000)
        self.ui({"type": "step", "id": sid, "status": st.status or "done", "label": st.final or label, "ms": ms,
                 "detail": scrub(st.detail) if st.detail is not None else None})


# --------------------------------------------------------------------------- the service
class AgentService:
    def __init__(self, settings: Settings, db: AgentDB, provider: MockProvider, model: Model, clock: Clock,
                 ids: IdFactory, trace_dir: str) -> None:
        self.settings = settings
        self.db = db
        self.provider = provider
        self.client = ProviderClient(provider, clock)
        self.clock = clock
        self.ids = ids
        self.gate = ApprovalGate(db, clock, ids, settings.approval_ttl_min)
        self.memory = MemoryStore(db, clock, ids)
        self.interpreter = Interpreter(model)
        self.model = model
        self.review_model = model if isinstance(model, AnthropicModel) else None
        self.tracer = Tracer(trace_dir, clock, settings.trace_capture_content)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.user_id = settings.user_id

    # ================================================================== public API
    def handle(self, session_id: str, inp: UserInput, on_event: Callable[[dict], None] | None = None) -> TurnResult:
        with self._locks[self.user_id]:
            state = self.db.load_state(session_id, self.user_id)
            state.turn += 1
            # Persist the turn number before doing anything: if this turn crashes, the next one
            # must not reuse its number, or the trace attributes this turn's actions to the next.
            self.db.save_state(state, self.clock.now().isoformat())
            trace = self.tracer.run(session_id, state.turn, on_event)
            by = UserTurnRef.issue(session_id, state.turn, turn_token())
            budget = Budget(self.settings.caps, self.clock, state,
                            on_charge=lambda k, u, c: trace.emit("budget", kind=k, used=u, cap=c))
            ctx = TurnContext(self, state, trace, budget, by, on_event)
            t0 = self.clock.now()
            trace.emit("turn_start", input_kind=inp.kind, text=inp.text)
            if inp.kind != "resume":
                self.db.add_message(session_id, state.turn, "user", inp.text or f"[{inp.kind}]", [], t0.isoformat())
            try:
                self._drop_forgotten(ctx)
                self._recover(ctx)
                if inp.kind != "resume":
                    self._dispatch(ctx, inp)
            except BudgetExhausted as exc:
                ctx.parts = [narrate.budget_exit(BUDGET_LABELS.get(exc.kind, exc.kind), self._truth(state))]
                ctx.options, ctx.approval, ctx.outcome = [], None, "budget_exhausted"
            except InjectedCrash:
                raise
            except Exception as exc:  # noqa: BLE001 - the one deliberate catch-all, at the boundary
                trace.emit("error", error_type=type(exc).__name__, message=str(exc)[:300])
                ctx.parts = [narrate.unexpected_exit(type(exc).__name__, self._truth(state))]
                ctx.options, ctx.approval, ctx.outcome = [], None, "error"
            text = "\n\n".join(ctx.parts).strip()
            if not text and inp.kind != "resume":
                text = "Okay."
            now = self.clock.now()
            self.db.save_state(state, now.isoformat())
            if text:
                self.db.add_message(session_id, state.turn, "agent", text, ctx.claims, now.isoformat())
            trace.emit("turn_end", outcome=ctx.outcome, reply=text, claims=ctx.claims,
                       latency_ms=round((now - t0).total_seconds() * 1000))
            return TurnResult(text, ctx.options, ctx.approval, ctx.chips, ctx.claims, ctx.outcome)

    def has_open_writes(self, session_id: str) -> bool:
        return bool(self.gate.outbox_rows(session_id, ("SENT", "UNRESOLVED")))

    def snapshot(self, session_id: str) -> dict:
        state = self.db.load_state(session_id, self.user_id)
        pending = self.gate.pending(session_id)
        return {"state": self.state_view(state), "memory": self.memory.view(self.user_id),
                "outbox": self._outbox_view(session_id), "transcript": self.db.transcript(session_id),
                "approval": narrate.approval_card(pending, (state.target_booking or {}).get("slot_start")
                                                  if pending and pending.action == "reschedule" else None)
                if pending else None,
                "clock": f"{fmt_day(self.clock.now())}, {self.clock.now():%H:%M}", "faults": self.provider.armed_faults(),
                "model": getattr(self.model, "name", "?")}

    # ================================================================== dispatch
    def _dispatch(self, ctx: TurnContext, inp: UserInput) -> None:
        s = ctx.state
        via_button = inp.kind in ("confirm", "decline") and inp.approval_id is not None
        if via_button:
            intent = Intent(kind=inp.kind)
        else:
            intent = self.interpreter.interpret(ctx, self._context(ctx), inp.text)
        s.unclear_streak = s.unclear_streak + 1 if intent.kind == "unclear" else 0
        handler = getattr(self, f"_on_{intent.kind}")
        handler(ctx, intent, inp, via_button)

    def _context(self, ctx: TurnContext) -> dict:
        s = ctx.state
        pending = self.gate.pending(s.session_id)
        options: list[dict] = []
        if s.pending_question and s.pending_question.get("kind") == "choose_booking":
            options = [{"index": i, "name": b["provider_name"], "slot": fmt_short_slot(b["slot_start"]),
                        "price": b["price"], "hour": parse_iso(b["slot_start"]).hour}
                       for i, b in enumerate(s.booking_choices, 1)]
        elif s.phase == "choosing" and s.slot_choices:
            name = (s.target_booking or {}).get("provider_name", "")
            options = [{"index": i, "name": name, "slot": fmt_short_slot(c["slot_start"]), "price": None,
                        "hour": parse_iso(c["slot_start"]).hour} for i, c in enumerate(s.slot_choices, 1)]
        elif s.phase == "choosing" and s.offers:
            options = [{"index": i, "name": o["name"], "slot": fmt_short_slot(o["slot_start"]), "price": o["price"],
                        "hour": parse_iso(o["slot_start"]).hour}
                       for i, o in enumerate(s.offers[: self.settings.max_options_shown], 1)]
        names: list[str] = []
        for n in [o["name"] for o in options] + [p["name"] for p in s.known_providers]:
            if n and n not in names:
                names.append(n)
        now = self.clock.now()
        known = {}
        for k, v in s.slots.items():
            if k == "provider":
                continue
            known[k] = v.get("label") or v.get("value")
        return {
            "today": now.date().isoformat(), "now": now.strftime("%H:%M"), "phase": s.phase, "task": s.task,
            "known": known, "awaiting_approval": pending is not None,
            "approval": {"action": pending.action, "provider": pending.provider_name,
                         "when": fmt_slot(pending.slot_start), "price": pending.price} if pending else None,
            "pending_question": (s.pending_question or {}).get("kind"),
            "options": options, "provider_names": names[:24],
        }

    # ================================================================== simple intents
    def _on_greeting(self, ctx, intent, inp, via_button) -> None:
        ctx.say("Hi! " + narrate.help_text())
        ctx.say(self._current_question(ctx))

    def _on_unclear(self, ctx, intent, inp, via_button) -> None:
        s = ctx.state
        if s.unclear_streak >= self.settings.caps.unclear_in_a_row:
            self._void_pending(ctx, "the conversation stalled")
            s.clear_task()
            s.unclear_streak = 0
            self._set_phase(ctx, "idle")
            ctx.outcome = "unclear_limit"
            ctx.say("I couldn’t follow the last few messages, so I’ve stopped this request. Nothing was booked. "
                    + narrate.help_text())
            return
        question = self._current_question(ctx)
        ctx.say("Sorry, I didn’t catch that. " + (question or "Tell me what you need done, for example "
                                                   "“find an electrician in HSR Layout tomorrow morning”."))

    def _on_abort(self, ctx, intent, inp, via_button) -> None:
        s = ctx.state
        self._void_pending(ctx, "you stopped the request")
        s.clear_task()
        self._set_phase(ctx, "idle")
        ctx.say("Okay, I’ve stopped. Nothing was booked.")

    def _on_list_bookings(self, ctx, intent, inp, via_button) -> None:
        rows = self._list_bookings(ctx, "Looking up your bookings")
        if rows is None:
            return
        now = self.clock.now()
        upcoming = [b for b in rows if b["status"] == "booked" and parse_iso(b["slot_start"]) > now]
        if not upcoming:
            ctx.say("You have no upcoming bookings.")
            return
        lines = [f"{b['booking_id']}: {b['provider_name']} ({narrate.service_label(b['service'])}), "
                 f"{fmt_slot(b['slot_start'])}, {fmt_inr(b['price'])}" for b in upcoming]
        ctx.say("Your upcoming bookings:\n" + "\n".join(lines))

    # ================================================================== memory
    def _on_memory(self, ctx, intent, inp, via_button) -> None:
        s, op = ctx.state, intent.memory_op
        if op == "view" or op is None:
            facts = self.memory.view(self.user_id)
            if not facts:
                ctx.say("I don’t have anything saved about you yet. I only keep what you ask me to remember, "
                        "or what you confirm.")
            else:
                lines = []
                for f in facts:
                    how = ("you told me" if f["source"] == "stated" else
                           "I inferred this and you confirmed it" if f["confirmed"] else
                           "I inferred this from your bookings and won’t use it until you confirm")
                    lines.append(f"{f['key']}: {f['value']} ({how})")
                ctx.say("Here’s what I remember:\n" + "\n".join(lines) +
                        "\n\nSay “forget my area” or correct anything that’s wrong.")
            self._push_memory(ctx)
            return
        if op in ("remember", "correct"):
            key, value = intent.memory_key, (intent.memory_value or "").strip()
            if key == "home_area":
                match = next((a for a in AREAS if a.lower() == value.lower()), None)
                if not match:
                    ctx.say(f"I can only save one of these areas: {', '.join(AREAS)}.")
                    return
                value = match
            elif key == "earliest_time":
                if not (len(value) == 5 and value[2] == ":" and value[:2].isdigit()):
                    ctx.say("Tell me the earliest time like “never before 10 a.m.”.")
                    return
            elif key == "preferred_technician":
                prov = policy.match_provider_name(value, s.known_providers)
                if not prov:
                    ctx.say(f"I don’t know a provider called {value} yet. Search for them first, then ask me to "
                            "remember them.")
                    return
                key, value = f"preferred_technician:{prov['service']}", prov["name"]
            else:
                ctx.say("I can remember your area, the earliest time you want visits, and preferred providers.")
                return
            with ctx.step("Saving to memory") as st:
                fact = self.memory.remember(ctx.trace, self.user_id, key, value, ctx.by,
                                            evidence=f"You said so on {fmt_day(self.clock.now())}.")
                st.done("Saved as stated by you", detail={"key": key, "value": value, "source": "stated",
                                                           "fact_id": fact.id, "write_requires": "a user turn"})
            ctx.say(f"Saved: {label_for(key).lower()} is {value}. I’ll use it next time unless you change it.")
            self._push_memory(ctx)
            if key == "home_area" and s.task == "book" and s.slot("area") is None:
                s.set_slot("area", value, "user", s.turn)
                self._advance(ctx)
            return
        if op == "forget":
            if intent.memory_key == "preferred_technician":
                keys = [f"preferred_technician:{intent.memory_value}"] if intent.memory_value in SERVICES \
                    else ["preferred_technician:"]
            elif intent.memory_key:
                keys = [intent.memory_key]
            else:
                keys = ["home_area", "earliest_time", "preferred_technician:"]
            with ctx.step("Forgetting") as st:
                n = sum(self.memory.forget(ctx.trace, self.user_id, k, ctx.by) for k in keys)
                st.done(f"Forgot {n} fact{'s' if n != 1 else ''} and blocked re-inference",
                        detail={"keys": keys, "tombstoned": True})
            for name, slot in list(s.slots.items()):
                fk = slot.get("fact_key") or ""
                if slot.get("source") == "memory" and any(fk == k or (k.endswith(":") and fk.startswith(k)) for k in keys):
                    del s.slots[name]
                    s.search_raw, s.offers, s.selection = [], [], None
            what = ", ".join(label_for(k.rstrip(":")).lower() if not k.endswith(":") else "preferred providers"
                             for k in keys)
            ctx.say(f"Done. I’ve forgotten {what}, and I won’t work it out again from your booking history."
                    if n else "There was nothing saved for that.")
            self._push_memory(ctx)
            if s.task:
                ctx.say(self._current_question(ctx))

    # ================================================================== reviews (untrusted text)
    def _on_ask_reviews(self, ctx, intent, inp, via_button) -> None:
        s = ctx.state
        prov = self._resolve_provider(ctx, intent.provider_ref)
        if prov is None:
            ctx.say("Which provider do you mean? Tell me their name.")
            return
        with ctx.step(f"Fetching {prov['name']}’s profile") as st:
            res = self.client.get_provider(ctx, prov["id"])
            if res.ok:
                st.done(f"{len(res.value['reviews'])} reviews, {res.value['rating']} rating",
                        detail={"tool": "get_provider", "provider_id": prov["id"], "rating": res.value["rating"],
                                "reviews": len(res.value["reviews"]), "free_text": "wrapped as UntrustedText"})
            else:
                st.fail(f"Couldn’t load the profile ({res.code})")
        if not res.ok:
            self._raise_unexpected(res) if res.error_class == "UNEXPECTED" else ctx.say(
                "I couldn’t load that provider’s profile right now.")
            return
        reviews = res.value["reviews"]
        hits = quarantine.screen(reviews)
        with ctx.step("Screening review text") as st:
            for h in hits:
                ctx.trace.emit("injection_suspected", provider_id=prov["id"], review_hash=h["digest"], rule=h["rule"])
            if hits:
                st.warn(f"{len(hits)} review{'s' if len(hits) > 1 else ''} tried to give me instructions. "
                        "Treated as data", detail={"review": hits[0]["excerpt"], "matched_rule": hits[0]["rule"],
                                                    "effect": "none: review text never reaches the interpreter, "
                                                              "approvals or memory"})
            else:
                st.done("No instructions found in review text")
        with ctx.step("Summarising reviews into fixed themes") as st:
            summary = quarantine.summarise_with_model(self.review_model, ctx, reviews) if self.review_model else None
            if summary is None:
                summary = quarantine.summarise_offline(reviews, {h["index"] for h in hits})
            st.done("Themes: " + (", ".join(summary["themes"]) or "none clear"),
                    detail={"output_schema": "sentiment enum + themes from a fixed list", **summary})
        s.focus_provider = prov
        words = [quarantine.THEME_WORDS[t] for t in summary["themes"]]
        sentence = (f"{quarantine.SENTIMENT_WORDS[summary['sentiment']]} across {len(reviews)} reviews of "
                    f"{prov['name']}" + (f": people say they’re {narrate.join(words)}." if words else "."))
        if hits or summary.get("contains_instructions"):
            sentence += ("\n\nOne review contained instructions aimed at an AI assistant. I treated it as an ordinary "
                         "review: nothing was booked, approved or saved because of it.")
        ctx.say(sentence)
        ctx.chips = [f"Book {prov['name']}"]

    def _resolve_provider(self, ctx, ref: str | None) -> dict | None:
        s = ctx.state
        if ref in (None, "that", "last_time"):
            if s.focus_provider:
                return s.focus_provider
            if s.selection:
                return {"id": s.selection["provider_id"], "name": s.selection["name"], "service": s.slot("service")}
            return None
        found = policy.match_provider_name(ref, s.known_providers)
        if found:
            return found
        area = s.slot("area") or (self.memory.usable(self.user_id).get("home_area").value
                                  if "home_area" in self.memory.usable(self.user_id) else AREAS[0])
        today = self.clock.now().date()
        with ctx.step(f"Looking up “{ref}”") as st:
            for service in SERVICES:
                res = self.client.search_providers(ctx, service, area, (today.isoformat(),
                                                                         (today + timedelta(days=6)).isoformat()))
                if res.ok:
                    self._remember_providers(s, res.value, service)
                    found = policy.match_provider_name(ref, s.known_providers)
                    if found:
                        break
            st.done(f"Found {found['name']}" if found else f"No provider called {ref}")
        return found

    # ================================================================== tasks and details
    def _on_start_task(self, ctx, intent, inp, via_button) -> None:
        s = ctx.state
        self._void_pending(ctx, "you started a new request")
        focus = s.focus_provider
        s.clear_task()
        s.task = intent.task or "book"
        self._set_phase(ctx, "collecting")
        self._apply_details(ctx, intent)
        if intent.provider_ref == "that" and focus:
            s.set_slot("provider", {"id": focus["id"], "name": focus["name"]}, "user", s.turn)
            if s.slot("service") is None and focus.get("service"):
                s.set_slot("service", focus["service"], "user", s.turn)
        if intent.booking_ref:
            s.set_slot("booking_ref", intent.booking_ref, "user", s.turn)
        self._advance(ctx)

    def _on_provide_details(self, ctx, intent, inp, via_button) -> None:
        s = ctx.state
        if s.pending_question and s.pending_question.get("kind") in ("confirm_provider", "confirm_fact") \
                and not intent.has_details():
            ctx.say(self._current_question(ctx))
            return
        if s.task is None:
            s.task = "book"
            self._set_phase(ctx, "collecting")
        changed, corrected = self._apply_details(ctx, intent)
        inval = policy.invalidations(changed)
        if "approval" in inval:
            self._void_pending(ctx, f"you changed the {narrate.join(sorted(_LABEL.get(c, c) for c in changed))}")
        if "search" in inval:
            s.search_raw = []
        if "offers" in inval:
            s.offers = []
        if "selection" in inval:
            s.selection, s.hold, s.slot_choices = None, None, []
        if "selection_if_over_budget" in inval and s.selection and "price" in s.selection:
            budget = policy.budget_value(s)
            if budget is not None and s.selection["price"] > budget:
                self._void_pending(ctx, "the new budget is below this price")
                s.selection, s.hold = None, None
        if corrected:
            ctx.say(f"Changed the {narrate.join(sorted(_LABEL.get(c, c) for c in corrected))}. "
                    "Everything else stays the same.")
        if s.phase in ("awaiting_approval", "choosing", "done") and changed:
            self._set_phase(ctx, "collecting")
        self._advance(ctx)

    def _apply_details(self, ctx, intent: Intent) -> tuple[set[str], set[str]]:
        s = ctx.state
        values = policy.detail_values(intent, self.clock.now())
        if "provider" in values and values["provider"]["ref"] != "last_time":
            p = policy.match_provider_name(values["provider"]["ref"], s.known_providers)
            if p:
                values["provider"] = {"value": {"id": p["id"], "name": p["name"]}, "ref": values["provider"]["ref"]}
                if "service" not in values and s.slot("service") is None:
                    values["service"] = {"value": p["service"]}
        before = {k: s.slot(k) for k in values}
        changed = policy.apply_details(s, values, "user")
        corrected = {k for k in changed if before.get(k) is not None}
        if changed:
            self._push_state(ctx)
        return changed, corrected

    def _on_select(self, ctx, intent, inp, via_button) -> None:
        s = ctx.state
        idx = intent.option_index or 0
        if s.pending_question and s.pending_question.get("kind") == "choose_booking":
            if 1 <= idx <= len(s.booking_choices):
                s.target_booking = s.booking_choices[idx - 1]
                s.pending_question, s.booking_choices = None, []
                self._advance(ctx)
                return
        elif s.phase == "choosing" and s.slot_choices:
            if 1 <= idx <= len(s.slot_choices):
                s.selection = dict(s.slot_choices[idx - 1])
                s.slot_choices = []
                self._advance(ctx)
                return
        elif s.phase == "choosing" and s.offers:
            shown = s.offers[: self.settings.max_options_shown]
            if 1 <= idx <= len(shown):
                offer, honoured = policy.pick_slot(shown[idx - 1], intent, self.clock.now())
                if not honoured:
                    ctx.say(f"{offer['name']} isn’t free at that time, so I’ve taken their "
                            f"{fmt_short_slot(offer['slot_start'])} slot instead.")
                s.selection = offer
                s.focus_provider = {"id": offer["provider_id"], "name": offer["name"], "service": s.slot("service")}
                self._advance(ctx)
                return
        ctx.say("There’s nothing to pick from right now. " + self._current_question(ctx))

    # ================================================================== confirm / decline
    def _on_confirm(self, ctx, intent, inp, via_button) -> None:
        s = ctx.state
        pending = self.gate.pending(s.session_id)
        if inp.approval_id and (pending is None or pending.id != inp.approval_id):
            old = self.gate.get(inp.approval_id)
            if old and old.status == "consumed":
                self._already_done(ctx)
                return
            ctx.say("That approval is no longer current: it was replaced or voided. Use the latest one.")
            return
        if pending:
            if not via_button and not YES.search(inp.text or ""):
                ctx.trace.emit("approval", op="grant_refused", id=pending.id,
                               reason="no explicit yes in the user's own words")
                ctx.say("I need a clear yes from you to go ahead. " + pending.prompt_text)
                return
            if self.gate.is_expired(pending):
                self.gate.void(ctx.trace, pending.id, "approval older than the time limit")
                ctx.ui({"type": "approval_update", "id": pending.id, "status": "void",
                        "reason": "Voided: this approval was too old, so I re-checked the slot."})
                s.pending_approval_id, s.hold = None, None
                ctx.say("That approval had expired, so I re-checked the slot before booking anything.")
                self._advance(ctx)
                return
            appr = self.gate.grant(ctx.trace, pending.id, ctx.by)
            {"book": self._execute_book, "reschedule": self._execute_change,
             "cancel": self._execute_change}[appr.action](ctx, appr)
            return
        if s.pending_question:
            self._answer_question(ctx, True)
            return
        self._already_done(ctx)

    def _already_done(self, ctx) -> None:
        s = ctx.state
        open_rows = self.gate.outbox_rows(s.session_id, ("SENT", "UNRESOLVED"))
        last = self.gate.latest_consumed(s.session_id)
        with ctx.step("Looking for an open approval") as st:
            if open_rows:
                st.done("None open. A request is still being confirmed", detail={"write_calls_planned": 0})
            elif last and s.result_booking_id:
                st.done(f"None open. {last.id} was already used for {s.result_booking_id}",
                        detail={"pending_approval": None, "last_approval": {"id": last.id, "status": "consumed"},
                                "write_calls_planned": 0})
            else:
                st.done("No approval is open", detail={"pending_approval": None, "write_calls_planned": 0})
        ctx.trace.emit("approval", op="no_pending", id=last.id if last else None)
        if open_rows:
            ctx.say("I’m still confirming the last request, and I won’t send it twice.")
        elif last and s.result_booking_id:
            done = {"book": f"That’s already booked as {s.result_booking_id}. I haven’t booked it again.",
                    "reschedule": f"That change is already done for {s.result_booking_id}. I haven’t sent it again.",
                    "cancel": f"Booking {s.result_booking_id} is already cancelled. I haven’t sent it again."}
            ctx.say(done.get(last.action, done["book"]))
            ctx.claims.append(s.result_booking_id)
        else:
            ctx.say("There’s nothing waiting for your approval, so I haven’t done anything. "
                    + (self._current_question(ctx) or "Tell me what you’d like to book."))

    def _on_decline(self, ctx, intent, inp, via_button) -> None:
        s = ctx.state
        pending = self.gate.pending(s.session_id)
        if pending:
            self.gate.decline(ctx.trace, pending.id, ctx.by)
            ctx.ui({"type": "approval_update", "id": pending.id, "status": "declined"})
            s.pending_approval_id, s.hold = None, None
            if pending.action == "book":
                s.selection = None
                if s.offers:
                    self._set_phase(ctx, "choosing")
                    ctx.options = self._option_cards(s)
                    ctx.say("Okay, I won’t book it. The held slot will lapse on its own. Pick another option, or "
                            "tell me what to change.")
                else:
                    self._set_phase(ctx, "idle")
                    ctx.say("Okay, I won’t book it.")
            else:
                s.clear_task()
                self._set_phase(ctx, "idle")
                ctx.say("Okay, I’ve left the booking as it is.")
            return
        if s.pending_question:
            self._answer_question(ctx, False)
            return
        ctx.say("Okay. Nothing is waiting for your approval.")

    def _answer_question(self, ctx, yes: bool) -> None:
        s = ctx.state
        q = s.pending_question or {}
        s.pending_question = None
        kind = q.get("kind")
        if kind == "confirm_provider":
            if yes:
                s.set_slot("provider", {"id": q["provider_id"], "name": q["provider_name"]}, "history", s.turn,
                           ref="last_time")
            else:
                s.slots.pop("provider", None)
                ctx.say("Okay, I’ll look at everyone.")
            s.search_raw, s.offers = [], []
        elif kind == "confirm_fact":
            fact = self.memory.get(q["fact_id"])
            if fact and yes:
                self.memory.confirm(ctx.trace, fact.id, ctx.by)
                s.set_slot("provider", {"id": None, "name": fact.value}, "memory", s.turn, fact_id=fact.id,
                           fact_key=fact.key)
                ctx.trace.emit("memory_use", key=fact.key, fact_id=fact.id, source="inferred_confirmed")
                ctx.say(f"Got it. I’ll remember that you prefer {fact.value}.")
            elif fact:
                self.memory.reject(ctx.trace, fact.id, self.user_id, ctx.by)
                ctx.say(f"Okay, I won’t suggest {fact.value} again.")
            self._push_memory(ctx)
        elif kind == "choose_booking" and not yes:
            s.clear_task()
            self._set_phase(ctx, "idle")
            ctx.say("Okay, I’ve left your bookings as they are.")
            return
        self._advance(ctx)

    # ================================================================== booking workflow
    def _advance(self, ctx) -> None:
        if ctx.state.task == "book":
            self._advance_book(ctx)
        elif ctx.state.task in ("reschedule", "cancel"):
            self._advance_change(ctx)

    def _advance_book(self, ctx) -> None:
        s = ctx.state
        if s.pending_question:
            ctx.say(self._current_question(ctx))
            return
        self._prefill_from_memory(ctx)
        service = s.slot("service")
        prov = s.slots.get("provider")
        if prov and prov.get("ref") == "last_time" and not prov.get("value"):
            if not service:
                ctx.say("Which service was that for: plumber, electrician, AC servicing, cleaning or carpenter?")
                return
            last = self._last_provider_for(ctx, service)
            if last:
                s.pending_question = {"kind": "confirm_provider", "provider_id": last["provider_id"],
                                      "provider_name": last["provider_name"]}
                s.focus_provider = {"id": last["provider_id"], "name": last["provider_name"], "service": service}
                ctx.say(f"Last time it was {last['provider_name']} on {fmt_day(parse_iso(last['slot_start']))}. "
                        "Book them again?")
                ctx.chips = ["Yes", "No, show me others"]
                self._push_state(ctx)
                return
            s.slots.pop("provider", None)
            ctx.say(f"I couldn’t find a past {narrate.service_label(service)} booking, so I’ll look at everyone.")
        if service and not s.slots.get("provider"):
            fact = self.memory.pending_inferred(self.user_id, f"preferred_technician:{service}")
            if fact and fact.id not in s.memory_notes:
                s.memory_notes.append(fact.id)
                s.pending_question = {"kind": "confirm_fact", "fact_id": fact.id, "provider_name": fact.value}
                ctx.say(f"You’ve booked {fact.value} for {narrate.service_label(service)} more than once. "
                        "Should I look at them first?")
                ctx.chips = ["Yes, prefer them", "No, show everyone"]
                self._push_memory(ctx)
                return
        missing = policy.missing_fields(s)
        if missing:
            for m in missing:
                if m in policy.ASK_ONCE and m not in s.asked:
                    s.asked.append(m)
            self._set_phase(ctx, "collecting")
            with ctx.step("Checking what’s still missing") as st:
                st.done("Missing: " + ", ".join(_LABEL.get(m, m) for m in missing),
                        detail={"required": list(policy.REQUIRED_BOOK), "known": sorted(s.slots), "missing": missing})
            ctx.say(narrate.ask_missing(missing, s))
            return
        if not s.search_raw:
            self._set_phase(ctx, "searching")
            if not self._search(ctx):
                return
        self._resolve_provider_slot(ctx)
        pref = (s.slot("provider") or {}).get("id") if isinstance(s.slot("provider"), dict) else None
        now = self.clock.now()
        s.offers = policy.build_offers(s, now, prefer_provider_id=pref)
        if s.selection is None:
            within = [o for o in s.offers if o["within_budget"]]
            if not within:
                self._set_phase(ctx, "collecting")
                ctx.say(narrate.no_offers(s, s.offers))
                return
            s.offers = within
            expr = (s.slots.get("time_window") or {}).get("expr") or ""
            if pref and expr.startswith("at:") and len(within) == 1:
                s.selection = within[0]
            else:
                self._set_phase(ctx, "choosing")
                ctx.options = self._option_cards(s)
                ctx.say(narrate.offers_intro(ctx.options, s))
                return
        if s.hold is None or s.hold.get("slot_start") != s.selection["slot_start"]:
            self._hold_and_propose(ctx)

    def _search(self, ctx) -> bool:
        s = ctx.state
        service, area, dr = s.slot("service"), s.slot("area"), s.slot("date_range")
        with ctx.step(f"Searching {narrate.PLURAL.get(service, 'providers')} in {area}") as st:
            res = self.client.search_providers(ctx, service, area, (dr[0], dr[1]))
            if res.ok:
                s.search_raw = res.value
                offers = policy.build_offers(s, self.clock.now())
                fit = sum(1 for o in offers if o["within_budget"])
                st.done(f"{len(res.value)} found. {fit} fit your time and budget",
                        detail={"tool": "search_providers", "args": {"service": service, "area": area,
                                                                      "date_range": dr}, "returned": len(res.value),
                                "fitting": fit})
            else:
                st.fail(f"Search failed ({res.code})")
        if not res.ok:
            if res.error_class == "UNEXPECTED":
                self._raise_unexpected(res)
            ctx.say("I couldn’t search right now. Nothing was booked; try again in a moment.")
            return False
        self._remember_providers(s, res.value, service)
        return True

    def _resolve_provider_slot(self, ctx) -> None:
        s = ctx.state
        prov = s.slots.get("provider")
        if not prov:
            return
        value = prov.get("value") if isinstance(prov.get("value"), dict) else None
        if value and value.get("id"):
            return
        name = (value or {}).get("name") or prov.get("ref")
        if not name or name == "last_time":
            return
        hit = policy.match_provider_name(name, [{"id": p["id"], "name": p["name"], "service": s.slot("service")}
                                                for p in s.search_raw])
        if hit:
            prov["value"] = {"id": hit["id"], "name": hit["name"]}
        else:
            s.slots.pop("provider", None)
            ctx.say(f"{name} isn’t available for this search, so here are other options.")

    def _hold_and_propose(self, ctx) -> None:
        s = ctx.state
        sel = s.selection
        with ctx.step(f"Holding {fmt_short_slot(sel['slot_start'])} with {sel['name']}") as st:
            res = self.client.hold_slot(ctx, sel["provider_id"], sel["slot_start"])
            if res.ok:
                q = res.value["quoted_price"]
                detail = {"tool": "hold_slot", "args": {"provider_id": sel["provider_id"], "slot": sel["slot_start"]},
                          "result": res.value}
                if q != sel["price"]:
                    st.warn(f"Held. The quote is {fmt_inr(q)}, not {fmt_inr(sel['price'])}",
                            detail={**detail, "note": "Quoted price differs from the search price; the approval "
                                                      "shows both."})
                else:
                    st.done(f"Held at {fmt_inr(q)}, same as listed", detail=detail)
            elif res.error_class == "OPTION_GONE":
                st.warn("That slot is no longer available")
            else:
                st.fail(f"Hold failed ({res.code})")
        if not res.ok:
            if res.error_class != "OPTION_GONE":
                self._raise_unexpected(res)
            s.excluded_slots.append(policy.slot_key(sel["provider_id"], sel["slot_start"]))
            s.selection, s.hold = None, None
            ctx.say(f"{sel['name']}’s {fmt_short_slot(sel['slot_start'])} slot was just taken.")
            self._advance_book(ctx)
            return
        hv = res.value
        s.hold = {"hold_id": hv["hold_id"], "expires_at": hv["expires_at"], "quoted_price": hv["quoted_price"],
                  "slot_start": sel["slot_start"]}
        self._propose_book(ctx, search_price=sel["price"])

    def _propose_book(self, ctx, search_price: int | None, note: bool = True) -> Approval:
        s = ctx.state
        sel, hold = s.selection, s.hold
        end = (parse_iso(sel["slot_start"]) + timedelta(hours=1)).isoformat()
        appr = self.gate.propose(ctx.trace, user_id=self.user_id, session_id=s.session_id, action="book",
                                 provider_id=sel["provider_id"], provider_name=sel["name"],
                                 slot_start=sel["slot_start"], slot_end=end, price=hold["quoted_price"], turn=s.turn,
                                 search_price=search_price, budget=policy.budget_value(s), hold_id=hold["hold_id"],
                                 hold_expires=hold["expires_at"])
        appr.prompt_text = narrate.approval_prompt(appr)
        self.gate.set_prompt(appr.id, appr.prompt_text)
        s.pending_approval_id = appr.id
        self._set_phase(ctx, "awaiting_approval")
        if note:
            ctx.say(narrate.quote_note(appr, hold["expires_at"]))
        ctx.approval = narrate.approval_card(appr)
        return appr

    # ================================================================== executing a booking
    def _execute_book(self, ctx, appr: Approval) -> None:
        s = ctx.state
        now = self.clock.now()
        if appr.hold_expires and parse_iso(appr.hold_expires) <= now:
            with ctx.step("Checking the hold behind your approval") as st:
                st.warn(f"The hold expired at {parse_iso(appr.hold_expires):%H:%M}",
                        detail={"approval_id": appr.id, "hold_id": appr.hold_id, "expired_at": appr.hold_expires,
                                "now": now.isoformat()})
            with ctx.step(f"Holding {fmt_short_slot(appr.slot_start)} again") as st:
                res = self.client.hold_slot(ctx, appr.provider_id, appr.slot_start)
                if res.ok and res.value["quoted_price"] == appr.price:
                    st.done(f"Held again at the same price, {fmt_inr(appr.price)}", detail=res.value)
                elif res.ok:
                    st.warn(f"Held again, but the quote is now {fmt_inr(res.value['quoted_price'])}", detail=res.value)
                else:
                    st.warn("The slot is no longer available")
            if not res.ok:
                if res.error_class != "OPTION_GONE":
                    self._raise_unexpected(res)
                reason = "Voided: the slot was taken while this approval was waiting. Nothing was booked."
                self.gate.void(ctx.trace, appr.id, "slot no longer available")
                ctx.ui({"type": "approval_update", "id": appr.id, "status": "void", "reason": reason})
                s.pending_approval_id, s.hold = None, None
                if s.selection:
                    s.excluded_slots.append(policy.slot_key(s.selection["provider_id"], s.selection["slot_start"]))
                s.selection = None
                ctx.say("While the approval was waiting, that slot was taken. Nothing was booked.")
                self._advance_book(ctx)
                return
            hv = res.value
            s.hold = {"hold_id": hv["hold_id"], "expires_at": hv["expires_at"], "quoted_price": hv["quoted_price"],
                      "slot_start": appr.slot_start}
            if hv["quoted_price"] != appr.price:
                new_fp = fingerprint("book", appr.provider_id, appr.slot_start, appr.slot_end, hv["quoted_price"], None)
                with ctx.step("Comparing with what you approved") as st:
                    st.warn("The price changed, so the approval is void. Nothing was sent",
                            detail={"approved_fingerprint": appr.fingerprint[:6], "new_fingerprint": new_fp[:6],
                                    "differs": [f"price: {appr.price} → {hv['quoted_price']}"], "write_calls_made": 0})
                reason = (f"Voided: the price changed from {fmt_inr(appr.price)} to {fmt_inr(hv['quoted_price'])} "
                          "after the hold expired.")
                self.gate.void(ctx.trace, appr.id, reason)
                ctx.ui({"type": "approval_update", "id": appr.id, "status": "void", "reason": reason})
                budget = policy.budget_value(s)
                over = f", {fmt_inr(hv['quoted_price'] - budget)} over your {fmt_inr(budget)} budget" \
                    if budget is not None and hv["quoted_price"] > budget else ""
                ctx.say(f"Your hold expired before the yes arrived, and the new quote is "
                        f"{fmt_inr(hv['quoted_price'])}{over}. I voided the earlier approval and haven’t booked "
                        "anything. Approve the new price, or decline and I’ll stop here.")
                self._propose_book(ctx, search_price=appr.search_price or appr.price, note=False)
                return
            self.gate.update_hold(appr.id, hv["hold_id"], hv["expires_at"])
            appr.hold_id, appr.hold_expires = hv["hold_id"], hv["expires_at"]
        sel, hold = s.selection, s.hold
        if not sel or not hold:
            raise ApprovalRequired("nothing is selected to book")
        end = (parse_iso(sel["slot_start"]) + timedelta(hours=1)).isoformat()
        planned = fingerprint("book", sel["provider_id"], sel["slot_start"], end, hold["quoted_price"], None)
        key = "bk_" + sha256(f"{appr.id}:{hold['hold_id']}")[:24]
        with ctx.step("Checking your approval") as st:
            self.gate.consume_with_outbox(ctx.trace, appr, planned_fingerprint=planned, idem_key=key, op="book",
                                          request_json=json.dumps({"hold_id": hold["hold_id"]}))
            st.done("Approval matches the provider, slot and price",
                    detail={"approval_id": appr.id, "fingerprint": appr.fingerprint[:6],
                            "transition": "granted → consumed (compare-and-set)",
                            "same_transaction": f"outbox row {key[:12]}… → SENT"})
        self._push_outbox(ctx)
        s.pending_approval_id = None
        self._maybe_crash(ctx, "after_outbox_write")
        self._set_phase(ctx, "executing")
        outcome = self._run_book(ctx, appr, key, hold["hold_id"], 1)
        self._finish_book(ctx, appr, outcome)

    def _run_book(self, ctx, appr: Approval, key: str, hold_id: str, first_attempt: int) -> tuple:
        retry = self.settings.retry
        for attempt in range(first_attempt, retry.write_attempts + 1):
            label = f"Booking with {appr.provider_name}" + ("" if attempt == 1 else f" (attempt {attempt}, same key)")
            with ctx.step(label) as st:
                res = self.client.book(ctx, hold_id, key, attempt)
                self.db.run("UPDATE outbox SET attempts=? WHERE idem_key=?", (attempt, key))
                self._maybe_crash(ctx, "after_book_sent")
                detail = {"tool": "book", "args": {"hold_id": hold_id, "idempotency_key": key}}
                if res.ok:
                    st.done("The provider accepted the booking", detail={**detail, "result": res.value})
                elif res.error_class == "UNKNOWN_OUTCOME":
                    ctx.flags.add("timeout")
                    st.warn(f"No reply after {ctx.last_tool_ms / 1000:.1f} s. Outcome unknown, so not retrying yet",
                            detail={**detail, "error_class": "UNKNOWN_OUTCOME",
                                    "next": "ask get_booking_status before any retry"})
                elif res.error_class == "OPTION_GONE":
                    st.warn("Someone else took the slot. Not retrying", detail={**detail, "error": res.code})
                elif res.error_class == "HOLD_GONE":
                    st.warn("The hold expired before the provider booked it", detail={**detail, "error": res.code})
                else:
                    st.fail(f"The provider returned {res.code}", detail={**detail, "error": res.code})
            if res.ok:
                return self._verify_book(ctx, key, res.value["booking_id"])
            if res.error_class == "OPTION_GONE":
                self.gate.outbox_set(ctx.trace, key, "FAILED", error=res.code)
                self._push_outbox(ctx)
                return ("option_gone",)
            if res.error_class == "HOLD_GONE":
                self.gate.outbox_set(ctx.trace, key, "FAILED", error=res.code)
                self._push_outbox(ctx)
                return ("hold_gone",)
            if res.error_class == "REQUEST_INVALID":
                self.gate.outbox_set(ctx.trace, key, "FAILED", error=res.code)
                self._push_outbox(ctx)
                return ("failed", res.code)
            status = self._booking_status(ctx, key, "Checking the booking status before anything else")
            if status is None:
                break
            if status["status"] == "booked":
                self.gate.outbox_set(ctx.trace, key, "CONFIRMED", booking_id=status["booking_id"])
                self._push_outbox(ctx)
                return ("booked", status["booking_id"])
            if status["status"] == "failed":
                self.gate.outbox_set(ctx.trace, key, "FAILED", error=status.get("reason"))
                self._push_outbox(ctx)
                reason = status.get("reason")
                return ("option_gone",) if reason == "slot_taken" else ("hold_gone",) \
                    if reason == "hold_expired" else ("failed", reason)
            if attempt < retry.write_attempts:
                self._backoff(ctx, key, attempt)
        self.gate.outbox_set(ctx.trace, key, "UNRESOLVED", error="outcome unknown after retries")
        self._push_outbox(ctx)
        return ("unresolved",)

    def _verify_book(self, ctx, key: str, expect: str) -> tuple:
        status = self._booking_status(ctx, key, "Verifying with the booking status")
        if status and status["status"] == "booked":
            self.gate.outbox_set(ctx.trace, key, "CONFIRMED", booking_id=status["booking_id"])
            self._push_outbox(ctx)
            return ("booked", status["booking_id"])
        self.gate.outbox_set(ctx.trace, key, "UNRESOLVED", error=f"provider said ok ({expect}) but status disagrees")
        self._push_outbox(ctx)
        return ("unresolved",)

    def _booking_status(self, ctx, key: str, label: str) -> dict | None:
        with ctx.step(label) as st:
            res = self.client.get_booking_status(ctx, key)
            if res.ok:
                v = res.value
                text = {"booked": f"Status: booked, {v.get('booking_id')}", "failed": f"Status: failed ({v.get('reason')})",
                        "not_found": "Status: not found. The provider never received it"}[v["status"]]
                st.done(text, detail={"tool": "get_booking_status", "args": {"idempotency_key": key}, "result": v})
                return v
            st.warn(f"Status check failed ({res.code})")
            return None

    def _finish_book(self, ctx, appr: Approval, outcome: tuple) -> None:
        s = ctx.state
        kind = outcome[0]
        if kind == "booked":
            bid = outcome[1]
            s.result_booking_id, s.hold, s.pending_approval_id = bid, None, None
            self._set_phase(ctx, "done")
            ctx.claims.append(bid)
            ctx.ui({"type": "approval_update", "id": appr.id, "status": "consumed", "booking_id": bid})
            ctx.say(narrate.booked(appr, bid, "timeout" in ctx.flags))
            self._after_booking(ctx, appr)
            return
        reasons = {"option_gone": "Voided: the slot was taken before the booking went through. Nothing was booked.",
                   "hold_gone": "Voided: the hold lapsed during booking. Nothing was booked.",
                   "failed": "Voided: the provider rejected the booking. Nothing was booked.",
                   "expired": "Voided: the request never reached the provider and the hold expired. Nothing was booked."}
        if kind in reasons:
            ctx.ui({"type": "approval_update", "id": appr.id, "status": "void", "reason": reasons[kind]})
        if kind == "option_gone":
            if s.selection:
                s.excluded_slots.append(policy.slot_key(s.selection["provider_id"], s.selection["slot_start"]))
            s.selection, s.hold = None, None
            ctx.say("That slot was taken by someone else just as I tried to book it. Nothing was booked, and I "
                    "didn’t retry it.")
            self._set_phase(ctx, "collecting")
            self._advance_book(ctx)
        elif kind == "hold_gone":
            s.hold = None
            ctx.say("The hold lapsed just as I tried to book. Nothing was booked. I’ve held the same slot again so "
                    "you can approve it fresh.")
            if s.selection:
                self._hold_and_propose(ctx)
        elif kind in ("failed", "expired"):
            s.selection, s.hold = None, None
            ctx.say("The booking didn’t go through, so nothing was booked. Want me to look again?")
            self._set_phase(ctx, "collecting")
        else:
            self._set_phase(ctx, "verifying")
            ctx.outcome = "unresolved"
            ctx.say(narrate.unresolved(appr))

    def _after_booking(self, ctx, appr: Approval) -> None:
        s = ctx.state
        area = s.slots.get("area") or {}
        if area.get("source") == "user" and "home_area" not in self.memory.usable(self.user_id):
            ctx.chips.append(f"Remember {area['value']} as my area")
        service = s.slot("service")
        if not service:
            return
        key = f"preferred_technician:{service}"
        if key in self.memory.usable(self.user_id) or self.memory.pending_inferred(self.user_id, key) \
                or self.memory.tombstoned(self.user_id, key):
            return
        rows = self._list_bookings(ctx, "Checking your booking history for patterns")
        if not rows:
            return
        same = [b for b in rows if b["service"] == service and b["provider_id"] == appr.provider_id
                and b["status"] in ("booked", "completed")]
        if len(same) >= 2:
            fact = self.memory.infer(ctx.trace, self.user_id, key, appr.provider_name,
                                     evidence="Inferred from bookings " + ", ".join(b["booking_id"] for b in same))
            if fact:
                self._push_memory(ctx)

    # ================================================================== reschedule and cancel
    def _advance_change(self, ctx) -> None:
        s = ctx.state
        verb = "move" if s.task == "reschedule" else "cancel"
        if s.pending_question and s.pending_question.get("kind") == "choose_booking":
            ctx.say(self._current_question(ctx))
            ctx.options = self._booking_cards(s)
            return
        if not s.target_booking:
            rows = self._list_bookings(ctx, "Looking up your upcoming bookings")
            if rows is None:
                return
            now = self.clock.now()
            svc, ref = s.slot("service"), s.slot("booking_ref")
            cand = [b for b in rows if b["status"] == "booked" and parse_iso(b["slot_start"]) > now
                    and (not ref or b["booking_id"] == ref) and (not svc or b["service"] == svc)]
            if not cand:
                what = narrate.service_label(svc) + " " if svc else ""
                ctx.say(f"I can’t find an upcoming {what}booking to {verb}.")
                s.clear_task()
                self._set_phase(ctx, "idle")
                return
            if len(cand) > 1:
                s.booking_choices = cand
                s.pending_question = {"kind": "choose_booking"}
                self._set_phase(ctx, "choosing")
                ctx.options = self._booking_cards(s)
                ctx.say(f"You have {len(cand)} upcoming bookings that match. Which one should I {verb}?")
                return
            s.target_booking = cand[0]
            s.focus_provider = {"id": cand[0]["provider_id"], "name": cand[0]["provider_name"],
                                "service": cand[0]["service"]}
        tb = s.target_booking
        if s.task == "cancel":
            if not s.pending_approval_id:
                self._propose_change(ctx, tb["slot_start"])
            return
        if not s.slot("date_range"):
            self._set_phase(ctx, "collecting")
            ctx.say(f"Which day should I move {tb['provider_name']} ({fmt_short_slot(tb['slot_start'])}) to?")
            return
        if not s.selection:
            slots = self._provider_slots(ctx, tb)
            if slots is None:
                return
            if not slots:
                ctx.say(f"{tb['provider_name']} has no free slots {narrate.date_phrase(s)} that fit. Which other day "
                        "would work?")
                s.slots.pop("date_range", None)
                self._set_phase(ctx, "collecting")
                return
            expr = (s.slots.get("time_window") or {}).get("expr") or ""
            exact = [x for x in slots if expr.startswith("at:") and parse_iso(x).hour == int(expr[3:5])]
            if exact or len(slots) == 1:
                s.selection = {"slot_start": (exact or slots)[0]}
            else:
                s.slot_choices = [{"slot_start": x} for x in slots[:4]]
                self._set_phase(ctx, "choosing")
                ctx.options = [{"id": f"slot{i}", "name": tb["provider_name"], "meta": f"booking {tb['booking_id']}",
                                "price": tb["price"], "slot": fmt_short_slot(x["slot_start"]),
                                "reply": f"Option {i}: {fmt_short_slot(x['slot_start'])}"}
                               for i, x in enumerate(s.slot_choices, 1)]
                ctx.say(f"{tb['provider_name']} can come at these times {narrate.date_phrase(s)}:")
                return
        self._propose_change(ctx, s.selection["slot_start"])

    def _provider_slots(self, ctx, tb: dict) -> list[str] | None:
        s = ctx.state
        dr = s.slot("date_range")
        hours = s.slot("time_window") or [8, 18]
        with ctx.step(f"Checking {tb['provider_name']}’s free slots") as st:
            res = self.client.search_providers(ctx, tb["service"], tb["area"], (dr[0], dr[1]))
            if not res.ok:
                st.fail(f"Search failed ({res.code})")
            else:
                entry = next((p for p in res.value if p["id"] == tb["provider_id"]), None)
                now = self.clock.now()
                slots = [x for x in (entry or {}).get("free_slots", [])
                         if x != tb["slot_start"] and policy.slot_key(tb["provider_id"], x) not in s.excluded_slots
                         and parse_iso(x) > now
                         and hours[0] <= parse_iso(x).hour <= hours[1]]
                st.done(f"{len(slots)} free slot{'s' if len(slots) != 1 else ''} {narrate.date_phrase(s)}",
                        detail={"tool": "search_providers", "provider_id": tb["provider_id"], "slots": slots[:6]})
        if not res.ok:
            if res.error_class == "UNEXPECTED":
                self._raise_unexpected(res)
            ctx.say("I couldn’t check availability right now. Nothing was changed.")
            return None
        return slots

    def _propose_change(self, ctx, slot_start: str) -> None:
        s = ctx.state
        tb = s.target_booking
        end = (parse_iso(slot_start) + timedelta(hours=1)).isoformat()
        appr = self.gate.propose(ctx.trace, user_id=self.user_id, session_id=s.session_id, action=s.task,
                                 provider_id=tb["provider_id"], provider_name=tb["provider_name"],
                                 slot_start=slot_start, slot_end=end, price=tb["price"], turn=s.turn,
                                 booking_id=tb["booking_id"])
        appr.prompt_text = narrate.approval_prompt(appr, old_slot=tb["slot_start"])
        self.gate.set_prompt(appr.id, appr.prompt_text)
        s.pending_approval_id = appr.id
        self._set_phase(ctx, "awaiting_approval")
        if s.task == "cancel":
            ctx.say(f"Here’s the cancellation for {tb['provider_name']}, {fmt_slot(tb['slot_start'])}. "
                    "Nothing changes until you approve it.")
        else:
            ctx.say(f"{tb['provider_name']} can come {fmt_slot(slot_start)} instead of "
                    f"{fmt_short_slot(tb['slot_start'])}, same price. Nothing moves until you approve it.")
        ctx.approval = narrate.approval_card(appr, old_slot=tb["slot_start"] if s.task == "reschedule" else None)

    def _execute_change(self, ctx, appr: Approval) -> None:
        s = ctx.state
        tb = s.target_booking
        if not tb:
            raise ApprovalRequired("no booking selected")
        new_slot = s.selection["slot_start"] if appr.action == "reschedule" else tb["slot_start"]
        end = (parse_iso(new_slot) + timedelta(hours=1)).isoformat()
        planned = fingerprint(appr.action, tb["provider_id"], new_slot, end, tb["price"], tb["booking_id"])
        key = ("rs_" if appr.action == "reschedule" else "cx_") + appr.id
        with ctx.step("Checking your approval") as st:
            self.gate.consume_with_outbox(ctx.trace, appr, planned_fingerprint=planned, idem_key=key, op=appr.action,
                                          request_json=json.dumps({"booking_id": tb["booking_id"], "new_slot": new_slot}))
            st.done("Approval matches the booking, slot and price",
                    detail={"approval_id": appr.id, "transition": "granted → consumed (compare-and-set)",
                            "same_transaction": f"outbox row {key[:12]}… → SENT"})
        self._push_outbox(ctx)
        s.pending_approval_id = None
        self._maybe_crash(ctx, "after_outbox_write")
        self._set_phase(ctx, "executing")
        outcome = self._run_change(ctx, appr, key, tb["booking_id"], new_slot, 1)
        self._finish_change(ctx, appr, outcome, new_slot)

    def _run_change(self, ctx, appr: Approval, key: str, booking_id: str, new_slot: str, first_attempt: int) -> tuple:
        retry = self.settings.retry
        for attempt in range(first_attempt, retry.write_attempts + 1):
            suffix = "" if attempt == 1 else f" (attempt {attempt}, same key)"
            label = (f"Moving {booking_id} to {fmt_short_slot(new_slot)}" if appr.action == "reschedule"
                     else f"Cancelling {booking_id}") + suffix
            with ctx.step(label) as st:
                if appr.action == "reschedule":
                    res = self.client.reschedule(ctx, booking_id, new_slot, key, attempt)
                else:
                    res = self.client.cancel(ctx, booking_id, key, attempt)
                self.db.run("UPDATE outbox SET attempts=? WHERE idem_key=?", (attempt, key))
                self._maybe_crash(ctx, "after_book_sent")
                detail = {"tool": appr.action, "args": {"booking_id": booking_id, "idempotency_key": key}}
                if res.ok:
                    st.done("The provider accepted the change", detail={**detail, "result": res.value})
                elif res.error_class == "RETRYABLE":
                    st.warn(f"Temporary error ({res.code}). Retrying with the same key", detail={**detail, "error": res.code})
                elif res.error_class == "OPTION_GONE":
                    st.warn("That slot conflicts on the provider’s side. Not retrying", detail={**detail, "error": res.code})
                else:
                    st.warn(f"The provider returned {res.code}", detail={**detail, "error": res.code})
            if res.ok:
                return self._verify_change(ctx, appr, key, booking_id, new_slot)
            if res.error_class == "OPTION_GONE":
                self.gate.outbox_set(ctx.trace, key, "FAILED", error=res.code)
                self._push_outbox(ctx)
                return ("conflict",)
            if res.error_class == "REQUEST_INVALID":
                self.gate.outbox_set(ctx.trace, key, "FAILED", error=res.code)
                self._push_outbox(ctx)
                return ("failed", res.code)
            if attempt < retry.write_attempts:
                self._backoff(ctx, key, attempt)
        verified = self._verify_change(ctx, appr, key, booking_id, new_slot, final=True)
        return verified

    def _verify_change(self, ctx, appr: Approval, key: str, booking_id: str, new_slot: str,
                       final: bool = False) -> tuple:
        rows = self._list_bookings(ctx, "Verifying with your booking list")
        b = next((r for r in rows or [] if r["booking_id"] == booking_id), None)
        ok = b is not None and ((appr.action == "cancel" and b["status"] == "cancelled")
                                or (appr.action == "reschedule" and b["slot_start"] == new_slot))
        if ok:
            self.gate.outbox_set(ctx.trace, key, "CONFIRMED", booking_id=booking_id)
            self._push_outbox(ctx)
            return ("done",)
        self.gate.outbox_set(ctx.trace, key, "UNRESOLVED", error="change not visible after retries" if final
                             else "provider said ok but booking list disagrees")
        self._push_outbox(ctx)
        return ("unresolved",)

    def _finish_change(self, ctx, appr: Approval, outcome: tuple, new_slot: str) -> None:
        s = ctx.state
        tb = s.target_booking or {}
        kind = outcome[0]
        if kind == "done":
            s.result_booking_id = tb.get("booking_id")
            self._set_phase(ctx, "done")
            ctx.claims.append(s.result_booking_id)
            ctx.ui({"type": "approval_update", "id": appr.id, "status": "consumed", "booking_id": s.result_booking_id})
            if appr.action == "reschedule":
                ctx.say(f"Done. {tb.get('provider_name')} will now come {fmt_slot(new_slot)}. "
                        f"Booking {s.result_booking_id} is updated.")
            else:
                ctx.say(f"Done. Booking {s.result_booking_id} with {tb.get('provider_name')} is cancelled.")
            return
        if kind == "conflict":
            ctx.ui({"type": "approval_update", "id": appr.id, "status": "void",
                    "reason": "Voided: that slot clashed on the provider’s side. Nothing changed."})
            s.excluded_slots.append(policy.slot_key(tb.get("provider_id", ""), new_slot))
            s.selection = None
            ctx.say("That slot clashed with another booking on the provider’s side, so nothing changed and I "
                    "didn’t retry it.")
            self._advance_change(ctx)
            return
        if kind == "failed":
            ctx.ui({"type": "approval_update", "id": appr.id, "status": "void",
                    "reason": "Voided: the provider rejected the change."})
            ctx.say("The provider rejected the change, so nothing changed.")
            s.clear_task()
            self._set_phase(ctx, "idle")
            return
        self._set_phase(ctx, "verifying")
        ctx.outcome = "unresolved"
        ctx.say("I couldn’t confirm the change yet because the provider isn’t answering. I won’t send it twice, and "
                "I’ll check again before anything else on your next message.")

    # ================================================================== recovery
    def _recover(self, ctx) -> None:
        s = ctx.state
        rows = self.gate.outbox_rows(s.session_id, ("SENT", "UNRESOLVED"))
        for row in rows:
            appr = self.gate.get(row["approval_id"])
            if appr is None:
                continue
            key, op = row["idem_key"], row["op"]
            ctx.trace.emit("recovery", key=key, op=op, status=row["status"])
            with ctx.step("Checking unfinished work from before") as st:
                st.done(f"Found an unfinished {op}: {key[:12]}…, sent before the interruption",
                        detail={"outbox": {"key": key, "op": op, "status": row["status"], "approval": appr.id,
                                           "attempts": row["attempts"]}})
            req = json.loads(row["request_json"] or "{}")
            if op == "book":
                status = self._booking_status(ctx, key, "Checking the booking status")
                if status is None:
                    ctx.say(narrate.unresolved(appr))
                    continue
                if status["status"] == "booked":
                    self.gate.outbox_set(ctx.trace, key, "CONFIRMED", booking_id=status["booking_id"])
                    outcome: tuple = ("booked", status["booking_id"])
                elif status["status"] == "failed":
                    self.gate.outbox_set(ctx.trace, key, "FAILED", error=status.get("reason"))
                    outcome = ("failed", status.get("reason"))
                elif appr.hold_expires and parse_iso(appr.hold_expires) > self.clock.now():
                    outcome = self._run_book(ctx, appr, key, req.get("hold_id"), max(1, row["attempts"] + 1)) \
                        if row["attempts"] < self.settings.retry.write_attempts else ("unresolved",)
                else:
                    self.gate.outbox_set(ctx.trace, key, "FAILED", error="never reached the provider; hold expired")
                    outcome = ("expired",)
                self._push_outbox(ctx)
                if outcome[0] == "booked":
                    bid = outcome[1]
                    s.result_booking_id, s.hold, s.pending_approval_id, s.selection = bid, None, None, None
                    s.phase = "done"
                    self._push_state(ctx)
                    ctx.claims.append(bid)
                    ctx.ui({"type": "approval_update", "id": appr.id, "status": "consumed", "booking_id": bid})
                    ctx.say(f"Picking up where I left off: your booking went through. {appr.provider_name}, "
                            f"{fmt_slot(appr.slot_start)}, {fmt_inr(appr.price)}. Booking {bid}. I didn’t send it twice.")
                elif outcome[0] == "unresolved":
                    ctx.say(narrate.unresolved(appr))
                else:
                    ctx.ui({"type": "approval_update", "id": appr.id, "status": "void",
                            "reason": "Voided: the request from before didn’t go through. Nothing was booked."})
                    s.phase, s.hold, s.selection = "idle", None, None
                    self._push_state(ctx)
                    ctx.say("The booking I started before the interruption didn’t go through, so nothing was booked. "
                            "Want me to look again?")
            else:
                if s.target_booking is None:
                    s.target_booking = {"booking_id": req.get("booking_id"), "provider_name": appr.provider_name,
                                        "provider_id": appr.provider_id, "slot_start": appr.slot_start,
                                        "price": appr.price}
                outcome = self._run_change(ctx, appr, key, req.get("booking_id"), req.get("new_slot"),
                                           max(1, row["attempts"] + 1)) \
                    if row["attempts"] < self.settings.retry.write_attempts else \
                    self._verify_change(ctx, appr, key, req.get("booking_id"), req.get("new_slot"), final=True)
                self._finish_change(ctx, appr, outcome, req.get("new_slot"))

    # ================================================================== helpers
    def _drop_forgotten(self, ctx) -> None:
        """A fact forgotten in another session must not survive in this session's state."""
        s = ctx.state
        for name, slot in list(s.slots.items()):
            fid = slot.get("fact_id")
            if fid:
                fact = self.memory.get(fid)
                if fact is None or fact.status != "active":
                    del s.slots[name]
                    s.search_raw, s.offers, s.selection = [], [], None
                    self._void_pending(ctx, "a remembered detail it relied on was forgotten")

    def _prefill_from_memory(self, ctx) -> None:
        s = ctx.state
        facts = self.memory.usable(self.user_id)
        notes = []
        if s.slot("area") is None and "home_area" in facts:
            f = facts["home_area"]
            s.set_slot("area", f.value, "memory", s.turn, fact_id=f.id, fact_key=f.key)
            ctx.trace.emit("memory_use", key=f.key, fact_id=f.id, source=f.source)
            notes.append(f"your area, {f.value}")
        if s.slot("time_window") is None and "earliest_time" in facts:
            f = facts["earliest_time"]
            h1, h2, label = resolve_time(f"after:{f.value}")
            s.set_slot("time_window", [h1, h2], "memory", s.turn, label=label, expr=f"after:{f.value}",
                       fact_id=f.id, fact_key=f.key)
            ctx.trace.emit("memory_use", key=f.key, fact_id=f.id, source=f.source)
            notes.append(f"visits from {f.value}")
        service = s.slot("service")
        key = f"preferred_technician:{service}"
        if service and not s.slots.get("provider") and key in facts:
            f = facts[key]
            s.set_slot("provider", {"id": None, "name": f.value}, "memory", s.turn, fact_id=f.id, fact_key=f.key)
            ctx.trace.emit("memory_use", key=f.key, fact_id=f.id, source=f.source)
            notes.append(f"your preferred {narrate.service_label(service)}, {f.value}")
        if notes:
            with ctx.step("Checking your saved preferences") as st:
                st.done("Using " + narrate.join(notes), detail={"rule": "only stated or confirmed facts pre-fill a slot",
                                                                "used": notes})
            ctx.say(f"Using {narrate.join(notes)} from what you’ve told me before. Say if that’s changed.")
            self._push_state(ctx)

    def _last_provider_for(self, ctx, service: str) -> dict | None:
        rows = self._list_bookings(ctx, "Looking up your past bookings")
        now = self.clock.now()
        past = [b for b in rows or [] if b["service"] == service and parse_iso(b["slot_start"]) < now
                and b["status"] in ("completed", "booked")]
        return max(past, key=lambda b: b["slot_start"]) if past else None

    def _list_bookings(self, ctx, label: str) -> list[dict] | None:
        with ctx.step(label) as st:
            res = self.client.list_bookings(ctx, self.user_id)
            if res.ok:
                st.done(f"{len(res.value)} bookings on record", detail={"tool": "list_bookings", "count": len(res.value)})
            else:
                st.fail(f"Couldn’t load bookings ({res.code})")
        if not res.ok:
            if res.error_class == "UNEXPECTED":
                self._raise_unexpected(res)
            ctx.say("I couldn’t load your bookings right now.")
            return None
        for b in res.value:
            self._remember_providers(ctx.state, [{"id": b["provider_id"], "name": b["provider_name"]}], b["service"])
        return res.value

    @staticmethod
    def _remember_providers(s: ErrandState, providers: list[dict], service: str) -> None:
        known = {p["id"] for p in s.known_providers}
        for p in providers:
            if p["id"] not in known:
                s.known_providers.append({"id": p["id"], "name": p["name"], "service": service})
                known.add(p["id"])

    def _option_cards(self, s: ErrandState) -> list[dict]:
        out = []
        for i, o in enumerate(s.offers[: self.settings.max_options_shown], 1):
            out.append({"id": o["provider_id"], "name": o["name"], "meta": f"{o['rating']} rating, {o['jobs']} jobs",
                        "price": o["price"], "slot": fmt_short_slot(o["slot_start"]),
                        "reply": f"Option {i}: {o['name']}, {fmt_short_slot(o['slot_start'])}"})
        return out

    def _booking_cards(self, s: ErrandState) -> list[dict]:
        return [{"id": b["booking_id"], "name": b["provider_name"],
                 "meta": f"{narrate.service_label(b['service'])}, booking {b['booking_id']}", "price": b["price"],
                 "slot": fmt_short_slot(b["slot_start"]), "reply": f"Option {i}: {b['provider_name']}"}
                for i, b in enumerate(s.booking_choices, 1)]

    def _current_question(self, ctx) -> str:
        s = ctx.state
        pending = self.gate.pending(s.session_id)
        if pending:
            return "The approval above is still open: reply yes to approve, or tell me what to change."
        q = s.pending_question or {}
        if q.get("kind") == "confirm_provider":
            return f"Should I book {q['provider_name']} again?"
        if q.get("kind") == "confirm_fact":
            return f"Should I look at {q['provider_name']} first?"
        if q.get("kind") == "choose_booking":
            return "Which booking do you mean? Pick one of the options."
        if s.task == "book" and s.phase == "collecting":
            missing = policy.missing_fields(s)
            return narrate.ask_missing(missing, s) if missing else ""
        if s.phase == "choosing":
            return "Pick one of the options above, or tell me what to change."
        return ""

    def _void_pending(self, ctx, reason: str) -> None:
        s = ctx.state
        pending = self.gate.pending(s.session_id)
        if pending:
            self.gate.void(ctx.trace, pending.id, reason)
            ctx.ui({"type": "approval_update", "id": pending.id, "status": "void", "reason": f"Voided: {reason}."})
        s.pending_approval_id, s.hold = None, None

    def _set_phase(self, ctx, phase: str) -> None:
        s = ctx.state
        if s.phase != phase:
            view = self.state_view(s)
            diff = {k: v for k, v in view.items() if ctx._last_view.get(k) != v and k != "phase"}
            ctx._last_view = view
            ctx.trace.emit("state_transition", **{"from": s.phase}, to=phase, diff=diff)
            s.phase = phase
            ctx.budget.charge_step()
        self._push_state(ctx)

    def _push_state(self, ctx) -> None:
        ctx.ui({"type": "state", "state": self.state_view(ctx.state)})

    def _push_memory(self, ctx) -> None:
        ctx.ui({"type": "memory", "facts": self.memory.view(self.user_id)})

    def _push_outbox(self, ctx) -> None:
        ctx.ui({"type": "outbox", "rows": self._outbox_view(ctx.state.session_id)})

    def _outbox_view(self, session_id: str) -> list[dict]:
        return [{"key": r["idem_key"][:14] + "…", "op": r["op"], "status": r["status"], "booking": r["booking_id"],
                 "attempts": r["attempts"]} for r in self.gate.outbox_rows(session_id)][-6:]

    def state_view(self, s: ErrandState) -> dict:
        def lab(name: str) -> Any:
            sl = s.slots.get(name)
            if not sl:
                return None
            v = sl.get("label") or sl.get("value")
            if name == "budget":
                v = "no limit" if sl["value"] == "ANY" else fmt_inr(sl["value"])
            elif name == "service":
                v = narrate.service_label(sl["value"])
            elif name == "provider":
                val = sl.get("value")
                v = val.get("name") if isinstance(val, dict) else ("last time’s provider" if sl.get("ref") == "last_time"
                                                                    else sl.get("ref"))
            if sl.get("source") in ("memory", "history"):
                v = f"{v} ({sl['source']})"
            return v
        missing = policy.missing_fields(s) if s.task == "book" and s.phase in ("collecting",) else []
        return {
            "phase": s.phase, "task": s.task, "service": lab("service"), "provider": lab("provider"),
            "area": lab("area"), "date_range": lab("date_range"), "time_window": lab("time_window"),
            "budget": lab("budget"),
            "booking_to_change": f"{s.target_booking['booking_id']}, {fmt_short_slot(s.target_booking['slot_start'])}"
            if s.target_booking else None,
            "selection": (f"{s.selection.get('name', (s.target_booking or {}).get('provider_name', ''))}, "
                          f"{fmt_short_slot(s.selection['slot_start'])}") if s.selection else None,
            "hold": f"{s.hold['hold_id']}, until {parse_iso(s.hold['expires_at']):%H:%M}" if s.hold else None,
            "pending_approval": s.pending_approval_id, "booking": s.result_booking_id, "missing": missing,
        }

    def _truth(self, s: ErrandState) -> dict:
        rows = self.gate.outbox_rows(s.session_id)
        if any(r["status"] in ("SENT", "UNRESOLVED") for r in rows):
            return {"kind": "open"}
        if rows and rows[-1]["status"] == "CONFIRMED":
            return {"kind": "confirmed", "booking_id": rows[-1]["booking_id"]}
        if rows and rows[-1]["status"] == "FAILED":
            return {"kind": "failed"}
        return {"kind": "nothing"}

    def _backoff(self, ctx, key: str, attempt: int) -> None:
        r = self.settings.retry
        delay = random.Random(f"{key}:{attempt}").uniform(0, min(r.max_backoff_s, r.base_backoff_s * 2 ** attempt))
        self.clock.sleep(delay)
        ctx.budget.check_time()

    def _maybe_crash(self, ctx, point: str) -> None:
        fault = self.provider.take_fault("agent", prefix=f"crash:{point}")
        if fault:
            ctx.trace.emit("crash", point=point)
            raise InjectedCrash(point)

    @staticmethod
    def _raise_unexpected(res: Result) -> None:
        raise ProviderFailure(f"{res.code}: {res.detail}")


_LABEL = {"service": "service", "area": "area", "date_range": "date", "time_window": "time", "budget": "budget",
          "provider": "provider"}

