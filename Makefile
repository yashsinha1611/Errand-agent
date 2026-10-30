PY ?= python3

.PHONY: test demo chat evals clean

test:            ## unit tests, negative controls, fuzz, then 33 eval scenarios -> reports/eval_table.md
	$(PY) -m unittest discover -s tests -t . && $(PY) -m evals.run

demo:            ## web console with chat and live trace on http://127.0.0.1:8765
	$(PY) -m errand serve

chat:            ## terminal chat
	$(PY) -m errand chat

evals:
	$(PY) -m evals.run

clean:
	rm -rf var __pycache__ */__pycache__ */*/__pycache__
