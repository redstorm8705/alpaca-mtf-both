#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""scripts/cohold_claim_eod.py — end-of-day summary of unreadable Day share counts (design record
logs/design_records/cohold_unreadable_claim_retry_2026-10-11.md). Run: PYTHONPATH=. python3 -m pytest -q tests/test_cohold_claim_eod.py
"""
import json
import unittest

from scripts import cohold_claim_eod as eod


def _ev(ts, sym="EWY", outcome="bounded_own_close", attempts=3, event="cohold_claim_unreadable"):
    return json.dumps({"ts": ts, "event": event, "symbol": sym, "tier": "intraday", "attempts": attempts,
                       "outcome": outcome})


class Summarize(unittest.TestCase):
    def test_groups_today_only(self):
        lines = [_ev("2026-10-12T07:00:00-07:00"), _ev("2026-10-12T09:30:00-07:00", outcome="no_own_qty_full_close"),
                 _ev("2026-10-09T10:00:00-07:00"), _ev("2026-10-12T08:00:00-07:00", event="exit"), "not json"]
        s = eod.summarize(lines, "2026-10-12")
        self.assertEqual(list(s), ["EWY"])
        self.assertEqual((s["EWY"]["events"], s["EWY"]["attempts"]), (2, 6))
        self.assertEqual(dict(s["EWY"]["outcomes"]), {"bounded_own_close": 1, "no_own_qty_full_close": 1})

    def test_et_date_boundary(self):
        # 9:30 PM PT on 10/11 is 12:30 AM ET on 10/12
        self.assertIn("EWY", eod.summarize([_ev("2026-10-11T21:30:00-07:00")], "2026-10-12"))

    def test_empty(self):
        self.assertEqual(eod.summarize([], "2026-10-12"), {})


if __name__ == "__main__":
    unittest.main()
