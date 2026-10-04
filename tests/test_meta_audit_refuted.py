#!/usr/bin/env python3
# ruff: noqa: E501
"""Meta-audit: findings verified FALSE (status "refuted") are never replayed as directives and are
shown as do-not-re-raise context instead (Claude 2026-10-03 — the same false findings re-fired in
Slack every weekday because each run replayed the previous runs' findings as directives)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auto_ai_audit as aa


class RefutedFindings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rows = [
            {"file": "execution/entry_logic.py", "finding": "Intraday positions not liquidated at close",
             "status": "refuted", "refutation": "By design: the 'intraday' tag is the overnight swing tier."},
            {"file": "execution/entry_logic.py", "finding": "Correlation gate uses a fixed 20-day look-back",
             "status": "skipped_risk_path"},
            {"file": "execution/stop_protection.py", "finding": "Profit buffer returns $0.00",
             "status": "refuted", "refutation": "Log artifact: R measured from a stop already at breakeven."},
            {"status": "context_only", "gro_directives_preview": "raw text"},
        ]
        (self.dir / "audit_directives.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.p = mock.patch.object(aa, "_LOGS_DIR", self.dir)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_refuted_are_not_replayed_as_directives(self):
        got = aa._load_prior_directives(10)
        self.assertEqual([d["finding"] for d in got], ["Correlation gate uses a fixed 20-day look-back"])

    def test_refuted_are_listed_with_their_refutation(self):
        got = aa._load_refuted_findings()
        self.assertEqual({d["file"] for d in got}, {"execution/entry_logic.py", "execution/stop_protection.py"})
        self.assertTrue(all(d["refutation"] for d in got))

    def test_absent_file_is_empty(self):
        (self.dir / "audit_directives.jsonl").unlink()
        self.assertEqual(aa._load_refuted_findings(), [])
        self.assertEqual(aa._load_prior_directives(), [])


if __name__ == "__main__":
    unittest.main()
