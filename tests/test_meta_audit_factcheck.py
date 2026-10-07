#!/usr/bin/env python3
# ruff: noqa: E501
"""auto_ai_audit fact-check gate (Rafael CEO order 2026-10-06): nothing unproven reaches the owner.

A claim passes ONLY when the checker returns SUPPORTED with a verbatim quote that is MECHANICALLY found in the audit
DATA or the named repo file. The checker's say-so alone never passes a claim; a checker failure withholds everything."""
import json
import unittest
from unittest import mock

import auto_ai_audit as aa

TODAY = aa.datetime.now(aa._ET).strftime("%Y-%m-%d")
DATA = f"=== TRADE EVENTS — PAST 7 DAYS ===\n[{TODAY}T07:10] ENTRY NVDA | tier=daytrade | dir=long | price=$240.00 | qty=6\n[{TODAY}T07:40] EXIT NVDA | pnl=$-4.20 | reason=protective_stop"
REPORT = (
    "5. FINAL VERDICT\nFAIL — the day tier entered NVDA without any stop.\n"
    "[DIRECTIVE-1] Raise MIN_SCORE | Evidence: 10/06 NVDA | stop\n"
    '```json\n[{"file": "auto_ai_audit.py", "finding": "day-tier entries have no stop", "recommended_fix": "x", "rc_class": "RC-4"}]\n```'
)


def _checker(rows):
    return mock.patch.object(aa, "call_gai", return_value="```json\n" + json.dumps(rows) + "\n```")


