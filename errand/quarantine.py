"""Untrusted review text: screened for telemetry, summarised into enums only.

Nothing here can change intent, approvals or memory. The summary is either a
keyword classifier (offline) or a separate, tool-less-in-effect model call whose only
possible output is a fixed set of enums: an injected sentence has nowhere to go.
"""
from __future__ import annotations

import json
import re

from .domain import UntrustedText
from .llm.base import ModelRequest
from .llm.prompts import REVIEW_SCHEMA, REVIEW_SYSTEM, REVIEW_TOOL

RULES = [
    ("override_phrase", r"ignore (all |any )?(the )?(previous|prior|above) instructions|disregard .{0,30}instructions"),
    ("role_tag", r"(^|\s)\[?(system|assistant|developer)\]?\s*[:\]]|</?(system|assistant)>"),
    ("approval_claim", r"already approved|approval granted|skip (the )?confirmation"),
    ("memory_write", r"save (preference|memory)|preferred_(technician|provider)\s*="),
    ("tool_call", r"\b(call|invoke) (book|cancel|reschedule)\b|book (it )?now"),
    ("link", r"https?://"),
]
THEMES = {
    "punctual": r"on time|punctual|quick", "late": r"\blate\b", "tidy": r"tidy|neat|clean(ed)? up|\bclean\b",
    "pricey": r"pricey|expensive|costly", "good_value": r"cheap|worth|value", "polite": r"polite|explained|courteous",
    "skilled": r"good work|good job|fixed|great|reliable|serviced", "unreliable": r"unreliable|no.show",
}


def screen(reviews: list[dict]) -> list[dict]:
    hits = []
    for i, r in enumerate(reviews):
        text: UntrustedText = r["text"]
        for rule, pattern in RULES:
            if re.search(pattern, text.raw(), re.I):
                hits.append({"index": i, "rule": rule, "digest": text.digest(), "excerpt": text.for_display(120)})
                break
    return hits


def summarise_offline(reviews: list[dict], flagged: set[int]) -> dict:
    clean = [r for i, r in enumerate(reviews) if i not in flagged]
    stars = [r["stars"] for r in clean] or [3]
    avg = sum(stars) / len(stars)
    sentiment = "mostly_positive" if avg >= 4 else "mixed" if avg >= 3 else "mostly_negative"
    counts: dict[str, int] = {}
    for r in clean:
        for theme, pat in THEMES.items():
            if re.search(pat, r["text"].raw(), re.I):
                counts[theme] = counts.get(theme, 0) + 1
    themes = [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:3]
    return {"sentiment": sentiment, "themes": themes, "contains_instructions": bool(flagged)}


def summarise_with_model(model, ctx, reviews: list[dict]) -> dict | None:
    """Quarantined call: sees the raw reviews, may only answer in enums, has no actions."""
    user = json.dumps({"reviews": [r["text"].raw() for r in reviews]}, ensure_ascii=False)
    req = ModelRequest(purpose="summarise_reviews", system=REVIEW_SYSTEM, user=user, tool_name=REVIEW_TOOL[0],
                       tool_description=REVIEW_TOOL[1], schema=REVIEW_SCHEMA)
    ctx.budget.charge_model()
    resp = model.complete(req)
    ctx.budget.add_cost(resp.cost_usd)
    data = resp.data or {}
    ok = (data.get("sentiment") in REVIEW_SCHEMA["properties"]["sentiment"]["enum"]
          and isinstance(data.get("themes"), list)
          and all(t in REVIEW_SCHEMA["properties"]["themes"]["items"]["enum"] for t in data["themes"])
          and isinstance(data.get("contains_instructions"), bool))
    ctx.trace.emit("model_call", purpose="summarise_reviews", intent=data.get("sentiment") if ok else None,
                   error=None if ok else "invalid summary",
                   **{"gen_ai.request.model": resp.model, "gen_ai.usage.input_tokens": resp.input_tokens,
                      "gen_ai.usage.output_tokens": resp.output_tokens, "cost_usd": resp.cost_usd,
                      "latency_ms": resp.latency_ms})
    if not ok:
        return None
    return {"sentiment": data["sentiment"], "themes": data["themes"][:4],
            "contains_instructions": data["contains_instructions"]}


THEME_WORDS = {"punctual": "punctual", "late": "sometimes late", "tidy": "tidy", "messy": "messy",
               "pricey": "a bit pricey", "good_value": "good value", "polite": "polite", "skilled": "good at the job",
               "unreliable": "unreliable"}
SENTIMENT_WORDS = {"mostly_positive": "Mostly positive", "mixed": "Mixed", "mostly_negative": "Mostly negative"}
