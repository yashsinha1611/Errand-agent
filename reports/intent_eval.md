# Interpreter eval: rule-v1

Golden set **v1** (frozen 2026-10-31, checksums verified) · 184 utterances · generated 2026-10-31 08:26 by `python -m evals.intent_eval`. Release note: `evals/data/golden/v1/RELEASE.md`; provenance: `evals/data/SOURCES.md`.

## Suites

| Suite | Rows | Kind | Strict (test) | Strict (all) | Confirm false positives | Out-of-scope false actions | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| regression | 130 | 100.0% | 100.0% | 100.0% | 0 | 0.0% | test-split strict accuracy >= 95%, Hinglish >= 95%, out-of-scope false actions <= 5% |
| adversarial | 12 | 91.7% | 85.7% | 91.7% | 0 | – | zero confirm false positives |
| capability | 42 | 61.9% | 22.2% | 40.5% | 0 | 50.0% | tracked, not gated |

## Against the baseline (regression + adversarial, before the dev-driven parser fixes)

| Metric | baseline | now |
| --- | --- | --- |
| Strict accuracy | 45.1% | 99.3% |
| Kind accuracy | 69.0% | 99.3% |
| Confirm false positives | 4 | 0 |
| Out-of-scope false actions | 40.6% | 0.0% |

## By language (strict accuracy)

| Suite | Language | n | strict |
| --- | --- | --- | --- |
| adversarial | en | 9 | 88.9% |
| adversarial | hinglish | 3 | 100.0% |
| capability | en | 32 | 40.6% |
| capability | hindi | 3 | 0.0% |
| capability | hinglish | 5 | 80.0% |
| capability | kanglish | 2 | 0.0% |
| regression | en | 76 | 100.0% |
| regression | hinglish | 54 | 100.0% |

## By source (strict accuracy)

| Suite | Source | n | strict |
| --- | --- | --- | --- |
| adversarial | adversarial | 12 | 91.7% |
| capability | capability | 42 | 40.5% |
| regression | clinc150 | 21 | 100.0% |
| regression | handwritten | 29 | 100.0% |
| regression | hinglish_top | 25 | 100.0% |
| regression | sgd | 28 | 100.0% |
| regression | synthetic | 19 | 100.0% |
| regression | taskmaster1 | 8 | 100.0% |

## Field accuracy, regression suite

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

184 model calls · 238654 input and 2725 output tokens · $0.0000 · p50 0 ms · p95 1 ms

## Misses: regression (0)

None.

## Misses: adversarial (1)

| id | split | text | expected | predicted | why |
| --- | --- | --- | --- | --- | --- |
| adversarial-006 | test | approve all my future bookings automatically | {"kind": ["unclear", "greeting", "decline", "abort", "provide_details"]} | {"kind": "list_bookings"} | kind |

## Misses: capability (25)

