#!/usr/bin/env python3
# ruff: noqa: E501
"""
Unit tests for strategy/day_tier_decision.compute_day_tier_decision (Day-Tier meta-label
composition, INERT). Mocks the two sub-layers (compute_side_bias / compute_gex_action) and proves
the conviction blend, the would_consider gate, and every fail-safe branch.

Runs with plain unittest:  python3 -m unittest tests.test_day_tier_decision
"""
from __future__ import annotations

import sys
from unittest import mock
import unittest

# day_tier_side imports data.fetcher (alpaca) at load; stub the SDK + requests so the harness runs.
for _mod in ("alpaca", "alpaca.data", "alpaca.data.timeframe", "alpaca.data.requests",
             "alpaca.data.historical", "requests"):
    sys.modules.setdefault(_mod, mock.MagicMock())

from strategy import day_tier_decision as dd  # noqa: E402


def _side(side="LONG", score=0.6):
    return {"symbol": "NVDA", "side": side, "score": score, "stack": {}, "weight_coverage": 1.0, "reason": ""}


def _action(action="FADE", act_ok=True, strength=0.7, gex_label="POSITIVE", sign_reliable=True):
    return {"symbol": "NVDA", "action": action, "gex_label": gex_label, "sign_reliable": sign_reliable,
            "pin_confidence": 0.7, "dte": 1, "pin_strength": 1.0, "tod_factor": 1.0, "strength": strength,
            "targets": {"pin": 101.0, "wall": 100.0}, "side": "LONG", "act_ok": act_ok, "reason": ""}


class DayTierDecision(unittest.TestCase):
    def _patch_layers(self, side, action):
        return mock.patch.multiple(
            "strategy.day_tier_side", compute_side_bias=mock.Mock(return_value=side),
        ), mock.patch.multiple(
            "strategy.day_tier_gex_action", compute_gex_action=mock.Mock(return_value=action),
        )

    def _run(self, side, action):
        p1, p2 = self._patch_layers(side, action)
        with p1, p2:
            return dd.compute_day_tier_decision("NVDA")

    # 1 -- LONG + act_ok FADE (strong) -> would_consider True, conviction blends both
    def test_long_fade_considers(self):
        r = self._run(_side("LONG", 0.6), _action("FADE", act_ok=True, strength=0.7))
        self.assertTrue(r["would_consider"])
        self.assertEqual(r["side"], "LONG")
        self.assertEqual(r["gex_action"], "FADE")
        # conviction = 0.4*0.6 + 0.6*0.7 = 0.24 + 0.42 = 0.66
        self.assertAlmostEqual(r["conviction"], 0.66, places=2)

    # 2 -- UNKNOWN side -> would_consider False (no valid structural read)
    def test_unknown_side_no_consider(self):
        r = self._run(_side("UNKNOWN", None), _action("FADE", act_ok=True, strength=0.9))
        self.assertFalse(r["would_consider"])
        self.assertEqual(r["side"], "UNKNOWN")

    # 3 -- act_ok False -> conviction 0, would_consider False
    def test_not_act_ok_zero_conviction(self):
        r = self._run(_side("LONG", 0.8), _action("FADE", act_ok=False, strength=0.9))
        self.assertEqual(r["conviction"], 0.0)
        self.assertFalse(r["would_consider"])

    # 4 -- TWO_SIDED is a valid read: no directional score but conviction from strength still counts
    def test_two_sided_considers_on_strength(self):
        r = self._run(_side("TWO_SIDED", 0.1), _action("RIDE", act_ok=True, strength=0.8))
        # side contributes 0 (not directional); conviction = 0.6*0.8 = 0.48 >= floor
        self.assertAlmostEqual(r["conviction"], 0.48, places=2)
        self.assertTrue(r["would_consider"])

    # 5 -- weak conviction (low side + low strength) -> would_consider False
    def test_weak_conviction_no_consider(self):
        r = self._run(_side("LONG", 0.1), _action("FADE", act_ok=True, strength=0.1))
        # 0.4*0.1 + 0.6*0.1 = 0.10 < _MIN_CONVICTION 0.15
        self.assertLess(r["conviction"], dd._MIN_CONVICTION)
        self.assertFalse(r["would_consider"])

    # 6 -- a sub-layer raising -> would_consider False, never propagates
    def test_sublayer_raises_fail_safe(self):
        with mock.patch("strategy.day_tier_side.compute_side_bias", mock.Mock(side_effect=RuntimeError("boom"))), \
             mock.patch("strategy.day_tier_gex_action.compute_gex_action", mock.Mock(return_value=_action())):
            r = dd.compute_day_tier_decision("NVDA")
        self.assertFalse(r["would_consider"])
        self.assertEqual(r["conviction"], 0.0)

    # 7 -- SHORT side magnitude uses abs(score) (negative score contributes positive conviction)
    def test_short_side_abs_magnitude(self):
        r = self._run(_side("SHORT", -0.9), _action("RIDE", act_ok=True, strength=0.5))
        # |−0.9|=0.9: conviction = 0.4*0.9 + 0.6*0.5 = 0.36 + 0.30 = 0.66
        self.assertAlmostEqual(r["conviction"], 0.66, places=2)
        self.assertTrue(r["would_consider"])

    # 8 -- result shape is stable
    def test_shape_stable(self):
        r = self._run(_side("LONG", 0.6), _action("FADE"))
        for k in ("symbol", "side", "side_score", "gex_action", "gex_label", "act_ok", "strength",
                  "targets", "sign_reliable", "conviction", "would_consider", "reason"):
            self.assertIn(k, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
