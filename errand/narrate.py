"""Reply templates. Every number, name and id in a reply comes from a record, never a model."""
from __future__ import annotations

from .domain import AREAS, SERVICES, Approval, ErrandState
from .util import fmt_inr, fmt_short_slot, fmt_slot, parse_iso

PLURAL = {"plumber": "plumbers", "electrician": "electricians", "ac_servicing": "AC servicing providers",
          "cleaning": "cleaning services", "carpenter": "carpenters"}
WHO = {"plumber": "plumber", "electrician": "electrician", "ac_servicing": "AC technician",
       "cleaning": "cleaner", "carpenter": "carpenter"}
NUMBER = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}


def join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def help_text() -> str:
    return ("I can book, move or cancel home services: plumbers, electricians, AC servicing, cleaning and "
            "carpentry in Indiranagar, Koramangala, HSR Layout and Whitefield. Try “Find a plumber for my kitchen "
            "tap this week, mornings only, under ₹1,500”. Nothing is booked until you approve the exact provider, "
            "slot and price.")


def ask_missing(missing: list[str], s: ErrandState) -> str:
    service = s.slot("service")
    who = WHO.get(service, "provider")
    required = [m for m in missing if m in ("service", "area", "date_range")]
    optional = [m for m in missing if m in ("time_window", "budget")]
    opt_phrase = join(["time of day" if "time_window" in optional else "", "budget" if "budget" in optional else ""])
    if required == ["area"]:
        q = f"Which area should the {who} come to: {', '.join(AREAS[:-1])} or {AREAS[-1]}?"
    elif required == ["date_range"]:
        q = f"When do you need the {who}? For example tomorrow, this week or Saturday."
    elif required == ["service"]:
        q = "What do you need done: a plumber, electrician, AC servicing, cleaning or a carpenter?"
    elif required:
        phrases = {"service": "what you need done", "area": "which area", "date_range": "when"}
        q = f"To find a {who}, tell me {join([phrases[r] for r in required])}."
    else:
        q = ""
    if optional:
        tail = f"Any {opt_phrase} in mind? Say “no preference” to skip."
        q = f"{q} {tail}".strip()
    return q


def offers_intro(offers: list[dict], s: ErrandState) -> str:
    service = s.slot("service")
    n = len(offers)
    tw = (s.slots.get("time_window") or {}).get("label")
    budget = s.slot("budget")
    plural = PLURAL.get(service, "providers") if n != 1 else WHO.get(service, "provider")
    expr = (s.slots.get("time_window") or {}).get("expr")
    when = {"morning": "morning slots", "afternoon": "afternoon slots", "evening": "evening slots"}.get(expr or "") \
        or (f"slots {tw}" if tw and tw != "any time" else "slots")
    budget_clause = f" under {fmt_inr(budget)}" if isinstance(budget, int) else ""
    verb = "have" if n != 1 else "has"
    return f"{NUMBER.get(n, str(n))} {plural} {verb} {when} {date_phrase(s)}{budget_clause}. Pick one:"


def date_phrase(s: ErrandState) -> str:
    dr = s.slots.get("date_range") or {}
    expr, label = dr.get("expr", ""), dr.get("label", "")
    return {"this_week": "this week", "next_week": "next week", "weekend": "this weekend",
            "today": "today", "tomorrow": "tomorrow"}.get(expr, f"on {label}" if "–" not in label else f"for {label}")


def option_lines(options: list[dict]) -> str:
    return "\n".join(f"{i}. {o['name']} ({o['meta']}): {o['slot']}, {fmt_inr(o['price'])}"
                     for i, o in enumerate(options, 1))


def no_offers(s: ErrandState, over_budget: list[dict]) -> str:
    who = PLURAL.get(s.slot("service"), "providers")
    if over_budget:
        cheapest = min(over_budget, key=lambda o: o["price"])
        return (f"No {who} fit your budget of {fmt_inr(s.slot('budget'))}. The cheapest with a matching slot is "
                f"{cheapest['name']} at {fmt_inr(cheapest['price'])}. Want me to raise the budget or change the time?")
    return f"No {who} have free slots that match. Try another day or a wider time window."


