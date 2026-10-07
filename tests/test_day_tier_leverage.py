#!/usr/bin/env python3
# ruff: noqa: E501
"""strategy/day_tier_leverage.py — Track-B 2x ETF pivot + 10/10 confidence (Rafael CEO directive 2026-10-06)."""
import unittest
from unittest import mock

from strategy import day_tier_leverage as lev


def _trig(direction="long", entry=100.0, level=98.0):
    return {"symbol": "TSLA", "trigger": "ENTER", "direction": direction, "mode": "DRIVE",
            "entry_ref": entry, "target": None, "wall_ref": level, "vol_confirmed": True, "reason": "t"}


def _gate(**over):
    al = {"aligned": True, "checks": {"ema2": True, "vwap2": True, "ema5": True, "vwap5": True},
          "info": {"ema15": True, "vwap15": True}}
    g = {"ok": True, "counter_trend": False, "alignment": al}
    g.update(over)
    return g


class Map(unittest.TestCase):
    def test_every_track_b_name_maps(self):
        for s in ("NVDA", "TSLA", "META", "AMD", "AMZN", "AAPL", "MSFT", "GOOGL", "AVGO", "NFLX", "MU", "COIN",
                  "PLTR", "SMCI", "UBER"):
            self.assertTrue(lev.bull_etf_for(s), s)
        self.assertEqual(lev.bull_etf_for("tsla"), "TSLL")
        self.assertIsNone(lev.bull_etf_for("XYZ"))

    def test_kill_flag(self):
        with mock.patch.object(lev.config, "DAYTRADE_LEVERAGED_PIVOT_ENABLED", False, create=True):
            self.assertFalse(lev.pivot_enabled())
        with mock.patch.object(lev.config, "DAYTRADE_LEVERAGED_PIVOT_ENABLED", None, create=True):
            self.assertTrue(lev.pivot_enabled())          # only an explicit False disables


class TenOfTen(unittest.TestCase):
    S = {"is_mover": True}
    M = {"trigger": "ENTER", "vol_confirmed": True}

    def test_all_true(self):
        self.assertTrue(lev.is_ten_of_ten(self.S, self.M, _gate())[0])

    def test_each_leg_required(self):
        self.assertFalse(lev.is_ten_of_ten({"is_mover": False}, self.M, _gate())[0])
        self.assertFalse(lev.is_ten_of_ten(self.S, {"trigger": "ENTER", "vol_confirmed": False}, _gate())[0])
        self.assertFalse(lev.is_ten_of_ten(self.S, self.M, _gate(ok=False))[0])
        self.assertFalse(lev.is_ten_of_ten(self.S, self.M, _gate(counter_trend=True))[0])
        g = _gate()
        g["alignment"]["info"]["vwap15"] = False
        self.assertFalse(lev.is_ten_of_ten(self.S, self.M, g)[0])
        g = _gate()
        g["alignment"]["checks"]["ema5"] = False
        self.assertFalse(lev.is_ten_of_ten(self.S, self.M, g)[0])

    def test_garbage_is_false(self):
        self.assertFalse(lev.is_ten_of_ten(None, None, None)[0])


class Pivot(unittest.TestCase):
    def test_stop_translates_at_2x(self):
        # stock live 100, level 98 (-2%) -> ETF 10.00 stop-ref 9.60 (-4%)
        d, t = lev.leveraged_entry({"symbol": "TSLA", "would_consider": True}, _trig(), "TSLL", 100.0, 10.0)
        self.assertEqual((t["symbol"], t["entry_ref"], t["wall_ref"]), ("TSLL", 10.0, 9.6))
        self.assertEqual((d["symbol"], d["underlying"], t["underlying"]), ("TSLL", "TSLA", "TSLA"))
        self.assertIsNone(t["target"])
        self.assertLess(t["wall_ref"], t["entry_ref"])

    def test_uses_live_stock_price_not_bar(self):
        # bar entry 100 but the stock now trades 99 -> distance to 98 is ~1.01% -> ETF stop ~2.02% below
        _, t = lev.leveraged_entry({}, _trig(entry=100.0), "TSLL", 99.0, 10.0)
        self.assertAlmostEqual(t["wall_ref"], 10.0 * (1 + 2 * (98 / 99 - 1)), places=4)

    def test_no_pivot_cases(self):
        self.assertIsNone(lev.leveraged_entry({}, _trig(direction="short", level=102.0), "TSLL", 100.0, 10.0))
        self.assertIsNone(lev.leveraged_entry({}, _trig(), "TSLL", 97.0, 10.0))      # stock already below level
        self.assertIsNone(lev.leveraged_entry({}, _trig(), "TSLL", None, 10.0))
        self.assertIsNone(lev.leveraged_entry({}, _trig(), "TSLL", 100.0, 0.0))
        self.assertIsNone(lev.leveraged_entry({}, _trig(), "TSLL", 100.0, float("nan")))
        self.assertIsNone(lev.leveraged_entry({}, _trig(level=40.0), "TSLL", 100.0, 10.0))  # -60% -> ETF stop <= 0
        self.assertIsNone(lev.leveraged_entry({}, {**_trig(), "trigger": "WAIT"}, "TSLL", 100.0, 10.0))
        self.assertIsNone(lev.leveraged_entry({}, _trig(), "", 100.0, 10.0))


class MaxSize(unittest.TestCase):
    """execution.day_trade_manager._bounded_entry_qty max_size (10/10): sizes to the account rooms, skips the
    per-trade risk basis and the Track-B budget cap; every room still binds."""

    def _q(self, max_size, track="B", budget=130.0):
        from execution import day_trade_manager as dtm
        return dtm._bounded_entry_qty(
            requested_qty=12, order_price=10.0, stop_price=9.6, equity=2500.0, open_trades={},
            positions_by_symbol={}, buying_power=9000.0, maintenance_margin=0.0, maintenance_rate=0.5,
            open_orders=[], risk_equity=2500.0, symbol="TSLL", track=track, track_budget=budget,
            max_size=max_size)

    def test_normal_track_b_is_budget_capped(self):
        q, _ = self._q(False)
        self.assertLessEqual(q, 13)                       # $130 budget / $10

    def test_max_size_uses_the_rooms(self):
        q, why = self._q(True)
        self.assertIn("MAX-SIZE", why)
        # TSLL is non-deep: thin-name cap $1300 and single-name 65% x $2500 = $1625 -> $1300 / $10 = 130 sh
        self.assertEqual(q, 130)

    def test_max_size_still_fails_closed_on_bad_input(self):
        from execution import day_trade_manager as dtm
        q, _ = dtm._bounded_entry_qty(12, 10.0, 9.6, 2500.0, {}, {}, 0.0, 0.0, 0.5, [], max_size=True)
        self.assertEqual(q, 0)                            # no buying power -> 0


class CoHold(unittest.TestCase):
    def test_primary_then_alternate_then_none(self):
        self.assertEqual(lev.etf_for_order("TSLA", set()), "TSLL")
        self.assertEqual(lev.etf_for_order("TSLA", {"TSLL"}), "TSLT")
        self.assertIsNone(lev.etf_for_order("TSLA", {"TSLL", "TSLT"}))
        self.assertIsNone(lev.etf_for_order("AMZN", {"AMZU"}))          # no second ETF -> stock path
        self.assertIsNone(lev.etf_for_order("TSLA", None))               # unknown book -> no pivot


if __name__ == "__main__":
    unittest.main()
