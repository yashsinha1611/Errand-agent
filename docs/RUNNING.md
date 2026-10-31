# Running Errand end to end

Everything needs only **Python 3.11 or newer**. There is nothing to install: no pip, no virtualenv, no Node.
Check with `python3 --version` (macOS/Linux) or `python --version` (Windows).

## 1. Get the code

Unzip `errand-agent.zip` (or clone the repo) and open a terminal in the `errand-agent` folder. All commands below
run from there.

## 2. Run the tests (offline, about 10 seconds)

| macOS / Linux | Windows (PowerShell) |
| --- | --- |
| `make test` | `python -m unittest discover -s tests -t .` then `python -m evals.intent_eval` then `python -m evals.run` |

You should see `OK` for the unit tests, the three golden suites with zero confirm false positives, and
`35/35 scenarios passed`. Reports land in `reports/`: `eval_table.md` (scenarios) and `intent_eval.md`
(interpreter).

## 3. Run the console offline (no API key)

| macOS / Linux | Windows |
| --- | --- |
| `make demo` | `python -m errand serve` |

Open <http://127.0.0.1:8765>. The header shows **Offline rule model**. Try the example chips, or type your own,
in English or Hinglish. Data is kept in `var/`; delete that folder, or use **Faults → Reset world**, to start fresh.

`make chat` (`python -m errand chat`) runs the same agent in the terminal; type `/help` for commands such as
`/fault book_timeout_booked` or `/clock 11`.

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

## 5. Measure Claude on the golden set

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

## 6. Record the demo

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
