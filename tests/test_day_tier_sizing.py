#!/usr/bin/env python3
# ruff: noqa: E501
"""
Unit tests for strategy/day_tier_sizing.compute_day_tier_size (Day-Tier sizing, PURE + INERT).
A pure function — inject decision + entry price + equity + track directly. Proves the budget math,
the min()-only cap, conviction scaling, the whole-share floor, Track A/B split, and every fail-safe.

Runs with plain unittest:  python3 -m unittest tests.test_day_tier_sizing
"""
from __future__ import annotations

import unittest

from strategy import day_tier_sizing as sz


def _dec(would_consider=True, conviction=0.5):
    return {"symbol": "NVDA", "would_consider": would_consider, "conviction": conviction,
            "side": "LONG", "gex_action": "FADE"}


class DayTierSizing(unittest.TestCase):
    # 1 -- Track A, conviction 0.5: budget 2500*0.15*0.65=243.75; target 121.875; 1 share @ 100
    def test_track_a_basic(self):
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=0.5), entry_ref=100.0, equity=2500.0, track="A")
        self.assertAlmostEqual(r["budget"], 243.75, places=2)
        self.assertEqual(r["shares"], 1)
        self.assertTrue(r["size_ok"])
        self.assertFalse(r["cash_only"])

    # 2 -- Track B is cash-only + uses the 35% share: budget 2500*0.15*0.35=131.25
    def test_track_b_cash_only(self):
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0, equity=2500.0, track="B")
        self.assertAlmostEqual(r["budget"], 131.25, places=2)
        self.assertTrue(r["cash_only"])
        self.assertEqual(r["shares"], 1)

    # 3 -- min()-only cap: conviction clamped to 1.0 -> notional == budget, never more
    def test_min_only_cap(self):
        # equity 100k, track A budget = 9750; conviction 2.0 clamps to 1.0 -> notional 9750 -> 97 sh @100
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=2.0), entry_ref=100.0, equity=100000.0, track="A")
        self.assertEqual(r["conviction"], 1.0)                 # clamped
        self.assertEqual(r["shares"], 97)                      # floor(9750/100)
        self.assertLessEqual(r["notional"], r["budget"] + 0.01)

    # 4 -- conviction scales DOWN: half conviction ~ half the shares (large budget so flooring is minor)
    def test_conviction_scales(self):
        full = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=10.0, equity=100000.0, track="A")
        half = sz.compute_day_tier_size("NVDA", _dec(conviction=0.5), entry_ref=10.0, equity=100000.0, track="A")
        self.assertEqual(full["shares"], 975)                  # 9750/10
        self.assertEqual(half["shares"], 487)                  # floor(4875/10)

    # 5 -- not a would_consider candidate -> 0 shares
    def test_not_candidate_zero(self):
        r = sz.compute_day_tier_size("NVDA", _dec(would_consider=False, conviction=1.0), entry_ref=100.0, equity=2500.0)
        self.assertEqual(r["shares"], 0)
        self.assertFalse(r["size_ok"])

    # 6 -- non-positive inputs -> 0 shares, never raises
    def test_nonpositive_inputs_zero(self):
        for eq, px, cv in ((0.0, 100.0, 0.5), (2500.0, 0.0, 0.5), (2500.0, 100.0, 0.0), (-5.0, 100.0, 0.5)):
            r = sz.compute_day_tier_size("NVDA", _dec(conviction=cv), entry_ref=px, equity=eq)
            self.assertEqual(r["shares"], 0, f"eq={eq} px={px} cv={cv}")
            self.assertFalse(r["size_ok"])

    # 7 -- budget can't afford a whole share (RC-7 floor) -> 0, size_ok False (skip, not a phantom 1)
    def test_cannot_afford_share(self):
        # track A budget 243.75, entry 500 -> floor(243.75/500)=0
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=500.0, equity=2500.0, track="A")
        self.assertEqual(r["shares"], 0)
        self.assertFalse(r["size_ok"])
        self.assertIn("< 1 share", r["reason"])

    # 8 -- garbage decision / non-numeric -> 0, never raises
    def test_garbage_fail_safe(self):
        self.assertEqual(sz.compute_day_tier_size("NVDA", None, 100.0, 2500.0)["shares"], 0)
        self.assertEqual(sz.compute_day_tier_size("NVDA", _dec(conviction="x"), 100.0, 2500.0)["shares"], 0)

    # 8b -- negative conviction clamps to 0 -> no trade
    def test_negative_conviction_zero(self):
        r = sz.compute_day_tier_size("NVDA", _dec(conviction=-0.5), entry_ref=100.0, equity=2500.0)
        self.assertEqual(r["shares"], 0)
        self.assertFalse(r["size_ok"])

    # 8c -- lowercase "b" resolves to Track B (35%, cash-only); unknown track defaults to A
    def test_track_normalization(self):
        rb = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0, equity=2500.0, track="b")
        self.assertEqual(rb["track"], "B")
        self.assertTrue(rb["cash_only"])
        ru = sz.compute_day_tier_size("NVDA", _dec(conviction=1.0), entry_ref=100.0, equity=2500.0, track="ZZZ")
        self.assertEqual(ru["track"], "A")
        self.assertFalse(ru["cash_only"])

    # 8d -- NaN / inf inputs -> 0 shares, never raises, never over-sizes
    def test_nonfinite_inputs_zero(self):
        nan, inf = float("nan"), float("inf")
        for eq, px, cv in ((nan, 100.0, 0.5), (2500.0, inf, 0.5), (inf, 100.0, 0.5), (2500.0, 100.0, nan)):
            r = sz.compute_day_tier_size("NVDA", _dec(conviction=cv), entry_ref=px, equity=eq)
            self.assertEqual(r["shares"], 0, f"eq={eq} px={px} cv={cv}")
            self.assertFalse(r["size_ok"])

    # 9 -- notional never exceeds the track budget (hard cap), any conviction
    def test_notional_within_budget(self):
        for cv in (0.2, 0.5, 0.9, 1.0):
            r = sz.compute_day_tier_size("NVDA", _dec(conviction=cv), entry_ref=1.0, equity=10000.0, track="A")
            self.assertLessEqual(r["notional"], r["budget"] + 0.01, f"cv={cv}")

    # 10 -- result shape is stable
    def test_shape_stable(self):
        r = sz.compute_day_tier_size("NVDA", _dec(), 100.0, 2500.0)
        for k in ("symbol", "shares", "notional", "budget", "track", "cash_only", "conviction", "size_ok", "reason"):
            self.assertIn(k, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
