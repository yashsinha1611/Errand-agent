"""All tunables in one place. Every eval can override any value."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# USD per million tokens. Verified against Anthropic's published pricing in Oct 2026;
# edit here if prices change. Haiku 5.5 is priced higher above 100K-token prompts,
# which this agent never approaches.
MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}


@dataclass(frozen=True)
class Caps:
    """Hard limits. Per-turn caps stop runaway loops; per-session caps bound cost."""

    model_calls_per_turn: int = 4
    tool_calls_per_turn: int = 20
    steps_per_turn: int = 12
    wall_seconds_per_turn: float = 45.0
    model_calls_per_session: int = 80
    tool_calls_per_session: int = 250
    cost_usd_per_session: float = 0.50
    unclear_in_a_row: int = 3


@dataclass(frozen=True)
class RetryPolicy:
    write_attempts: int = 3          # book / cancel / reschedule attempts per idempotency key
    base_backoff_s: float = 0.5
    max_backoff_s: float = 4.0


@dataclass(frozen=True)
class Settings:
    data_dir: str = "var"
    user_id: str = "u_demo"
    caps: Caps = field(default_factory=Caps)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    approval_ttl_min: int = 15
    max_options_shown: int = 3
    trace_capture_content: bool = True
    model: str = "rule"              # "rule" or a Claude model id such as claude-haiku-5-5
    anthropic_api_key: str | None = None
    demo_mode: bool = False          # enables fault injection, clock and reset endpoints

    def with_caps(self, **kw: object) -> "Settings":
        return replace(self, caps=replace(self.caps, **kw))

    @staticmethod
    def from_env() -> "Settings":
        key = os.environ.get("ANTHROPIC_API_KEY") or None
        model = os.environ.get("ERRAND_MODEL") or ("claude-haiku-5-5" if key else "rule")
        return Settings(
            data_dir=os.environ.get("ERRAND_DATA_DIR", "var"),
            model=model,
            anthropic_api_key=key,
            trace_capture_content=os.environ.get("TRACE_CAPTURE_CONTENT", "true").lower() != "false",
            demo_mode=os.environ.get("ERRAND_DEMO", "1") == "1",
        )
