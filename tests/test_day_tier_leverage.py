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


class ExactStop(unittest.TestCase):
    """Daily-reset stop translation (board Harris 2026-10-07)."""

    def test_flat_day_equals_first_order(self):
        # stock at its prior close -> exact == first order
        self.assertAlmostEqual(lev.etf_stop_level(10.0, 100.0, 98.0, 2.0, False, prior_close=100.0), 9.6, places=9)
        self.assertAlmostEqual(lev.etf_stop_level(10.0, 100.0, 102.0, 2.0, True, prior_close=100.0), 9.6, places=9)

    def test_inverse_after_a_down_day(self):
        # NVDA -5% on the day (C 100, U 95), stop +2% at 96.9 -> NVD falls 3.45%, not 4%
        e = lev.etf_stop_level(10.0, 95.0, 96.9, 2.0, True, prior_close=100.0)
        self.assertAlmostEqual(e, 10.0 * (1 - 0.038 / 1.10), places=9)
        self.assertAlmostEqual(e, 10.0 * (1 - 2 * (96.9 / 100 - 1)) / (1 - 2 * (95 / 100 - 1)), places=9)

    def test_bull_after_an_up_day(self):
        e = lev.etf_stop_level(10.0, 105.0, 102.9, 2.0, False, prior_close=100.0)
        self.assertAlmostEqual(e, 10.0 * (1 + 2 * (102.9 / 100 - 1)) / (1 + 2 * (105 / 100 - 1)), places=9)

    def test_missing_prior_close_falls_back(self):
        self.assertAlmostEqual(lev.etf_stop_level(10.0, 99.0, 98.0, 2.0, False), 10.0 * (1 + 2 * (98 / 99 - 1)), places=9)
        self.assertIsNone(lev.etf_stop_level(None, 99.0, 98.0, 2.0, False))
        self.assertIsNone(lev.etf_stop_level(10.0, 99.0, 98.0, 0.0, False))

    def test_bull_pivot_uses_prior_close(self):
        _, t = lev.leveraged_entry({}, _trig(level=98.0), "TSLL", 100.0, 10.0, prior_close=95.0)
        self.assertAlmostEqual(t["wall_ref"], round(lev.etf_stop_level(10.0, 100.0, 98.0, 2.0, False, 95.0), 4))


class Inverse(unittest.TestCase):
    def test_map_and_held(self):
        self.assertEqual(lev.inverse_etf_for_order("NVDA", set()), ("NVD", 2.0))
        self.assertEqual(lev.inverse_etf_for_order("NVDA", {"NVD"}), ("NVDQ", 2.0))
        self.assertIsNone(lev.inverse_etf_for_order("NVDA", {"NVD", "NVDQ"}))
        self.assertEqual(lev.inverse_etf_for_order("msft", set()), ("MSFD", 1.0))
        for thin in ("GOOGL", "NFLX", "META", "AMD", "COIN"):
            self.assertIsNone(lev.inverse_etf_for_order(thin, set()), thin)
        self.assertIsNone(lev.inverse_etf_for_order("NVDA", None))

    def test_kill_flag(self):
        with mock.patch.object(lev.config, "DAYTRADE_INVERSE_PIVOT", False, create=True):
            self.assertFalse(lev.inverse_pivot_enabled())
        with mock.patch.object(lev.config, "DAYTRADE_INVERSE_PIVOT", None, create=True):
            self.assertTrue(lev.inverse_pivot_enabled())

    def test_short_becomes_a_buy_with_stop_below(self):
        tr = {**_trig(direction="short", level=102.0), "symbol": "NVDA"}
        d, t = lev.inverse_entry({"symbol": "NVDA"}, tr, "NVD", 2.0, 100.0, 3.30)
        self.assertEqual((t["symbol"], t["direction"], t["signal_direction"], t["instrument"]),
                         ("NVD", "long", "short", "inverse_etf"))
        self.assertAlmostEqual(t["wall_ref"], round(3.30 * (1 - 2 * 0.02), 4))
        self.assertLess(t["wall_ref"], t["entry_ref"])
        self.assertIsNone(t["target"])
        self.assertEqual((d["symbol"], d["underlying"], d["signal_direction"], d["leverage"]), ("NVD", "NVDA", "short", 2.0))

    def test_no_route_cases(self):
        self.assertIsNone(lev.inverse_entry({}, _trig(direction="long"), "NVD", 2.0, 100.0, 3.3))
        self.assertIsNone(lev.inverse_entry({}, _trig(direction="short", level=99.0), "NVD", 2.0, 100.0, 3.3))   # level below price
        self.assertIsNone(lev.inverse_entry({}, _trig(direction="short", level=160.0), "NVD", 2.0, 100.0, 3.3))  # >=100% stop
        self.assertIsNone(lev.inverse_entry({}, _trig(direction="short", level=102.0), "NVD", 2.0, None, 3.3))
        self.assertIsNone(lev.inverse_entry({}, _trig(direction="short", level=102.0), "", 2.0, 100.0, 3.3))

    def test_exposure_sign(self):
        self.assertEqual(lev.exposure_sign("NVDA", "long"), ("NVDA", 1))
        self.assertEqual(lev.exposure_sign("NVDA", "short"), ("NVDA", -1))
        self.assertEqual(lev.exposure_sign("NVDL", "long"), ("NVDA", 1))
        self.assertEqual(lev.exposure_sign("NVDU", "long"), ("NVDA", 1))
        self.assertEqual(lev.exposure_sign("NVD", "long"), ("NVDA", -1))
        self.assertEqual(lev.exposure_sign("NVDQ", "long"), ("NVDA", -1))
        self.assertEqual(lev.exposure_sign("QQQ", "long"), ("QQQ", 1))


