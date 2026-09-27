from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import weekly_postmortem as wtp


class WeeklyPostmortemTruth(unittest.TestCase):
    def _trade(self, *, size=4):
        return {
            "symbol": "AAPL", "direction": "long", "entry_price": 100.0,
            "exit_price": 101.0, "entry_time": "2026-09-21T09:30:00-07:00",
            "exit_time": "2026-09-21T10:30:00-07:00", "pnl": float(size),
            "exit_reason": "target", "score": "?", "mri_level": "?", "size": size,
            "exit_date": date(2026, 9, 21), "_unmatched": False,
        }

    def test_friday_delta_is_per_share_diagnostic_not_false_dollars(self):
        trades = [self._trade(size=4)]
        with mock.patch.object(wtp, "_load_postmortem", return_value={}), \
             mock.patch.object(wtp, "_had_earnings_during_hold", return_value=False):
            table, slack, stats = wtp._build_wtp_table(
                trades, {"AAPL": 110.0}, {"AAPL": [90, 95, 100, 110]}, {}
            )
        self.assertIn("Δ/share Exit→Fri", table)
        self.assertIn("+$9.00", table)  # per share: 110 Friday - 101 exit
        self.assertIn("Fri Δ/sh +$9.00", slack)
        self.assertNotIn("agg_missed", stats)
        self.assertEqual(stats["positive_friday_diagnostics"], 1)
        # The old report falsely published 4 × $9 = $36 as dollars left on the table.
        self.assertNotIn("$36", table + slack + str(stats))

    def test_prompt_forbids_missing_as_zero_and_false_opportunity_cost(self):
        trade = self._trade()
        trade.update(_stage="Stage 2", _earn=False, _tqi="?", _hold="1h",
                     _wkly=110.0, _delta_s="+$9.00")
        stats = {"total_pnl": 4.0, "count": 1, "winners": 1, "losers": 0,
                 "earnings_cnt": 0, "positive_friday_diagnostics": 1}
        prompt = wtp._build_gemini_prompt(
            "2026-09-21 → 2026-09-25", "table", [trade], stats
        )
        self.assertIn("UNKNOWN, never zero", prompt)
        self.assertIn("must not be\n                     summed", prompt)
        self.assertIn("Do not invent dollar opportunity cost", prompt)
        self.assertNotIn("Aggregate missed directional move", prompt)

    def test_slack_summary_never_claims_dollars_left_on_table(self):
        stats = {"total_pnl": 4.0, "winners": 1, "losers": 0, "unmatched": 0,
                 "earnings_cnt": 0, "positive_friday_diagnostics": 1}
        sent = []
        with mock.patch.object(wtp, "SLACK_WEBHOOK", "test"), \
             mock.patch.object(wtp, "_slack_raw", side_effect=sent.append):
            wtp._post_slack(
                "AAPL    +$4.00", stats, Path("wtp_2026-09-25.md"),
                "2026-09-21 → 2026-09-25",
            )
        self.assertEqual(len(sent), 1)
        self.assertIn("Fri-positive diagnostics `1`", sent[0])
        self.assertNotIn("left on table", sent[0])


if __name__ == "__main__":
    unittest.main()
