# ruff: noqa: E501
"""SWING-OWNER-1025 (owner design 2026-10-25): top-50 universe, pending breakout setups that must be recovered,
stop-based fractional-Kelly sizing, Invariant #11 limb 1 retired for Swing, gap check. Every broker call is a fake."""
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

from execution import swing_breakout_manager as sb
from tests.test_swing_breakout_manager import FakeBroker, _bars, _breakout_df, _fresh, _mods

PRIOR = {"n": 1053, "mean_r": 0.18344412425521434, "sd_r": 1.5173938075184141, "loss_mult_stopped": 1.0641876266031192}
CFG = dict(SWING_BREAKOUT_MAX_RISK_PCT=0.02, SWING_BREAKOUT_MIN1_RISK_PCT=0.03, SWING_BREAKOUT_MIN1_MAX_PCT=0.25,
           SWING_BREAKOUT_KELLY_FRACTION=0.25, KELLY_MIN_SAMPLE_SIZE=30, SWING_BREAKOUT_STOP_ATR=2.5)


class Sizing(unittest.TestCase):
    def setUp(self):
        self.c = mock.patch.multiple(sb.config, create=True, **CFG)
        self.c.start()

    def tearDown(self):
        self.c.stop()

    def test_prior_kelly_matches_the_replay(self):
        k = sb.kelly_risk([], PRIOR)
        self.assertEqual(k["source"], "replay_prior")
        se = PRIOR["sd_r"] / math.sqrt(PRIOR["n"])
        self.assertAlmostEqual(k["r"], 0.25 * (PRIOR["mean_r"] - se) / PRIOR["sd_r"] ** 2, places=9)
        self.assertAlmostEqual(k["r"], 0.01484, places=4)        # 1.48% of equity per lot
        self.assertAlmostEqual(k["loss_mult"], 1.0642, places=4)

    def test_live_kelly_takes_over_at_30_closed_lots_and_negative_edge_is_zero(self):
        lots = [{"realized_pnl": -10.0, "risk_usd": 10.0, "exit_reason": "protective_stop", "coid": f"c{i}"} for i in range(20)]
        lots += [{"realized_pnl": 5.0, "risk_usd": 10.0, "exit_reason": "trend_break", "coid": f"w{i}"} for i in range(10)]
        k = sb.kelly_risk(lots, PRIOR)
        self.assertEqual((k["source"], k["n"]), ("fills", 30))
        self.assertEqual(k["r"], 0.0)                              # mean R < 0 → r 0 (the one-share floor still trades)
        self.assertAlmostEqual(k["loss_mult"], 1.0)                # stopped lots lost exactly 1R
        self.assertEqual(sb.kelly_risk(lots[:29], PRIOR)["source"], "replay_prior")

    def test_kelly_never_above_the_two_percent_ceiling_and_none_without_prior(self):
        lots = [{"realized_pnl": 30.0 + i, "risk_usd": 10.0, "coid": f"x{i}"} for i in range(40)]
        self.assertEqual(sb.kelly_risk(lots, PRIOR)["r"], 0.02)
        self.assertEqual(sb.kelly_risk([], None)["r"], 0.0)

    def test_loss_multiple_floored_at_one(self):
        lots = [{"realized_pnl": -5.0, "risk_usd": 10.0, "exit_reason": "protective_stop", "coid": f"s{i}"} for i in range(30)]
        lots += [{"realized_pnl": 50.0, "risk_usd": 10.0, "coid": f"w{i}"} for i in range(30)]
        self.assertEqual(sb.kelly_risk(lots, PRIOR)["loss_mult"], 1.0)

    def test_size_by_own_stop(self):
        # NVDA-like: $2,600 equity, $180 ask, $12 stop, r 1.48%, loss x1.064 → $38.48 / $12.77 → 3 sh
        q, info = sb.size_order(2600.0, 180.0, 12.0, 0.0148, 1.064, 1.9, 1.9, 0.0, 0.0, 1000.0)
        self.assertEqual(q, 3)
        self.assertEqual((info["q_risk"], info["q_cap"]), (3, 4))

    def test_k_below_ceiling_shrinks_risk(self):
        q, info = sb.size_order(2600.0, 180.0, 12.0, 0.0148, 1.064, 0.95, 1.9, 0.0, 0.0, 0.0)
        self.assertAlmostEqual(info["k_scalar"], 0.5)
        self.assertEqual(q, 1)                                      # $19.24 / $12.77 → 1

    def test_limb_one_no_longer_binds_swing(self):
        # swing book already at 120% of equity; the K limb (1.9x) still has room → the lot is sized
        q, info = sb.size_order(2600.0, 180.0, 12.0, 0.0148, 1.064, 1.9, 1.9, 0.0, 3120.0, 0.0)
        self.assertEqual(q, 3)
        self.assertGreater(info["room_total"], 3 * 180.0)

    def test_k_limb_blocks(self):
        q, _ = sb.size_order(2600.0, 180.0, 12.0, 0.0148, 1.064, 1.9, 1.9, 1000.0, 2000.0, 1900.0)   # room $40
        self.assertEqual(q, 0)

    def test_one_share_floor_for_wide_stops(self):
        # COST-like: one share $900 > 25% of equity → 0; a $300 share with a $60 stop (2.3% risk ≤ 3%) → 1
        q, _ = sb.size_order(2600.0, 900.0, 40.0, 0.0148, 1.0, 1.9, 1.9, 0.0, 0.0, 0.0)
        self.assertEqual(q, 0)
        q, info = sb.size_order(2600.0, 300.0, 60.0, 0.0148, 1.0, 1.9, 1.9, 0.0, 0.0, 0.0)
        self.assertEqual(q, 1)
        self.assertIn("1-share floor", info["why"])
        q, _ = sb.size_order(2600.0, 300.0, 80.0, 0.0148, 1.0, 1.9, 1.9, 0.0, 0.0, 0.0)          # $80 > 3% → 0
        self.assertEqual(q, 0)

    def test_bad_inputs_size_zero(self):
        for args in [(float("nan"), 180.0, 12.0, 0.01, 1.0, 1.9, 1.9, 0.0, 0.0, 0.0),
                     (2600.0, 180.0, 0.0, 0.01, 1.0, 1.9, 1.9, 0.0, 0.0, 0.0),
                     (2600.0, 180.0, 12.0, 0.01, 1.0, 1.9, 1.9, 0.0, 0.0, -1.0)]:
            self.assertEqual(sb.size_order(*args)[0], 0)

    def test_stop_distance_floored_at_own_atr_percentile(self):
        rng = np.random.default_rng(3)
        closes = 100 * np.cumprod(1 + rng.normal(0, 0.02, 260))
        df = _bars(list(closes))
        d, floor = sb.stop_distance(df, 100.0, 0.0001)              # near-zero ATR → the floor binds
        self.assertGreater(floor, 0)
        self.assertAlmostEqual(d, floor * 100.0)
        d2, _ = sb.stop_distance(df, 100.0, 5.0)                    # a normal ATR → 2.5 x ATR
        self.assertAlmostEqual(d2, 12.5)
        self.assertIsNone(sb.stop_distance(df, 100.0, float("nan")))

    def test_p99_down(self):
        closes = [100.0, 100.0, 100.0, 100.0, 90.0] * 52              # a 10% drop on 1 day in 5
        self.assertAlmostEqual(sb.p99_down(_bars(closes)), 0.10)
        self.assertEqual(sb.p99_down(_bars([100.0] * 30)), 0.0)
        self.assertIsNone(sb.p99_down(_bars([100.0])))


