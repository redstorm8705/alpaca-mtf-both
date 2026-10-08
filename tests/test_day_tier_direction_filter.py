#!/usr/bin/env python3
# ruff: noqa: E501
"""Day tier never trades against the Layer-A trend side; no clear trend = no trade (Rafael 2026-10-03). A
counter-trend FADE is the one exception: its 2m/5m indicators must agree AND today's intraday trend must have
failed (15m lead + structure break, 30m confirmation — Rafael 2026-10-04).

Live evidence 2026-09-15..10-02: trades opposite the trend side or on TWO_SIDED went 0/13 (-$32.93);
aligned trades 3/6 (-$3.06). The rule is enforced in run_day_tier.run_tick for Track A and Track B.

2026-10-06 CEO order ("the day tier must trade"): the gate's verdict is recorded on the entry but no longer
blocks unless config.DAYTRADE_ALIGN_GATE_BLOCKS is True. The blocking tests below run with blocks=True."""
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

    def _run(self, side, direction, mode=None, failed=True, log_ok=True, track_m_result=None, blocks=True,
             watch_day=True, held=frozenset(), wall_ref=None, inverse=(None, "no free liquid inverse ETF for this stock"),
             exposure=None):
        import contextlib
        placed, logged = [], []
        acct = SimpleNamespace(equity=2500.0, last_equity=2500.0, buying_power=9000.0)
        decision = {"would_consider": True, "side": side}
        trigger = {"trigger": "ENTER", "direction": direction, "entry_ref": 100.0, "mode": mode}
        if wall_ref is not None:
            trigger.update(wall_ref=wall_ref, symbol="NVDA")   # the live trigger carries its symbol
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
            mock.patch.object(config, "DAYTRADE_ALIGN_GATE_BLOCKS", blocks, create=True),
            # watch-day rule (2026-10-06) is unit-tested in tests/test_day_tier_watch_day.py; offline here
            mock.patch.object(rdt, "_watch_day_ok", return_value=(watch_day, "watch day (test)")),
            mock.patch.object(rdt, "_held_by_other_tiers", return_value=held),
            mock.patch.object(rdt, "_prior_close", return_value=None),            # first-order stop math offline
            mock.patch.object(rdt, "_day_tier_exposure",
                              return_value=None if exposure == "unreadable" else dict(exposure or {})),
            mock.patch.object(rdt, "_inverse_pivot", return_value=inverse),
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


    # ── CEO order 2026-10-06: "the day tier must trade" — the gate RECORDS its verdict, it does not block ──────
    def test_default_gate_records_but_does_not_block(self):
        with mock.patch.object(config, "DAYTRADE_ALIGN_GATE_BLOCKS", None, create=True):
            self.assertFalse(rdt._gate_blocks())          # anything but an explicit True = not blocking
        for side, direction, mode, failed in (("LONG", "short", None, True), ("TWO_SIDED", "short", None, True),
                                              ("LONG", "short", "FADE", False)):
            out, placed, logged = self._run(side, direction, mode=mode, failed=failed, blocks=False)
            self.assertEqual([p[0] for p in placed], ["NVDA"], (side, direction, mode))
            self.assertEqual(out["entered"], 1)
            trg = placed[0][1]
            self.assertFalse(trg["gate_ok"])
            self.assertTrue(trg["gate_reason"])
            self.assertEqual(trg["counter_trend"], side == "LONG")   # against a clear daily side -> tagged

    def test_counter_trend_trade_still_writes_its_intent_record_first(self):
        out, placed, logged = self._run("LONG", "short", blocks=False)
        self.assertTrue(placed[0][2]["decision_id"].startswith("CT-NVDA-"))
        self.assertEqual(logged[0][0], placed[0][2]["decision_id"])
        _, placed, _ = self._run("LONG", "short", blocks=False, log_ok=False)
        self.assertEqual(placed, [])                      # never-mask-a-loss: no durable tag -> no order

    def test_counter_trend_short_without_a_watch_day_is_skipped_and_logged(self):
        out, placed, logged = self._run("LONG", "short", blocks=False, watch_day=False)
        self.assertEqual(placed, [])
        self.assertEqual(out["entered"], 0)
        self.assertIn("watch day", logged[-1][2]["trigger"]["skip_reason"])

    def test_counter_trend_short_after_a_watch_day_is_tagged(self):
        out, placed, _ = self._run("LONG", "short", blocks=False, watch_day=True)
        self.assertEqual(out["entered"], 1)
        self.assertTrue(placed[0][1]["watch_day"])


    def test_symbol_held_by_another_tier_long_routes_to_the_2x_etf(self):
        from data.live_price import LivePrice
        lp = {"NVDA": LivePrice(100.0, "iex_trade", 1.0), "NVDL": LivePrice(40.0, "iex_trade", 1.0)}
        with mock.patch("data.live_price.live_price", side_effect=lambda s, **k: lp.get(s)):
            out, placed, _ = self._run("LONG", "long", mode="RIDE", blocks=False, held=frozenset({"NVDA"}),
                                       wall_ref=98.0)
        self.assertEqual([p[0] for p in placed], ["NVDL"])
        self.assertEqual(placed[0][1]["underlying"], "NVDA")

    def test_symbol_held_by_another_tier_short_without_an_inverse_route_is_skipped(self):
        out, placed, logged = self._run("SHORT", "short", blocks=False, held=frozenset({"NVDA"}))
        self.assertEqual(placed, [])
        self.assertIn("another tier holds this symbol", logged[-1][2]["trigger"]["skip_reason"])
        self.assertIn("no free liquid inverse", logged[-1][2]["trigger"]["skip_reason"])

    def test_symbol_held_by_another_tier_short_buys_the_inverse_etf(self):
        dec = {"symbol": "NVD", "underlying": "NVDA", "signal_direction": "short"}
        trg = {"symbol": "NVD", "trigger": "ENTER", "direction": "long", "signal_direction": "short",
               "instrument": "inverse_etf", "underlying": "NVDA", "entry_ref": 3.3, "wall_ref": 3.17}
        out, placed, _ = self._run("SHORT", "short", blocks=False, held=frozenset({"NVDA"}),
                                   inverse=((dec, trg, {"size_ok": True, "shares": 50}, "NVD"), "inverse route -> NVD"))
        self.assertEqual([p[0] for p in placed], ["NVD"])
        self.assertEqual((placed[0][1]["direction"], placed[0][1]["signal_direction"]), ("long", "short"))
        self.assertEqual(out["entered"], 1)

    def test_not_held_short_shorts_the_stock_without_the_inverse_route(self):
        out, placed, _ = self._run("SHORT", "short", blocks=False,
                                   inverse=(({}, {"symbol": "NVD", "direction": "long"}, {"size_ok": True}, "NVD"), "x"))
        self.assertEqual([(p[0], p[1]["direction"]) for p in placed], [("NVDA", "short")])   # route offered, not used

    def test_one_direction_per_stock(self):
        # the day tier already holds NVD (short NVDA exposure): a long NVDA entry is refused and logged
        out, placed, logged = self._run("LONG", "long", blocks=False, exposure={"NVDA": -1})
        self.assertEqual(placed, [])
        self.assertIn("opposite direction on NVDA", logged[-1][2]["trigger"]["skip_reason"])
        # same direction: one day-tier lot per stock (cold-2nd 2026-10-08 — NVDA must never stack on an open NVDL)
        out, placed, _ = self._run("LONG", "long", blocks=False, exposure={"NVDA": 1})
        self.assertEqual(placed, [])
        # a different stock is unaffected
        out, placed, _ = self._run("LONG", "long", blocks=False, exposure={"AAPL": 1})
        self.assertEqual([p[0] for p in placed], ["NVDA"])

    def test_unreadable_exposure_does_not_block(self):
        out, placed, _ = self._run("LONG", "long", blocks=False, exposure="unreadable")
        self.assertEqual([p[0] for p in placed], ["NVDA"])


