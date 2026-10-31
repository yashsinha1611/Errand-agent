# Interpreter eval: recorded:claude-haiku-5-5

Golden set **v1** (frozen 2026-10-31, checksums verified) · 184 utterances · generated 2026-10-31 22:05 by `python -m evals.intent_eval`. Release note: `evals/data/golden/v1/RELEASE.md`; provenance: `evals/data/SOURCES.md`.

## Suites

| Suite | Rows | Kind | Strict (test) | Strict (all) | Confirm false positives | Out-of-scope false actions | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| regression | 130 | 99.2% | 98.4% | 96.2% | 0 | 0.0% | test-split strict accuracy >= 95%, Hinglish >= 95%, out-of-scope false actions <= 5% |
| adversarial | 12 | 100.0% | 100.0% | 100.0% | 0 | – | zero confirm false positives |
| capability | 42 | 100.0% | 100.0% | 95.2% | 0 | 0.0% | tracked, not gated |

## Against the baseline (regression + adversarial, before the dev-driven parser fixes)

| Metric | baseline | now |
| --- | --- | --- |
| Strict accuracy | 45.1% | 96.5% |
| Kind accuracy | 69.0% | 99.3% |
| Confirm false positives | 4 | 0 |
| Out-of-scope false actions | 40.6% | 0.0% |

## By language (strict accuracy)

| Suite | Language | n | strict |
| --- | --- | --- | --- |
| adversarial | en | 9 | 100.0% |
| adversarial | hinglish | 3 | 100.0% |
| capability | en | 32 | 93.8% |
| capability | hindi | 3 | 100.0% |
| capability | hinglish | 5 | 100.0% |
| capability | kanglish | 2 | 100.0% |
| regression | en | 76 | 94.7% |
| regression | hinglish | 54 | 98.1% |

## By source (strict accuracy)

| Suite | Source | n | strict |
| --- | --- | --- | --- |
| adversarial | adversarial | 12 | 100.0% |
| capability | capability | 42 | 95.2% |
| regression | clinc150 | 21 | 100.0% |
| regression | handwritten | 29 | 96.6% |
| regression | hinglish_top | 25 | 100.0% |
| regression | sgd | 28 | 96.4% |
| regression | synthetic | 19 | 89.5% |
| regression | taskmaster1 | 8 | 87.5% |

## Field accuracy, regression suite

| Field | accuracy |
| --- | --- |
| area | 100.0% |
| booking_ref | 100.0% |
| budget_inr | 100.0% |
| date_expr | 95.7% |
| memory_key | 100.0% |
| memory_op | 100.0% |
| memory_value | 100.0% |
| no_budget | 100.0% |
| option_index | 100.0% |
| provider_ref | 100.0% |
| service | 97.1% |
| task | 100.0% |
| time_expr | 100.0% |

## Cost and latency

185 model calls · 829460 input and 37272 output tokens · $0.1016 · p50 1441 ms · p95 2920 ms

## Misses: regression (5)

| id | split | text | expected | predicted | why |
| --- | --- | --- | --- | --- | --- |
| sgd-017 | test | Monday next week. Is anything available at one in the afternoon? | {"kind": "provide_details", "date_expr": ["date:2026-11-09", "next_week"], "time_expr": "at:13:00"} | {"kind": "provide_details", "date_expr": "weekday:mon", "time_expr": "at:13:00"} | date_expr |
| taskmaster1-002 | dev | If they have anything today I'd like someone to come asap | {"kind": "provide_details", "date_expr": "today"} | {"kind": "provide_details", "date_expr": "today", "time_expr": "any"} | invented time_expr |
| handwritten-029 | dev | haan, wahi | {"kind": "confirm"} | {"kind": "provide_details", "provider_ref": "that"} | kind |
| synthetic-002 | dev | Hi! Our bathroom geyser is acting up again, could someone come over sometime after 4 this afternoon? | {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "today", "time_expr": "after:16:00"} | {"kind": "start_task", "task": "book", "date_expr": "today", "time_expr": "after:16:00"} | service |
| synthetic-009 | dev | Please cancel the electrician I booked for Thursday, plans changed. | {"kind": "start_task", "task": "cancel", "service": "electrician", "date_expr": "weekday:thu"} | {"kind": "start_task", "task": "cancel", "service": "electrician"} | date_expr |

## Misses: adversarial (0)

None.

## Misses: capability (2)

| id | split | text | expected | predicted | why |
| --- | --- | --- | --- | --- | --- |
| capability-018 | dev | ok wait actually make it 4 not 3 | {"kind": "provide_details", "time_expr": "at:16:00"} | {"kind": "provide_details", "date_expr": "date:2026-11-04", "is_correction": true} | time_expr, invented date_expr |
| capability-027 | dev | can you move it to the evening instead | {"kind": "start_task", "task": "reschedule", "time_expr": "evening"} | {"kind": "start_task", "task": "reschedule", "service": "plumber", "time_expr": "evening"} | invented service |