class GapCheck(unittest.TestCase):
    def test_resize_down_to_fit_buffer(self):
        # E 2,600, L0 5,000, m 0.3 → buffer (2600 − 1500)/0.7 = 1,571; book loss 4,000 × 0.30 = 1,200
        # q ≤ (1571 − 1200) / (180 × (0.10 + 0.4286)) = 3.9 → 3
        q, info = sb.gap_resize(10, 180.0, 0.10, [(4000.0, 0.30)], 2600.0, 5000.0, 0.3)
        self.assertEqual((q, info["q_max"]), (3, 3))

    def test_never_below_one_share(self):
        q, info = sb.gap_resize(10, 180.0, 0.10, [(9000.0, 0.30)], 2600.0, 9000.0, 0.3)
        self.assertEqual(q, 1)
        self.assertLess(info["q_max"], 1)

    def test_unchanged_when_it_fits(self):
        self.assertEqual(sb.gap_resize(4, 180.0, 0.05, [], 2600.0, 0.0, 0.3)[0], 4)

    def test_unknown_inputs_size_one_share(self):
        self.assertEqual(sb.gap_resize(5, 180.0, None, [], 2600.0, 0.0, 0.3)[0], 1)
        self.assertEqual(sb.gap_resize(5, 180.0, 0.05, [], 2600.0, 0.0, None)[0], 1)
        # a book lot with an unknown p99 takes the largest known one (the new lot's)
        q, info = sb.gap_resize(10, 100.0, 0.20, [(1000.0, None)], 2600.0, 1000.0, 0.3)
        self.assertEqual(info["book_loss"], 200.0)


