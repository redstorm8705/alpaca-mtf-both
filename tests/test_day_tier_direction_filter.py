#!/usr/bin/env python3
# ruff: noqa: E501
"""Day tier never trades against the Layer-A trend side; no clear trend = no trade (Rafael 2026-10-03).

Live evidence 2026-09-15..10-02: trades opposite the trend side or on TWO_SIDED went 0/13 (-$32.93);
aligned trades 3/6 (-$3.06). The rule is enforced in run_day_tier.run_tick for Track A and Track B."""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import config
import run_day_tier as rdt
trend_direction_conflict = rdt._trend_direction_conflict


class Rule(unittest.TestCase):
    def test_aligned_is_allowed(self):
        self.assertEqual(trend_direction_conflict("LONG", "long"), "")
        self.assertEqual(trend_direction_conflict("SHORT", "short"), "")

    def test_against_trend_is_blocked(self):
        self.assertIn("against", trend_direction_conflict("LONG", "short"))
        self.assertIn("against", trend_direction_conflict("SHORT", "long"))

    def test_no_clear_trend_is_blocked(self):
        for side in ("TWO_SIDED", "UNKNOWN", None, ""):
            self.assertIn("no clear trend", trend_direction_conflict(side, "long"))

    def test_bad_direction_is_blocked(self):
        self.assertIn("invalid", trend_direction_conflict("LONG", None))
        self.assertIn("invalid", trend_direction_conflict("LONG", "sideways"))


class RunTickTrackA(unittest.TestCase):
    """Drives run_tick with function-level patches on the real modules (the RunnerWindowGate pattern) —
    never a sys.modules swap, which would unload modules other test files rely on."""

    def _run(self, side, direction):
        import contextlib
        placed, logged = [], []
        acct = SimpleNamespace(equity=2500.0, last_equity=2500.0, buying_power=9000.0)
        decision = {"would_consider": True, "side": side}
        trigger = {"trigger": "ENTER", "direction": direction, "entry_ref": 100.0}
        patches = [
            mock.patch.object(rdt, "_clock_state", return_value=("open", 300.0)),
            mock.patch.object(rdt, "_touch_heartbeat"),
            mock.patch.object(rdt, "_maybe_sample_prices"),
            mock.patch("execution.broker.get_account", return_value=acct),
            mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 0}),
            mock.patch("execution.day_trade_manager.tier_kill_check", return_value=False),
            mock.patch("execution.day_trade_manager._account_entry_halt_reason", return_value=None),
            mock.patch("execution.day_trade_manager.bar_id_for", return_value="20261005-1000"),
            mock.patch("execution.day_trade_manager.place_entry",
                       side_effect=lambda sym, *a, **k: placed.append(sym) or True),
            mock.patch("execution.risk_manager.RiskManager"),
            mock.patch("strategy.day_tier_decision.compute_day_tier_decision", return_value=decision),
            mock.patch("strategy.day_tier_entry_trigger.compute_entry_trigger", return_value=trigger),
            mock.patch("strategy.day_tier_sizing.compute_day_tier_size", return_value={"size_ok": True}),
            mock.patch("strategy.day_tier_logger.log_decision",
                       side_effect=lambda did, sym, **kw: logged.append((did, sym, kw)) or True),
            mock.patch.object(config, "DAYTRADE_ENABLED", True),
            mock.patch.object(config, "DAYTRADE_TRACK_B_ENABLED", False),
            mock.patch.object(config, "DAYTRADE_UNIVERSE", ["NVDA"]),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            rm = sys.modules["execution.risk_manager"].RiskManager
            rm.return_value.check_kill_switch.return_value = False
            out = rdt.run_tick()
        return out, placed, logged

    def test_against_trend_never_reaches_place_entry_and_logs_skip(self):
        out, placed, logged = self._run("LONG", "short")
        self.assertEqual(placed, [])
        self.assertEqual(out["entered"], 0)
        self.assertTrue(logged and "against" in logged[0][2]["trigger"]["skip_reason"])

    def test_two_sided_never_reaches_place_entry(self):
        _, placed, logged = self._run("TWO_SIDED", "short")
        self.assertEqual(placed, [])
        self.assertIn("no clear trend", logged[0][2]["trigger"]["skip_reason"])

    def test_aligned_reaches_place_entry(self):
        out, placed, logged = self._run("SHORT", "short")
        self.assertEqual(placed, ["NVDA"])
        self.assertEqual(out["entered"], 1)
        self.assertEqual(logged, [])


if __name__ == "__main__":
    unittest.main()
