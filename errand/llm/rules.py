"""A deterministic, offline stand-in for the LLM interpreter, for English and Hinglish.

It reads the same structured context a real model receives and returns the same Intent
JSON. It powers the credential-free CLI and every scenario eval, and it is measured, like
the live model, on evals/data/intents.jsonl (real phrasing from SGD, Taskmaster-1, CLINC150
and Hinglish-TOP). When unsure it returns ``unclear`` rather than guessing.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .base import ModelRequest, ModelResponse, estimate_tokens, payload_for

# ----------------------------------------------------------------------------- yes / no
YES_LEAD = re.compile(
    r"^\s*[\"']?(yes|yeah|yea|yep|yup|sure|ok|okay|confirm(ed)?|go ahead|book it|do it|please do|correct|right|"
    r"sounds good|perfect|great|absolutely|definitely|fine|book for|move it|cancel the booking|"
    r"that'?s (right|fine|it|correct)|let'?s do|lets do|haan|haa|ha|ji|theek hai|thik hai|chalo|pakka|"
    r"bilkul|sahi hai|kar do|book kar do|wahi)\b", re.I)
HEDGE = re.compile(r"\?|\b(not|n't|no|nahi|nahin|nai|mat|ruko|wait|but|par|lekin|if|agar|first|pehle|maybe|"
                   r"shayad|would|could|can you|later|baad mein|all|future|automatically|unless|except|only|provided|"
                   r"as long as|assuming|in case|otherwise|warna|sirf|jab tak|na ho|nahi ho|varna)\b", re.I)
NO_LEAD = re.compile(r"^\s*(no|nope|nah|don'?t|do not|not now|never ?mind|keep it as is|cancel that|not that one|"
                     r"nahi|nahin|nai|mat karo|rehne do|nahi chahiye|abhi nahi)\b", re.I)
ABORT = re.compile(r"^\s*(never ?mind|forget it|start over|stop|leave it|nothing|rehne do|chhodo|chodo|kuch nahi)\b",
                   re.I)
GREETING = re.compile(r"^\s*(hi|hello|hey|namaste|namaskar|good (morning|afternoon|evening)|help|what can you do|"
                      r"thanks?|thank you|shukriya|dhanyavaad)\b", re.I)
NO_PREFERENCE = re.compile(
    r"^\s*(no (particular |real )?preference|no pref|anything( is)?( fine| works| ok)?|any ?time( is fine| works)?|"
    r"doesn'?t matter|whatever( works)?|either is fine|skip|koi (bhi|preference nahi|farak nahi)|kuch bhi( chalega)?|"
    r"jo bhi( chalega)?|kabhi bhi( chalega)?)\s*[.!]*\s*$", re.I)
CORRECTION = re.compile(r"\b(actually|instead|make it|change (it|that) to|rather|switch to|not .{1,20} but|"
                        r"balki|ki jagah)\b", re.I)


def is_affirmation(text: str) -> bool:
    """A clear, unconditional yes in the user's own words. Shared with the approval guard.

    Short and unhedged: "yes, book it", "haan kar do", "theek hai". Not "yes, can you...?",
    "theek hai par pehle price batao", "approve all my future bookings", "agar sasta ho to haan".
    """
    t = (text or "").strip()
    if not YES_LEAD.search(t) or HEDGE.search(t):
        return False
    return len(re.findall(r"[\w₹']+", t)) <= 7


# ----------------------------------------------------------------------------- vocabulary
SERVICE_PATTERNS = [
    ("ac_servicing", r"\bac\b|\ba\.c\.|air ?con|air conditioner"),
    ("plumber", r"plumb|\btaps?\b|leak|pipe|drain|toilet|flush|geyser|\bsink\b|\bnal\b|tapak|tanki"),
    ("electrician", r"electric|wiring|switch|socket|\bfans?\b|mcb|bijli|pankha"),
    ("cleaning", r"clean|maid|safai|safaai|jhadu"),
    ("carpenter", r"carpent|furniture|wardrobe|hinge|\bwood|badhai|almari|darwaza"),
]
AREA_PATTERNS = [("Indiranagar", r"indira ?nagar"), ("Koramangala", r"koramangala"), ("HSR Layout", r"\bhsr\b"),
                 ("Whitefield", r"white ?field")]
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12}
MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
WEEKDAYS = {"mon": "mon", "monday": "mon", "somvar": "mon", "somwar": "mon",
            "tue": "tue", "tues": "tue", "tuesday": "tue", "mangalvar": "tue", "mangalwar": "tue",
            "wed": "wed", "wednesday": "wed", "budhvar": "wed", "budhwar": "wed",
            "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu", "guruvar": "thu", "guruwar": "thu",
            "veervar": "thu",
            "fri": "fri", "friday": "fri", "shukravar": "fri", "shukrawar": "fri",
            "sat": "sat", "saturday": "sat", "satuday": "sat", "shanivar": "sat", "shaniwar": "sat",
            "sun": "sun", "sunday": "sun", "ravivar": "sun", "raviwar": "sun", "itwaar": "sun", "itwar": "sun"}
WEEKDAY_RE = r"\b(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b"
DAY_INDEX = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "ek": 1, "teen": 3, "char": 4, "paanch": 5, "saat": 7,
            "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12}
ORDINALS = {"first": 1, "1st": 1, "pehla": 1, "pehli": 1, "pehle wala": 1, "second": 2, "2nd": 2, "doosra": 2,
            "dusra": 2, "doosri": 2, "dusri": 2, "third": 3, "3rd": 3, "teesra": 3, "tisra": 3, "teesri": 3,
            "fourth": 4, "4th": 4}
PM_CUES = re.compile(r"\b(afternoon|evening|night|tonight|dopahar|dopehar|shaam|sham|raat|pm)\b")
AM_CUES = re.compile(r"\b(morning|subah|am)\b")
CLOCK = re.compile(
    r"(?P<pre>\b(?:at|by|around|from|after|before|till|until|evening|afternoon|morning|subah|sham|shaam|dopahar|"
    r"raat|do|try)\s+)?(?<![\d₹,.])\b(?P<h>\d{1,2})(?:[:.](?P<m>\d{2})|\s(?P<m2>\d{2})(?=\s*(?:am|pm|baje)))?"
    r"\s*(?P<u>am|pm|baje|o'?clock)?(?![\d,])")


def _normalize(t: str) -> str:
    t = t.lower().replace("₹", " ₹")
    t = re.sub(r"\bp\s*\.\s*m\b\.?", "pm", t)
    t = re.sub(r"\ba\s*\.\s*m\b\.?", "am", t)
    t = re.sub(r"\b(bajhe|bje)\b", "baje", t)
    t = re.sub(r"\b(tmrw|tmr|tomo|tomorow|tommorow|tommorrow)\b", "tomorrow", t)
    t = re.sub(r"\b(" + "|".join(WORD_NUM) + r")\b(?=\s*(o'?clock|in the|baje|pm|am|:\d))",
               lambda m: str(WORD_NUM[m.group(1)]), t)
    return re.sub(r"\s+", " ", t)


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


def _next_week_day(today: date, wd: str) -> date:
    return today + timedelta(days=7 - today.weekday() + DAY_INDEX.index(wd))


def _day_of_month(today: date, day: int, month: int | None = None) -> str | None:
    try:
        if month is None:
            d = date(today.year, today.month, day)
            if d < today:
                d = date(today.year + (today.month == 12), today.month % 12 + 1, day)
        else:
            d = date(today.year, month, day)
            if d < today:
                d = date(today.year + 1, month, day)
        return f"date:{d.isoformat()}"
    except ValueError:
        return None


def find_date(raw: str, today: date) -> str | None:
    t = _normalize(raw)
    has_weekday = re.search(WEEKDAY_RE, t)
    if re.search(r"day after tomorrow|\bparso\b|\bparson\b", t):
        return "day_after_tomorrow"
    if re.search(r"\btomorrow\b|\bkal\b", t):
        return "tomorrow"
    if re.search(r"\b(next|agle|agla) (week|hafte|haftey|hafta)\b", t) and not has_weekday:
        return "next_week"
    if re.search(r"\b(this|is|iss) (week|hafte|haftey|hafta)\b|\bwithin the week\b", t) and not has_weekday:
        return "this_week"
    if re.search(r"\bweekend\b", t):
        return "weekend"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?(?: of)? " + MONTH_RE + r"\b", t)
    if m:
        return _day_of_month(today, int(m.group(1)), MONTHS[m.group(2)])
    m = re.search(r"\b" + MONTH_RE + r" (\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        return _day_of_month(today, int(m.group(2)), MONTHS[m.group(1)])
    if has_weekday:
        word = has_weekday.group(1)
        wd = WEEKDAYS[word]
        if re.search(r"\bnext " + word + r"\b|\b" + word + r" (of )?next week\b|\bagle " + word, t):
            return f"date:{_next_week_day(today, wd).isoformat()}"
        return f"weekday:{wd}"
    m = re.search(r"\b(\d{1,2})(st|nd|rd|th)\b", t)
    if m and 1 <= int(m.group(1)) <= 31:
        return _day_of_month(today, int(m.group(1)))
    if re.search(r"\b(today|tonight|aaj|aj|immediately|right away|asap|as soon as possible|"
                 r"this (morning|afternoon|evening))\b|\babhi (chahiye|bhej|bulao|aa)", t):
        return "today"
    return None


def _to_24h(h: int, minute: int, unit: str | None, context: str) -> tuple[int, int]:
    if unit == "pm" and h < 12:
        h += 12
    elif unit == "am" and h == 12:
        h = 0
    elif unit not in ("am", "pm"):
        if PM_CUES.search(context) and h < 12:
            h += 12
        elif not AM_CUES.search(context) and h < 8:
            h += 12
    return h, minute


def _parse_clock(seg: str, context: str, bare_ok: bool = False) -> tuple[int, int] | None:
    q = re.search(r"\b(quarter|half) (past|to) (\d{1,2})\b", seg)
    if q:
        h, minute = int(q.group(3)), {"quarter": 15, "half": 30}[q.group(1)]
        if q.group(2) == "to":
            h, minute = h - 1, 60 - minute
        return _to_24h(h, minute, None, context)
    m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s+in the (morning|afternoon|evening|night)\b", seg)
    if m:
        return _to_24h(int(m.group(1)), int(m.group(2) or 0), None, m.group(3))
    for i, c in enumerate(CLOCK.finditer(seg)):
        h, minute = int(c.group("h")), int(c.group("m") or c.group("m2") or 0)
        unit = (c.group("u") or "").replace("'", "") or None
        if not (c.group("pre") or unit or c.group("m") or (bare_ok and i == 0)):
            continue
        if 0 < h <= 23 and minute < 60:
            return _to_24h(h, minute, unit, context)
    return None


def find_time(raw: str) -> str | None:
    t = _normalize(raw)
    t = re.sub(r"(₹|rs\.?|inr)\s?[\d,]+k?", " ", t)
    t = re.sub(r"\b\d{3,}\b(?!\s*(am|pm|baje))", " ", t)
    t = re.sub(r"\b\d{1,2}(st|nd|rd|th)\b", " ", t)
    t = re.sub(r"day after tomorrow", " ", t)  # the "after" in it is not a time bound
    m = re.search(r"(.{0,30}?)\s*se pehle (nahi|mat)\b", t)
    if m and (clock := _parse_clock(m.group(1), t, bare_ok=True)):
        return f"after:{clock[0]:02d}:00"
    m = re.search(r"(not before|no earlier than|after|from|starting|earliest)\s+(.{1,30})", t)
    if m and (clock := _parse_clock(m.group(2), t, bare_ok=True)):
        return f"after:{clock[0]:02d}:00"
    m = re.search(r"(.{0,30}?)\s*(ke baad|ke bad)\b", t)
    if m and (clock := _parse_clock(m.group(1), t, bare_ok=True)):
        return f"after:{clock[0]:02d}:00"
    m = re.search(r"(not after|no later than|before|until|latest)\s+(.{1,30})", t)
    if m and (clock := _parse_clock(m.group(2), t, bare_ok=True)):
        return f"before:{clock[0]:02d}:00"
    m = re.search(r"(.{0,30}?)\s*(se pehle|tak)\b", t)
    if m and re.search(r"baje|am|pm", m.group(1)) and (clock := _parse_clock(m.group(1), t)):
        return f"before:{clock[0]:02d}:00"
    m = re.search(r"\b(evening|afternoon|morning|subah|sham|shaam|dopahar|raat) (\d{1,2})(?:[:.](\d{2}))?\b", t)
    if m:
        h, minute = _to_24h(int(m.group(2)), int(m.group(3) or 0), None, m.group(1))
        return f"at:{h:02d}:{minute:02d}"
    if (clock := _parse_clock(t, t)):
        return f"at:{clock[0]:02d}:{clock[1]:02d}"
    if re.search(r"\bmornings?\b|\bsubah\b", t):
        return "morning"
    if re.search(r"\bafternoons?\b|\bdopahar\b|\bdopehar\b", t):
        return "afternoon"
    if re.search(r"\bevenings?\b|after work|\bsham\b|\bshaam\b|\braat\b|\btonight\b", t):
        return "evening"
    if re.search(r"any ?time|whenever|kabhi bhi", t):
        return "any"
    return None


def find_budget(raw: str) -> tuple[int | None, bool]:
    t = _normalize(raw)
    if re.search(r"no budget|any price|price doesn'?t matter|whatever it costs|any budget|budget doesn'?t matter|"
                 r"koi budget nahi|budget nahi hai|koi limit nahi|price matter nahi", t):
        return None, True
    m = re.search(r"(under|below|less than|max(?:imum)?|up ?to|within|budget(?: of| is)?|no more than|cheaper than|"
                  r"nothing (?:above|over|more than)|not (?:above|over|more than))\s*(?:₹|rs\.?|inr)?\s*([\d,]+)\s*(k)?\b",
                  t)
    if m:
        value = int(m.group(2).replace(",", "") or 0) * (1000 if m.group(3) else 1)
        if value >= 50:
            return value, False
    m = re.search(r"(?:₹|rs\.?|inr)?\s*([\d,]+)\s*(k)?\s*(se kam|tak|ke andar|ke under|se zyada nahi|se upar nahi)\b", t)
    if m:
        value = int(m.group(1).replace(",", "") or 0) * (1000 if m.group(2) else 1)
        if value >= 100:
            return value, False
    return None, False


def find_provider(t: str, ctx: dict) -> str | None:
    tl = _normalize(t)
    if re.search(r"same (\w+ )?(one |person |guy )?as (last time|before)|same one as last|last time'?s|"
                 r"(the )?usual (plumber|electrician|cleaner|carpenter|one)|same (plumber|electrician|cleaner|carpenter)|"
                 r"pichli baar|pichhli baar|last time wala", tl):
        return "last_time"
    for name in ctx.get("provider_names", []):
        words = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2 and w not in
                 ("and", "the", "services", "service", "plumbing", "plumbers", "electric", "electricals", "home")]
        if name.lower() in tl or (words and re.search(rf"\b{re.escape(words[0])}\b", tl)):
            return name
    if re.search(r"\b(book|about|with|for|reviews of)\s+(them|that one|that provider|him|her)\b|"
                 r"\bthat (one|provider|plumber|electrician)\b|\b(unko|usko|unhe|use) (book|bulao|bhejo)", tl):
        return "that"
    return None


def find_option(t: str, ctx: dict) -> int | None:
    options = ctx.get("options") or []
    if not options:
        return None
    tl = _normalize(t).strip()
    if re.fullmatch(r"#?\s*(\d)", tl):
        n = int(re.sub(r"\D", "", tl))
        return n if 1 <= n <= len(options) else None
    m = re.search(r"\b(option|number|no\.?|#)\s*(\d)\b", tl)
    if m and 1 <= int(m.group(2)) <= len(options):
        return int(m.group(2))
    for word, n in ORDINALS.items():
        if re.search(rf"\b{word}\b", tl) and n <= len(options):
            return n
    if re.search(r"\b(last one|the last|aakhri|akhri)\b", tl):
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
    tl = _normalize(t)
    if re.search(r"what do you (remember|know) about me|what('?s| is) (saved|stored|in (your )?memory)|"
                 r"show (me )?(my )?(memory|preferences)|^my preferences|what have you saved|"
                 r"kya yaad hai|mere baare mein kya (pata|yaad)", tl):
        return {"kind": "memory", "memory_op": "view"}
    if re.search(r"\b(forget|bhool jao|bhul jao)\b", tl):
        obj = re.sub(r"\b(forget|bhool jao|bhul jao|about|my|the|mera|meri|mere)\b", " ", tl).strip(" .!")
        if obj in ("", "it", "that", "this"):
            return None
        if re.search(r"area|address|location|home|where i live|ghar", obj):
            return {"kind": "memory", "memory_op": "forget", "memory_key": "home_area"}
        if re.search(r"time|earliest|mornings?|before|after|samay", obj):
            return {"kind": "memory", "memory_op": "forget", "memory_key": "earliest_time"}
        if re.search(r"everything|all|sab", obj):
            return {"kind": "memory", "memory_op": "forget"}
        service = find_service(obj)
        if service or re.search(r"preferred|usual|favourite|favorite", obj):
            return {"kind": "memory", "memory_op": "forget", "memory_key": "preferred_technician",
                    "memory_value": service}
    remember = re.search(r"\b(remember|save|note( down)?|keep in mind|yaad rakh(o|na)?)\b", tl)
    correct = re.search(r"\b(i (now )?live in|i'?ve moved to|i moved to|i have moved to|my (home )?area is|"
                        r"my address is now|maine ghar badal)", tl)
    if remember or correct:
        op = "correct" if correct and not remember else "remember"
        area = find_area(t)
        if area:
            return {"kind": "memory", "memory_op": op, "memory_key": "home_area", "memory_value": area}
        when = find_time(t)
        if when and when.startswith(("after:", "at:", "before:")):
            return {"kind": "memory", "memory_op": op, "memory_key": "earliest_time", "memory_value": when.split(":", 1)[1]}
        prov = find_provider(t, ctx)
        if prov and prov not in ("that", "last_time"):
            return {"kind": "memory", "memory_op": op, "memory_key": "preferred_technician", "memory_value": prov}
    if re.search(r"never .{0,40}before|nothing before|from now on .{0,30}(after|before)|always after|"
                 r"se pehle kabhi nahi|hamesha .{0,20}ke baad", tl):
        when = find_time(t)
        if when and when.startswith(("after:", "before:")):
            return {"kind": "memory", "memory_op": "remember", "memory_key": "earliest_time",
                    "memory_value": when.split(":", 1)[1]}
    return None


BOOK_VERBS = (r"\b(book|find|need|get|schedule|arrange|looking for|want|send|hire|fix|repair|chahiye|bhej do|bhejo|"
              r"bula do|bulao|karwana|karwani|karna hai|book karo|book kar do|dhundo|dhoondo|mil sakta|aa sakta|"
              r"search)\b")
RESCHEDULE_VERBS = (r"\b(move|reschedule|reshedule|reschdule|shift|postpone|prepone|push)\b|change the (date|time|day)|"
                    r"\bbadal do\b|\baage kar")


def interpret(message: str, ctx: dict) -> dict:
    t = message.strip()
    if not t:
        return {"kind": "unclear"}
    today = date.fromisoformat(ctx.get("today", date.today().isoformat()))
    tl = _normalize(t)

    mem = memory_intent(t, ctx)
    if mem:
        return mem

    if NO_PREFERENCE.match(t) and ctx.get("task") and ctx.get("phase") == "collecting" \
            and not ctx.get("awaiting_approval"):
        # "no preference" answers the optional time/budget question: skip only what isn't known yet
        known = ctx.get("known") or {}
        out: dict = {"kind": "provide_details"}
        if not known.get("time_window"):
            out["time_expr"] = "any"
        if not known.get("budget"):
            out["no_budget"] = True
        return out

    details: dict = {}
    if (s := find_service(tl)):
        details["service"] = s
    if (a := find_area(tl)):
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
    correction = bool(CORRECTION.search(tl)) and bool(ctx.get("task"))
    substantive = set(details) - {"provider_ref"}
    affirmative = is_affirmation(t)

    if (ctx.get("awaiting_approval") or ctx.get("pending_question")) and not substantive:
        if affirmative:
            return {"kind": "confirm"}
        if NO_LEAD.search(tl):
            return {"kind": "decline"}
    if affirmative and not substantive:
        return {"kind": "confirm"}

    if re.search(r"\breviews?\b|what do (people|others|customers|users) say|ratings? (of|for)|how good is|"
                 r"is .{2,30} (any good|reliable|good)|kaisa hai|kaise hain|log kya kehte", tl):
        out = {"kind": "ask_reviews"}
        ref = details.get("provider_ref")
        if not ref:
            m = re.search(r"(?i:about|of|for|is)\s+((?:[A-Z][\w&']*\s?){1,4})", t) or \
                re.search(r"((?:[A-Z][\w&']*\s?){1,4})\s+(?i:ke reviews|ka review|kaisa|kaise)", t)
            ref = m.group(1).strip() if m else None
        if ref:
            out["provider_ref"] = ref[:60]
        return out

    if re.search(r"\b(my|upcoming|current|show|list|all|meri|mere)\b.{0,20}\bbookings?\b|what (have i|did i) book|"
                 r"what'?s booked|coming up|scheduled for me|bookings dikhao", tl) \
            and not re.search(r"\b(cancel|move|reschedule|shift|postpone|book (a|an|the)|change)\b", tl):
        return {"kind": "list_bookings"}

    if ABORT.search(tl) and not substantive and not ctx.get("awaiting_approval"):
        return {"kind": "abort"}
    if NO_LEAD.search(tl) and not substantive:
        return {"kind": "decline"}

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
    elif re.search(RESCHEDULE_VERBS, tl):
        task = "reschedule"
    elif re.search(BOOK_VERBS, tl) or ("service" in details and not ctx.get("task")):
        task = "book"
    home_hint = "service" in details or "provider_ref" in details or "booking_ref" in details
    if task == "book" and not home_hint and not ctx.get("task"):
        return {"kind": "unclear"}  # "book a place to stay", "get gum out of my rug": not a home service
    if task in ("cancel", "reschedule") and not home_hint and not ctx.get("task") \
            and not re.search(r"\b(my|booking|appointment|visit|meri|mera)\b", tl):
        return {"kind": "unclear"}

    if task:
        same_task_running = ctx.get("task") == task and ctx.get("phase") not in (None, "idle", "done", "failed")
        if correction or (same_task_running and task == "book" and "service" not in details
                          and not re.search(r"\b(another|new|also|one more)\b", tl)):
            return {"kind": "provide_details", "is_correction": correction, **details}
        return {"kind": "start_task", "task": task, **details}

    if details:
        if not ctx.get("task") and not ctx.get("pending_question") and "service" not in details:
            return {"kind": "unclear"}  # details with nothing to attach them to
        known = ctx.get("known", {})
        fields = {"service": "service", "area": "area", "date_expr": "date_range", "time_expr": "time_window",
                  "budget_inr": "budget"}
        is_corr = correction or bool(NO_LEAD.search(tl)) or any(known.get(fields[k]) for k in details if k in fields)
        if ctx.get("task") or ctx.get("pending_question"):
            return {"kind": "provide_details", "is_correction": is_corr, **details}
        return {"kind": "start_task", "task": "book", **details}

    if GREETING.search(tl):
        return {"kind": "greeting"}
    return {"kind": "unclear"}


YES = YES_LEAD  # quick lexical check kept for older callers


class RuleModel:
    name = "rule-v1"

    def complete(self, req: ModelRequest) -> ModelResponse:
        data = interpret(req.message, req.context)
        raw = payload_for(data)
        return ModelResponse(data=data, raw=raw, model=self.name,
                             input_tokens=estimate_tokens(req.system + req.user),
                             output_tokens=estimate_tokens(raw), cost_usd=0.0, latency_ms=0)