def quote_note(appr: Approval, hold_until: str) -> str:
    until = parse_iso(hold_until).strftime("%H:%M")
    parts = []
    if appr.search_price is not None and appr.search_price != appr.price:
        diff = appr.price - appr.search_price
        parts.append(f"The hold came back with a quote of {fmt_inr(appr.price)}, {fmt_inr(abs(diff))} "
                     f"{'above' if diff > 0 else 'below'} the search price")
    else:
        parts.append(f"I’ve held {fmt_short_slot(appr.slot_start)} with {appr.provider_name} at {fmt_inr(appr.price)}")
    if appr.budget:
        over = appr.price - appr.budget
        parts.append(f"{fmt_inr(over)} over your {fmt_inr(appr.budget)} budget" if over > 0
                     else f"within your {fmt_inr(appr.budget)} budget")
    return ", ".join(parts) + f". The slot is held until {until}."


def approval_prompt(appr: Approval, old_slot: str | None = None) -> str:
    slot = fmt_slot(appr.slot_start)
    if appr.action == "reschedule":
        return (f"Move booking {appr.booking_id} with {appr.provider_name} from {fmt_slot(old_slot or appr.slot_start)} "
                f"to {slot}, {fmt_inr(appr.price)}. Reply yes to approve.")
    if appr.action == "cancel":
        return f"Cancel booking {appr.booking_id}: {appr.provider_name}, {slot}, {fmt_inr(appr.price)}. Reply yes to approve."
    return f"Book {appr.provider_name}, {slot}, {fmt_inr(appr.price)}. Reply yes to approve."


def approval_card(appr: Approval, old_slot: str | None = None) -> dict:
    return {"id": appr.id, "action": appr.action, "provider": appr.provider_name, "when": fmt_slot(appr.slot_start),
            "from": fmt_slot(old_slot) if old_slot else None, "price": appr.price,
            "search_price": appr.search_price, "budget": appr.budget, "fingerprint": appr.fingerprint[:6],
            "expires": parse_iso(appr.hold_expires).strftime("%H:%M") if appr.hold_expires else None,
            "booking_id": appr.booking_id}


def booked(appr: Approval, booking_id: str, after_timeout: bool) -> str:
    line = f"{appr.provider_name}, {fmt_slot(appr.slot_start)}, {fmt_inr(appr.price)}"
    if after_timeout:
        return ("Booked, after one hiccup: the provider’s system timed out, so I checked the booking status "
                f"instead of trying again. It went through once.\n\nBooking {booking_id}: {line}.")
    return f"Booked. {appr.provider_name} will come on {fmt_slot(appr.slot_start)}. Booking {booking_id}, {fmt_inr(appr.price)}."


def unresolved(appr: Approval) -> str:
    return (f"I couldn’t confirm the booking with {appr.provider_name} yet: the provider isn’t answering. I have "
            "not booked anything twice, and I’ll check its status before doing anything else on your next message.")


def truth_sentence(truth: dict) -> str:
    kind = truth["kind"]
    if kind == "confirmed":
        return f"Booking {truth['booking_id']} is confirmed."
    if kind == "open":
        return "A request to the provider is still unconfirmed; I’ll check its status first on your next message."
    if kind == "failed":
        return "The last request didn’t go through, so nothing was booked."
    return "Nothing was booked, moved or cancelled."


def budget_exit(label: str, truth: dict) -> str:
    return f"I stopped because I reached my limit of {label}. {truth_sentence(truth)} Send a message to carry on."


def unexpected_exit(kind: str, truth: dict) -> str:
    return f"Something failed on my side ({kind}). {truth_sentence(truth)} You can try again."


def service_label(service: str | None) -> str:
    return SERVICES.get(service or "", "service")
