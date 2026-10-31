"""All scenarios against a recorded model: Claude's real outputs, captured once, replayed offline.

    make record-scenarios    one live run (needs ANTHROPIC_API_KEY, a few cents); writes the recording
    make evals-recorded      replays every scenario from that recording: no network, no key, reproducible

Replay is strict. A prompt that is not in the recording (because the prompt, the context or a scenario changed
after recording) makes that scenario STALE: it is never silently answered by another model. Re-record after
changing the prompt. Scenarios that bring their own scripted model (to make the model misbehave on purpose) keep
it, so they read the same in every mode.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from errand.config import Settings
from errand.llm.anthropic import AnthropicModel
from errand.llm.base import ModelRequest, ModelResponse
from errand.llm.scripted import RecordedModel
from errand.mock.provider import MockConfig

from .harness import Harness
from .invariants import check_all
from .scenarios import SCENARIOS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC_ROOT = os.path.join(ROOT, "evals", "data", "recordings", "scenarios")


class StrictRecording(RecordedModel):
    """Counts prompts missing from the recording, so a stale recording can't pass quietly."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.misses = 0
        self.hits = 0

    def complete(self, req: ModelRequest) -> ModelResponse:
        try:
            resp = super().complete(req)
        except FileNotFoundError:
            self.misses += 1
            raise
        self.hits += 1
        return resp


def run_one(sc, rec_dir: str, mode: str, inner_factory) -> dict:
    scripted = sc.model is not None
    model = sc.model() if scripted else StrictRecording(
        os.path.join(rec_dir, sc.id), inner=inner_factory() if mode == "record" else None, mode=mode)
    h = Harness(sc.id, mock=MockConfig(mode="calm", **sc.mock), caps=sc.caps or None, model=model)
    error = None
    try:
        actual = sc.run(h)
    except Exception as exc:  # noqa: BLE001
        actual, error = f"crashed: {type(exc).__name__}: {exc}", traceback.format_exc()
    invariants = check_all(h)
    failed = [c.desc for c in h.checks if not c.ok] + [f"{n.split(' ')[0]}: {d}" for n, ok, d in invariants if not ok]
    h.cleanup()
    misses = 0 if scripted else model.misses
    status = "stale" if misses else ("pass" if error is None and not failed else "fail")
    return {"id": sc.id, "title": sc.title, "status": status, "actual": actual, "failed": failed,
            "model": "scripted (own)" if scripted else "recorded", "replayed": 0 if scripted else model.hits,
            "missing": misses}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--record", action="store_true", help="call the live model once and write the recording")
    ap.add_argument("--model", help="recording to use (default: ERRAND_MODEL, else claude-haiku-5-5)")
    ap.add_argument("--dir", default=REC_ROOT, help=argparse.SUPPRESS)
    ap.add_argument("--only", help="comma-separated scenario ids")
    args = ap.parse_args(argv)
    s = Settings.from_env()
    name = args.model or (s.model if s.model != "rule" else "claude-haiku-5-5")
    rec_dir = os.path.join(args.dir, name)
    mode = "record" if args.record else "replay"
    factory = None
    if mode == "record":
        if name == "rule":
            from errand.llm.rules import RuleModel
            factory = RuleModel  # used by the self-test only
        else:
            if not s.anthropic_api_key:
                sys.exit("Recording needs ANTHROPIC_API_KEY (see docs/RUNNING.md); run `make check-llm` first.")
            factory = lambda: AnthropicModel(s.anthropic_api_key, name)  # noqa: E731
    elif not os.path.isdir(rec_dir):
        print(f"No recording for {name} yet. Run once with your key:  make record-scenarios\n"
              f"(it writes {os.path.relpath(rec_dir, ROOT)}; commit it, and this replay then runs offline).")
        return 2
    chosen = [sc for sc in SCENARIOS if not args.only or sc.id in args.only.split(",")]
    rows = [run_one(sc, rec_dir, mode, factory) for sc in chosen]
    for r in rows:
        mark = {"pass": "PASS", "fail": "FAIL", "stale": "STALE"}[r["status"]]
        print(f"{mark:5} {r['id']:4} {r['title'][:58]:58} {r['model']}")
        if r["status"] == "fail":
            print(f"      {'; '.join(r['failed'])[:240] or r['actual'][:240]}")
        if r["status"] == "stale":
            print(f"      {r['missing']} prompt(s) not in the recording: re-record (make record-scenarios)")
    n = {k: sum(r["status"] == k for r in rows) for k in ("pass", "fail", "stale")}
    rec = sum(r["model"] == "recorded" for r in rows)
    print(f"\n{mode}: {n['pass']}/{len(rows)} pass, {n['fail']} fail, {n['stale']} stale · {rec} scenarios on "
          f"{name}'s recorded outputs, {len(rows) - rec} on their own scripted model")
    if mode == "replay" and os.path.abspath(args.dir) == os.path.abspath(REC_ROOT):
        os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
        with open(os.path.join(ROOT, "reports", f"recorded_{name}.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
    return 0 if n["pass"] == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
