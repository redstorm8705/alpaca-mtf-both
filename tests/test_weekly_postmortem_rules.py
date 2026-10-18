#!/usr/bin/env python3
# ruff: noqa: E501
"""RULES-1018 rules 2 and 5 in the weekly report (logs/design_records/article_rules_2026-10-18.md).

No network, no broker: the raw-fills fetch is stubbed.
  * the independent recount agrees -> OK; disagrees -> MISMATCH with both numbers; partial fetch or a close opened
    before the fill window -> SOURCE MISSING (never one unverified figure);
  * tier goals: too few trades -> "insufficient trades"; a week far below the trailing range -> data audit first;
  * the report write is atomic (no temp file left behind)."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import weekly_postmortem as wpm

MON, FRI = date(2026, 10, 12), date(2026, 10, 16)


def fill(sym, side, qty, price, when):
    return {"id": f"{sym}-{side}-{when}", "symbol": sym, "side": side, "qty": str(qty), "price": str(price),
            "transaction_time": when}


class IndependentRecountTests(unittest.TestCase):
    def fills(self):
        return [fill("AAA", "buy", 10, 100.0, "2026-10-13T14:00:00Z"),
                fill("AAA", "sell", 10, 101.5, "2026-10-13T15:00:00Z")]      # +15.00

    def test_agreement_is_ok(self):
        with mock.patch.object(wpm, "_fetch_fills_range", return_value=(self.fills(), True)):
            r = wpm._independent_realized(MON, FRI, 15.0)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["independent"], 15.0)

    def test_disagreement_is_mismatch_with_both_numbers(self):
        with mock.patch.object(wpm, "_fetch_fills_range", return_value=(self.fills(), True)):
            r = wpm._independent_realized(MON, FRI, 40.0)
        self.assertEqual(r["status"], "MISMATCH")
        self.assertEqual((r["canonical"], r["independent"]), (40.0, 15.0))

    def test_partial_fetch_or_pre_window_close_is_source_missing(self):
        with mock.patch.object(wpm, "_fetch_fills_range", return_value=(self.fills(), False)):
            self.assertEqual(wpm._independent_realized(MON, FRI, 15.0)["status"], "SOURCE MISSING")
        lone_close = [fill("BBB", "sell", 5, 50.0, "2026-10-14T15:00:00Z")]   # opened before the window
        with mock.patch.object(wpm, "_fetch_fills_range", return_value=(lone_close, True)):
            self.assertEqual(wpm._independent_realized(MON, FRI, 0.0)["status"], "SOURCE MISSING")


def rt(tier, exit_day: date, pnl, k):
    t = datetime(exit_day.year, exit_day.month, exit_day.day, 17, 0, tzinfo=timezone.utc)
    return {"symbol": f"S{k}", "direction": "long", "tier": tier, "entry_order_id": f"o{k}",
            "entry_time": (t - timedelta(hours=1)).isoformat(), "exit_time": t.isoformat(),
            "entry_price": 10.0, "qty": 1, "pnl": pnl, "lifecycle_complete": True}


class TierGoalTests(unittest.TestCase):
    def test_insufficient_and_below_range(self):
        trail = [rt("swing", MON - timedelta(days=d), 5.0 + (d % 3), d) for d in range(3, 25)]
        week = [rt("swing", MON + timedelta(days=1), -20.0, 100), rt("swing", MON + timedelta(days=2), -25.0, 101)]
        goals = {g["tier"]: g for g in wpm._tier_goals(trail + week, MON, FRI, rounds=500)}
        self.assertTrue(goals["swing"]["status"].startswith("below range"))
        self.assertEqual(goals["day"]["status"], "insufficient trades")

    def test_good_week_beats_trailing(self):
        trail = [rt("swing", MON - timedelta(days=d), 2.0 + (d % 2), d) for d in range(3, 25)]
        week = [rt("swing", MON + timedelta(days=1), 9.0, 100), rt("swing", MON + timedelta(days=2), 8.0, 101)]
        goals = {g["tier"]: g for g in wpm._tier_goals(trail + week, MON, FRI, rounds=500)}
        self.assertEqual(goals["swing"]["status"], "beat")


class AtomicWriteTests(unittest.TestCase):
    def test_whole_file_and_no_temp_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wtp.md"
            wpm._atomic_write(p, "# report\n")
            self.assertEqual(p.read_text(), "# report\n")
            self.assertEqual(sorted(os.listdir(d)), ["wtp.md"])


if __name__ == "__main__":
    unittest.main()
