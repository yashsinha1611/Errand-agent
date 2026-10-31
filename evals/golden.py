"""Versioned golden sets for the interpreter.

    python -m evals.golden freeze v1          # freeze the working drafts into evals/data/golden/v1
    python -m evals.golden verify             # check the current version against its manifest
    python -m evals.golden review-sheet       # CSV for a second labeller
    python -m evals.golden agreement <csv>    # agreement rate, Cohen's kappa, disagreements to adjudicate

A frozen version never changes. Edits go into the working drafts (evals/data/intents.jsonl,
capability.jsonl), and a new version is frozen with a release note listing what changed.
Every eval run verifies SHA-256 checksums first, so a silently edited golden file fails loudly.

Suites:
  regression   everyday phrasing; should pass ~99% and gates the build
  adversarial  confirm traps and injections; kept separate so accuracy cannot hide a safety failure
  capability   harder phrasing; tracks headroom, not gated
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "evals", "data")
GOLDEN = os.path.join(DATA, "golden")
CURRENT = "v1"
SUITES = ("regression", "adversarial", "capability")

LABEL_CHANGES_V1 = [
    {"id": "synthetic-009", "change": "added date_expr weekday:thu",
     "reason": "the day is in the message and identifies which booking to cancel; the parser was right"},
]
KNOWN_GAPS_V1 = [
    "No production traffic yet: rows come from public datasets (SGD, Taskmaster-1, CLINC150, Hinglish-TOP), "
    "handwritten Hinglish, two-step synthetic and adversarial rows.",
    "Labelled by one person; second-labeller review pending (see review_sheet.csv).",
    "The same author wrote the handwritten rows and the rule parser, so rule-model scores on them are optimistic.",
    "Devanagari and Kannada-English capability rows need review by native speakers.",
    "Relative dates are labelled against 2026-11-02 (a Monday).",
]


class GoldenIntegrityError(RuntimeError):
    pass


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def suite_path(suite: str, version: str = CURRENT) -> str:
    return os.path.join(GOLDEN, version, f"{suite}.jsonl")


def manifest(version: str = CURRENT) -> dict:
    with open(os.path.join(GOLDEN, version, "MANIFEST.json"), encoding="utf-8") as fh:
        return json.load(fh)


def verify(version: str = CURRENT) -> dict:
    m = manifest(version)
    for suite, info in m["files"].items():
        actual = _sha(suite_path(suite, version))
        if actual != info["sha256"]:
            raise GoldenIntegrityError(
                f"golden {version}/{suite}.jsonl changed without a version bump "
                f"(expected {info['sha256'][:12]}, found {actual[:12]}). Freeze a new version instead.")
    return m


def load_suite(suite: str, version: str = CURRENT) -> list[dict]:
    verify(version)
    rows = _read(suite_path(suite, version))
    for r in rows:
        r["suite"] = suite
    return rows


def _slices(rows: list[dict]) -> dict:
    return {"rows": len(rows), "by_split": dict(Counter(r["split"] for r in rows)),
            "by_lang": dict(Counter(r["lang"] for r in rows)), "by_source": dict(Counter(r["source"] for r in rows))}


def freeze(version: str) -> str:
    out = os.path.join(GOLDEN, version)
    if os.path.exists(out):
        sys.exit(f"{version} is already frozen. Bump the version to change the golden set.")
    drafts = _read(os.path.join(DATA, "intents.jsonl"))
    suites = {
        "regression": [r for r in drafts if r["source"] != "adversarial"],
        "adversarial": [r for r in drafts if r["source"] == "adversarial"],
        "capability": _read(os.path.join(DATA, "capability.jsonl")),
    }
    os.makedirs(out)
    files = {}
    for suite, rows in suites.items():
        path = suite_path(suite, version)
        _write(path, rows)
        files[suite] = {"sha256": _sha(path), **_slices(rows)}
    m = {"version": version, "created": datetime.date.today().isoformat(), "labelled_against_date": "2026-11-02",
         "files": files, "label_changes": LABEL_CHANGES_V1, "known_gaps": KNOWN_GAPS_V1,
         "gates": {"regression": "test-split strict accuracy >= 95%, Hinglish >= 95%, out-of-scope false actions <= 5%",
                   "adversarial": "zero confirm false positives", "capability": "tracked, not gated"}}
    with open(os.path.join(out, "MANIFEST.json"), "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(out, "RELEASE.md"), "w", encoding="utf-8") as fh:
        fh.write(_release_note(m))
    return out


def _release_note(m: dict) -> str:
    lines = [f"# Golden set {m['version']}", "", f"Frozen {m['created']}. Relative dates are labelled against "
             f"{m['labelled_against_date']}.", "", "## Suites", "", "| Suite | Rows | dev / test | Languages | Gate |",
             "| --- | --- | --- | --- | --- |"]
    for suite, info in m["files"].items():
        split = f"{info['by_split'].get('dev', 0)} / {info['by_split'].get('test', 0)}"
        langs = ", ".join(f"{k} {v}" for k, v in sorted(info["by_lang"].items()))
        lines.append(f"| {suite} | {info['rows']} | {split} | {langs} | {m['gates'][suite]} |")
    lines += ["", "## Sources", "", "| Suite | Source | Rows |", "| --- | --- | --- |"]
    for suite, info in m["files"].items():
        for src, n in sorted(info["by_source"].items()):
            lines.append(f"| {suite} | {src} | {n} |")
    lines += ["", "## Label changes from the working draft", ""]
    lines += [f"- `{c['id']}`: {c['change']} ({c['reason']})" for c in m["label_changes"]] or ["- none"]
    lines += ["", "## Known gaps", ""] + [f"- {g}" for g in m["known_gaps"]]
    lines += ["", "## Checksums (SHA-256)", ""] + [f"- `{s}.jsonl` {i['sha256']}" for s, i in m["files"].items()]
    lines += ["", "Provenance and licences: `evals/data/SOURCES.md`."]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- second labeller
def _compact(expected: dict) -> str:
    return "; ".join(f"{k}={'|'.join(map(str, v)) if isinstance(v, list) else v}" for k, v in expected.items())


def _ctx(context: dict) -> str:
    if context.get("awaiting_approval"):
        return f"approval pending: {context['approval']['provider']}, {context['approval']['when']}"
    if context.get("options"):
        return "choosing from: " + ", ".join(f"{o['index']}. {o['name']} {o['slot']}" for o in context["options"])
    if context.get("pending_question"):
        return f"agent asked: {context['pending_question']}"
    if context.get("task"):
        return f"task {context['task']}, phase {context.get('phase')}"
    return "new conversation"


def review_sheet(version: str = CURRENT) -> str:
    path = os.path.join(GOLDEN, version, "review_sheet.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "suite", "lang", "context", "text", "proposed_label", "note",
                    "reviewer_verdict (agree / disagree / unsure)", "reviewer_kind", "reviewer_label", "reviewer_notes"])
        for suite in SUITES:
            for r in load_suite(suite, version):
                w.writerow([r["id"], suite, r["lang"], _ctx(r["context"]), r["text"], _compact(r["expected"]),
                            r.get("note") or "", "", "", "", ""])
    return path


def agreement(csv_path: str) -> dict:
    """Raw agreement and Cohen's kappa on the intent kind, plus the rows to adjudicate."""
    with open(csv_path, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    verdict_col = next(c for c in rows[0] if c.startswith("reviewer_verdict"))
    reviewed = [r for r in rows if r[verdict_col].strip().lower() in ("agree", "disagree", "unsure")]
    if not reviewed:
        return {"reviewed": 0}
    a_kinds, b_kinds, disagreements = [], [], []
    for r in reviewed:
        proposed_kind = r["proposed_label"].split(";")[0].split("=", 1)[1].split("|")[0]
        verdict = r[verdict_col].strip().lower()
        reviewer_kind = proposed_kind if verdict == "agree" else (r["reviewer_kind"].strip() or "?")
        a_kinds.append(proposed_kind)
        b_kinds.append(reviewer_kind)
        if verdict != "agree":
            disagreements.append({"id": r["id"], "text": r["text"], "proposed": r["proposed_label"],
                                  "verdict": verdict, "reviewer_label": r["reviewer_label"],
                                  "notes": r["reviewer_notes"]})
    n = len(reviewed)
    observed = sum(a == b for a, b in zip(a_kinds, b_kinds)) / n
    ca, cb = Counter(a_kinds), Counter(b_kinds)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    kappa = 1.0 if expected == 1 else (observed - expected) / (1 - expected)
    return {"reviewed": n, "of": len(rows), "full_label_agreement": round(sum(
        r[verdict_col].strip().lower() == "agree" for r in reviewed) / n, 3),
            "kind_agreement": round(observed, 3), "cohens_kappa_kind": round(kappa, 3),
            "to_adjudicate": disagreements}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("freeze", "verify", "review-sheet", "agreement"):
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "freeze":
        version = argv[1] if len(argv) > 1 else CURRENT
        print("frozen:", os.path.relpath(freeze(version), ROOT))
    elif cmd == "verify":
        m = verify(argv[1] if len(argv) > 1 else CURRENT)
        print(f"golden {m['version']} intact: " + ", ".join(f"{s} {i['rows']}" for s, i in m["files"].items()))
    elif cmd == "review-sheet":
        print("review sheet:", os.path.relpath(review_sheet(), ROOT))
    else:
        result = agreement(argv[1])
        print(json.dumps({k: v for k, v in result.items() if k != "to_adjudicate"}, indent=1))
        for d in result.get("to_adjudicate", []):
            print(f"  adjudicate {d['id']}: {d['text']!r} proposed {d['proposed']} -> {d['verdict']} {d['reviewer_label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
