# ruff: noqa: E501 — prompt fixture strings are long
"""nightly_audit × broker ground truth: the ground truth is prompt context + deterministic card
alarms that can only RAISE the verdict; it never removes or downgrades an LLM finding."""
import unittest
from unittest import mock

import nightly_audit as na

SOFI_REPORT = """### CATASTROPHIC ALERT: 1
* `execution.stop_protection` | `[SOFI] STOP-PROTECT: placed MISSING buy stop x4 @ $18.36 (DAY) — order cf194550` | SOFI
  * **Failure Condition:** leaving the position effectively naked despite the attempt to place the stop.

### VERDICT: FAIL
"""


class TestEscalation(unittest.TestCase):
    def test_critical_forces_fail(self):
        for v in ("PASS", "WARN", "UNKNOWN", "FAIL"):
            self.assertEqual(na._escalate_card_verdict(v, [{"severity": "critical"}]), "FAIL")

    def test_high_raises_pass_and_unknown_only(self):
        hi = [{"severity": "high"}]
        self.assertEqual(na._escalate_card_verdict("PASS", hi), "WARN")
        self.assertEqual(na._escalate_card_verdict("UNKNOWN", hi), "WARN")
        self.assertEqual(na._escalate_card_verdict("WARN", hi), "WARN")
        self.assertEqual(na._escalate_card_verdict("FAIL", hi), "FAIL")

    def test_low_or_none_never_changes_the_verdict(self):
        for v in ("PASS", "WARN", "FAIL", "UNKNOWN"):
            self.assertEqual(na._escalate_card_verdict(v, [{"severity": "low"}]), v)
            self.assertEqual(na._escalate_card_verdict(v, []), v)


class TestPostFilterUntouched(unittest.TestCase):
    def test_llm_naked_claim_is_never_downgraded_by_this_change(self):
        with mock.patch.object(na, "_load_suppressions", return_value=[]):
            out, verdict, n_sup, n_ack = na._apply_suppressions(SOFI_REPORT, "FAIL")
        self.assertEqual((out, verdict, n_sup, n_ack), (SOFI_REPORT, "FAIL", 0, 0))

    def test_fifo_orphan_line_never_matches_a_directive(self):
        sup = [{"status": "false_alarm", "match_keywords": ["goog"]}]
        self.assertIsNone(na._match_directive("FIFO orphan: closing fill for GOOGL has no prior lot", sup))
        self.assertIsNotNone(na._match_directive("GOOGL replay noise", sup))


class TestPromptCarriesGroundTruth(unittest.TestCase):
    def test_block_and_rules_in_prompt(self):
        with mock.patch.object(na, "_build_config_constants_block", return_value=""):
            p = na._build_prompt("", "", "", {}, "BROKER GROUND TRUTH (x): SOFI BY-DESIGN")
            empty = na._build_prompt("", "", "", {})
        self.assertIn("BROKER GROUND TRUTH (x): SOFI BY-DESIGN", p)
        self.assertIn('"broker-held 0" does NOT mean "no stops"', p)
        self.assertIn("STOP COVERAGE IS OWNED BY CODE", p)
        self.assertIn("never inferred from log text", p)
        self.assertIn("stop coverage UNVERIFIED", empty)
        # masked-loss seat R5: on an UNKNOWN/unavailable day the LLM may still report a naked
        # position the bot's own log states (quoted, broker-unverified) — never silenced
        for prompt in (p, empty):
            self.assertIn("broker-unverified", prompt)
            self.assertNotIn("never a naked claim", prompt)


class TestOutagePathPrompt(unittest.TestCase):
    def test_collect_unknown_rendered_into_prompt_allows_quoted_self_report(self):
        from reporting import broker_ground_truth as bgt
        with mock.patch.object(bgt, "_session_bounds", side_effect=RuntimeError("HTTP 503")):
            gt = bgt.collect("2026-09-18")
        with mock.patch.object(na, "_build_config_constants_block", return_value=""):
            p = na._build_prompt("", "", "", {}, bgt.render(gt))
        self.assertIn("UNKNOWN", p)
        self.assertEqual(p.count("broker-unverified") >= 2, True)
        self.assertNotIn("do not assert 'naked'", p)


class TestComplianceCounter(unittest.TestCase):
    def test_counts_violation_days_in_last_five(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "gt.jsonl"
            f.write_text("".join(f'{{"date": "2026-09-{x:02d}", "violations": {v}}}\n'
                                 for x, v in ((1, '["A"]'), (2, "[]"), (3, '["B"]'), (4, "[]"))))
            with mock.patch.object(na, "GT_COMPLIANCE_FILE", f), mock.patch.object(na, "AUDIT_DATE", "2026-09-05"):
                self.assertEqual(na._record_gt_compliance(["SOFI"], True), 3)
                self.assertEqual(na._record_gt_compliance([], False), 3)     # not recorded
            self.assertEqual(len(f.read_text().splitlines()), 5)

    def test_malformed_non_object_line_is_skipped(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "gt.jsonl"
            f.write_text('{"date": "2026-09-01", "violations": ["A"]}\n42\n{"date": "2026-09-02", "violations": ["B"]}\n')
            with mock.patch.object(na, "GT_COMPLIANCE_FILE", f), mock.patch.object(na, "AUDIT_DATE", "2026-09-03"):
                self.assertEqual(na._record_gt_compliance(["C"], True), 3)

    def test_counter_never_raises(self):
        with mock.patch.object(na, "GT_COMPLIANCE_FILE", mock.Mock(exists=mock.Mock(side_effect=OSError("x")))):
            self.assertEqual(na._record_gt_compliance(["A"], True), 0)


if __name__ == "__main__":
    unittest.main()
