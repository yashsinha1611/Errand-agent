"""Builds a fully wired agent. Used by the CLI, the web server and the eval harness."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .agent import AgentService
from .clock import Clock, SystemClock
from .config import Settings
from .llm.anthropic import AnthropicModel
from .llm.base import Model
from .llm.rules import RuleModel
from .mock.provider import MockConfig, MockProvider
from .storage import AgentDB
from .util import IdFactory


@dataclass
class World:
    agent: AgentService
    provider: MockProvider
    db: AgentDB
    clock: Clock
    settings: Settings

    def close(self) -> None:
        self.db.close()
        self.provider.close()


def build_model(settings: Settings) -> Model:
    if settings.model != "rule" and settings.anthropic_api_key:
        return AnthropicModel(settings.anthropic_api_key, settings.model)
    return RuleModel()


def build(settings: Settings, clock: Clock | None = None, mock_config: MockConfig | None = None,
          model: Model | None = None, seed: int | None = None) -> World:
    os.makedirs(settings.data_dir, exist_ok=True)
    clock = clock or SystemClock()
    provider = MockProvider(os.path.join(settings.data_dir, "provider.db"), clock, mock_config or MockConfig(),
                            settings.user_id)
    db = AgentDB(os.path.join(settings.data_dir, "agent.db"))
    boot = db.next_boot(clock.now().isoformat())
    ids = IdFactory(None if seed is None else seed * 1000 + boot)
    agent = AgentService(settings, db, provider, model or build_model(settings), clock, ids,
                         os.path.join(settings.data_dir, "traces"))
    return World(agent, provider, db, clock, settings)
