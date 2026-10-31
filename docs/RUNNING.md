# Running Errand end to end

Everything needs only **Python 3.11 or newer**. There is nothing to install: no pip, no virtualenv, no Node.
Check with `python3 --version` (macOS/Linux) or `python --version` (Windows).

## 1. Get the code

Unzip the submission zip (or clone the repo) and open a terminal in the `errand-agent` folder. All commands
below run from there.

## 2. Run the tests (offline, about 10 seconds)

| macOS / Linux | Windows (PowerShell) |
| --- | --- |
| `make test` | `python -m unittest discover -s tests -t .` then `python -m evals.intent_eval` then `python -m evals.run` |

You should see `OK` for the unit tests, the three golden suites with zero confirm false positives, and
`42/42 scenarios passed`. Reports land in `reports/`: `eval_table.md` (scenarios) and `intent_eval.md`
(interpreter).

## 3. Run the console offline (no API key)

| macOS / Linux | Windows |
| --- | --- |
| `make demo` | `python -m errand serve` |

Open <http://127.0.0.1:8765>. The header shows **Offline rule model** (or the Claude model, see section 4).
Data is kept in `var/`; delete that folder, or use **Faults → Reset world**, to start fresh.

### Using the console

The left side is the chat; the right side shows what the agent is doing, live.

| Part | What it shows |
| --- | --- |
| Chat | Your messages and the agent's replies. Each reply lists the steps it took (search, hold, book, verify) with timings. Example chips under the input are one-click messages |
| Option buttons | Providers with slot, price and rating. Click one, or type "Option 2" |
| Approval card | The exact action, provider, slot and price, plus a short fingerprint. **Book it for ₹…** approves, **Don't book** declines. Nothing is written until you approve |
| Trace tab | Every event of the run: state transitions, model calls (tokens, cost, latency), tool calls (args, result, latency), approvals, outbox, errors. Click a row for its details |
| State tab | The workflow state: phase, every known detail and where it came from (you, memory or history) |
| Memory tab | Saved preferences, marked *stated* or *inferred*; inferred ones wait for your yes |
| Outbox tab | Every write the agent has sent and its status (SENT → CONFIRMED) |
| Faults tab | Arm a failure for the next matching call (timeout after success, slot taken, price change, crash, search error), move the agent clock, clear faults, or reset the world |

A five-minute tour:

1. **A full booking:** click the chip *Find a plumber for my kitchen tap this week, mornings only, under ₹1,500*,
   answer *Indiranagar* when asked for the area, pick an option, then **Book it**. Watch Trace: hold → approval
   grant → consume → outbox → book → status check.
2. **Prompt injection:** ask *What do people say about Sharma Plumbing?* The hostile review is shown as data
   (expand **Screening review text**); Trace shows `injection_suspected`; State, Memory and approvals don't change.
3. **Timeout after success:** start another booking, then in **Faults** arm *Next booking times out after it has
   succeeded* and approve. The agent checks the booking's status instead of retrying, and books exactly once.
4. **Memory:** say *remember Indiranagar as my area*, then *what do you remember about me?*, then *forget my area*.
5. **A restart mid-booking:** arm *Kill the agent right after it sends a booking* and approve. The server process
   really exits and restarts; the console reconnects and the agent reconciles the booking before anything else.

### Using the terminal chat

`make chat` (Windows: `python -m errand chat`) runs the same agent in the terminal. Each reply prints the steps
the agent took, then the reply, the options and any approval. Type messages as in the console; reply `yes` or
`no` to an approval.

| Command | What it does |
| --- | --- |
| `/help` | List the commands |
| `/faults` | List the failures you can arm, and what is armed now |
| `/fault <id>` | Arm one, e.g. `/fault book_timeout_booked`, `/fault book_slot_taken`, `/fault hold_price_up`, `/fault crash_after_book_sent` |
| `/clock <minutes>` | Move the agent's clock forward (default 11, enough to expire a 10-minute hold) |
| `/memory` | Show saved preferences |
| `/state` | Show the workflow state as JSON |
| `/trace` | Print the path of this session's trace file (JSONL) |
| `/new` | Start a new session (memory is kept) |
| `/reset` | Wipe bookings, memory and sessions |
| `/quit` | Exit |

Example (real output; your dates and times will differ):

```
you> I need a plumber in Indiranagar tomorrow morning under 1500
  ✓ Understood: book a plumber 7 ms
  ✓ 4 found. 4 fit your time and budget 1 ms

Three plumbers have morning slots tomorrow under ₹1,500. Pick one:
  1. Ravi Plumbing (4.7 rating, 212 jobs): Sun 1 Nov, 10:00, ₹1,200
  2. Kumar & Sons Plumbing (4.5 rating, 98 jobs): Sun 1 Nov, 08:00, ₹1,100
  3. Sharma Plumbing (4.4 rating, 143 jobs): Sun 1 Nov, 10:00, ₹1,250

you> 1
  ✓ Understood: option 1 3 ms
  ✓ Held at ₹1,200, same as listed 2 ms

  Approve: book Ravi Plumbing, Sun 1 Nov, 10:00–11:00, ₹1,200  (approval ap_AQPQH90F, fingerprint bcb9e9)
  Reply “yes” to approve or “no” to decline.

you> yes
  ✓ Approval matches the provider, slot and price 1 ms
  ✓ The provider accepted the booking 2 ms
  ✓ Status: booked, B-2041 0 ms
```

