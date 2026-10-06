#!/usr/bin/env python3
# ruff: noqa: E501
"""Day tier never trades against the Layer-A trend side; no clear trend = no trade (Rafael 2026-10-03). A
counter-trend FADE is the one exception: its 2m/5m indicators must agree AND today's intraday trend must have
failed (15m lead + structure break, 30m confirmation — Rafael 2026-10-04).

Live evidence 2026-09-15..10-02: trades opposite the trend side or on TWO_SIDED went 0/13 (-$32.93);
aligned trades 3/6 (-$3.06). The rule is enforced in run_day_tier.run_tick for Track A and Track B."""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import config
import run_day_tier as rdt


ALIGNED = {"aligned": True, "checks": {}, "reason": "all short-term indicators agree"}
NOT_ALIGNED = {"aligned": False, "checks": {"ema5": False}, "reason": "not aligned: ema5"}
FAILED = {"failed": True, "checks": {}, "levels": {}, "reason": "intraday trend failed"}
NOT_FAILED = {"failed": False, "checks": {"b3_body_break": False}, "levels": {}, "reason": "trend not confirmed failed: b3_body_break"}


class Gate(unittest.TestCase):
    def _gate(self, side, direction, mode="RIDE", aligned=True, ct_ok=(True, "ok"), allow_counter_fade=True,
              failed=True):
        with mock.patch.object(rdt, "_alignment_for", return_value=ALIGNED if aligned else NOT_ALIGNED), \
                mock.patch.object(rdt, "_trend_failure_for", return_value=FAILED if failed else NOT_FAILED) as tf, \
                mock.patch.object(rdt, "_counter_trend_fades_ok", return_value=ct_ok):
            self._tf_mock = tf
            return rdt._entry_direction_gate("NVDA", side, direction, mode, allow_counter_fade=allow_counter_fade)

    def test_with_trend_aligned_is_allowed(self):
        g = self._gate("LONG", "long")
        self.assertTrue(g["ok"])
        self.assertFalse(g["counter_trend"])

    def test_with_trend_not_aligned_is_blocked(self):
        g = self._gate("SHORT", "short", aligned=False)
        self.assertFalse(g["ok"])
        self.assertIn("not aligned", g["reason"])

    def test_no_clear_trend_is_blocked_even_if_aligned(self):
        for side in ("TWO_SIDED", "UNKNOWN", None, ""):
            self.assertIn("no clear trend", self._gate(side, "long")["reason"])

    def test_counter_trend_ride_is_blocked(self):
        g = self._gate("LONG", "short", mode="RIDE")
        self.assertFalse(g["ok"])
        self.assertIn("only for aligned fades", g["reason"])

    def test_counter_trend_fade_aligned_is_allowed_and_tagged(self):
        g = self._gate("LONG", "short", mode="FADE")
        self.assertTrue(g["ok"])
        self.assertTrue(g["counter_trend"])

    def test_counter_trend_fade_needs_a_failed_intraday_trend(self):
        g = self._gate("LONG", "short", mode="FADE", failed=False)
        self.assertFalse(g["ok"])
        self.assertIn("not confirmed failed", g["reason"])
        self.assertEqual(g["trend_failure"], NOT_FAILED)

    def test_with_trend_entry_never_needs_trend_failure(self):
        g = self._gate("LONG", "long", failed=False)
        self.assertTrue(g["ok"])
        self.assertFalse(self._tf_mock.called)

    def test_counter_trend_fade_not_aligned_is_blocked(self):
        self.assertFalse(self._gate("LONG", "short", mode="FADE", aligned=False)["ok"])

    def test_counter_trend_fade_blocked_after_reversal_criterion(self):
        g = self._gate("LONG", "short", mode="FADE", ct_ok=(False, "counter-trend fades disabled: 3 consecutive losses"))
        self.assertFalse(g["ok"])
        self.assertIn("disabled", g["reason"])

    def test_track_b_never_gets_the_counter_trend_exception(self):
        self.assertFalse(self._gate("LONG", "short", mode="FADE", allow_counter_fade=False)["ok"])

    def test_bad_direction_is_blocked(self):
        self.assertIn("invalid", self._gate("LONG", None)["reason"])


