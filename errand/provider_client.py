"""The only path from the agent to the provider.

Every call is budget-checked, traced, and turned into a ``Result`` whose
``error_class`` alone decides what the executor does next. Free text comes back
wrapped as ``UntrustedText``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .clock import Clock
from .domain import UntrustedText
from .mock.provider import MockProvider, ProviderError, ProviderTimeout
from .util import sanitize_label

ERROR_CLASS = {
    "timeout": "UNKNOWN_OUTCOME",
    "transient_503": "RETRYABLE",
    "slot_taken": "OPTION_GONE",
    "slot_unavailable": "OPTION_GONE",
    "slot_conflict": "OPTION_GONE",
    "hold_expired": "HOLD_GONE",
    "invalid_input": "REQUEST_INVALID",
    "not_found": "REQUEST_INVALID",
    "idempotency_mismatch": "REQUEST_INVALID",
}


@dataclass
class Result:
    ok: bool
    value: Any = None
    code: str | None = None
    error_class: str | None = None
    detail: str = ""


class ProviderClient:
    def __init__(self, provider: MockProvider, clock: Clock) -> None:
        self.provider = provider
        self.clock = clock

    def _call(self, ctx, name: str, fn: Callable[..., Any], args: dict, summary: Callable[[Any], Any],
              attempt: int = 1) -> Result:
        ctx.budget.charge_tool()
        t0 = self.clock.now()
        try:
            res = Result(True, fn(**args))
        except ProviderTimeout:
            res = Result(False, code="timeout", error_class="UNKNOWN_OUTCOME", detail="no reply in time")
        except ProviderError as exc:
            res = Result(False, code=exc.code, error_class=ERROR_CLASS.get(exc.code, "UNEXPECTED"), detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - the boundary with an external system
            res = Result(False, code="unexpected", error_class="UNEXPECTED", detail=f"{type(exc).__name__}: {exc}")
        ms = round((self.clock.now() - t0).total_seconds() * 1000)
        ctx.trace.emit("tool_call", name=name, args=args, latency_ms=ms, attempt=attempt,
                       result=summary(res.value) if res.ok else None,
                       error=None if res.ok else res.code, error_class=res.error_class)
        ctx.last_tool_ms = ms
        return res

    # ---------------------------------------------------------------- tools
    def search_providers(self, ctx, service: str, area: str, date_range: tuple[str, str]) -> Result:
        def run(service: str, area: str, date_range: list[str]):
            out = self.provider.search_providers(service, area, (date_range[0], date_range[1]))
            for p in out:
                p["name"] = sanitize_label(p["name"])
            return out
        return self._call(ctx, "search_providers", run,
                          {"service": service, "area": area, "date_range": list(date_range)},
                          lambda v: {"count": len(v), "providers": [p["id"] for p in v]})

    def get_provider(self, ctx, provider_id: str) -> Result:
        def run(provider_id: str):
            d = self.provider.get_provider(provider_id)
            d["name"] = sanitize_label(d["name"])
            d["reviews"] = [{"stars": r["stars"], "text": UntrustedText(r["text"])} for r in d["reviews"]]
            return d
        return self._call(ctx, "get_provider", run, {"provider_id": provider_id},
                          lambda v: {"id": v["id"], "rating": v["rating"], "reviews": len(v["reviews"])})

    def list_bookings(self, ctx, user_id: str) -> Result:
        def run(user_id: str):
            out = self.provider.list_bookings(user_id)
            for b in out:
                b["provider_name"] = sanitize_label(b["provider_name"])
            return out
        return self._call(ctx, "list_bookings", run, {"user_id": user_id}, lambda v: {"count": len(v)})

    def hold_slot(self, ctx, provider_id: str, slot: str) -> Result:
        return self._call(ctx, "hold_slot", self.provider.hold_slot, {"provider_id": provider_id, "slot": slot},
                          lambda v: v)

    def book(self, ctx, hold_id: str, key: str, attempt: int = 1) -> Result:
        return self._call(ctx, "book", self.provider.book, {"hold_id": hold_id, "idempotency_key": key},
                          lambda v: v, attempt)

    def get_booking_status(self, ctx, key: str) -> Result:
        return self._call(ctx, "get_booking_status", self.provider.get_booking_status, {"idempotency_key": key},
                          lambda v: v)

    def cancel(self, ctx, booking_id: str, key: str, attempt: int = 1) -> Result:
        return self._call(ctx, "cancel", self.provider.cancel, {"booking_id": booking_id, "idempotency_key": key},
                          lambda v: v, attempt)

    def reschedule(self, ctx, booking_id: str, new_slot: str, key: str, attempt: int = 1) -> Result:
        return self._call(ctx, "reschedule", self.provider.reschedule,
                          {"booking_id": booking_id, "new_slot": new_slot, "idempotency_key": key},
                          lambda v: {"booking_id": v["booking_id"], "slot_start": v["slot_start"]}, attempt)
