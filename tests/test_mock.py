import json
import os
import tempfile
import unittest

from errand.clock import FakeClock
from errand.mock.provider import MockConfig, MockProvider, ProviderError, ProviderTimeout


def provider(**cfg):
    d = tempfile.mkdtemp()
    clock = FakeClock()
    return MockProvider(os.path.join(d, "p.db"), clock, MockConfig(**cfg)), clock


def first_hold(p):
    r = p.search_providers("plumber", "Indiranagar", ("2026-11-02", "2026-11-08"))
    return r, p.hold_slot(r[0]["id"], r[0]["free_slots"][0])


class MockContractTest(unittest.TestCase):
    def test_search_returns_3_to_8_providers(self):
        p, _ = provider()
        for service in ("plumber", "electrician", "ac_servicing", "cleaning", "carpenter"):
            for area in ("Indiranagar", "Koramangala", "HSR Layout", "Whitefield"):
                n = len(p.search_providers(service, area, ("2026-11-02", "2026-11-08")))
                self.assertTrue(3 <= n <= 8, (service, area, n))

    def test_book_outcome_distribution_matches_the_brief(self):
        p, _ = provider(mode="realistic", seed=3)
        counts = {"ok": 0, "slot_taken": 0, "timeout_booked": 0, "timeout_lost": 0}
        for _ in range(4000):
            counts[p._decide_book(None)] += 1
        n = sum(counts.values())
        self.assertAlmostEqual(counts["ok"] / n, 0.70, delta=0.03)
        self.assertAlmostEqual(counts["slot_taken"] / n, 0.15, delta=0.03)
        self.assertAlmostEqual((counts["timeout_booked"] + counts["timeout_lost"]) / n, 0.15, delta=0.03)
        self.assertAlmostEqual(counts["timeout_booked"] / (counts["timeout_booked"] + counts["timeout_lost"]),
                               0.5, delta=0.1)

    def test_outcomes_are_reproducible_across_instances(self):
        a, _ = provider(mode="realistic", seed=9)
        b, _ = provider(mode="realistic", seed=9)
        self.assertEqual([a._decide_book(None) for _ in range(50)], [b._decide_book(None) for _ in range(50)])

    def test_timeout_after_commit_is_visible_to_status(self):
        p, _ = provider()
        _, h = first_hold(p)
        p.arm("book", "timeout_booked")
        with self.assertRaises(ProviderTimeout):
            p.book(h["hold_id"], "k1")
        self.assertEqual(p.get_booking_status("k1")["status"], "booked")

    def test_idempotent_replay_and_mismatch(self):
        p, _ = provider()
        _, h = first_hold(p)
        first = p.book(h["hold_id"], "k1")
        self.assertEqual(p.book(h["hold_id"], "k1"), first)
        with self.assertRaises(ProviderError) as e:
            p.book("H-999", "k1")
        self.assertEqual(e.exception.code, "idempotency_mismatch")
        self.assertEqual(len([b for b in p.all_bookings() if b["idem_key"]]), 1)

    def test_hold_expires_after_ten_minutes(self):
        p, clock = provider()
        _, h = first_hold(p)
        clock.advance(minutes=11)
        with self.assertRaises(ProviderError) as e:
            p.book(h["hold_id"], "k1")
        self.assertEqual(e.exception.code, "hold_expired")

    def test_cancel_first_call_503_then_success(self):
        p, _ = provider()
        with self.assertRaises(ProviderError) as e:
            p.cancel("B-1990", "c1")
        self.assertEqual(e.exception.code, "transient_503")
        self.assertEqual(p.cancel("B-1990", "c1")["status"], "cancelled")

    def test_reschedule_conflict_on_configured_slot(self):
        p, _ = provider()
        b = next(x for x in p.list_bookings("u_demo") if x["booking_id"] == "B-1990")
        free = p.search_providers("ac_servicing", "Indiranagar", ("2026-11-07", "2026-11-07"))
        slot = next(x for x in free if x["id"] == b["provider_id"])["free_slots"][0]
        p.config.reschedule_conflicts = ((b["provider_id"], slot),)
        with self.assertRaises(ProviderError) as e:
            p.reschedule("B-1990", slot, "r1")
        self.assertEqual(e.exception.code, "slot_conflict")

    def test_reviews_include_an_injection_attempt(self):
        p, _ = provider()
        texts = [r["text"] for r in p.get_provider("P-04")["reviews"]]
        self.assertTrue(any("Ignore previous instructions" in t for t in texts))

    def test_call_log_records_every_write(self):
        p, _ = provider()
        _, h = first_hold(p)
        p.book(h["hold_id"], "k1")
        calls = p.calls("book")
        self.assertEqual(json.loads(calls[0]["args"])["idempotency_key"], "k1")


if __name__ == "__main__":
    unittest.main()
