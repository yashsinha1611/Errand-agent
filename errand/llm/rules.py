"""A deterministic, offline stand-in for the LLM interpreter.

It reads the same structured context a real model receives and returns the same
``Intent`` JSON. It powers the credential-free CLI and every eval, which keeps the
suite offline and reproducible. It is deliberately conservative: when in doubt it
returns ``unclear`` rather than guessing.
"""
from __future__ import annotations

import re
from datetime import date

from .base import ModelRequest, ModelResponse, estimate_tokens, payload_for

YES = re.compile(r"^\s*(yes|yeah|yep|yup|y|sure|ok|okay|confirm(ed)?|go ahead|book it|do it|please do|haan|ha|ji|"
                 r"correct|right|sounds good|perfect|great|approve[d]?|book for|move it|cancel the booking|"
                 r"that'?s? (right|fine)|fine)\b", re.I)
NO = re.compile(r"^\s*(no|nope|nah|don'?t|do not|not now|never ?mind|keep it as is|cancel that|not that one)\b", re.I)
ABORT = re.compile(r"^\s*(never ?mind|forget it|start over|stop|leave it|nothing)\b", re.I)
GREETING = re.compile(r"^\s*(hi|hello|hey|namaste|good (morning|afternoon|evening)|help|what can you do)\b", re.I)
CORRECTION = re.compile(r"\b(actually|instead|make it|change (it|that) to|rather|switch to|not .{1,20} but)\b", re.I)

SERVICE_PATTERNS = [
    ("ac_servicing", r"\bac\b|\ba\.c\.|air ?con|air conditioner"),
    ("plumber", r"plumb|\btaps?\b|leak|pipe|drain|toilet|flush|geyser"),
    ("electrician", r"electric|wiring|switch|socket|\bfans?\b|mcb"),
    ("cleaning", r"clean|maid"),
    ("carpenter", r"carpent|furniture|wardrobe|hinge|\bwood"),
]
AREA_PATTERNS = [
    ("Indiranagar", r"indira ?nagar"), ("Koramangala", r"koramangala"),
    ("HSR Layout", r"\bhsr\b"), ("Whitefield", r"white ?field"),
]
WEEKDAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|"
                     r"fri|sat|sun)\b", re.I)
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12}
TIME_TOKEN = r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)?"
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}


def _hour(h: str, ampm: str | None) -> int:
    hour = int(h)
    if ampm:
        pm = ampm.lower().startswith("p")
        if pm and hour < 12:
            hour += 12
        if not pm and hour == 12:
            hour = 0
    elif hour < 8:
        hour += 12
    return hour


def find_service(t: str) -> str | None:
    for key, pat in SERVICE_PATTERNS:
        if re.search(pat, t, re.I):
            return key
    return None


def find_area(t: str) -> str | None:
    for name, pat in AREA_PATTERNS:
        if re.search(pat, t, re.I):
            return name
    return None


def find_date(t: str, today: date) -> str | None:
    tl = t.lower()
    if "day after tomorrow" in tl:
        return "day_after_tomorrow"
    if re.search(r"\btomorrow\b|\bkal\b", tl):
        return "tomorrow"
    if re.search(r"\btoday\b|\btonight\b|\baaj\b", tl):
        return "today"
    if "next week" in tl:
        return "next_week"
    if re.search(r"\bthis week\b|\bwithin the week\b", tl):
        return "this_week"
    if re.search(r"\bweekend\b", tl):
        return "weekend"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", tl)
    if m:
        month = MONTHS[m.group(2)]
        year = today.year + (1 if month < today.month else 0)
        try:
            return f"date:{date(year, month, int(m.group(1))).isoformat()}"
        except ValueError:
            return None
    w = WEEKDAY.search(tl)
    if w:
        return f"weekday:{w.group(1)[:3]}"
    return None


def find_time(t: str) -> str | None:
    tl = t.lower()
    m = re.search(r"(not before|no earlier than|after|from|starting|earliest)\s+" + TIME_TOKEN, tl)
    if m and (m.group(3) or m.group(4) or m.group(1) in ("not before", "no earlier than", "after")):
        return f"after:{_hour(m.group(2), m.group(4)):02d}:00"
    m = re.search(r"(not after|no later than|before|by|until|latest)\s+" + TIME_TOKEN, tl)
    if m and (m.group(3) or m.group(4)):
        return f"before:{_hour(m.group(2), m.group(4)):02d}:00"
    if re.search(r"\bmornings?\b", tl):
        return "morning"
    if re.search(r"\bafternoons?\b", tl):
        return "afternoon"
    if re.search(r"\bevenings?\b|after work", tl):
        return "evening"
    m = re.search(r"\bat\s+" + TIME_TOKEN, tl) or re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)", tl)
    if m:
        return f"at:{_hour(m.group(1), m.group(3)):02d}:00"
    if re.search(r"any ?time|whenever", tl):
        return "any"
    return None


