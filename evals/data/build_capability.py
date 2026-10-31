"""Build evals/data/capability.jsonl: harder utterances that measure headroom, not regressions.

A regression suite should pass at ~99% and gate the build. A capability suite should be hard
enough that a good system scores around 70%, so the number can move. These rows are
handwritten from patterns seen in real assistant traffic: rambling voice-style messages,
abbreviations, mid-sentence corrections, emoji approvals, two requests in one message, Hindi
in Devanagari, and Kannada-English mixing common in Bangalore. Rows marked
"needs native review" must be checked by a fluent speaker before they are trusted.

    python -m evals.data.build_capability
"""
from __future__ import annotations

import hashlib
import json
import os

from .build_intents import APPROVAL, COLLECT, NONE, SAFE_NON_ACTION, TODAY

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "capability.jsonl")
DONE_BOOKED = {"task": "book", "phase": "done", "known": {"service": "plumber"}}
ROWS: list[dict] = []


def row(text: str, expected: dict, context: dict | None = None, *, lang: str = "en", pattern: str,
        note: str | None = None) -> None:
    rid = f"capability-{len(ROWS) + 1:03d}"
    ROWS.append({"id": rid, "source": "capability", "license": "MIT (this repo)", "lang": lang, "text": text,
                 "context": context or {}, "expected": expected, "pattern": pattern, "note": note,
                 "source_id": None, "original": None, "adaptation": None})


# --- rambling, voice-transcript style
row("umm hi so the kitchen tap has been dripping all night and i was wondering if someone could maybe come over "
    "tomorrow morning like before 11 if thats possible",
    {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "tomorrow",
     "time_expr": ["before:11:00", "morning"]}, pattern="voice-style ramble")
row("I was wondering whether it might be possible to arrange for somebody to take a look at our ceiling fan, it has "
    "been making a rattling noise", {"kind": "start_task", "task": "book", "service": "electrician"},
    pattern="indirect, polite")
row("this coming Friday around ten thirty", {"kind": "provide_details", "date_expr": "weekday:fri",
                                             "time_expr": "at:10:30"}, COLLECT, pattern="spelled-out time")
row("the ac is not cooling at all and its 38 degrees", {"kind": "start_task", "task": "book",
                                                       "service": "ac_servicing"}, pattern="symptom, stray number")

# --- abbreviations and typos
row("need plmbr 2mrw aftr 5 hsr", {"kind": "start_task", "task": "book", "service": "plumber", "area": "HSR Layout",
                                   "date_expr": "tomorrow", "time_expr": "after:17:00"}, pattern="abbreviations")
row("elec needed asap koramangala switchboard sparking",
    {"kind": "start_task", "task": "book", "service": "electrician", "area": "Koramangala", "date_expr": "today"},
    pattern="abbreviations, urgency")
row("budget is like 800 bucks max", {"kind": "provide_details", "budget_inr": 800}, COLLECT, pattern="slang budget")
row("price doesn't matter just send the best one", {"kind": "provide_details", "no_budget": True}, COLLECT,
    pattern="no budget, informal")

# --- implicit services
row("There's water dripping from the ceiling of the bathroom", {"kind": "start_task", "task": "book",
                                                                "service": "plumber"}, pattern="implicit service")
row("the switchboard in the hall gives a small shock when I touch it",
    {"kind": "start_task", "task": "book", "service": "electrician"}, pattern="implicit service")
row("can I get someone for termite treatment in my cupboard", {"kind": SAFE_NON_ACTION},
    pattern="unsupported service (pest control)")

# --- corrections, negation, relative time
row("Tuesday, no wait, Wednesday morning works better",
    {"kind": "provide_details", "date_expr": "weekday:wed", "time_expr": "morning"}, COLLECT,
    pattern="mid-sentence correction")
row("not on Saturday, any other day this week is fine", {"kind": "provide_details", "date_expr": "this_week"},
    COLLECT, pattern="negated day")
row("end of the week, evening", {"kind": "provide_details", "date_expr": ["weekday:fri", "weekend", "this_week"],
                                 "time_expr": "evening"}, COLLECT, pattern="vague date")
row("in about 2 hours if anyone's free", {"kind": "provide_details", "date_expr": "today"}, COLLECT,
    pattern="relative time")
row("what's the earliest you can get someone here today", {"kind": "provide_details", "date_expr": "today"},
    COLLECT, pattern="earliest available")
row("anyone available right now?", {"kind": "provide_details", "date_expr": "today"}, COLLECT, pattern="now")
row("ok wait actually make it 4 not 3", {"kind": "provide_details", "time_expr": "at:16:00"}, APPROVAL,
    pattern="correction during approval")
row("sure, but can you make it 10 instead?", {"kind": "provide_details", "time_expr": "at:10:00"}, APPROVAL,
    pattern="yes-but change")

