#!/usr/bin/env python3
# ruff: noqa: E501
"""Day-tier entry fill rate (2026-10-07; board Harris + Taleb, Gro, GAI). Evidence: 6 of 14 day-tier entries since
2026-09-21 filled partly (47 of 61 wired shares); EWY 10/07 sell limit $181.12 vs bid $180.44 -> 1 of 7 filled.
  * the marketable limit is priced at the touch when the IEX quote is usable (long max(ask, trade)+slip, short
    min(bid, trade)-slip), capped at DAYTRADE_ENTRY_TOUCH_CAP_PCT through the trade price;
  * _confirm_fill waits for the WHOLE order (not the first partial);
  * after the remainder cancel the entry order must be terminal before the stop is sized, else a page."""
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import day_trade_manager as dtm


def _o(fq, status="new"):
    return SimpleNamespace(filled_qty=fq, status=status)


class ConfirmFill(unittest.TestCase):
    def _run(self, seq, expected):
        it = iter(seq)
        with mock.patch("execution.broker.get_order", side_effect=lambda _id: next(it)), \
                mock.patch.object(dtm.time, "sleep"):
            return dtm._confirm_fill("O1", expected)

    def test_waits_past_a_partial_until_full(self):
        seq = [_o(1, "partially_filled"), _o(3, "partially_filled"), _o(7, "filled")]
        with mock.patch("execution.broker.get_order", side_effect=seq) as g, mock.patch.object(dtm.time, "sleep"):
            self.assertTrue(dtm._confirm_fill("O1", 7))
        self.assertEqual(g.call_count, 3)                         # did not stop at the first partial

    def test_partial_at_budget_end_still_true(self):
        self.assertTrue(self._run([_o(1, "partially_filled")] * 8, 7))

    def test_nothing_filled(self):
        self.assertFalse(self._run([_o(0)] * 8, 7))
        self.assertFalse(self._run([_o(0, "canceled")], 7))

    def test_terminal_partial_returns_true(self):
        self.assertTrue(self._run([_o(2, "canceled")], 7))

    def test_legacy_no_expected_qty_returns_at_first_fill(self):
        self.assertTrue(self._run([_o(1, "partially_filled")], 0))


class AwaitTerminal(unittest.TestCase):
    def test_terminal_and_not(self):
        with mock.patch("execution.broker.get_order", side_effect=[_o(1, "pending_cancel"), _o(1, "canceled")]), \
                mock.patch.object(dtm.time, "sleep"):
            self.assertTrue(dtm._await_entry_terminal("O1"))
        with mock.patch("execution.broker.get_order", return_value=_o(1, "pending_cancel")), \
                mock.patch.object(dtm.time, "sleep"):
            self.assertFalse(dtm._await_entry_terminal("O1"))
        with mock.patch("execution.broker.get_order", side_effect=RuntimeError("down")), \
                mock.patch.object(dtm.time, "sleep"):
            self.assertFalse(dtm._await_entry_terminal("O1"))


class TouchLimit(unittest.TestCase):
    """place_entry's submitted limit price, captured at broker.submit_limit_order."""

    def _limit(self, direction, trade, quote, entry_ref=None):
        from execution import broker
        from data.live_price import LivePrice
        entry_ref = entry_ref or trade
        sub = {}
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)

        def _submit(sym, qty, side, px, **k):
            sub.update(px=px, side=side)
            return None                                           # stop right after pricing (no order)
        stop = entry_ref * (1.03 if direction == "short" else 0.97)
        trigger = {"trigger": "ENTER", "direction": direction, "mode": "RIDE", "entry_ref": entry_ref,
                   "target": None, "wall_ref": stop}
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_load_state", return_value={}), \
                mock.patch.object(dtm, "_save_state", return_value=True), \
                mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _l, st: (st, "kept")), \
                mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None), \
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")), \
                mock.patch("data.live_price.live_price", return_value=LivePrice(trade, "iex_trade", 1.0)), \
                mock.patch("data.alpaca_data.get_latest_quote", return_value=quote), \
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch("strategy.day_tier_logger.log_decision", return_value=True), \
                mock.patch.object(broker, "get_account", return_value=acct), \
                mock.patch.object(broker, "get_open_positions", return_value=[]), \
                mock.patch.object(broker, "get_open_orders", return_value=[]), \
                mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30), \
                mock.patch("execution.tier_capital_allocator.live_admit",
                           return_value=SimpleNamespace(approved=True, lease=None, reason="")), \
                mock.patch("execution.tier_capital_allocator.live_order_id", return_value="DT-x"), \
                mock.patch("execution.tier_capital_allocator.live_release", return_value=None), \
                mock.patch.object(broker, "submit_limit_order", side_effect=_submit):
            dtm.place_entry("EWY", {"would_consider": True}, trigger, {"size_ok": True, "shares": 5},
                            bar_id="20261007-0930", equity=2500.0)
        return sub.get("px")

    def test_ewy_short_sells_at_the_bid(self):
        # 10/07: trade 181.48, bid 180.44 / ask 180.60 -> min(bid, trade) x 0.998 = 180.08 (old: 181.12)
        self.assertEqual(self._limit("short", 181.48, {"bid": 180.44, "ask": 180.60}), round(180.44 * 0.998, 2))

    def test_long_buys_at_the_ask(self):
        self.assertEqual(self._limit("long", 100.0, {"bid": 100.30, "ask": 100.40}), round(100.40 * 1.002, 2))

    def test_trade_inside_the_quote_keeps_the_trade_limit(self):
        # long: trade 100.50 above the ask 100.40 -> max(ask, trade) = trade
        self.assertEqual(self._limit("long", 100.50, {"bid": 100.30, "ask": 100.40}), round(100.50 * 1.002, 2))

    def test_capped_at_one_percent_through_the_trade(self):
        # short: bid 98.0 is 2% under the trade 100 -> capped at 100 x 0.99 = 99.00 (spread 0.5% passes the 2% test)
        self.assertEqual(self._limit("short", 100.0, {"bid": 98.0, "ask": 98.5}), 99.0)

    def test_unusable_quote_keeps_the_trade_limit(self):
        for q in (None, {"bid": 0, "ask": 100.4}, {"bid": 100.5, "ask": 100.4}, {"bid": 95.0, "ask": 105.0}):
            self.assertEqual(self._limit("long", 100.0, q), round(100.0 * 1.002, 2), q)


if __name__ == "__main__":
    unittest.main()
