"""Scripted and recorded models: offline, reproducible stand-ins for a live LLM."""
from __future__ import annotations

import json
import os

from ..util import sha256
from .base import Model, ModelRequest, ModelResponse, estimate_tokens, payload_for


class ScriptedModel:
    """Returns pre-written outputs in order. ``None`` entries defer to ``fallback``.

    Evals use this to make the model misbehave on purpose, for example claiming the
    user confirmed when they did not.
    """

    name = "scripted"

    def __init__(self, outputs: list[dict | None], fallback: Model | None = None) -> None:
        self.outputs = list(outputs)
        self.fallback = fallback
        self.requests: list[ModelRequest] = []

    def complete(self, req: ModelRequest) -> ModelResponse:
        self.requests.append(req)
        nxt = self.outputs.pop(0) if self.outputs else None
        if nxt is None:
            if self.fallback is None:
                raise AssertionError("ScriptedModel ran out of outputs")
            return self.fallback.complete(req)
        raw = payload_for(nxt)
        return ModelResponse(data=nxt, raw=raw, model=self.name, input_tokens=estimate_tokens(req.user),
                             output_tokens=estimate_tokens(raw))


class RecordedModel:
    """Replays model outputs captured from a live run, keyed by a hash of the prompt.

    ``mode="record"`` wraps a live model and writes fixtures; ``mode="replay"`` never
    touches the network and fails loudly on a missing fixture.
    """

    def __init__(self, fixture_dir: str, inner: Model | None = None, mode: str = "replay") -> None:
        self.dir = fixture_dir
        self.inner = inner
        self.mode = mode
        self.name = f"recorded:{getattr(inner, 'name', 'fixtures')}"
        os.makedirs(fixture_dir, exist_ok=True)

    def key(self, req: ModelRequest) -> str:
        return sha256(req.purpose + "\n" + req.system + "\n" + req.user)[:20]

    def complete(self, req: ModelRequest) -> ModelResponse:
        path = os.path.join(self.dir, f"{self.key(req)}.json")
        if self.mode == "replay":
            if not os.path.exists(path):
                raise FileNotFoundError(f"no recorded model output for this prompt ({path})")
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            return ModelResponse(**d)
        assert self.inner is not None, "record mode needs a live model"
        resp = self.inner.complete(req)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(resp.__dict__, fh, ensure_ascii=False, indent=1)
        return resp