## 4. Run it with Claude

**Put your key in a `.env` file** in the `errand-agent` folder (it is git-ignored, so it can't be committed):

```
ANTHROPIC_API_KEY=sk-ant-...
ERRAND_MODEL=claude-haiku-5-5
```

Or set it for the current terminal only:

| macOS / Linux | Windows PowerShell | Windows cmd |
| --- | --- | --- |
| `export ANTHROPIC_API_KEY=sk-ant-...` | `$env:ANTHROPIC_API_KEY="sk-ant-..."` | `set ANTHROPIC_API_KEY=sk-ant-...` |

Then start the console exactly as before (`make demo` or `python -m errand serve`). The terminal prints
`interpreter: claude-haiku-5-5`, and the console header shows **Claude: claude-haiku-5-5**.

**What changes with Claude**

- Claude does exactly one job: it turns each message into a structured intent (`emit_intent`). Everything else,
  including every booking decision, approval and retry, is the same code as offline.
- When you ask about reviews, a separate, isolated Claude call summarises them into fixed categories only.
- In the Trace tab, each `model_call` row shows the model, input and output tokens, and cost.
- If Claude is unreachable or the key is wrong, that turn falls back to the offline parser and the trace says
  so (`falling back to rule-v1`). The agent keeps working.
- Tests never call Claude, with or without a key.

**Cost.** Each message is roughly 1,500 to 2,000 input tokens (prompt, tool schema and context) and about 50 output tokens. At Haiku 5.5 prices ($0.10 / $0.50 per
million tokens) that is about $0.0002 per message; a full demo costs well under one rupee. Each conversation
is capped at $0.50 (`errand/config.py`).

**Using another model.** Set `ERRAND_MODEL`, for example `claude-sonnet-5-5`. Add its price to
`MODEL_PRICES_PER_MTOK` in `errand/config.py` so the trace reports cost.

## 5. Test everything with Claude

Run `make check-llm` first (three probes, no fallback, clear error hints). Then follow
[`docs/TESTING.md`](TESTING.md): automated checks (`make intent-eval MODEL=claude RECORD=1`,
`make live-scenarios`) and a hands-on script covering every feature, guardrail and injection.

To make the scenarios replayable offline against Claude's real outputs (what the brief means by a "recorded
model"), record once and commit the folder:

```
make record-scenarios          # one live pass, a few cents; writes evals/data/recordings/scenarios/<model>/
make evals-recorded            # from then on: offline, no key, same result every time
git add evals/data/recordings && git commit -m "Record scenarios on Claude"
```

If you change the prompt later, replay marks the affected scenarios STALE; record again.

## 6. Measure Claude on the golden set

```
make intent-eval MODEL=claude RECORD=1        # Windows: python -m evals.intent_eval --model claude --record
```

This scores Claude on all three golden suites (184 messages, about 4 cents) and writes `reports/intent_eval.md`.
`RECORD=1` saves every Claude answer under `evals/fixtures/claude/`, so you can replay them offline at no cost:

```
make intent-eval MODEL=recorded               # Windows: python -m evals.intent_eval --model recorded
```

Commit the fixtures if you want CI to check Claude's answers without a key. Add Claude's numbers to the README
next to the rule model's: regression, adversarial (confirm false positives must be 0) and capability, where
Claude should clearly beat the rule model on Devanagari, Kannada-English and messy phrasing.

## 7. Record the demo

Follow `docs/DEMO.md`. Start with **Faults → Reset world** so booking ids begin at B-2041, keep the Trace tab
visible, and put a terminal next to the browser for the crash step: the process really exits and the
supervisor restarts it.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `SyntaxError` or `ModuleNotFoundError: zoneinfo` | Python is older than 3.11. Install a newer Python. |
| `make: command not found` (Windows) | Use the `python -m …` commands shown above. |
| `Address already in use` | Another process uses port 8765: `python -m errand serve --port 8800`. |
| Header still says Offline rule model | The key is not visible to the process. Check `.env` is in the folder you run from, or export the variable in the same terminal. |
| Trace shows `HTTP 401` | The key is wrong or revoked. |
| Trace shows `HTTP 400` mentioning the model | `ERRAND_MODEL` is not a valid model id for your account. |
| Trace shows `network: … CERTIFICATE_VERIFY_FAILED` | A corporate proxy inspects TLS. Point Python at your organisation's CA bundle: `export SSL_CERT_FILE=/path/to/ca-bundle.pem`. On macOS with python.org Python, also run *Install Certificates.command* once. |
| Trace shows `network: …` timeouts | Behind a proxy: set `HTTPS_PROXY` (Python's standard library uses it automatically). |
| Strange state after experiments | Stop the server and delete `var/`. |

On a corporate laptop, check that sending test conversations to the Claude API with your own key is allowed by
your organisation's policy. The demo data is fictional, but your typed messages are sent to Anthropic.