# --- confirmations in the wild
row("yes yes yes book it before it's gone!!", {"kind": "confirm"}, APPROVAL, pattern="emphatic yes")
row("👍", {"kind": "confirm"}, APPROVAL, pattern="emoji approval")
row("ok", {"kind": "confirm"}, APPROVAL, pattern="minimal yes")

# --- several requests or references to earlier context
row("book a plumber tomorrow and also cancel my AC servicing",
    {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "tomorrow"},
    pattern="two requests", note="the second request should be acknowledged; only the first is scored")
row("I need an electrician and a plumber tomorrow",
    {"kind": "start_task", "task": "book", "service": ["electrician", "plumber"], "date_expr": "tomorrow"},
    pattern="two services")
row("Same guy as last time for the AC please", {"kind": "start_task", "task": "book", "service": "ac_servicing",
                                                "provider_ref": "last_time"}, pattern="reference to history")
row("same plumber as before but on sunday", {"kind": "start_task", "task": "book", "service": "plumber",
                                             "provider_ref": "last_time", "date_expr": "weekday:sun"},
    pattern="reference to history")
row("can you move it to the evening instead", {"kind": "start_task", "task": "reschedule", "time_expr": "evening"},
    DONE_BOOKED, pattern="pronoun for the last booking")
row("cancel it", {"kind": "start_task", "task": "cancel"}, DONE_BOOKED, pattern="pronoun for the last booking")
row("can you book Ravi again, the electrician one, for saturday afternoon",
    {"kind": "start_task", "task": "book", "service": "electrician", "provider_ref": "Ravi",
     "date_expr": "weekday:sat", "time_expr": "afternoon"}, pattern="named provider, disambiguated")
row("pls dont send sharma plumbing again, anyone else is fine",
    {"kind": "start_task", "task": "book", "service": "plumber"}, pattern="negative provider preference",
    note="exclusion is not representable in the schema yet; the task itself is scored")

# --- out of scope that looks close
row("remind me tomorrow to pay the plumber", {"kind": SAFE_NON_ACTION}, pattern="reminder, mentions a service")
row("Is it safe to let the cleaner in when I'm not home?", {"kind": SAFE_NON_ACTION},
    pattern="advice, mentions a service")

# --- Hinglish, harder
row("plumber chahiye but not before 11, budget around 1200 max",
    {"kind": "start_task", "task": "book", "service": "plumber", "time_expr": "after:11:00", "budget_inr": 1200},
    lang="hinglish", pattern="mixed clauses")
row("bhaiya kal shaam ko AC ka gas bharna hai",
    {"kind": "start_task", "task": "book", "service": "ac_servicing", "date_expr": "tomorrow", "time_expr": "evening"},
    lang="hinglish", pattern="colloquial task verb")
row("safai wali didi ko parso bulana hai, 3 ghante ke liye",
    {"kind": "start_task", "task": "book", "service": "cleaning", "date_expr": "day_after_tomorrow"},
    lang="hinglish", pattern="duration, colloquial")
row("kal nahi, parso", {"kind": "provide_details", "date_expr": "day_after_tomorrow"}, COLLECT, lang="hinglish",
    pattern="negated then corrected")
row("subah jaldi, 7 baje tak", {"kind": "provide_details", "time_expr": "before:07:00"}, COLLECT, lang="hinglish",
    pattern="early morning bound")

# --- Hindi in Devanagari
row("कल सुबह प्लंबर चाहिए", {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "tomorrow",
                             "time_expr": "morning"}, lang="hindi", pattern="Devanagari", note="needs native review")
row("हाँ, बुक कर दो", {"kind": "confirm"}, APPROVAL, lang="hindi", pattern="Devanagari yes", note="needs native review")
row("नहीं, अभी नहीं", {"kind": "decline"}, APPROVAL, lang="hindi", pattern="Devanagari no", note="needs native review")

# --- Kannada-English (Bangalore)
row("naale beligge plumber beku Indiranagar alli",
    {"kind": "start_task", "task": "book", "service": "plumber", "area": "Indiranagar", "date_expr": "tomorrow",
     "time_expr": "morning"}, lang="kanglish", pattern="Kannada-English", note="needs native review")
row("howdu, book maadi", {"kind": "confirm"}, APPROVAL, lang="kanglish", pattern="Kannada-English yes",
    note="needs native review")


def main() -> None:
    for r in ROWS:
        r["split"] = "dev" if int(hashlib.sha256(r["id"].encode()).hexdigest(), 16) % 2 == 0 else "test"
        r["today"] = TODAY
    with open(OUT, "w", encoding="utf-8") as fh:
        for r in ROWS:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(ROWS)} rows to {os.path.relpath(OUT)}")


if __name__ == "__main__":
    main()
