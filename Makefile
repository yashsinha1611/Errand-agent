PY ?= python3

.PHONY: test demo chat evals clean intent-eval golden-verify review-sheet agreement check-llm live-scenarios hard-probes record-scenarios evals-recorded

test:            ## unit tests, negative controls, fuzz, intent eval and 42 scenarios -> reports/
	$(PY) -m unittest discover -s tests -t . && $(PY) -m evals.intent_eval && $(PY) -m evals.run

record-scenarios: ## record Claude's outputs locally for every scenario once (needs ANTHROPIC_API_KEY)
	$(PY) -m evals.recorded --record $(if $(MODEL),--model $(MODEL),)

evals-recorded:  ## replay all scenarios offline from the recording: Claude's real outputs, no key, no network
	$(PY) -m evals.recorded $(if $(MODEL),--model $(MODEL),)

intent-eval:     ## interpreter accuracy on golden v1 (add MODEL=claude RECORD=1 for live Claude)
	$(PY) -m evals.intent_eval --model $(or $(MODEL),rule) $(if $(RECORD),--record,)

demo:            ## web console with chat and live trace on http://127.0.0.1:8765
	$(PY) -m errand serve

chat:            ## terminal chat
	$(PY) -m errand chat

evals:
	$(PY) -m evals.run

clean:
	rm -rf var __pycache__ */__pycache__ */*/__pycache__

golden-verify:   ## check golden-set checksums
	$(PY) -m evals.golden verify

review-sheet:    ## CSV for a second labeller
	$(PY) -m evals.golden review-sheet

agreement:       ## agreement and Cohen's kappa from a filled review sheet
	$(PY) -m evals.golden agreement evals/data/golden/v1/review_sheet.csv

check-llm:       ## preflight: three probes against the live model (needs ANTHROPIC_API_KEY)
	$(PY) -m errand check-llm

live-scenarios:  ## all 42 scenarios with Claude reading every message, TRIALS times each (default 3)
	$(PY) -m evals.live --trials $(or $(TRIALS),3) $(if $(ONLY),--only $(ONLY),)

hard-probes:     ## 79 hard English and Hinglish probes against Claude (MODEL=rule for the offline baseline)
	$(PY) -m evals.hard --model $(or $(MODEL),claude) $(if $(ONLY),--only $(ONLY),) $(if $(LANG_),--lang $(LANG_),)
