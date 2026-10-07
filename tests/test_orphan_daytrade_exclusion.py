#!/usr/bin/env python3
# ruff: noqa: E501
"""orphan_manager._get_daytrade_syms (2026-10-07): the main bot's restart adopted the day-tier EWY SHORT (ledger
daytrade qty -1.0 failed the old `> 0`) and the AAPL long (entered 6 min before the 20-min ledger refresh). The
exclusion set now takes any NON-ZERO ledger daytrade qty AND the day tier's real-time durable open set."""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from execution import orphan_manager as om


def _now_ts(days_back=0):
    return (datetime.now(om.ET) - timedelta(days=days_back)).isoformat()


def _ledger(**tiers):
    return {"positions": {s: {"tiers": {"daytrade": {"qty": q}}} for s, q in tiers.items()}}


class DaytradeSyms(unittest.TestCase):
    def _run(self, ledger=None, ledger_err=False, opens=None, log_err=False):
        lp = mock.patch.object(om, "_og_load_ledger", side_effect=RuntimeError("x")) if ledger_err \
            else mock.patch.object(om, "_og_load_ledger", return_value=ledger or {"positions": {}})
        dp = mock.patch("strategy.day_tier_logger.open_trades_from_log", side_effect=RuntimeError("y")) if log_err \
            else mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value=opens or {})
        with lp, dp:
            return om._get_daytrade_syms()

    def test_short_in_ledger_is_excluded(self):
        self.assertEqual(self._run(_ledger(EWY=-1.0, AAPL=4.0, TSLA=0.0)), {"EWY", "AAPL"})

    def test_durable_log_covers_the_ledger_lag(self):
        self.assertEqual(self._run(_ledger(), opens={"DT-1": {"symbol": "AAPL", "entry_ts": _now_ts()}}), {"AAPL"})

    def test_union(self):
        self.assertEqual(self._run(_ledger(EWY=-1.0), opens={"DT-1": {"symbol": "AAPL", "entry_ts": _now_ts()}}), {"EWY", "AAPL"})

    def test_each_source_fails_independently(self):
        self.assertEqual(self._run(ledger_err=True, opens={"DT-1": {"symbol": "AAPL", "entry_ts": _now_ts()}}), {"AAPL"})
        self.assertEqual(self._run(_ledger(EWY=-1.0), log_err=True), {"EWY"})
        self.assertEqual(self._run(ledger_err=True, log_err=True), set())


class StaleLogRecord(unittest.TestCase):
    """Cold-2nd 2026-10-07: a prior-day day-tier open record (no exit_fill — e.g. a lot another tier closed) must NOT
    hide a real orphan; only today's entries count (the day tier is flat by every close)."""

    def test_prior_day_record_is_not_excluded(self):
        with mock.patch.object(om, "_og_load_ledger", return_value={"positions": {}}), \
                mock.patch("strategy.day_tier_logger.open_trades_from_log",
                           return_value={"DT-1": {"symbol": "AAPL", "entry_ts": _now_ts(days_back=1)},
                                         "DT-2": {"symbol": "EWY", "entry_ts": "garbage"},
                                         "DT-3": {"symbol": "TSLA", "entry_ts": _now_ts()}}):
            self.assertEqual(om._get_daytrade_syms(), {"TSLA"})


class ReconcileSkipsDayTier(unittest.TestCase):
    def test_day_tier_short_is_not_adopted(self):
        from types import SimpleNamespace
        pos = SimpleNamespace(symbol="EWY", qty="-1", avg_entry_price="181.46")
        tracker = SimpleNamespace(open_trades={}, closed_trades=[], traded_today=set(), _save_log=lambda: None)
        with mock.patch.object(om, "get_open_positions", return_value=[pos]), \
                mock.patch.object(om, "_get_qhm_syms", return_value=frozenset()), \
                mock.patch.object(om, "_get_forever6_syms", return_value=set()), \
                mock.patch.object(om, "_get_breakout_syms", return_value=set()), \
                mock.patch.object(om, "_og_load_ledger", return_value=_ledger(EWY=-1.0)), \
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}), \
                mock.patch.object(om, "submit_gtc_stop_order", side_effect=AssertionError("no adoption stop")), \
                mock.patch.object(om, "send_slack"):
            om.reconcile_positions(tracker)
        self.assertNotIn("EWY", tracker.open_trades)


if __name__ == "__main__":
    unittest.main()
