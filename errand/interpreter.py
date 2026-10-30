"""Turns one user message into a validated Intent. The only model call that sees user text."""
from __future__ import annotations

import json

from .domain import INTENT_SCHEMA, Intent, IntentError, contains_untrusted, parse_intent
from .llm.base import Model, ModelRequest
from .llm.prompts import INTERPRETER_SYSTEM, INTERPRETER_TOOL


class Interpreter:
    def __init__(self, model: Model) -> None:
        self.model = model

    def interpret(self, ctx, context: dict, message: str) -> Intent:
        if contains_untrusted(context):
            raise AssertionError("untrusted tool text must never reach the interpreter")
        user = json.dumps({"context": context, "message": message}, ensure_ascii=False)
        req = ModelRequest(purpose="interpret", system=INTERPRETER_SYSTEM, user=user,
                           tool_name=INTERPRETER_TOOL[0], tool_description=INTERPRETER_TOOL[1],
                           schema=INTENT_SCHEMA, context=context, message=message)
        last_error = ""
        for attempt in (1, 2):
            ctx.budget.charge_model()
            resp = self.model.complete(req)
            ctx.budget.add_cost(resp.cost_usd)
            try:
                if resp.data is None:
                    raise IntentError(resp.error or "no output")
                intent = parse_intent(resp.data)
            except IntentError as exc:
                last_error = str(exc)
                ctx.trace.emit("model_call", purpose="interpret", attempt=attempt, error=last_error,
                               **_usage(resp))
                req.user = user + f"\n\nYour previous output was invalid ({last_error}). Call emit_intent again."
                continue
            ctx.trace.emit("model_call", purpose="interpret", attempt=attempt, intent=intent.kind, **_usage(resp))
            return intent
        return Intent(kind="unclear")


def _usage(resp) -> dict:
    return {"gen_ai.request.model": resp.model, "gen_ai.usage.input_tokens": resp.input_tokens,
            "gen_ai.usage.output_tokens": resp.output_tokens, "cost_usd": resp.cost_usd,
            "latency_ms": resp.latency_ms}