class InversePivotReal(unittest.TestCase):
    """The real _inverse_pivot (every run_tick harness patches it out): argument order + gates, data mocked."""

    def _run(self, quote=None, vol=900_000.0, etf_prev=10.0, size_ok=True):
        from data.live_price import LivePrice
        lp = {"NVDA": LivePrice(95.0, "iex_trade", 1.0), "NVD": LivePrice(11.0, "iex_trade", 1.0)}
        trig = {"symbol": "NVDA", "trigger": "ENTER", "direction": "short", "mode": "RIDE", "entry_ref": 95.0,
                "wall_ref": 96.9, "target": None, "reason": "t"}
        with mock.patch("data.live_price.live_price", side_effect=lambda s, **k: lp.get(s)), \
                mock.patch.object(rdt, "_prior_close", side_effect=lambda s: {"NVDA": 100.0, "NVD": etf_prev}.get(s)), \
                mock.patch.object(rdt, "_today_iex_volume", return_value=vol), \
                mock.patch("data.alpaca_data.get_latest_quote", return_value=quote or {"bid": 10.99, "ask": 11.0}), \
                mock.patch("strategy.day_tier_sizing.compute_day_tier_size",
                           return_value={"size_ok": size_ok, "shares": 40, "reason": "r"}):
            return rdt._inverse_pivot("NVDA", {"symbol": "NVDA"}, trig, set(), 2500.0, 9000.0, "A")

    def test_route_ok(self):
        res, why = self._run()
        dec, trg, size, etf = res
        self.assertEqual((etf, trg["symbol"], trg["direction"], trg["signal_direction"]), ("NVD", "NVD", "long", "short"))
        self.assertAlmostEqual(trg["wall_ref"], round(11.0 * (1 - 0.038 / 1.10), 4))
        self.assertIn("tracking ok", trg["tracking"])

    def test_each_gate_blocks(self):
        self.assertIsNone(self._run(etf_prev=10.6)[0])                               # tracking off
        self.assertIsNone(self._run(vol=1000.0)[0])                                  # thin
        self.assertIsNone(self._run(quote={"bid": 10.80, "ask": 11.0})[0])           # wide
        self.assertIsNone(self._run(size_ok=False)[0])


if __name__ == "__main__":
    unittest.main()
