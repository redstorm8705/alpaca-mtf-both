#!/usr/bin/env python3
# ruff: noqa: E501
"""
Unit tests for strategy/day_tier_sizing.compute_day_tier_size (Day-Tier sizing, PURE — no order).
A pure function — inject decision + entry price + equity + buying_power + track directly. Proves the
BP-based Track-A budget math (capped by its equity ceiling), the min()-only cap,
conviction scaling, the whole-share floor, the Track A(BP)/B(cash) split, the BP fail-CLOSED guard,
and every fail-safe. (Aggregate gross cap + main-bot reserve + cushion are enforced at wire-time in
day_trade_manager._bounded_entry_qty, tested separately — NOT here.)

Runs with plain unittest:  python3 -m unittest tests.test_day_tier_sizing
"""
from __future__ import annotations

import unittest

import config
from strategy import day_tier_sizing as sz

_BP_PCT = float(config.DAYTRADE_TRACK_A_PER_TRADE_BP_PCT)   # per-trade Track-A fraction of buying power (0.20)


def _dec(would_consider=True, conviction=0.5):
    return {"symbol": "NVDA", "would_consider": would_consider, "conviction": conviction,
            "side": "LONG", "gex_action": "FADE"}


class DayTierSizing(unittest.TestCase):
    # 1 -- Track A sizes off BUYING POWER: budget = BP*0.20; conviction 0.5 -> notional half; whole-share floor.
    def test_track_a_basic(self):
        # BP 1000 -> budget 200; conviction 0.5 -> notional 100; 1 share @ 100.
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=0.5), entry_ref=100.0, equity=2500.0, buying_power=1000.0, track="A")
        self.assertAlmostEqual(r["budget"], 1000.0 * _BP_PCT, places=2)   # 200.0
        self.assertEqual(r["shares"], 1)
        self.assertTrue(r["size_ok"])
        self.assertFalse(r["cash_only"])

    # 1b -- Track A budget follows BUYING POWER, NOT equity (the whole point of the 2026-09-08 change).
    def test_track_a_sizes_off_bp_not_equity(self):
        # Huge equity, small BP -> budget tracks BP (200), not equity.
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0, equity=1_000_000.0, buying_power=1000.0, track="A")
        self.assertAlmostEqual(r["budget"], 1000.0 * _BP_PCT, places=2)   # 200.0, not an equity slice
        self.assertEqual(r["shares"], 2)                                  # floor(200/100)

    def test_track_a_per_trade_budget_is_equity_capped(self):
        # BP slice is $2,000, but 60% × $2,500 = $1,500 is the per-trade hard ceiling.
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0,
                                     equity=2500.0, buying_power=10000.0, track="A")
        self.assertEqual(r["budget"], 1500.0)
        self.assertEqual(r["shares"], 15)

    # 1c -- BP fail-CLOSED: missing / non-positive / non-finite buying_power on Track A -> size 0, size_ok False.
    def test_track_a_bp_unavailable_fail_closed(self):
        for bad in (None, 0.0, -100.0, float("nan"), float("inf")):
            r = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0, equity=2500.0, buying_power=bad, track="A")
            self.assertEqual(r["shares"], 0, f"bp={bad}")
            self.assertFalse(r["size_ok"], f"bp={bad}")
            self.assertIn("buying_power", r["reason"], f"bp={bad}")

    # 2 -- Track B is cash-only + uses the 35% EQUITY share (unchanged): budget 2500*0.15*0.35=131.25.
    def test_track_b_cash_only(self):
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0, equity=2500.0, track="B")
        self.assertAlmostEqual(r["budget"], 131.25, places=2)
        self.assertTrue(r["cash_only"])
        self.assertEqual(r["shares"], 1)

    # 3 -- min()-only cap: conviction clamped to 1.0 -> notional == budget, never more.
    def test_min_only_cap(self):
        # BP 50k, track A budget = 10000; conviction 2.0 clamps to 1.0 -> notional 10000 -> 100 sh @100.
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=2.0), entry_ref=100.0, equity=100000.0, buying_power=50000.0, track="A")
        self.assertEqual(r["conviction"], 1.0)                 # clamped
        self.assertEqual(r["shares"], 100)                     # floor(10000/100)
        self.assertLessEqual(r["notional"], r["budget"] + 0.01)

    # 4 -- conviction scales DOWN: half conviction ~ half the shares (large budget so flooring is minor).
    def test_conviction_scales(self):
        full = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=10.0, equity=100000.0, buying_power=100000.0, track="A")
        half = sz.compute_day_tier_size("NVDA", _dec(conviction=0.5), entry_ref=10.0, equity=100000.0, buying_power=100000.0, track="A")
        self.assertEqual(full["shares"], 2000)                 # floor(20000/10)
        self.assertEqual(half["shares"], 1000)                 # floor(10000/10)

    # 5 -- not a would_consider candidate -> 0 shares (returns before the BP basis is read).
    def test_not_candidate_zero(self):
        r = sz.compute_day_tier_size("NVDA", _dec(would_consider=False, conviction=1.0), entry_ref=100.0, equity=2500.0, buying_power=10000.0)
        self.assertEqual(r["shares"], 0)
        self.assertFalse(r["size_ok"])

    # 6 -- non-positive inputs -> 0 shares, never raises (returns before the BP basis is read).
    def test_nonpositive_inputs_zero(self):
        for eq, px, cv in ((0.0, 100.0, 0.5), (2500.0, 0.0, 0.5), (2500.0, 100.0, 0.0), (-5.0, 100.0, 0.5)):
            r = sz.compute_day_tier_size("NVDA", _dec(conviction=cv), entry_ref=px, equity=eq, buying_power=10000.0)
            self.assertEqual(r["shares"], 0, f"eq={eq} px={px} cv={cv}")
            self.assertFalse(r["size_ok"])

    # 7 -- budget can't afford a whole share (RC-7 floor) -> 0, size_ok False (skip, not a phantom 1).
    def test_cannot_afford_share(self):
        # BP 1000 -> track A budget 200, entry 500 -> floor(200/500)=0.
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=500.0, equity=2500.0, buying_power=1000.0, track="A")
        self.assertEqual(r["shares"], 0)
        self.assertFalse(r["size_ok"])
        self.assertIn("< 1 share", r["reason"])

    # 8 -- garbage decision / non-numeric -> 0, never raises.
    def test_garbage_fail_safe(self):
        self.assertEqual(sz.compute_day_tier_size("NVDA", None, 100.0, 2500.0, buying_power=10000.0)["shares"], 0)
        self.assertEqual(sz.compute_day_tier_size("NVDA", _dec(conviction="x"), 100.0, 2500.0, buying_power=10000.0)["shares"], 0)

    # 8b -- negative conviction clamps to 0 -> no trade.
    def test_negative_conviction_zero(self):
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=-0.5), entry_ref=100.0, equity=2500.0, buying_power=10000.0)
        self.assertEqual(r["shares"], 0)
        self.assertFalse(r["size_ok"])

    # 8c -- lowercase "b" resolves to Track B (35%, cash-only); unknown track defaults to A.
    def test_track_normalization(self):
        rb = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0, equity=2500.0, buying_power=10000.0, track="b")
        self.assertEqual(rb["track"], "B")
        self.assertTrue(rb["cash_only"])
        ru = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0, equity=2500.0, buying_power=10000.0, track="ZZZ")
        self.assertEqual(ru["track"], "A")
        self.assertFalse(ru["cash_only"])

    # 8d -- NaN / inf equity|price|conviction -> 0 shares, never raises (returns before the BP basis is read).
    def test_nonfinite_inputs_zero(self):
        nan, inf = float("nan"), float("inf")
        for eq, px, cv in ((nan, 100.0, 0.5), (2500.0, inf, 0.5), (inf, 100.0, 0.5), (2500.0, 100.0, nan)):
            r = sz.compute_day_tier_size("NVDA", _dec(conviction=cv), entry_ref=px, equity=eq, buying_power=10000.0)
            self.assertEqual(r["shares"], 0, f"eq={eq} px={px} cv={cv}")
            self.assertFalse(r["size_ok"])

    # 9 -- notional never exceeds the track budget (hard cap), any conviction.
    def test_notional_within_budget(self):
        for cv in (0.2, 0.5, 0.9, 1.0):
            r = sz.compute_day_tier_size("NVDA", _dec(conviction=cv), entry_ref=1.0, equity=10000.0, buying_power=10000.0, track="A")
            self.assertLessEqual(r["notional"], r["budget"] + 0.01, f"cv={cv}")

    # 10 -- result shape is stable (all keys present even on an early fail-closed return).
    def test_shape_stable(self):
        r = sz.compute_day_tier_size("NVDA", _dec(), 100.0, 2500.0, buying_power=10000.0)
        for k in ("symbol", "shares", "notional", "budget", "track", "cash_only", "conviction", "size_ok", "reason"):
            self.assertIn(k, r)

    # 11 -- MIN-1-SHARE FLOOR (Rafael 2026-09-15): a fired ENTER whose conviction-scaled notional floors
    #       below 1 share still takes 1 share WHEN the per-trade track_budget affords a whole share
    #       (track_budget >= px). The floored notional (1×px) never exceeds the per-trade budget.
    def test_min_one_share_floor_fires(self):
        # BP 4260 -> budget min(4260*0.20, 2382*0.60)=min(852,1429.2)=852; conviction 0.33, entry 360:
        # notional 852*0.33=281.16 -> floor(281.16/360)=0, but track_budget 852 >= 360 -> floor to 1 sh.
        r = sz.compute_day_tier_size("TSLA", _dec(conviction=0.33), entry_ref=360.0, equity=2382.0, buying_power=4260.0, track="A")
        self.assertEqual(r["shares"], 1)
        self.assertTrue(r["size_ok"])
        self.assertLessEqual(r["notional"], r["budget"] + 0.01)   # floored notional never exceeds the budget
        self.assertIn("FLOOR", r["reason"])

    # 11b -- floor is GATED by the per-trade budget: when even 1 share exceeds track_budget
    #        (track_budget < px) it does NOT fire -> stays 0 (the existing "cannot afford" behavior, test 7).
    def test_min_one_share_floor_gated_by_budget(self):
        # BP 1000 -> budget 200; entry 500 -> track_budget 200 < 500 -> NO floor -> 0 shares.
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=500.0, equity=2500.0, buying_power=1000.0, track="A")
        self.assertEqual(r["shares"], 0)
        self.assertFalse(r["size_ok"])
        self.assertIn("< 1 share", r["reason"])

    # 11c -- kill flag: DAYTRADE_MIN_ONE_SHARE_FLOOR=False disables the floor (reverts to skip); the skip
    #        reason honestly names the disabled floor (Rule D per-feature kill flag). Restores config state.
    def test_min_one_share_floor_killable(self):
        had = hasattr(config, "DAYTRADE_MIN_ONE_SHARE_FLOOR")
        orig = getattr(config, "DAYTRADE_MIN_ONE_SHARE_FLOOR", None)
        try:
            config.DAYTRADE_MIN_ONE_SHARE_FLOOR = False
            r = sz.compute_day_tier_size("TSLA", _dec(conviction=0.33), entry_ref=360.0, equity=2382.0, buying_power=4260.0, track="A")
            self.assertEqual(r["shares"], 0)
            self.assertFalse(r["size_ok"])
            self.assertIn("disabled", r["reason"])
        finally:
            if had:
                config.DAYTRADE_MIN_ONE_SHARE_FLOOR = orig
            else:
                delattr(config, "DAYTRADE_MIN_ONE_SHARE_FLOOR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