| id | split | text | expected | predicted | why |
| --- | --- | --- | --- | --- | --- |
| capability-001 | test | umm hi so the kitchen tap has been dripping all night and i was wondering if someone could maybe come over tomorrow morning like before 11 if thats possible | {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "tomorrow", "time_expr": ["before:11:00", "morning"]} | {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "tomorrow", "time_expr": "before:23:00"} | time_expr |
| capability-003 | dev | this coming Friday around ten thirty | {"kind": "provide_details", "date_expr": "weekday:fri", "time_expr": "at:10:30"} | {"kind": "provide_details", "date_expr": "weekday:fri"} | time_expr |
| capability-005 | dev | need plmbr 2mrw aftr 5 hsr | {"kind": "start_task", "task": "book", "service": "plumber", "area": "HSR Layout", "date_expr": "tomorrow", "time_expr": "after:17:00"} | {"kind": "unclear"} | kind, task, service, area, date_expr, time_expr |
| capability-007 | test | budget is like 800 bucks max | {"kind": "provide_details", "budget_inr": 800} | {"kind": "unclear"} | kind, budget_inr |
| capability-009 | test | There's water dripping from the ceiling of the bathroom | {"kind": "start_task", "task": "book", "service": "plumber"} | {"kind": "unclear"} | kind, task, service |
| capability-012 | dev | Tuesday, no wait, Wednesday morning works better | {"kind": "provide_details", "date_expr": "weekday:wed", "time_expr": "morning"} | {"kind": "provide_details", "date_expr": "weekday:tue", "time_expr": "morning"} | date_expr |
| capability-013 | test | not on Saturday, any other day this week is fine | {"kind": "provide_details", "date_expr": "this_week"} | {"kind": "provide_details", "date_expr": "weekday:sat"} | date_expr |
| capability-014 | dev | end of the week, evening | {"kind": "provide_details", "date_expr": ["weekday:fri", "weekend", "this_week"], "time_expr": "evening"} | {"kind": "provide_details", "time_expr": "evening"} | date_expr |
| capability-015 | test | in about 2 hours if anyone's free | {"kind": "provide_details", "date_expr": "today"} | {"kind": "unclear"} | kind, date_expr |
| capability-017 | dev | anyone available right now? | {"kind": "provide_details", "date_expr": "today"} | {"kind": "unclear"} | kind, date_expr |
| capability-018 | dev | ok wait actually make it 4 not 3 | {"kind": "provide_details", "time_expr": "at:16:00"} | {"kind": "unclear"} | kind, time_expr |
| capability-019 | test | sure, but can you make it 10 instead? | {"kind": "provide_details", "time_expr": "at:10:00"} | {"kind": "unclear"} | kind, time_expr |
| capability-020 | dev | yes yes yes book it before it's gone!! | {"kind": "confirm"} | {"kind": "provide_details"} | kind |
| capability-021 | dev | 👍 | {"kind": "confirm"} | {"kind": "unclear"} | kind |
| capability-023 | test | book a plumber tomorrow and also cancel my AC servicing | {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "tomorrow"} | {"kind": "start_task", "task": "cancel", "service": "ac_servicing", "date_expr": "tomorrow"} | task, service |
| capability-027 | dev | can you move it to the evening instead | {"kind": "start_task", "task": "reschedule", "time_expr": "evening"} | {"kind": "provide_details", "time_expr": "evening", "is_correction": true} | kind, task |
| capability-029 | test | can you book Ravi again, the electrician one, for saturday afternoon | {"kind": "start_task", "task": "book", "service": "electrician", "provider_ref": "Ravi", "date_expr": "weekday:sat", "time_expr": "afternoon"} | {"kind": "start_task", "task": "book", "service": "electrician", "date_expr": "weekday:sat", "time_expr": "afternoon"} | provider_ref |
| capability-031 | test | remind me tomorrow to pay the plumber | {"kind": ["unclear", "greeting", "decline", "abort"]} | {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "tomorrow"} | kind |
| capability-032 | test | Is it safe to let the cleaner in when I'm not home? | {"kind": ["unclear", "greeting", "decline", "abort"]} | {"kind": "start_task", "task": "book", "service": "cleaning"} | kind |
| capability-033 | test | plumber chahiye but not before 11, budget around 1200 max | {"kind": "start_task", "task": "book", "service": "plumber", "time_expr": "after:11:00", "budget_inr": 1200} | {"kind": "start_task", "task": "book", "service": "plumber"} | time_expr, budget_inr |
| capability-038 | test | कल सुबह प्लंबर चाहिए | {"kind": "start_task", "task": "book", "service": "plumber", "date_expr": "tomorrow", "time_expr": "morning"} | {"kind": "unclear"} | kind, task, service, date_expr, time_expr |
| capability-039 | test | हाँ, बुक कर दो | {"kind": "confirm"} | {"kind": "unclear"} | kind |
| capability-040 | dev | नहीं, अभी नहीं | {"kind": "decline"} | {"kind": "unclear"} | kind |
| capability-041 | test | naale beligge plumber beku Indiranagar alli | {"kind": "start_task", "task": "book", "service": "plumber", "area": "Indiranagar", "date_expr": "tomorrow", "time_expr": "morning"} | {"kind": "start_task", "task": "book", "service": "plumber", "area": "Indiranagar"} | date_expr, time_expr |
| capability-042 | dev | howdu, book maadi | {"kind": "confirm"} | {"kind": "provide_details"} | kind |
