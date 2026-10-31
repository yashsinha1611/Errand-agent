# Golden set v1

Frozen 2026-10-31. Relative dates are labelled against 2026-11-02.

## Suites

| Suite | Rows | dev / test | Languages | Gate |
| --- | --- | --- | --- | --- |
| regression | 130 | 69 / 61 | en 76, hinglish 54 | test-split strict accuracy >= 95%, Hinglish >= 95%, out-of-scope false actions <= 5% |
| adversarial | 12 | 5 / 7 | en 9, hinglish 3 | zero confirm false positives |
| capability | 42 | 24 / 18 | en 32, hindi 3, hinglish 5, kanglish 2 | tracked, not gated |

## Sources

| Suite | Source | Rows |
| --- | --- | --- |
| regression | clinc150 | 21 |
| regression | handwritten | 29 |
| regression | hinglish_top | 25 |
| regression | sgd | 28 |
| regression | synthetic | 19 |
| regression | taskmaster1 | 8 |
| adversarial | adversarial | 12 |
| capability | capability | 42 |

## Label changes from the working draft

- `synthetic-009`: added date_expr weekday:thu (the day is in the message and identifies which booking to cancel; the parser was right)

## Known gaps

- No production traffic yet: rows come from public datasets (SGD, Taskmaster-1, CLINC150, Hinglish-TOP), handwritten Hinglish, two-step synthetic and adversarial rows.
- Labelled by one person; second-labeller review pending (see review_sheet.csv).
- The same author wrote the handwritten rows and the rule parser, so rule-model scores on them are optimistic.
- Devanagari and Kannada-English capability rows need review by native speakers.
- Relative dates are labelled against 2026-11-02 (a Monday).

## Checksums (SHA-256)

- `regression.jsonl` bf9d13673b0a65a5e28547d2b4817a03dbad698ff8971b3faa474661b0854c8d
- `adversarial.jsonl` e49851e4197ac0788a9020904663063950a0d08cb1308b869f4299938b703c83
- `capability.jsonl` 783a9e9369d53cdc95ba27eada243d4940ffaf5f9c7d12fd49ec16c40de3947c

Provenance and licences: `evals/data/SOURCES.md`.
