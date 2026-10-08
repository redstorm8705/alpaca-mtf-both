#!/usr/bin/env python3
# ruff: noqa: E501
"""post_live_due.py / record_post_live.py (Rafael mandate 2026-10-08: every live diff audited + replayed after its
first full trading day). Run: python3 .claude/preship/test_post_live_due.py  (exit 0 = pass)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import post_live_due as pld  # noqa: E402
import record_post_live as rpl  # noqa: E402

ET = ZoneInfo("America/New_York")


class SessionEnd(unittest.TestCase):
    def test_after_close_thursday_due_friday_close(self):
        self.assertEqual(pld.first_full_session_end(datetime(2026, 10, 8, 17, 20, tzinfo=ET)),
                         datetime(2026, 10, 9, 16, 0, tzinfo=ET))

    def test_premarket_due_same_day(self):
        self.assertEqual(pld.first_full_session_end(datetime(2026, 10, 9, 8, 0, tzinfo=ET)),
                         datetime(2026, 10, 9, 16, 0, tzinfo=ET))

    def test_midsession_is_not_a_full_day(self):
        self.assertEqual(pld.first_full_session_end(datetime(2026, 10, 9, 11, 0, tzinfo=ET)),
                         datetime(2026, 10, 12, 16, 0, tzinfo=ET))   # Friday mid-session -> Monday

    def test_weekend_rolls_to_monday(self):
        self.assertEqual(pld.first_full_session_end(datetime(2026, 10, 10, 12, 0, tzinfo=ET)),
                         datetime(2026, 10, 12, 16, 0, tzinfo=ET))


class BotCode(unittest.TestCase):
    def test_classification(self):
        for p in ("run_day_tier.py", "execution/broker.py", "strategy/x.py", "config.py", "scripts/a.sh"):
            self.assertTrue(pld.is_bot_code(p), p)
        for p in ("tests/test_x.py", ".claude/preship/x.py", "logs/a.md", "handoff.md", ".github/w.yml", ""):
            self.assertFalse(pld.is_bot_code(p), p)


class Status(unittest.TestCase):
    MERGED = datetime(2026, 10, 8, 17, 20, tzinfo=ET)

    def _status(self, now, done=()):
        with mock.patch.object(pld, "merged_prs", return_value=[(531, self.MERGED, ["run_day_tier.py"])]), \
                mock.patch.object(pld, "audited_prs", return_value=set(done)):
            return pld.status(now)

    def test_pending_then_overdue_then_done(self):
        self.assertEqual(self._status(datetime(2026, 10, 9, 15, 59, tzinfo=ET))[0][1], "pending")
        self.assertEqual(self._status(datetime(2026, 10, 9, 16, 0, tzinfo=ET))[0][1], "OVERDUE")
        self.assertEqual(self._status(datetime(2026, 10, 9, 17, 0, tzinfo=ET), done={531})[0][1], "done")

    def test_hook_prints_only_when_overdue(self):
        rows_over = [(531, "OVERDUE", datetime(2026, 10, 9, 16, 0, tzinfo=ET), ["run_day_tier.py"])]
        with mock.patch.object(pld, "status", return_value=rows_over), mock.patch.object(sys, "argv", ["x", "--hook"]), \
                mock.patch("builtins.print") as pr:
            self.assertEqual(pld.main(), 0)
        self.assertIn("post-live audit OVERDUE", pr.call_args[0][0])
        with mock.patch.object(pld, "status", return_value=[]), mock.patch.object(sys, "argv", ["x", "--hook"]), \
                mock.patch("builtins.print") as pr:
            self.assertEqual(pld.main(), 0)
        pr.assert_not_called()


class Records(unittest.TestCase):
    def test_record_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "post_live_audits.jsonl")
            with mock.patch.object(rpl, "RECORDS", path):
                self.assertEqual(rpl.main(["531", "--session", "2026-10-09", "--verdict", "PASS", "--notes",
                                           "routing executed (log lines cited), decisions replayed vs fills, no bugs"]), 0)
                self.assertEqual(rpl.main(["531", "--session", "bad", "--verdict", "PASS", "--notes", "x" * 50]), 2)
                self.assertEqual(rpl.main(["531", "--session", "2026-10-09", "--verdict", "PASS", "--notes", "short"]), 2)
            with open(path) as fh:
                lines = [json.loads(x) for x in fh]
            self.assertEqual([(r["pr"], r["verdict"]) for r in lines], [(531, "PASS")])
            with mock.patch.object(pld, "REPO", d), mock.patch.object(pld, "RECORDS", "post_live_audits.jsonl"), \
                    mock.patch.object(pld, "_git", return_value=None):
                self.assertEqual(pld.audited_prs(), {531})


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
