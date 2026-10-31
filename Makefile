PY ?= python3

.PHONY: test demo chat evals clean

test:            ## unit tests, negative controls, fuzz, intent eval and 35 scenarios -> reports/
	$(PY) -m unittest discover -s tests -t . && $(PY) -m evals.intent_eval && $(PY) -m evals.run

intent-eval:     ## interpreter accuracy on 142 labelled utterances (add MODEL=claude RECORD=1 for live Claude)
	$(PY) -m evals.intent_eval --model $(or $(MODEL),rule) $(if $(RECORD),--record,)

demo:            ## web console with chat and live trace on http://127.0.0.1:8765
	$(PY) -m errand serve

chat:            ## terminal chat
	$(PY) -m errand chat

evals:
	$(PY) -m evals.run

clean:
	rm -rf var __pycache__ */__pycache__ */*/__pycache__