class RunTickTrackA(unittest.TestCase):
    """Drives run_tick with function-level patches on the real modules (the RunnerWindowGate pattern) —
    never a sys.modules swap, which would unload modules other test files rely on."""

    def _run(self, side, direction, mode=None, failed=True, log_ok=True, track_m_result=None):
        import contextlib
        placed, logged = [], []
        acct = SimpleNamespace(equity=2500.0, last_equity=2500.0, buying_power=9000.0)
        decision = {"would_consider": True, "side": side}
        trigger = {"trigger": "ENTER", "direction": direction, "entry_ref": 100.0, "mode": mode}
        patches = [
            mock.patch.object(rdt, "_clock_state", return_value=("open", 300.0)),
            mock.patch.object(rdt, "_touch_heartbeat"),
            mock.patch.object(rdt, "_maybe_sample_prices"),
            mock.patch.object(rdt, "_alignment_for", return_value=ALIGNED),
            mock.patch.object(rdt, "_counter_trend_fades_ok", return_value=(True, "ok")),
            mock.patch.object(rdt, "_trend_failure_for", return_value=FAILED if failed else NOT_FAILED),
            mock.patch("execution.broker.get_account", return_value=acct),
            mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 0}),
            mock.patch("execution.day_trade_manager.tier_kill_check", return_value=False),
            mock.patch("execution.day_trade_manager._account_entry_halt_reason", return_value=None),
            mock.patch("execution.day_trade_manager.bar_id_for", return_value="20261005-1000"),
            mock.patch("execution.day_trade_manager.place_entry",
                       side_effect=lambda sym, dec, trg, *a, **k: placed.append((sym, trg, k)) or True),
            mock.patch("execution.risk_manager.RiskManager"),
            mock.patch("strategy.day_tier_decision.compute_day_tier_decision", return_value=decision),
            mock.patch("strategy.day_tier_entry_trigger.compute_entry_trigger", return_value=trigger),
            mock.patch("strategy.day_tier_sizing.compute_day_tier_size", return_value={"size_ok": True}),
            mock.patch("strategy.day_tier_logger.log_decision",
                       side_effect=lambda did, sym, **kw: logged.append((did, sym, kw)) or log_ok),
            mock.patch.object(config, "DAYTRADE_ENABLED", True),
            mock.patch.object(config, "DAYTRADE_TRACK_B_ENABLED", False),
            mock.patch.object(config, "DAYTRADE_UNIVERSE", ["NVDA"]),
        ]
        if track_m_result is not None:
            patches.extend([
                mock.patch.object(config, "DAYTRADE_TRACK_M_ENABLED", True),
                mock.patch.object(rdt, "_run_track_m", return_value=track_m_result),
            ])
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
        self.assertEqual([p[0] for p in placed], ["NVDA"])
        self.assertFalse(placed[0][1]["counter_trend"])
        self.assertEqual(out["entered"], 1)
        self.assertEqual(logged, [])

    def test_track_m_router_denial_does_not_block_track_a(self):
        out, placed, _ = self._run(
            "SHORT", "short", track_m_result=(0, "router_denied")
        )
        self.assertEqual(out["track_m_note"], "router_denied")
        self.assertEqual(out["entered"], 1)
        self.assertEqual([p[0] for p in placed], ["NVDA"])

    def test_counter_trend_fade_reaches_place_entry_tagged(self):
        out, placed, logged = self._run("LONG", "short", mode="FADE")
        self.assertEqual(out["entered"], 1)
        self.assertTrue(placed[0][1]["counter_trend"])
        self.assertIn("alignment", placed[0][1])
        self.assertEqual(placed[0][1]["trend_failure"], FAILED)
        # the counter-trend tag was written durably BEFORE the order, under the decision_id place_entry stamps
        did = placed[0][2]["decision_id"]
        self.assertTrue(did.startswith("CT-NVDA-"))
        self.assertEqual(logged[0][0], did)
        self.assertTrue(logged[0][2]["trigger"]["counter_trend"])

    def test_fade_not_placed_when_its_tag_cannot_be_written(self):
        out, placed, logged = self._run("LONG", "short", mode="FADE", log_ok=False)
        self.assertEqual(placed, [])
        self.assertEqual(out["entered"], 0)

    def test_with_trend_entry_writes_no_intent_record(self):
        out, placed, logged = self._run("SHORT", "short")
        self.assertNotIn("decision_id", placed[0][2])
        self.assertEqual(logged, [])

    def test_fade_of_an_intact_trend_is_skipped_and_logged(self):
        out, placed, logged = self._run("LONG", "short", mode="FADE", failed=False)
        self.assertEqual(placed, [])
        self.assertEqual(out["entered"], 0)
        trg = logged[0][2]["trigger"]
        self.assertIn("not confirmed failed", trg["skip_reason"])
        self.assertEqual(trg["trend_failure"], NOT_FAILED)


if __name__ == "__main__":
    unittest.main()
