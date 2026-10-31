# Interpreter eval data: sources, method and licences

The working drafts (`intents.jsonl`, `capability.jsonl`) are frozen into versioned golden sets under
`golden/`. Golden v1 splits them into regression (130), adversarial (12) and capability (42) suites; see
`golden/v1/RELEASE.md`. The capability rows are handwritten (MIT) from common real-world patterns; the Devanagari
and Kannada-English rows need review by native speakers.

`intents.jsonl` holds 142 labelled utterances for the interpreter, the one step where a model is in charge.
Every row records its source, licence, original text or span, and how it was adapted.

## Where the rows come from

| Source | Rows | What was taken | Licence |
| --- | --- | --- | --- |
| [Schema-Guided Dialogue (SGD)](https://github.com/google-research-datasets/dstc8-schema-guided-dialogue), `Services_1/2/3` (stylist, dentist, doctor booking) | 28 | User turns from FindProvider / BookAppointment dialogues. Service nouns and US city names were swapped for this domain; every date and time phrase is verbatim. | CC BY-SA 4.0 |
| [Taskmaster-1](https://github.com/google-research-datasets/Taskmaster), `auto_repair` self-dialogs | 8 | User turns annotated with appointment date/time. Car wording swapped for a home visit; time phrasing verbatim. | CC BY 4.0 |
| [CLINC150 / oos-eval](https://github.com/clinc/oos-eval) | 21 | Verbatim out-of-scope queries and near-domain intents (hotels, cabs, restaurant tables, car service, reminders). | CC BY 3.0 |
| [Hinglish-TOP](https://github.com/google-research-datasets/Hinglish-TOP-Dataset), human-annotated validation split | 25 | 19 real Hinglish date/time spans (with their English parse) embedded in booking sentences, plus 6 verbatim Hinglish requests from other domains (alarms, reminders, timers) as out-of-scope rows. | Apache-2.0 |
| Handwritten Hinglish | 29 | Confirmations, declines, picks, budgets, memory commands and tasks in everyday Bangalore Hinglish. | MIT |
| Synthetic (two-step) | 19 | Hamel Husain's method: tuples of (task, specificity, style, constraint) written first, then one utterance per tuple. Each row's note gives its tuple. | MIT |
| Adversarial | 12 | User-side injections and confirm traps, including AgentDojo's "important message" style. | MIT |

`python -m evals.data.build_intents --verify` checks every sourced original against the downloaded files.

## Method

- **Grounding.** Real phrasing comes from crowd-sourced dialogue datasets. Only the parts that would not occur in
  this domain (service names, US cities, cars) were swapped, and each row says what changed.
- **Labels.** Written by hand against this project's `Intent` schema, in the context the agent would have
  (collecting details, choosing, awaiting approval). Ambiguous phrases accept several labels ("next Wednesday"
  accepts this coming Wednesday or the one in next week). One label was corrected after review and is marked
  in its note. Labels should be reviewed by a second person.
- **Splits.** Rows are assigned to `dev` or `test` by a hash of their id. Parser changes were driven only by
  dev misses; test misses were counted but not inspected.
- **Honest caveat.** The same author wrote the handwritten rows and the rule parser, so the rule model's score on
  those rows is optimistic. The real measure of Hinglish handling is the live model on this set, and then real
  users. Every bug fixed from this set has a regression test in `tests/test_intents.py`.

## Licence of this file

Each row keeps its source licence in the `license` field. Because the file contains adapted SGD rows, it is
shared as a whole under CC BY-SA 4.0. Hinglish-TOP rows are used under the Apache License 2.0
(Copyright Google LLC). Attribution: Rastogi et al. 2020 (SGD); Byrne et al. 2019 (Taskmaster-1); Larson et al.
2019 (CLINC150); Agarwal et al. 2022 (Hinglish-TOP, CST5). If you publish this repository, check the mix
against your organisation's open-source policy.