class FactCheck(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(aa.os.environ, {"GEMINI_API_KEY": "k"})
        p.start()
        self.addCleanup(p.stop)

    def test_supported_without_a_real_quote_is_withheld(self):
        rows = [{"id": "verdict", "status": "SUPPORTED", "source": "DATA", "quote": "NVDA entered with no stop at all"},
                {"id": "directive0", "status": "SUPPORTED", "source": "DATA", "quote": ""},
                {"id": "finding0", "status": "SUPPORTED", "source": "auto_ai_audit.py", "quote": "this text is not in the file"}]
        with _checker(rows):
            fc = aa._fact_check(REPORT, DATA)
        self.assertEqual((fc["kept"], fc["checked"]), (0, 3))
        self.assertEqual((fc["verdict"], fc["directives"], fc["findings"]), ("", [], []))
        self.assertEqual(len(fc["withheld"]), 3)

    def test_a_mechanically_present_quote_passes(self):
        rows = [{"id": "verdict", "status": "SUPPORTED", "source": "DATA",
                 "quote": "EXIT NVDA | pnl=$-4.20 | reason=protective_stop"},
                {"id": "directive0", "status": "UNSUPPORTED", "source": "DATA", "quote": ""},
                {"id": "finding0", "status": "SUPPORTED", "source": "auto_ai_audit.py",
                 "quote": "def _fact_check(text: str | None, data: str) -> dict:"}]
        with _checker(rows):
            fc = aa._fact_check(REPORT, DATA)
        self.assertEqual(fc["kept"], 2)
        self.assertTrue(fc["verdict"].startswith("FAIL"))
        self.assertEqual(fc["directives"], [])
        self.assertEqual(fc["findings"][0]["file"], "auto_ai_audit.py")

    def test_checker_failure_withholds_everything(self):
        with mock.patch.object(aa, "call_gai", side_effect=RuntimeError("down")):
            fc = aa._fact_check(REPORT, DATA)
        self.assertEqual(fc["kept"], 0)
        self.assertIn("withheld", fc["note"])
        self.assertEqual(len(fc["withheld"]), 3)

    def test_slack_card_shows_only_proven_claims(self):
        res = {"text": REPORT, "error": None,
               "fact_check": {"checked": 3, "kept": 0, "verdict": "", "directives": [], "findings": [],
                              "withheld": ["a", "b", "c"], "note": ""}}
        txt = aa._meta_report_blocks(res, "Groq")[0]["text"]["text"]
        self.assertIn("verdict withheld", txt)
        self.assertIn("0 of 3 claims proven", txt)
        self.assertNotIn("without any stop", txt)
        self.assertNotIn("Raise MIN_SCORE", txt)

    def test_frame_opens_both_prompts(self):
        for pre in (aa._GRO_ROLE_PREAMBLE, aa._GAI_ROLE_PREAMBLE):
            self.assertTrue(pre.startswith("NORTH STAR"))
            self.assertIn("$25K", pre)
        self.assertNotIn("should be paused or its parameters tightened", aa._GRO_ROLE_PREAMBLE)


    def test_secret_and_outside_files_are_never_read(self):
        self.assertIsNone(aa._safe_repo_file(".env"))
        self.assertIsNone(aa._safe_repo_file(".git/config"))
        self.assertIsNone(aa._safe_repo_file("/etc/hosts"))
        self.assertIsNone(aa._safe_repo_file("../outside.py"))
        self.assertIsNotNone(aa._safe_repo_file("auto_ai_audit.py"))
        rep = '5. FINAL VERDICT\nWARN — x\n```json\n[{"file": ".env", "finding": "secret handling", "recommended_fix": "", "rc_class": "x"}]\n```'
        captured = {}
        with mock.patch.object(aa, "call_gai", side_effect=lambda p, *a, **k: captured.setdefault("p", p) and "[]"):
            aa._fact_check(rep, DATA)
        self.assertNotIn("===== FILE .env", captured["p"])

    def test_a_claim_cannot_be_proven_by_quoting_its_own_prior_copy(self):
        prompt = ("=== PRIOR AUDIT DIRECTIVES ===\n  Finding: day-tier entries are placed without any protective stop order\n"
                  "=== TRADE EVENTS — PAST 7 DAYS ===\n" + DATA)
        rep = '5. FINAL VERDICT\nFAIL — day-tier entries are placed without any protective stop order'
        rows = [{"id": "verdict", "status": "SUPPORTED", "source": "DATA",
                 "quote": "Finding: day-tier entries are placed without any protective stop order"}]
        with _checker(rows):
            fc = aa._fact_check(rep, prompt)
        self.assertEqual(fc["kept"], 0)
        self.assertIn("EXIT NVDA", aa._evidence_only(prompt))
        self.assertNotIn("PRIOR AUDIT", aa._evidence_only(prompt))

    def test_a_finding_is_proven_only_by_its_own_file(self):
        rep = '```json\n[{"file": "run_day_tier.py", "finding": "x happens", "recommended_fix": "", "rc_class": "x"}]\n```'
        rows = [{"id": "finding0", "status": "SUPPORTED", "source": "auto_ai_audit.py",
                 "quote": "def _fact_check(text: str | None, data: str) -> dict:"}]
        with _checker(rows):
            self.assertEqual(aa._fact_check(rep, DATA)["kept"], 0)


    def test_only_the_audit_days_lines_can_prove(self):
        prompt = (f"=== TRADE EVENTS — PAST 7 DAYS ===\n[2026-09-01T10:00] EXIT NVDA | pnl=$-9.99 | reason=protective_stop\n"
                  f"[{TODAY}T10:00] EXIT AAPL | pnl=$1.00 | reason=take_profit\n"
                  "=== PER-SYMBOL SUMMARY (multi-week pattern detection) ===\n  NVDA: entries=9 exits=9 stops=9")
        ev = aa._evidence_only(prompt)
        self.assertIn("EXIT AAPL", ev)
        self.assertNotIn("2026-09-01", ev)          # a prior session never proves anything
        self.assertNotIn("entries=9", ev)           # multi-week aggregates are not that day's data


    def test_report_findings_are_fact_checked_before_queueing(self):
        rows = [{"id": "finding0", "status": "SUPPORTED", "source": "auto_ai_audit.py", "quote": "nope not there"}]
        with _checker(rows):
            kept = aa._prove_findings([{"file": "auto_ai_audit.py", "finding": "x", "recommended_fix": "", "rc_class": "x"}], DATA)
        self.assertEqual(kept, [])

    def test_prior_day_report_file_is_never_used(self):
        with mock.patch.object(aa, "_LOGS_DIR", aa.Path("/nonexistent-dir")):
            self.assertIsNone(aa._today_audit_file("gemini_audit_{date}.txt"))


if __name__ == "__main__":
    unittest.main()
