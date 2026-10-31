# Errand

Errand is a home-services booking demo for booking, rescheduling, and cancelling jobs through a mocked provider.
Its core rule is **the model interprets messages; application code controls actions**. Every provider write
requires approval, is recorded in an outbox, and is verified before the agent reports success.

Requires Python 3.11+. The application uses the Python standard library. Claude is optional.

## Run it

Open a terminal in the repository root. Run the tests and start the browser console:

```bash
make test
make demo
```

The console is at <http://127.0.0.1:8765>. `make chat` starts the same agent in a terminal instead. On Windows,
run the Python commands directly if `make` is unavailable:

```powershell
python -m unittest discover -s tests -t .
python -m evals.intent_eval
python -m evals.run
python -m errand serve
```

The first three commands run the offline checks; the last starts the console. To start terminal chat, use
`python -m errand chat`.

### Use the console

The left side is the conversation; the right side is the live inspector. Suggested starter messages are shown in
the empty conversation.

| Console area | What to look for |
| --- | --- |
| Conversation | Messages, workflow steps, provider options, and the approval request |
| Approval card | Exact action, provider, time, and price. Approve or decline here; no write happens before approval |
| Trace | Model/tool calls, state transitions, approvals, outbox updates, errors, and timing |
| State | Current task, phase, collected details, and where each detail came from |
| Memory | Stated preferences and inferred suggestions; inferred facts require confirmation before use |
| Outbox | Provider writes and their status, such as `SENT` and `CONFIRMED` |
| Faults | Inject timeouts, conflicts, price changes, and crashes; move the test clock or reset the mock world |

The console uses a local mock provider. `Faults → Reset world` returns its demo data to the starting state. Runtime
data and traces are written locally under `var/`.

### Five-minute demo path

1. **Book:** click the plumber example, answer `Indiranagar`, choose an option, and approve the displayed price.
	In Trace, follow `hold_slot` → approval → outbox → `book` → status check.
2. **Prompt injection:** ask what people say about Sharma Plumbing. Show the review as untrusted data and the
	`injection_suspected` trace event; no booking or memory write should occur.
3. **Timeout after success:** arm the matching fault under Faults, start and approve a booking, and show that the
	agent checks status instead of sending a duplicate booking.
4. **Memory:** ask it to remember your area, inspect Memory, then ask what it remembers. Try forgetting the area.
5. **Crash recovery:** arm the crash-after-send fault, approve a booking, and show the server restart and recovery
	in the trace and outbox.

Reset the mock world before recording. The repository has this run guide, but **does not contain a recorded video
or a video link**.

### Run with Claude (optional)

Set `ANTHROPIC_API_KEY` in your local environment or in a `.env` file at the repository root. `.env` is ignored
by Git. Start with the preflight:

```bash
python -m errand check-llm
```

Then start the console or chat as above. The console header identifies the active model. Without a usable key,
the app uses the offline rule model. Live evaluation commands also require the key; some commands read the key
from the environment, so export it in the shell before running them. Never commit your key or generated model
recordings.

## Design overview

```text
user message -> interpreter -> typed intent -> deterministic workflow
												  |                  |
										 no write tools      approval gate
																			|
																outbox -> provider
																			|
																  status check
```

The model converts language into a typed intent and has no provider write tools. `AgentService` owns workflow
state and business decisions. The mock provider stands in for an external service; the same safety checks apply
regardless of what the interpreter returns.

### Workflow and state

Each session has persisted workflow state and a phase such as collecting details, searching, choosing, awaiting
approval, or verifying. Service, area, and date are required; time and budget are optional. Corrections update
only the named details and invalidate dependent offers or approvals. A second request in one message is queued.
After a booking is complete, a change asks whether to move that booking or create another instead of silently
starting a new booking.

### Approval and provider writes

The approval records the action, provider, slot, price, and booking reference. The displayed approval is tied to
those details by a fingerprint. A valid user turn must grant it, and the executor checks the fingerprint again
before writing. In one database transaction, the approval is consumed and the outbox row is created. If the
approval is missing, expired, or no longer matches the planned action, the provider is not called.

### Idempotency and recovery

Write requests use deterministic idempotency keys, so a retry or restart refers to the same intended operation.
If a booking call times out, the outcome is treated as unknown: the agent asks the provider for status before
deciding whether to retry. It reports success only after status confirms the booking. Pending outbox work is
reconciled when the session resumes or the console reconnects.

