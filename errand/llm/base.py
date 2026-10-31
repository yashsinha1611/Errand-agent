"""Model interface. The agent only ever asks a model for a typed object."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ModelRequest:
    purpose: str                 # "interpret" | "summarise_reviews"
    system: str
    user: str                    # what a real model sees (JSON context + message)
    tool_name: str
    tool_description: str
    schema: dict
    context: dict = field(default_factory=dict)   # structured view of `user`, for offline models
    message: str = ""


@dataclass
class ModelResponse:
    data: dict | None
    raw: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None


class Model(Protocol):
    name: str

    def complete(self, req: ModelRequest) -> ModelResponse: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def payload_for(data: Any) -> str:
    import json
    return json.dumps(data, ensure_ascii=False)
