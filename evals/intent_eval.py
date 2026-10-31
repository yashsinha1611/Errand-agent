"""Evaluate the interpreter (the LLM's only job) on evals/data/intents.jsonl.

    python -m evals.intent_eval                       # offline rule model
    python -m evals.intent_eval --model claude --record   # live Claude, saving fixtures (needs ANTHROPIC_API_KEY)
    python -m evals.intent_eval --model recorded          # replay saved Claude outputs offline
    python -m evals.intent_eval --save-baseline           # store this run as the comparison baseline

Metrics, per split (dev / test) and per source and language:
  kind accuracy        the intent kind is one of the accepted kinds
  strict accuracy      kind right, every labelled field right, and no invented critical field
  field accuracy       per field (service, area, date, time, budget, option, provider, memory)
  confirm false-positive rate   the safety metric: "confirm" predicted where the user did not agree
  out-of-scope false-action rate   an out-of-scope request turned into a task, detail, pick or confirm
Times are compared at hour granularity because slots are hourly.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from collections import defaultdict

from errand.domain import Intent
from errand.interpreter import Interpreter
from errand.llm.base import Model
from errand.llm.rules import RuleModel

from . import golden

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "evals", "fixtures", "claude")
REPORT_MD = os.path.join(ROOT, "reports", "intent_eval.md")
REPORT_JSON = os.path.join(ROOT, "reports", "intent_eval.json")
BASELINE = os.path.join(ROOT, "reports", "intent_eval_baseline.json")

ACTION_KINDS = {"start_task", "provide_details", "select", "confirm", "memory", "list_bookings", "ask_reviews"}
CRITICAL = ("task", "service", "area", "date_expr", "time_expr", "budget_inr")
FIELDS = ("task", "service", "area", "date_expr", "time_expr", "budget_inr", "no_budget", "option_index",
          "provider_ref", "booking_ref", "memory_op", "memory_key", "memory_value")


class _NullTrace:
    def emit(self, *a, **k):
        return {}


class _NullBudget:
    def charge_model(self, *a):
        pass

    def add_cost(self, *a):
        pass


class _Ctx:
    trace = _NullTrace()
    budget = _NullBudget()


class _Meter:
    """Wraps a model to count tokens, cost and latency per call."""

    def __init__(self, inner: Model) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "?")
        self.calls = self.tin = self.tout = 0
        self.cost = 0.0
        self.latencies: list[float] = []

    def complete(self, req):
        t0 = time.monotonic()
        resp = self.inner.complete(req)
        self.latencies.append((time.monotonic() - t0) * 1000)
        self.calls += 1
        self.tin += resp.input_tokens
        self.tout += resp.output_tokens
        self.cost += resp.cost_usd
        return resp


def load(split: str | None = None, suites: tuple[str, ...] = golden.SUITES,
         version: str = golden.CURRENT) -> list[dict]:
    """Rows from the frozen golden suites; checksums are verified first."""
    rows = [r for suite in suites for r in golden.load_suite(suite, version)]
    return [r for r in rows if split is None or r["split"] == split]


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def _norm_time(v):
    if not isinstance(v, str) or ":" not in v:
        return v
    kind, rest = v.split(":", 1)
    return f"{kind}:{int(rest.split(':')[0]):02d}"


def _field_ok(field: str, expected, predicted) -> bool:
    options = _as_list(expected)
    if field == "time_expr":
        return _norm_time(predicted) in {_norm_time(o) for o in options}
    if field == "provider_ref":
        if predicted is None:
            return False
        p = predicted.lower().strip()
        for o in options:
            o = o.lower()
            if o == "last_time" or p == "last_time":
                if p == o:
                    return True
                continue
            if p == o or p.split()[0] == o.split()[0]:
                return True
        return False
    if field == "memory_value":
        return predicted is not None and str(predicted).lower() in {str(o).lower() for o in options}
    return predicted in options


def score(row: dict, pred: Intent) -> dict:
    exp = row["expected"]
    kinds = _as_list(exp["kind"])
    kind_ok = pred.kind in kinds
    got = pred.to_dict()
    fields = {}
    for f in FIELDS:
        if f in exp:
            fields[f] = _field_ok(f, exp[f], getattr(pred, f))
    spurious = []
    if kind_ok and isinstance(exp["kind"], str) and pred.kind in ("start_task", "provide_details", "select"):
        for f in CRITICAL:
            if f not in exp and got.get(f) is not None and not (f == "task" and pred.kind == "start_task"):
                spurious.append(f)
    strict = kind_ok and all(fields.values()) and not spurious
    confirm_fp = pred.kind == "confirm" and "confirm" not in kinds
    oos = set(kinds) <= {"unclear", "greeting", "decline", "abort"} or row.get("note", "") in (
        "out of scope", "near-domain out of scope: must not start a booking")
    oos_false_action = oos and pred.kind in ACTION_KINDS
    return {"kind_ok": kind_ok, "fields": fields, "spurious": spurious, "strict": strict,
            "confirm_fp": confirm_fp, "oos": oos, "oos_false_action": oos_false_action, "pred": got}


def evaluate(model: Model, rows: list[dict]) -> list[dict]:
    interp = Interpreter(model)
    out = []
    for r in rows:
        context = {"today": r["today"], **r["context"]}
        try:
            pred = interp.interpret(_Ctx(), context, r["text"])
            error = None
        except Exception as exc:  # noqa: BLE001 - a crash is scored as a wrong answer
            pred, error = Intent(kind="unclear"), f"{type(exc).__name__}: {exc}"
        s = score(r, pred)
        out.append({**{k: r.get(k) for k in ("id", "suite", "split", "source", "lang", "text", "expected")}, **s,
                    "error": error})
    return out


def block(rs: list[dict]) -> dict:
    n = len(rs)
    if not n:
        return {"n": 0}
    non_confirm = [r for r in rs if "confirm" not in _as_list(r["expected"]["kind"])]
    confirms = [r for r in rs if "confirm" in _as_list(r["expected"]["kind"])]
    oos = [r for r in rs if r["oos"]]
    field_tot, field_ok = defaultdict(int), defaultdict(int)
    for r in rs:
        for f, ok in r["fields"].items():
            field_tot[f] += 1
            field_ok[f] += ok
    return {
        "n": n,
        "kind_acc": round(sum(r["kind_ok"] for r in rs) / n, 3),
        "strict_acc": round(sum(r["strict"] for r in rs) / n, 3),
        "confirm_fp_rate": round(sum(r["confirm_fp"] for r in non_confirm) / max(1, len(non_confirm)), 3),
        "confirm_fp_count": sum(r["confirm_fp"] for r in non_confirm),
        "confirm_recall": round(sum(r["pred"].get("kind") == "confirm" for r in confirms) / max(1, len(confirms)), 3)
        if confirms else None,
        "oos_false_action_rate": round(sum(r["oos_false_action"] for r in oos) / max(1, len(oos)), 3) if oos else None,
        "fields": {f: round(field_ok[f] / field_tot[f], 3) for f in sorted(field_tot)},
    }


def summarize(results: list[dict]) -> dict:
    out: dict = {"suites": {}}
    for suite in sorted({r["suite"] for r in results}):
        rs = [r for r in results if r["suite"] == suite]
        out["suites"][suite] = {
            "all": block(rs), "dev": block([r for r in rs if r["split"] == "dev"]),
            "test": block([r for r in rs if r["split"] == "test"]),
            "by_lang": {lang: block([r for r in rs if r["lang"] == lang]) for lang in sorted({r["lang"] for r in rs})},
            "by_source": {s_: block([r for r in rs if r["source"] == s_]) for s_ in sorted({r["source"] for r in rs})},
        }
    gated = [r for r in results if r["suite"] in ("regression", "adversarial")]
    out["gated"] = {"all": block(gated), "dev": block([r for r in gated if r["split"] == "dev"]),
                    "test": block([r for r in gated if r["split"] == "test"])}
    out["confirm_fp_total"] = sum(r["confirm_fp"] for r in results)
    return out


def build_model(name: str, record: bool) -> Model:
    if name == "rule":
        return RuleModel()
    if name == "recorded":
        from errand.llm.scripted import RecordedModel
        return RecordedModel(FIXTURES, mode="replay")
    if name == "claude":
        from errand.llm.anthropic import AnthropicModel
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            sys.exit("Set ANTHROPIC_API_KEY to evaluate Claude.")
        live = AnthropicModel(key, os.environ.get("ERRAND_MODEL", "claude-haiku-5-5"))
        if record:
            from errand.llm.scripted import RecordedModel
            return RecordedModel(FIXTURES, inner=live, mode="record")
        return live
    sys.exit(f"unknown model {name}")


def _pct(x) -> str:
    return "–" if x is None else f"{x * 100:.1f}%"


def write_report(model_name: str, summary: dict, results: list[dict], meter: _Meter) -> str:
    base = None
    if os.path.exists(BASELINE):
        with open(BASELINE, encoding="utf-8") as fh:
            base = json.load(fh)
    m = golden.manifest()
    lines = [f"# Interpreter eval: {model_name}", "",
             f"Golden set **{m['version']}** (frozen {m['created']}, checksums verified) · {len(results)} utterances · "
             f"generated {datetime.datetime.now():%Y-%m-%d %H:%M} by `python -m evals.intent_eval`. "
             "Release note: `evals/data/golden/v1/RELEASE.md`; provenance: `evals/data/SOURCES.md`.", "",
             "## Suites", "",
             "| Suite | Rows | Kind | Strict (test) | Strict (all) | Confirm false positives | Out-of-scope false actions | Gate |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for suite in ("regression", "adversarial", "capability"):
        b = summary["suites"].get(suite)
        if not b:
            continue
        a = b["all"]
        lines.append(f"| {suite} | {a['n']} | {_pct(a['kind_acc'])} | {_pct(b['test'].get('strict_acc'))} | "
                     f"{_pct(a['strict_acc'])} | {a['confirm_fp_count']} | {_pct(a['oos_false_action_rate'])} | "
                     f"{m['gates'][suite]} |")
    if base:
        b0, g = base["summary"]["all"], summary["gated"]["all"]
        lines += ["", "## Against the baseline (regression + adversarial, before the dev-driven parser fixes)", "",
                  "| Metric | baseline | now |", "| --- | --- | --- |",
                  f"| Strict accuracy | {_pct(b0['strict_acc'])} | {_pct(g['strict_acc'])} |",
                  f"| Kind accuracy | {_pct(b0['kind_acc'])} | {_pct(g['kind_acc'])} |",
                  f"| Confirm false positives | {b0['confirm_fp_count']} | {g['confirm_fp_count']} |",
                  f"| Out-of-scope false actions | {_pct(b0['oos_false_action_rate'])} | {_pct(g['oos_false_action_rate'])} |"]
    lines += ["", "## By language (strict accuracy)", "", "| Suite | Language | n | strict |", "| --- | --- | --- | --- |"]
    for suite, b in summary["suites"].items():
        for lang, lb in b["by_lang"].items():
            lines.append(f"| {suite} | {lang} | {lb['n']} | {_pct(lb['strict_acc'])} |")
    lines += ["", "## By source (strict accuracy)", "", "| Suite | Source | n | strict |", "| --- | --- | --- | --- |"]
    for suite, b in summary["suites"].items():
        for src, sb in b["by_source"].items():
            lines.append(f"| {suite} | {src} | {sb['n']} | {_pct(sb['strict_acc'])} |")
    lines += ["", "## Field accuracy, regression suite", "", "| Field | accuracy |", "| --- | --- |"]
    for f, v in summary["suites"]["regression"]["all"]["fields"].items():
        lines.append(f"| {f} | {_pct(v)} |")
    if meter.calls:
        lat = sorted(meter.latencies)
        p50, p95 = lat[len(lat) // 2], lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        lines += ["", "## Cost and latency", "", f"{meter.calls} model calls · {meter.tin} input and {meter.tout} "
                  f"output tokens · ${meter.cost:.4f} · p50 {p50:.0f} ms · p95 {p95:.0f} ms"]
    for suite in ("regression", "adversarial", "capability"):
        fails = [r for r in results if r["suite"] == suite and not r["strict"]]
        lines += ["", f"## Misses: {suite} ({len(fails)})", ""]
        if not fails:
            lines.append("None.")
            continue
        lines += ["| id | split | text | expected | predicted | why |", "| --- | --- | --- | --- | --- | --- |"]
        for r in fails:
            why = [] if r["kind_ok"] else ["kind"]
            why += [f for f, ok in r["fields"].items() if not ok] + [f"invented {f}" for f in r["spurious"]]
            if r["confirm_fp"]:
                why.insert(0, "**CONFIRM FALSE POSITIVE**")
            cell = lambda v: json.dumps(v, ensure_ascii=False).replace("|", "\\|")  # noqa: E731
            lines.append(f"| {r['id']} | {r['split']} | {r['text'].replace('|', '/')} | {cell(r['expected'])} | "
                         f"{cell(r['pred'])} | {', '.join(why)} |")
    md = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(REPORT_MD), exist_ok=True)
    with open(REPORT_MD, "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(REPORT_JSON, "w", encoding="utf-8") as fh:
        json.dump({"model": model_name, "golden": m["version"], "summary": summary, "results": results}, fh,
                  ensure_ascii=False, indent=1)
    return md


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="rule", choices=["rule", "claude", "recorded"])
    ap.add_argument("--record", action="store_true", help="with --model claude: save outputs as fixtures")
    ap.add_argument("--suite", choices=golden.SUITES, action="append", help="default: all suites")
    args = ap.parse_args(argv)
    meter = _Meter(build_model(args.model, args.record))
    results = evaluate(meter, load(suites=tuple(args.suite or golden.SUITES)))
    summary = summarize(results)
    write_report(meter.name, summary, results, meter)
    print(f"{meter.name} on golden {golden.CURRENT} (checksums verified)")
    for suite, b in summary["suites"].items():
        a = b["all"]
        print(f"  {suite:11} n={a['n']:3}  kind {_pct(a['kind_acc']):>6}  strict {_pct(a['strict_acc']):>6}  "
              f"test strict {_pct(b['test'].get('strict_acc')):>6}  confirm FP {a['confirm_fp_count']}")
        for lang, lb in b["by_lang"].items():
            print(f"      {lang:10} n={lb['n']:3}  strict {_pct(lb['strict_acc'])}")
    print(f"Report: {os.path.relpath(REPORT_MD, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
