"""Runs the eval scenarios under unittest, plus negative controls and a seeded fuzz."""
import random
import unittest
from unittest import mock

from errand.agent import AgentService, InjectedCrash
from errand.mock.provider import MockConfig
from evals.harness import Harness
from evals.invariants import check_all
from evals.run import run_one
from evals.scenarios import PLUMBER, SCENARIOS


def failing(h: Harness) -> set[str]:
    return {name.split(" ")[0] for name, ok, _ in check_all(h) if not ok}


class ScenarioTest(unittest.TestCase):
    def test_every_scenario_passes(self):
        for sc in SCENARIOS:
            with self.subTest(scenario=sc.id):
                result = run_one(sc)
                self.assertTrue(result["passed"], f"{sc.id}: {result['notes']}\n{result['error'] or ''}")


class NegativeControlTest(unittest.TestCase):
    """If the safeguards are removed, the invariants must notice. Otherwise they prove nothing."""

    def test_invariants_catch_a_booking_without_approval(self):
        h = Harness("nc-approval")

        def rogue(self, ctx, search_price, note=True):  # a bug: book straight after the hold
            self.client.book(ctx, ctx.state.hold["hold_id"], "rogue-key")

        with mock.patch.object(AgentService, "_propose_book", rogue):
            h.say(PLUMBER)
            h.say("Indiranagar")
            h.say("Option 1")
        self.assertTrue({"I1", "I3"} <= failing(h), failing(h))
        h.cleanup()

    def test_invariants_catch_an_unverified_success_claim(self):
        h = Harness("nc-claim")
        with mock.patch.object(AgentService, "_verify_book", lambda self, ctx, key, expect: ("booked", "B-9999")):
            h.say(PLUMBER)
            h.say("Indiranagar")
            h.say("Option 1")
            h.say("yes")
        self.assertIn("I4", failing(h))
        h.cleanup()

    def test_invariants_catch_a_grant_without_a_yes(self):
        from errand.llm.rules import RuleModel
        from errand.llm.scripted import ScriptedModel
        h = Harness("nc-grant", model=ScriptedModel([None, None, None, {"kind": "confirm"}], fallback=RuleModel()))
        h.say(PLUMBER)
        h.say("Indiranagar")
        h.say("Option 1")
        with mock.patch("errand.agent.is_affirmation", return_value=True):  # a bug: any text counts as a yes
            h.say("tell me about the weather")
        self.assertIn("I7", failing(h))
        h.cleanup()


VOCAB = [
    PLUMBER, "Indiranagar", "Koramangala", "Option 1", "Option 2", "yes", "yes", "yes, book it", "no",
    "Actually make it Wednesday", "make it under ₹900", "Move my AC servicing to Saturday", "Cancel my AC servicing",
    "What do people say about Sharma Plumbing?", "Remember Indiranagar as my area", "Forget my area",
    "Find an electrician tomorrow morning", "Book AC servicing in Koramangala tomorrow, no budget", "never mind",
    "zxqv", "what do you remember about me?", "show my bookings", "Book the same electrician as last time",
]


class FuzzTest(unittest.TestCase):
    """Random conversations against the realistic 70/15/15 provider, with price drift, clock jumps,
    restarts and crashes. Every invariant must hold for every seed."""

    SEEDS = range(40)

    def test_invariants_hold_under_random_conversations(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                h = Harness(f"fz{seed}", mock=MockConfig(mode="realistic", price_drift="random", seed=seed))
                try:
                    for _ in range(16):
                        x = rng.random()
                        if x < 0.06:
                            h.clock.advance(minutes=rng.choice([2, 6, 11, 16]))
                        elif x < 0.10:
                            point = rng.choice(["after_outbox_write", "after_book_sent"])
                            h.crash_on(point, lambda: h.say(rng.choice(["yes", "yes, book it"])))
                            h.resume()
                        elif x < 0.13:
                            h.restart()
                            h.resume()
                        elif x < 0.15:
                            h.new_session()
                        else:
                            h.say(rng.choice(VOCAB))
                    bad = [(n, d) for n, ok, d in check_all(h) if not ok]
                    self.assertFalse(bad, f"seed {seed}: {bad}")
                    # The boundary handler turns exceptions into polite replies; in the fuzz no fault
                    # raises exceptions, so any error event is a real bug being hidden.
                    errors = [e for e in h.traces() if e["type"] == "error"]
                    self.assertFalse(errors, f"seed {seed}: {errors[:1]}")
                except InjectedCrash as exc:  # pragma: no cover - a crash outside crash_on is a bug
                    self.fail(f"seed {seed}: unexpected crash {exc}")
                finally:
                    h.cleanup()


if __name__ == "__main__":
    unittest.main()