class LotR(unittest.TestCase):
    def test_r_and_archive(self):
        self.assertAlmostEqual(sb.lot_r({"realized_pnl": -12.0, "risk_usd": 10.0}), -1.2)
        self.assertAlmostEqual(sb.lot_r({"realized_pnl": 6.0, "entry_px": 100.0, "stop_px": 95.0, "qty": 2}), 0.6)
        self.assertIsNone(sb.lot_r({"realized_pnl": None, "risk_usd": 10.0}))
        self.assertIsNone(sb.lot_r({"realized_pnl": 5.0, "risk_usd": 10.0, "entry_px_estimated": True}))
        self.assertIsNone(sb.lot_r({"realized_pnl": 5.0, "entry_px": 100.0, "stop_px": 95.0, "qty": 2, "partial_pnl": 1.0}))
        state = {"positions": {"AAA": {"status": "closed", "coid": "c1", "realized_pnl": 5.0, "risk_usd": 10.0}}}
        sb._archive_closed(state, state["positions"]["AAA"])
        sb._archive_closed(state, state["positions"]["AAA"])
        self.assertEqual(len(state["closed_lots"]), 1)
        self.assertEqual(len(sb.closed_lots(state)), 1)          # archive + positions de-duplicated


class Setups(unittest.TestCase):
    """Pending breakout setup lifecycle with a fake broker (never the real one)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(sb, "_STATE", Path(self.tmp.name) / "sb.json"),
                        mock.patch.object(sb, "_log", lambda ev, sym, **kw: self.logs.append((ev, sym, kw))),
                        mock.patch.object(sb, "_page", lambda m: self.logs.append(("page", "", {"m": m}))),
                        mock.patch.object(sb, "_load_prior", return_value=PRIOR),
                        mock.patch.multiple(sb.config, create=True, SWING_BREAKOUT_ENABLED=True,
                                            SWING_BREAKOUT_TOP_N=50, SWING_BREAKOUT_MAX_SLOTS=10,
                                            SWING_BREAKOUT_FILL_WAIT_S=0.0, **CFG)]
        self.logs = []
        for p in self.patches:
            p.start()
        self.alloc = SimpleNamespace(
            live_admit=lambda *a, **k: SimpleNamespace(approved=True, reason="ok", lease=None),
            live_order_id=lambda lease: None, live_bind=lambda lease, order: True, live_release=lambda lease, reason: None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _scan(self, b, bars, open_px, ask=125.0, day=30):
        fakes = {"execution.broker": b,
                 "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: {"bid": ask - 0.1, "ask": ask},
                                                     get_latest_trade_with_time=lambda s: _fresh(ask)),
                 "execution.quarterly_hold_manager": SimpleNamespace(get_quarterly_hold_symbols=lambda: []),
                 "execution.tier_capital_allocator": self.alloc}
        pool = getattr(sb.config, "SWING_BREAKOUT_POOL", None)
        pool = pool if pool and set(pool) <= set(bars) else list(bars)
        with _mods(fakes), mock.patch.object(sb.config, "SWING_BREAKOUT_POOL", pool, create=True), \
                mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: bars[s])), \
                mock.patch.object(sb.SwingBreakoutManager, "_today_open",
                                  staticmethod(lambda s, t, n: open_px.get(s) if isinstance(open_px, dict) else open_px)), \
                mock.patch.object(sb.SwingBreakoutManager, "_maint_ratio", staticmethod(lambda a, p: 0.3)), \
                mock.patch("execution.overnight_capacity.overnight_k", return_value=1.9), \
                mock.patch("time.sleep"):
            return sb.SwingBreakoutManager().run_entries(datetime(2026, 9, day, 10, 10, tzinfo=sb.ET))

    def test_open_below_level_waits_then_recovery_enters_once(self):
        bars = {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)}
        level = sb.breakout_signal(bars["AAA"])["prior_high"]
        b = FakeBroker()
        self.assertEqual(self._scan(b, bars, level - 1.0, day=29), [])          # gap below the level: waits
        st = sb._load_state()
        self.assertIn("AAA", st["setups"])
        self.assertEqual(b.orders, {})
        self.assertTrue(any(kw.get("reason", "").startswith("open below") for _, _, kw in self.logs))
        got = self._scan(b, bars, level + 1.0, day=30)                          # next session opens above: enters
        self.assertEqual(got, ["AAA"])
        st = sb._load_state()
        self.assertNotIn("AAA", st["setups"])                                   # one entry per setup
        rec = st["positions"]["AAA"]
        self.assertEqual(rec["change"], "swing-owner-1025")
        self.assertAlmostEqual(rec["stop_px"], round(rec["entry_px"] - rec["stop_dist"], 2))
        self.assertGreater(rec["risk_usd"], 0)
        for k in ("kelly", "gap", "aggregate_risk", "k", "r", "loss_mult", "atr_floor_pct"):
            self.assertIn(k, rec["sizing"])

    def test_ask_below_level_is_never_entered(self):
        bars = {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)}
        level = sb.breakout_signal(bars["AAA"])["prior_high"]
        b = FakeBroker()
        self.assertEqual(self._scan(b, bars, level + 1.0, ask=level - 0.5), [])
        self.assertEqual(b.orders, {})
        self.assertIn("AAA", sb._load_state()["setups"])

    def test_setup_dropped_on_close_below_prior_20_low(self):
        bars = {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)}
        level = sb.breakout_signal(bars["AAA"])["prior_high"]
        self._scan(FakeBroker(), bars, level - 1.0, day=29)
        self.assertIn("AAA", sb._load_state()["setups"])
        # bars ending on the session the setup was created (09-29): that session closed below the prior 20 lows
        start = str(pd.bdate_range(end="2026-09-29", periods=260)[0].date())
        broke = _bars(list(np.linspace(100, 120, 258)) + [125.0, 90.0], start=start)
        st = sb._load_state()
        st["last_entry_scan"] = None
        sb._save_state(st)
        self._scan(FakeBroker(), {"AAA": broke, "BBB": bars["BBB"]}, 130.0, day=30)
        self.assertNotIn("AAA", sb._load_state()["setups"])

    def test_missed_session_break_still_drops_setup(self):
        # the break happened on 09-29, then a recovery bar on 09-30; the 10-01 scan still sees the 09-29 break
        bars = {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)}
        level = sb.breakout_signal(bars["AAA"])["prior_high"]
        self._scan(FakeBroker(), bars, level - 1.0, day=29)
        start = str(pd.bdate_range(end="2026-09-30", periods=261)[0].date())
        broke = _bars(list(np.linspace(100, 120, 258)) + [125.0, 90.0, 126.0], start=start)
        st = sb._load_state()
        st["last_entry_scan"] = None
        sb._save_state(st)
        fakes_day = 30
        self._scan(FakeBroker(), {"AAA": broke, "BBB": bars["BBB"]}, 130.0, day=fakes_day)
        self.assertNotIn("AAA", sb._load_state()["setups"])

    def test_setup_used_when_fill_is_flattened_for_no_stop(self):
        bars = {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)}
        b = FakeBroker(stop_ok=False)
        self.assertEqual(self._scan(b, bars, 125.0), [])
        st = sb._load_state()
        self.assertEqual(st["positions"]["AAA"]["status"], "closed")       # filled, no stop → flattened
        self.assertNotIn("AAA", st["setups"])                               # the one entry was used

    def test_rank_order_and_slot_cap(self):
        bars = {f"S{i}": _bars(list(np.linspace(100, 120, 259)) + [125.0], vol=1e6 * (10 - i)) for i in range(4)}
        with mock.patch.multiple(sb.config, SWING_BREAKOUT_MAX_SLOTS=2, SWING_BREAKOUT_POOL=list(bars), create=True):
            got = self._scan(FakeBroker(), bars, 125.0)
        self.assertEqual(got, ["S0", "S1"])                                     # highest $ volume first; 2 slots
        self.assertTrue(any(kw.get("reason") == "all slots full" for _, s, kw in self.logs if s == "S2"))

    def test_exit_logs_slippage_vs_stop_and_archives_r(self):
        state = {"positions": {"AAA": {"status": "open", "qty": 2, "entry_px": 125.0, "stop_px": 110.0,
                                       "risk_usd": 30.0, "coid": "IN-1"}}}
        sb.SwingBreakoutManager()._book_exit("AAA", state["positions"]["AAA"], state, "protective_stop", 107.0, 2)
        ev = [kw for e, s, kw in self.logs if e == "exit"][0]
        self.assertEqual((ev["stop_level"], ev["slippage_vs_stop"]), (110.0, -3.0))
        self.assertAlmostEqual(ev["r_multiple"], -36.0 / 30.0)
        self.assertEqual(state["closed_lots"][0]["coid"], "IN-1")


class MaintRatio(unittest.TestCase):
    def test_live_then_state_fallback(self):
        acct = SimpleNamespace(maintenance_margin=900.0)
        pos = [SimpleNamespace(market_value=3000.0)]
        self.assertAlmostEqual(sb.SwingBreakoutManager._maint_ratio(acct, pos), 0.3)
        with mock.patch("execution.overnight_capacity._load", return_value={"inputs": {"maint_ratio": 0.327}}):
            self.assertAlmostEqual(sb.SwingBreakoutManager._maint_ratio(SimpleNamespace(), pos), 0.327)
        with mock.patch("execution.overnight_capacity._load", return_value=None):
            self.assertIsNone(sb.SwingBreakoutManager._maint_ratio(SimpleNamespace(), pos))


class PriorFile(unittest.TestCase):
    def test_tracked_prior_loads(self):
        d = sb._load_prior()
        self.assertEqual(d["n"], 1053)
        self.assertTrue(set(sb._DEFAULT_POOL) >= {"AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA"})


if __name__ == "__main__":
    unittest.main()