class LiquidityTracking(unittest.TestCase):
    Q = {"bid": 3.29, "ask": 3.30}

    def test_liquid_passes(self):
        self.assertTrue(lev.etf_liquidity_ok(self.Q, 900_000, 100, 3.30, 3.17)[0])

    def test_each_fail(self):
        self.assertFalse(lev.etf_liquidity_ok(None, 900_000, 100, 3.30, 3.17)[0])
        self.assertFalse(lev.etf_liquidity_ok({"bid": 3.31, "ask": 3.30}, 900_000, 100, 3.30, 3.17)[0])   # crossed
        self.assertFalse(lev.etf_liquidity_ok({"bid": 3.25, "ask": 3.30}, 900_000, 100, 3.30, 3.17)[0])   # 1.5% wide
        self.assertFalse(lev.etf_liquidity_ok(self.Q, 900_000, 100, 3.30, 3.27)[0])                       # 3-tick stop
        self.assertFalse(lev.etf_liquidity_ok(self.Q, 40_000, 100, 3.30, 3.17)[0])                        # < 50K (full day)
        self.assertFalse(lev.etf_liquidity_ok(self.Q, 60_000, 1000, 3.30, 3.17)[0])                       # < 100x qty
        self.assertFalse(lev.etf_liquidity_ok(self.Q, None, 100, 3.30, 3.17)[0])
        # spread 1 tick but stop only 4 ticks... 25%-of-stop rule: 0.01 > 0.25 x 0.03? stop 3 ticks already fails
        self.assertFalse(lev.etf_liquidity_ok({"bid": 9.60, "ask": 9.64}, 900_000, 10, 9.64, 9.50)[0])    # 4c > 25% of 14c

    def test_volume_floor_is_prorated_by_time_of_day(self):
        # 30 min in: expected-by-now = 50K x 30/390 = 3,846 -> 6K passes; full-day floor would have failed it
        self.assertTrue(lev.etf_liquidity_ok(self.Q, 6_000, 40, 3.30, 3.17, session_frac=30 / 390)[0])
        self.assertFalse(lev.etf_liquidity_ok(self.Q, 3_000, 20, 3.30, 3.17, session_frac=30 / 390)[0])
        self.assertFalse(lev.etf_liquidity_ok(self.Q, 6_000, 100, 3.30, 3.17, session_frac=30 / 390)[0])   # < 100x qty
        self.assertTrue(lev.etf_liquidity_ok(self.Q, 4_000, 10, 3.30, 3.17, session_frac=-0.5)[0])        # pre-open clamps to 0

    def test_tracking(self):
        # stock -5% -> 2x inverse should be +10%
        self.assertTrue(lev.etf_tracking_ok(11.0, 10.0, 95.0, 100.0, 2.0, inverse=True)[0])
        self.assertFalse(lev.etf_tracking_ok(10.6, 10.0, 95.0, 100.0, 2.0, inverse=True)[0])
        self.assertTrue(lev.etf_tracking_ok(10.0, 10.0, 100.0, 100.0, 1.0, inverse=True)[0])
        self.assertFalse(lev.etf_tracking_ok(10.0, None, 100.0, 100.0, 1.0, inverse=True)[0])


if __name__ == "__main__":
    unittest.main()
