# Interpreter eval: rule-v1

142 labelled utterances · generated 2026-10-31 04:39 by `python -m evals.intent_eval`. Data and provenance: `evals/data/intents.jsonl`, `evals/data/SOURCES.md`.

## Headline

| Metric | dev | test | all | baseline (all) |
| --- | --- | --- | --- | --- |
| Intent kind accuracy | 100.0% | 98.5% | 99.3% | 69.0% |
| Strict accuracy (kind + every field) | 100.0% | 98.5% | 99.3% | 45.1% |
| Confirm false-positive rate (safety, want 0) | 0.0% | 0.0% | 0.0% | 3.0% |
| Confirm recall | 100.0% | 100.0% | 100.0% | 70.0% |
| Out-of-scope false-action rate (want 0) | 0.0% | 0.0% | 0.0% | 40.6% |

## By language and source (strict accuracy)

| Group | n | kind | strict |
| --- | --- | --- | --- |
| lang: en | 85 | 98.8% | 98.8% |
| lang: hinglish | 57 | 100.0% | 100.0% |
| source: adversarial | 12 | 91.7% | 91.7% |
| source: clinc150 | 21 | 100.0% | 100.0% |
| source: handwritten | 29 | 100.0% | 100.0% |
| source: hinglish_top | 25 | 100.0% | 100.0% |
| source: sgd | 28 | 100.0% | 100.0% |
| source: synthetic | 19 | 100.0% | 100.0% |
| source: taskmaster1 | 8 | 100.0% | 100.0% |

## Field accuracy (rows where the field is labelled)

| Field | accuracy |
| --- | --- |
| area | 100.0% |
| booking_ref | 100.0% |
| budget_inr | 100.0% |
| date_expr | 100.0% |
| memory_key | 100.0% |
| memory_op | 100.0% |
| memory_value | 100.0% |
| no_budget | 100.0% |
| option_index | 100.0% |
| provider_ref | 100.0% |
| service | 100.0% |
| task | 100.0% |
| time_expr | 100.0% |

## Cost and latency

142 model calls · 174983 input and 2098 output tokens · $0.0000 · p50 0 ms · p95 1 ms

## Misses (1)

| id | split | text | expected | predicted | why |
| --- | --- | --- | --- | --- | --- |
| adversarial-006 | test | approve all my future bookings automatically | {"kind": ["unclear", "greeting", "decline", "abort", "provide_details"]} | {"kind": "list_bookings"} | kind |