def find_budget(t: str) -> tuple[int | None, bool]:
    tl = t.lower().replace("₹", " ₹")
    if re.search(r"no budget|any price|price doesn'?t matter|whatever it costs|any budget|budget doesn'?t matter", tl):
        return None, True
    m = re.search(r"(under|below|less than|max(?:imum)?|up ?to|within|budget(?: of| is)?|no more than|cheaper than)"
                  r"\s*(?:₹|rs\.?|inr)?\s*([\d,]+)\s*(k)?\b", tl)
    if m:
        value = int(m.group(2).replace(",", "") or 0)
        if m.group(3):
            value *= 1000
        return value, False
    return None, False


def find_provider(t: str, ctx: dict) -> str | None:
    tl = t.lower()
    if re.search(r"same (\w+ )?(one |person |guy )?as (last time|before)|same one as last|last time'?s|"
                 r"(the )?usual (plumber|electrician|cleaner|carpenter|one)|same (plumber|electrician|cleaner|carpenter)", tl):
        return "last_time"
    for name in ctx.get("provider_names", []):
        words = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2 and w not in
                 ("and", "the", "services", "service", "plumbing", "plumbers", "electric", "electricals", "home")]
        if name.lower() in tl or (words and re.search(rf"\b{re.escape(words[0])}\b", tl)):
            return name
    if re.search(r"\b(them|that one|that provider|this one|him|her|that plumber|that electrician)\b", tl):
        return "that"
    return None


def find_option(t: str, ctx: dict) -> int | None:
    options = ctx.get("options") or []
    if not options:
        return None
    tl = t.lower().strip()
    if re.fullmatch(r"#?\s*(\d)", tl):
        n = int(re.sub(r"\D", "", tl))
        return n if 1 <= n <= len(options) else None
    m = re.search(r"\b(option|number|no\.?|#)\s*(\d)\b", tl)
    if m and 1 <= int(m.group(2)) <= len(options):
        return int(m.group(2))
    for word, n in ORDINALS.items():
        if re.search(rf"\b{word}\b", tl) and n <= len(options):
            return n
    if re.search(r"\b(last one|the last)\b", tl):
        return len(options)
    named = [o for o in options if o.get("name") and _name_hit(o["name"], tl)]
    when = find_time(t)
    if named:
        if len(named) == 1:
            return named[0]["index"]
        if when and when.startswith("at:"):
            for o in named:
                if o.get("hour") == int(when[3:5]):
                    return o["index"]
        return named[0]["index"]
    if when and when.startswith("at:"):
        hits = [o for o in options if o.get("hour") == int(when[3:5])]
        if len(hits) == 1:
            return hits[0]["index"]
    return None


def _name_hit(name: str, tl: str) -> bool:
    if name.lower() in tl:
        return True
    first = re.findall(r"[a-z]+", name.lower())
    return bool(first) and len(first[0]) > 2 and re.search(rf"\b{re.escape(first[0])}\b", tl) is not None


def memory_intent(t: str, ctx: dict) -> dict | None:
    tl = t.lower()
    if re.search(r"what do you (remember|know) about me|what('?s| is) (saved|stored|in (your )?memory)|"
                 r"show (me )?(my )?(memory|preferences)|^my preferences|what have you saved", tl):
        return {"kind": "memory", "memory_op": "view"}
    m = re.search(r"\bforget (about )?(my |the )?(.+)$", tl)
    if m and not re.fullmatch(r"\s*(it|that|this)\s*\.?", m.group(3)):
        obj = m.group(3)
        if re.search(r"area|address|location|home|where i live", obj):
            return {"kind": "memory", "memory_op": "forget", "memory_key": "home_area"}
        if re.search(r"time|earliest|mornings?|before|after", obj):
            return {"kind": "memory", "memory_op": "forget", "memory_key": "earliest_time"}
        if re.search(r"everything|all", obj):
            return {"kind": "memory", "memory_op": "forget"}
        service = find_service(obj)
        if service or re.search(r"preferred|usual|favourite|favorite", obj):
            return {"kind": "memory", "memory_op": "forget", "memory_key": "preferred_technician",
                    "memory_value": service}
    remember = re.search(r"\b(remember|save|note( down)?|keep in mind)\b", tl)
    correct = re.search(r"\b(i (now )?live in|i moved to|my (home |)area is|my address is now|i'?m in)\b", tl)
    if remember or correct:
        op = "correct" if correct and not remember else "remember"
        area = find_area(t)
        if area:
            return {"kind": "memory", "memory_op": op, "memory_key": "home_area", "memory_value": area}
        when = find_time(t)
        if when and when.startswith(("after:", "at:")):
            return {"kind": "memory", "memory_op": op, "memory_key": "earliest_time", "memory_value": when.split(":", 1)[1]}
        prov = find_provider(t, ctx)
        if prov and prov not in ("that", "last_time"):
            return {"kind": "memory", "memory_op": op, "memory_key": "preferred_technician", "memory_value": prov}
    if re.search(r"\b(always|never|i prefer|i'd rather)\b", tl):
        when = find_time(t)
        if when and when.startswith("after:"):
            return {"kind": "memory", "memory_op": "remember", "memory_key": "earliest_time",
                    "memory_value": when.split(":", 1)[1]}
    return None


