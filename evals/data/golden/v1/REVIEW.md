# Reviewing golden v1 (second labeller)

Open `review_sheet.csv` in Excel or Google Sheets (it is UTF-8 with a BOM, so Devanagari displays correctly).
Review independently: judge each row before looking at how any model scored it.

For each row, read **context** (what the agent was doing) and **text** (what the user said), then fill:

| Column | What to write |
| --- | --- |
| `reviewer_verdict` | `agree` if the proposed label is what the agent should understand; `disagree` if not; `unsure` if the message is genuinely ambiguous |
| `reviewer_kind` | only when you disagree: the intent kind you would choose |
| `reviewer_label` | only when you disagree: the full label in the same `key=value; key=value` format |
| `reviewer_notes` | why, especially for Hinglish, Hindi and Kannada rows |

Intent kinds: `start_task` (new booking, reschedule or cancel), `provide_details` (answering or correcting),
`select` (picking a shown option), `confirm` (a clear, unconditional yes to the pending approval), `decline`,
`memory`, `ask_reviews`, `list_bookings`, `greeting`, `abort`, `unclear`. Several values separated by `|` mean
any of them is acceptable. Dates resolve against Monday 2 Nov 2026; times are compared to the hour.

The rule that matters most: mark `disagree` on any row labelled `confirm` unless you would be comfortable with
the agent booking and charging on that message alone.

Then run `python -m evals.golden agreement evals/data/golden/v1/review_sheet.csv`. It reports raw agreement and
Cohen's kappa on the intent kind, and lists rows to adjudicate. When two careful reviewers disagree, the spec is
ambiguous: fix the definition, not just the row. Corrections go into the working drafts and ship as golden v2
with a release note; v1 never changes.
