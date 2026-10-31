"""Claude adapter using only the standard library (no SDK dependency).

Structured output comes from a single tool whose input schema is the Intent schema.
``tool_choice`` stays ``auto`` because some current models reject forced tool use;
the system prompt asks for exactly one tool call and the interpreter validates and
repairs whatever comes back.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..config import MODEL_PRICES_PER_MTOK
from .base import ModelRequest, ModelResponse

API_URL = "https://api.anthropic.com/v1/messages"


class AnthropicModel:
    def __init__(self, api_key: str, model: str = "claude-haiku-5-5", timeout_s: float = 20.0) -> None:
        self._key = api_key
        self.name = model
        self.timeout_s = timeout_s

    def complete(self, req: ModelRequest) -> ModelResponse:
        body = {
            "model": self.name,
            "max_tokens": 800,
            "system": req.system,
            "tools": [{"name": req.tool_name, "description": req.tool_description, "input_schema": req.schema}],
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": req.user}],
        }
        request = urllib.request.Request(
            API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            return ModelResponse(data=None, raw="", model=self.name, error=f"HTTP {exc.code}: {detail}",
                                 latency_ms=int((time.monotonic() - t0) * 1000))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return ModelResponse(data=None, raw="", model=self.name, error=f"network: {exc}",
                                 latency_ms=int((time.monotonic() - t0) * 1000))
        latency = int((time.monotonic() - t0) * 1000)
        usage = payload.get("usage", {})
        tin, tout = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        price_in, price_out = MODEL_PRICES_PER_MTOK.get(self.name, (0.0, 0.0))
        cost = (tin * price_in + tout * price_out) / 1_000_000
        data, raw = None, ""
        for block in payload.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == req.tool_name:
                data = block.get("input")
                raw = json.dumps(data, ensure_ascii=False)
                break
            if block.get("type") == "text":
                raw = block.get("text", "")
                try:
                    start, end = raw.index("{"), raw.rindex("}") + 1
                    data = json.loads(raw[start:end])
                except ValueError:
                    pass
        return ModelResponse(data=data, raw=raw, model=self.name, input_tokens=tin, output_tokens=tout,
                             cost_usd=round(cost, 8), latency_ms=latency,
                             error=None if data is not None else "no structured output")