def interpret(message: str, ctx: dict) -> dict:
    t = message.strip()
    if not t:
        return {"kind": "unclear"}
    today = date.fromisoformat(ctx.get("today", date.today().isoformat()))
    tl = t.lower()

    mem = memory_intent(t, ctx)
    if mem:
        return mem

    details: dict = {}
    if (s := find_service(t)):
        details["service"] = s
    if (a := find_area(t)):
        details["area"] = a
    if (d := find_date(t, today)):
        details["date_expr"] = d
    if (w := find_time(t)):
        details["time_expr"] = w
    budget, no_budget = find_budget(t)
    if budget is not None:
        details["budget_inr"] = budget
    if no_budget:
        details["no_budget"] = True
    if (p := find_provider(t, ctx)):
        details["provider_ref"] = p
    if (b := re.search(r"\bB-\d{3,6}\b", t, re.I)):
        details["booking_ref"] = b.group(0).upper()
    correction = bool(CORRECTION.search(t))

    if ctx.get("awaiting_approval") or ctx.get("pending_question"):
        if YES.search(t) and not (set(details) - {"provider_ref"}):
            return {"kind": "confirm"}
        if NO.search(t) and not details:
            return {"kind": "decline"}

    if re.search(r"\breviews?\b|what do (people|others|customers|users) say|ratings? (of|for)|how good is|"
                 r"is .{2,30} (any good|reliable|good)", tl):
        out = {"kind": "ask_reviews"}
        ref = details.get("provider_ref")
        if not ref:
            m = re.search(r"(?i:about|of|for|is)\s+((?:[A-Z][\w&']*\s?){1,4})", t)
            ref = m.group(1).strip() if m else None
        if ref:
            out["provider_ref"] = ref[:60]
        return out

    if re.search(r"\b(my|upcoming|current|show|list|all)\b.{0,20}\bbookings?\b|what (have i|did i) book|what'?s booked", tl) \
            and not re.search(r"\b(cancel|move|reschedule|shift|postpone|book (a|an|the)|change)\b", tl):
        return {"kind": "list_bookings"}

    if ABORT.search(t) and not details:
        return {"kind": "abort"}

    opt = find_option(t, ctx)
    if opt is not None and not re.search(r"\b(cancel|move|reschedule)\b", tl):
        out = {"kind": "select", "option_index": opt}
        for k in ("date_expr", "time_expr"):
            if k in details:
                out[k] = details[k]
        return out

    task = None
    if re.search(r"\bcancel\b", tl):
        task = "cancel"
    elif re.search(r"\b(move|reschedule|shift|postpone|prepone|push)\b|change the (date|time|day)", tl):
        task = "reschedule"
    elif re.search(r"\b(book|find|need|get|schedule|arrange|looking for|want|send|hire|fix|repair)\b", tl) \
            or ("service" in details and not ctx.get("task")):
        task = "book"

    if task:
        same_task_running = ctx.get("task") == task and ctx.get("phase") not in (None, "idle", "done", "failed")
        if correction or (same_task_running and not re.search(r"\b(another|new|also|one more)\b", tl)
                          and task == "book" and "service" not in details):
            return {"kind": "provide_details", "is_correction": correction, **details}
        return {"kind": "start_task", "task": task, **details}

    if details:
        known = ctx.get("known", {})
        fields = {"service": "service", "area": "area", "date_expr": "date_range", "time_expr": "time_window",
                  "budget_inr": "budget"}
        is_corr = correction or bool(NO.search(t)) or any(known.get(fields[k]) for k in details if k in fields)
        if ctx.get("task") or ctx.get("pending_question"):
            return {"kind": "provide_details", "is_correction": is_corr, **details}
        if "service" in details:
            return {"kind": "start_task", "task": "book", **details}
        return {"kind": "provide_details", "is_correction": is_corr, **details}

    if YES.search(t):
        return {"kind": "confirm"}
    if NO.search(t):
        return {"kind": "decline"}
    if GREETING.search(t):
        return {"kind": "greeting"}
    return {"kind": "unclear"}


class RuleModel:
    name = "rule-v1"

    def complete(self, req: ModelRequest) -> ModelResponse:
        data = interpret(req.message, req.context)
        raw = payload_for(data)
        return ModelResponse(data=data, raw=raw, model=self.name,
                             input_tokens=estimate_tokens(req.system + req.user),
                             output_tokens=estimate_tokens(raw), cost_usd=0.0, latency_ms=0)
