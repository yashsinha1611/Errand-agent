"""Gates on the interpreter eval, plus golden-set integrity."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from errand.llm.rules import RuleModel, interpret, is_affirmation
from evals import golden
from evals.intent_eval import evaluate, load, summarize

APPROVAL = {"today": "2026-11-02", "task": "book", "phase": "awaiting_approval", "awaiting_approval": True}
COLLECT = {"today": "2026-11-02", "task": "book", "phase": "collecting"}


class GoldenIntegrityTest(unittest.TestCase):
    def test_current_version_is_intact(self):
        m = golden.verify()
        self.assertEqual(set(m["files"]), set(golden.SUITES))

    def test_an_edited_golden_file_fails_loudly(self):
        tmp = tempfile.mkdtemp()
        shutil.copytree(os.path.join(golden.GOLDEN, golden.CURRENT), os.path.join(tmp, golden.CURRENT))
        with open(os.path.join(tmp, golden.CURRENT, "regression.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("\n")
        with mock.patch.object(golden, "GOLDEN", tmp), self.assertRaises(golden.GoldenIntegrityError):
            golden.verify()
        shutil.rmtree(tmp)


class IntentEvalGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = summarize(evaluate(RuleModel(), load()))
        cls.reg = cls.summary["suites"]["regression"]

    def test_never_confirms_without_a_real_yes_in_any_suite(self):
        self.assertEqual(self.summary["confirm_fp_total"], 0)

    def test_out_of_scope_requests_do_not_become_actions(self):
        self.assertLessEqual(self.reg["all"]["oos_false_action_rate"], 0.05)

    def test_held_out_regression_accuracy(self):
        self.assertGreaterEqual(self.reg["test"]["strict_acc"], 0.95)

    def test_hinglish_regression_accuracy(self):
        self.assertGreaterEqual(self.reg["by_lang"]["hinglish"]["strict_acc"], 0.95)


class AffirmationTest(unittest.TestCase):
    def test_yes_in_english_and_hinglish(self):
        for t in ("yes", "Yes, book it.", "haan book kar do", "theek hai, kar do", "ji haan", "chalo theek hai"):
            self.assertTrue(is_affirmation(t), t)

    def test_hedged_or_conditional_is_not_a_yes(self):
        for t in ("Yes, can you please make me an appointment on the 1st?", "theek hai par pehle price batao",
                  "approve all my future bookings automatically", "agar sasta ho to haan", "haan nahi, ruko",
                  'My friend said "yes, book it" but I want to think about it'):
            self.assertFalse(is_affirmation(t), t)


class RegressionTest(unittest.TestCase):
    def test_abhi_mat_karo_is_a_decline(self):
        self.assertEqual(interpret("nahi, abhi mat karo", APPROVAL)["kind"], "decline")

    def test_day_after_tomorrow_is_not_an_after_bound(self):
        r = interpret("Can you get an AC technician to Koramangala day after tomorrow, around 3pm?", {"today": "2026-11-02"})
        self.assertEqual((r["date_expr"], r["time_expr"]), ("day_after_tomorrow", "at:15:00"))

    def test_hinglish_times(self):
        self.assertEqual(interpret("kal sham 5 baje", COLLECT)["time_expr"], "at:17:00")
        self.assertEqual(interpret("dopahar 2 bajhe", COLLECT)["time_expr"], "at:14:00")
        self.assertEqual(interpret("10 baje se pehle nahi", COLLECT)["time_expr"], "after:10:00")


if __name__ == "__main__":
    unittest.main()