### Untrusted reviews and memory

Provider reviews are untrusted text, not instructions. The interpreter receives a constrained summary rather
than raw review commands; a review cannot grant approval or write memory. Memory distinguishes user-stated facts
from inferred suggestions. Inferred facts require confirmation, and forgetting a fact records a tombstone so it
cannot be inferred back from old booking history.

### Budgets and traces

Each run is bounded by model, tool, step, wall-clock, and cost limits. JSONL traces record workflow changes,
model and tool calls, approvals, outbox transitions, memory events, and errors. Event fields are allow-listed and
secret-like values are scrubbed. The example timeout trace is
[`docs/traces/timeout_after_success.jsonl`](docs/traces/timeout_after_success.jsonl).

## Test and evaluate

### Offline checks

These commands use no Claude API and no network:

| Command | What it checks | Main output |
| --- | --- | --- |
| `python -m unittest discover -s tests -t .` | Unit tests, regressions, and mock-provider behavior | Test summary |
| `python -m evals.intent_eval` | Rule interpreter against 184 frozen utterances; verifies checksums | [`reports/intent_eval.md`](reports/intent_eval.md) |
| `python -m evals.run` | 42 scripted scenarios and 10 invariants | [`reports/eval_table.md`](reports/eval_table.md), [`reports/eval_results.json`](reports/eval_results.json) |
| `make test` | Runs all three commands above in sequence | Updated offline reports |

Golden datasets and their manifest are in [`evals/data/golden/v1/`](evals/data/golden/v1/). Dataset sources and
licensing notes are in [`evals/data/SOURCES.md`](evals/data/SOURCES.md).

### Claude evaluations

After setting `ANTHROPIC_API_KEY`, run:

| Command | What it checks | Report or output |
| --- | --- | --- |
| `python -m errand check-llm` | English, Hinglish approval, and injection preflight probes | Terminal output |
| `make intent-eval MODEL=claude RECORD=1` | Claude on the 184 golden utterances | `reports/intent_eval.md`; answer fixtures stay local under ignored `evals/fixtures/claude/` |
| `python -m evals.live --trials 1` | Claude interprets messages in all 42 scenarios once | `reports/live/scenarios_claude-haiku-5-5.md` and `.json` |
| `python -m evals.hard` | 79 English, Hinglish, Hindi, and Kannada-English probes | `reports/live/hard_probes_claude-haiku-5-5.md` and `.json` |
| `python -m evals.recorded --record`, then `python -m evals.recorded` | Record scenarios, then replay offline | `reports/recorded_claude-haiku-5-5.json`; raw recordings stay local under ignored `evals/data/recordings/` |

Live results are snapshots, not guarantees for future model runs. The latest committed snapshots are:

| Evaluation | Result |
| --- | --- |
| Intent regression (130) | 96.2% strict overall; 98.4% test split |
| Intent adversarial (12) | 100%; zero false confirmations |
| Intent capability (42) | 95.2% |
| Live scenarios (one trial) | 41/42; zero invalid fallbacks |
| Hard probes (79) | 77/79; zero safety failures |
| Recorded scenario replay | 42/42 passed |

Scenario and probe reports include the per-case outcomes and misses. Scenario coverage and invariant definitions
are in [`evals/scenarios.py`](evals/scenarios.py) and [`evals/invariants.py`](evals/invariants.py).

## Files and further reference

| Path | Contents |
| --- | --- |
| [`errand/`](errand/) | Application, deterministic workflow, persistence, tracing, and model adapters |
| [`errand/mock/`](errand/mock/) | Mock provider, seeded world, and fault injection |
| [`evals/`](evals/) | Scenario harness, invariants, golden-set evaluator, and live/recorded runners |
| [`tests/`](tests/) | Unit and regression tests |
| [`web/index.html`](web/index.html) | Single-file browser UI |
| [`docs/TESTING.md`](docs/TESTING.md) | Extended manual console checklist and live-test procedure |
| [`docs/BUGS.md`](docs/BUGS.md) | Three discovered bugs, fixes, and regression coverage |
| [`docs/DEPLOY_AWS.md`](docs/DEPLOY_AWS.md) | AWS Lightsail and EC2 deployment instructions |
| [`docs/DESIGN.md`](docs/DESIGN.md) / [`docs/DESIGN.pdf`](docs/DESIGN.pdf) | Standalone design-note deliverables; core design is explained above |
