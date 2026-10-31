"""Run the 35 scenarios with the live model as interpreter, k times each, and score pass^k.

    python -m evals.live --trials 3               # needs ANTHROPIC_API_KEY (and optionally ERRAND_MODEL)

The offline suite proves the system is safe whatever the model does. This run checks the real
thing end to end: same scripted users, same faults, same 10 invariants, but Claude reads every
message. A run where the agent had to fall back to the offline parser (key, network or HTTP error)
is marked INVALID rather than counted as a pass, so outages can't inflate the score.

pass^1 is the share of all trials that passed; pass^k is the share of scenarios that passed every
trial (the reliability measure from tau-bench). Writes reports/live/scenarios_<model>.md and .json.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import traceback

from errand.config import Settings
from errand.llm.anthropic import AnthropicModel
from errand.mock.provider import MockConfig

from .harness import Harness
from .invariants import check_all
from .scenarios import SCENARIOS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_trial(sc, model_factory) -> dict:
    model = sc.model() if sc.model else model_factory()
    h = Harness(sc.id, mock=MockConfig(mode="calm", **sc.mock), caps=sc.caps or None, model=model)
    error = None
    try:
        actual = sc.run(h)
    except Exception as exc:  # noqa: BLE001
        actual, error = f"crashed: {type(exc).__name__}: {exc}", traceback.format_exc()
    invariants = check_all(h)
    calls = [e for e in h.traces() if e.get("type") == "model_call"]
    fallbacks = [e for e in calls if "falling back" in str(e.get("error", ""))]
    failed = [c.desc for c in h.checks if not c.ok] + [f"{n.split(' ')[0]}: {d}" for n, ok, d in invariants if not ok]
    status = "invalid" if fallbacks else ("pass" if error is None and not failed else "fail")
    out = {"status": status, "actual": actual, "failed": failed, "error": error,
           "model_calls": len(calls), "fallbacks": len(fallbacks),
           "fallback_error": str(fallbacks[0].get("error"))[:160] if fallbacks else None,
           "tokens_in": sum(int(e.get("gen_ai.usage.input_tokens", 0)) for e in calls),
           "tokens_out": sum(int(e.get("gen_ai.usage.output_tokens", 0)) for e in calls),
           "cost_usd": sum(float(e.get("cost_usd", 0)) for e in calls),
           "latencies": [int(e.get("latency_ms", 0)) for e in calls]}
    h.cleanup()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--only", help="comma-separated scenario ids, e.g. S01,S04,S10")
    args = ap.parse_args(argv)
    s = Settings.from_env()
    if not s.anthropic_api_key:
        sys.exit("Set ANTHROPIC_API_KEY (see docs/RUNNING.md). Run `python -m errand check-llm` first.")
    name = s.model if s.model != "rule" else "claude-haiku-5-5"
    factory = lambda: AnthropicModel(s.anthropic_api_key, name)  # noqa: E731
    chosen = [sc for sc in SCENARIOS if not args.only or sc.id in args.only.split(",")]
    rows = []
    for sc in chosen:
        trials = [run_trial(sc, factory) for _ in range(args.trials)]
        statuses = [t["status"] for t in trials]
        rows.append({"id": sc.id, "title": sc.title, "covers": sc.covers, "expected": sc.expected,
                     "trials": trials, "statuses": statuses,
                     "pass_all": all(x == "pass" for x in statuses),
                     "uses_live_model": sc.model is None})
        mark = " ".join({"pass": "✓", "fail": "✗", "invalid": "!"}[x] for x in statuses)
        print(f"{sc.id:4} {mark:12} {sc.title[:58]}")
        for t in trials:
            if t["status"] == "fail":
                print(f"       ✗ {('; '.join(t['failed']) or (t['error'] or '').strip().splitlines()[-1])[:200]}")
            if t["status"] == "invalid":
                print(f"       ! fell back to the offline parser: {t['fallback_error']}")
    valid = [t for r in rows for t in r["trials"] if t["status"] != "invalid"]
    invalid = sum(t["status"] == "invalid" for r in rows for t in r["trials"])
    pass1 = sum(t["status"] == "pass" for t in valid) / max(1, len(valid))
    passk = sum(r["pass_all"] for r in rows) / max(1, len(rows))
    cost = sum(t["cost_usd"] for r in rows for t in r["trials"])
    lat = sorted(x for r in rows for t in r["trials"] for x in t["latencies"])
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0
    os.makedirs(os.path.join(ROOT, "reports", "live"), exist_ok=True)
    md = [f"# Live scenarios: {name}", "",
          f"{len(rows)} scenarios × {args.trials} trials · {datetime.datetime.now():%Y-%m-%d %H:%M} · "
          "same scripted users, faults and 10 invariants as the offline suite, with the live model reading every message.",
          "", "| Metric | Value |", "| --- | --- |",
          f"| pass^1 (share of valid trials passed) | {pass1 * 100:.1f}% |",
          f"| pass^{args.trials} (scenarios passed on every trial) | {passk * 100:.1f}% |",
          f"| Invalid trials (fell back to offline parser) | {invalid} |",
          f"| Cost | ${cost:.4f} |", f"| Model latency p50 / p95 | {p50} / {p95} ms |", "",
          "Scenarios marked *scripted* use a scripted model on purpose (for example S31, a model that hallucinates "
          "a confirmation) and do not exercise the live model.", "",
          "| Scenario | Trials | Notes |", "| --- | --- | --- |"]
    for r in rows:
        notes = "; ".join(sorted({f for t in r["trials"] for f in t["failed"]}))[:300] or ""
        tag = "" if r["uses_live_model"] else " *(scripted)*"
        md.append(f"| {r['id']} {r['title']}{tag} | {' '.join(r['statuses'])} | {notes.replace('|', '/')} |")
    path = os.path.join(ROOT, "reports", "live", f"scenarios_{name}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    with open(path[:-3] + ".json", "w", encoding="utf-8") as fh:
        json.dump({"model": name, "trials": args.trials, "pass1": pass1, "passk": passk, "invalid": invalid,
                   "cost_usd": cost, "rows": rows}, fh, indent=1, default=str)
    k_part = f" · pass^{args.trials} {passk * 100:.1f}%" if args.trials > 1 else ""
    print(f"\npass^1 {pass1 * 100:.1f}%{k_part} · invalid {invalid} · cost ${cost:.4f} · "
          f"report {os.path.relpath(path, ROOT)}")
    return 0 if passk == 1.0 and not invalid else 1


if __name__ == "__main__":
    sys.exit(main())
