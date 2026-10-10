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


class ReconcileCoholdR1(unittest.TestCase):
    """Co-hold R1 (2026-10-10): the startup size reconcile compares the Swing book with net minus the Day tier's
    same-day claim, so a co-held Day lot is never absorbed into (or banked out of) the Swing tracker."""

    def _run(self, net, trk_qty, claim, direction="long"):
        from types import SimpleNamespace
        pos = SimpleNamespace(symbol="NVDA", qty=str(net), avg_entry_price="180.00")
        trade = {"symbol": "NVDA", "direction": direction, "qty": trk_qty, "qty_remaining": trk_qty,
                 "entry_price": 180.0, "partial_pnl": 0.0}
        tracker = SimpleNamespace(open_trades={"NVDA": trade}, closed_trades=[], traded_today=set(),
                                  _save_log=lambda: None)
        slack = mock.Mock()
        fill = mock.Mock(return_value=170.0)
        with mock.patch.object(om, "get_open_positions", return_value=[pos]), \
                mock.patch.object(om, "_get_qhm_syms", return_value=frozenset()), \
                mock.patch.object(om, "_get_forever6_syms", return_value=set()), \
                mock.patch.object(om, "_get_breakout_syms", return_value=set()), \
                mock.patch.object(om, "_get_daytrade_syms", return_value=set()), \
                mock.patch.object(om, "_reconcile_day_claim", return_value=claim), \
                mock.patch.object(om, "fetch_actual_fill_price", fill), \
                mock.patch.object(om, "submit_gtc_stop_order", side_effect=AssertionError("no stop")), \
                mock.patch.object(om, "send_slack", slack):
            om.reconcile_positions(tracker)
        return trade, slack, fill

    def test_day_lot_is_not_absorbed(self):
        trade, slack, fill = self._run(net=10, trk_qty=7, claim=3.0)
        self.assertEqual(trade["qty_remaining"], 7)
        fill.assert_not_called()
        slack.assert_not_called()

    def test_real_swing_shrink_is_banked_net_of_day(self):
        trade, slack, fill = self._run(net=8, trk_qty=7, claim=3.0)      # Swing lost 2 externally
        self.assertEqual(trade["qty_remaining"], 5)
        self.assertEqual(trade["partial_pnl"], -20.0)                    # (170-180) x 2

    def test_short_cohold(self):
        trade, slack, fill = self._run(net=-6, trk_qty=4, claim=-2.0, direction="short")
        self.assertEqual(trade["qty_remaining"], 4)
        self.assertEqual(trade["direction"], "short")

    def test_no_day_lot_keeps_old_behaviour(self):
        trade, slack, fill = self._run(net=10, trk_qty=7, claim=0.0)
        self.assertEqual(trade["qty_remaining"], 10)

    def test_unreadable_claim_never_grows_swing_and_pages(self):
        trade, slack, fill = self._run(net=10, trk_qty=7, claim=None)
        self.assertEqual(trade["qty_remaining"], 7)
        slack.assert_called_once()

    def test_unreadable_claim_still_banks_a_shrink(self):
        trade, slack, fill = self._run(net=5, trk_qty=7, claim=None)
        self.assertEqual(trade["qty_remaining"], 5)
        self.assertEqual(trade["partial_pnl"], -20.0)
        slack.assert_called_once()

    def test_all_day_shares_leaves_swing_unchanged_and_pages(self):
        trade, slack, fill = self._run(net=3, trk_qty=7, claim=3.0)
        self.assertEqual(trade["qty_remaining"], 7)
        fill.assert_not_called()
        slack.assert_called_once()

    def test_opposite_day_lot_never_flips_swing(self):
        trade, slack, fill = self._run(net=-3, trk_qty=7, claim=-10.0)   # Swing long 7 + Day short 10
        self.assertEqual(trade["direction"], "long")
        self.assertEqual(trade["qty_remaining"], 7)
        slack.assert_called_once()

    def test_day_lot_masking_a_swing_loss_is_banked(self):
        # cold-2nd round 2: tracker 7, Day +3, net 7 -> Swing really holds 4; the 3 lost shares are banked.
        trade, slack, fill = self._run(net=7, trk_qty=7, claim=3.0)
        self.assertEqual(trade["qty_remaining"], 4)
        self.assertEqual(trade["partial_pnl"], -30.0)

    def test_matching_book_with_unreadable_claim_is_quiet(self):
        trade, slack, fill = self._run(net=7, trk_qty=7, claim=None)
        self.assertEqual(trade["qty_remaining"], 7)
        slack.assert_not_called()


class ReconcileDayClaimSource(unittest.TestCase):
    def test_broker_first_then_day_log(self):
        from types import SimpleNamespace
        with mock.patch("execution.broker._day_tier_claim", return_value=2.0):
            self.assertEqual(om._reconcile_day_claim("NVDA"), 2.0)
        snap = SimpleNamespace(errors=(), claimed_qty=lambda s, t: 4.0)
        with mock.patch("execution.broker._day_tier_claim", return_value=None), \
                mock.patch("execution.ownership_snapshot.build_ownership_snapshot", return_value=snap):
            self.assertEqual(om._reconcile_day_claim("NVDA"), 4.0)
        bad = SimpleNamespace(errors=("daytrade_log:ValueError",), claimed_qty=lambda s, t: 0.0)
        with mock.patch("execution.broker._day_tier_claim", return_value=None), \
                mock.patch("execution.ownership_snapshot.build_ownership_snapshot", return_value=bad):
            self.assertIsNone(om._reconcile_day_claim("NVDA"))


if __name__ == "__main__":
    unittest.main()
