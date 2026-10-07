#!/usr/bin/env python3
# ruff: noqa: E501  — mock-heavy fixtures run long (matches tests/ convention)
"""
tests/test_day_tier_track_b_live.py — Track B Increment-2 PART 2 (live wiring, RISK-PATH).
Design: logs/design_records/day_tier_track_b_inc2_2026-09-22.md (PART 2 + the 2026-09-23 corrections).

Guards the Part-2 behaviors a regression could silently break:
  1. TRACK-B EXPOSURE CAP — _bounded_entry_qty caps a Track-B entry at its budget share count (requested_qty)
     and open Track-B notional at the budget; Track A keeps risk-basis sizing; min()-only; honors
     DAYTRADE_TRACK_B_CASH_ONLY (an exposure cap, not a funding rule).
  2. place_entry threads the track: a Track-B size dict submits the budget-capped qty and stamps track="B" on
     the durable entry_fill + the state record; a Track-A dict is unchanged (track="A").
  3. _track_b_daily_context: prior_close from SETTLED SIP (end = today 00:00 ET), ADV from IEX (same basis as
     the IEX frame's volume), today's bar dropped, once-per-day file cache, fail-safe (None, None).
  4. fetch_bars_window: feed "iex" maps to DataFeed.IEX; an unknown feed is honest-empty (no client call).
Plus the durable-log stamp (log_entry_fill/open_trades_from_log) and the runner's window gate.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import config
import run_day_tier
from execution import broker
from execution import day_trade_manager as dtm

ET = ZoneInfo("America/New_York")


def _bounded(requested, track="A", order_price=70.0, stop_price=69.0, equity=2500.0, bp=4000.0, symbol="UBER",
             track_budget=131.25, open_trades=None, positions=None):
    """A wide-room account (no positions/orders, zero maintenance) so ONLY risk/track caps can bind."""
    return dtm._bounded_entry_qty(requested, order_price, stop_price, equity, open_trades or {},
                                  {p.symbol: p for p in (positions or [])}, bp, 0.0, 0.30, [],
                                  risk_equity=equity, symbol=symbol, track=track, track_budget=track_budget)


class TrackBBudgetCap(unittest.TestCase):
    def test_track_b_is_capped_at_budget_share_count(self):
        # risk-basis: 1.5% x 2500 / $1 stop = 37 sh; thin-name cap $1000/$70 = 14 sh -> Track A would wire 14.
        qty_a, _ = _bounded(1, track="A")
        qty_b, why_b = _bounded(1, track="B")
        self.assertGreater(qty_a, 1)
        self.assertEqual(qty_b, 1)
        self.assertIn("track-B budget cap", why_b)

    def test_share_count_cap_binds_when_budget_affords_more(self):
        # T1: a $500 budget affords 7 shares at $70, but the sizing asked for 1 -> wired 1 (the share-count cap).
        self.assertEqual(_bounded(1, track="B", track_budget=500.0)[0], 1)
        # And the budget-room cap binds when it is the smaller one: 3 requested, $150 budget -> 2 at $70.
        self.assertEqual(_bounded(3, track="B", track_budget=150.0)[0], 2)

    def test_cap_is_min_only_never_upsizes(self):
        # A very wide stop -> risk_qty (0) below requested (3): the cap must not raise it.
        qty_b, _ = _bounded(3, track="B", stop_price=20.0)
        self.assertEqual(qty_b, 0)

    def test_cap_honors_the_config_switch(self):
        with mock.patch.object(config, "DAYTRADE_TRACK_B_CASH_ONLY", False):
            qty_b, _ = _bounded(1, track="B")
        self.assertGreater(qty_b, 1)  # board-gated switch off -> Track-A-style risk sizing

    def test_non_bool_switch_keeps_cap_on(self):
        for junk in ("False", None, 0, ""):  # only an explicit False disables the cap
            with mock.patch.object(config, "DAYTRADE_TRACK_B_CASH_ONLY", junk):
                self.assertEqual(_bounded(1, track="B")[0], 1, repr(junk))

    def test_open_track_b_notional_consumes_the_budget(self):
        # 1 NFLX B lot open at $72 -> $59.25 of the $131.25 budget left -> room 0 sh at $70. CEO order 2026-10-06:
        # the Track-B budget never cuts an entry below ONE share (account rooms + daily budget still bound it).
        open_b = {"t1": {"symbol": "NFLX", "track": "B", "fill_qty": 1, "entry_price": 72.0}}
        self.assertEqual(_bounded(1, track="B", open_trades=open_b)[0], 1)
        # ... but it still SHRINKS a larger request: 3 requested, $59.25 room at $70 -> floored at 1, not 3.
        self.assertEqual(_bounded(3, track="B", open_trades=open_b)[0], 1)
        # An open Track-A lot does NOT consume Track B's budget (a small one, so the shared day-tier gross
        # room — equity x 0.60 for a thin name — is not what binds).
        open_a = {"t2": {"symbol": "MSFT", "track": "A", "fill_qty": 1, "entry_price": 100.0}}
        self.assertEqual(_bounded(1, track="B", open_trades=open_a)[0], 1)

    def test_state_only_b_lot_counts_against_the_budget(self):
        # Risk seat R2: a B lot missing from the durable log (failed log write) but present in state still
        # consumes the Track-B budget (a 3-share request is floored at 1, never 0); the same lot does not touch Track A.
        extra = {"entry::NFLX::x": {"symbol": "NFLX", "track": "B", "fill_qty": 1, "entry_price": 72.0}}
        # the state-only lot still COUNTS (3 requested -> budget room 0 -> floored at 1 share, CEO order 2026-10-06)
        self.assertEqual(dtm._bounded_entry_qty(3, 70.0, 69.0, 2500.0, {}, {}, 4000.0, 0.0, 0.30, [],
                                                risk_equity=2500.0, symbol="UBER", track="B",
                                                track_budget=131.25, extra_b_lots=extra)[0], 1)
        qa = dtm._bounded_entry_qty(1, 70.0, 69.0, 2500.0, {}, {}, 4000.0, 0.0, 0.30, [], risk_equity=2500.0,
                                    symbol="UBER", track="A", extra_b_lots=extra)[0]
        self.assertEqual(qa, _bounded(1, track="A")[0])

    def test_budget_measured_at_order_price_closes_slippage_overshoot(self):
        # budget $131.25 affords 1 share at entry_ref $131.00, but the marketable limit is $131.26 -> 0 by the budget;
        # CEO order 2026-10-06: the Track-B budget never cuts below ONE share -> 1 (rooms/daily budget still bound it).
        self.assertEqual(_bounded(1, track="B", order_price=131.26, stop_price=129.0)[0], 1)
        self.assertEqual(_bounded(2, track="B", order_price=131.26, stop_price=129.0)[0], 1)   # still shrinks 2 -> 1

    def test_missing_budget_fails_closed(self):
        for bad in (None, 0, -5, float("nan"), "x"):
            self.assertEqual(_bounded(1, track="B", track_budget=bad)[0], 0, repr(bad))

    def test_unknown_track_is_track_a(self):
        self.assertEqual(_bounded(1, track="Z")[0], _bounded(1, track="A")[0])
        self.assertEqual(_bounded(1, track=None)[0], _bounded(1, track="A")[0])  # type: ignore[arg-type]

    def test_track_b_lowercase_is_capped(self):
        self.assertEqual(_bounded(1, track="b")[0], 1)


class PlaceEntryTrackThreading(unittest.TestCase):
    """place_entry: Track B submits the budget-capped qty and stamps track B; Track A's code path unchanged."""

    def _run(self, track, decision_track=None, state=None, open_log=None, events=None, events_ok=True,
             *, fill_price=70.1, trigger_override=None, flatten_ok=True, exit_recorded=None,
             stop_state=False, pending_close=False, emergency_cancelled=True,
             emergency_fill_qty=0.0, emergency_readable=True, live_net_covers=True,
             emergency_submit_status="live", pin_fallback=False, positions=None):
        state = {} if state is None else state
        self.last_state = state
        submitted: dict = {}
        logged: dict = {}
        acct = SimpleNamespace(buying_power="4000", equity="2500", last_equity="2500", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)

        def _submit_limit(symbol, qty, side, px, tier=None, client_order_id=None):
            submitted["qty"] = qty
            return SimpleNamespace(id="ENT1", client_order_id=client_order_id or "DT-UBER-b-1-x")

        def _log_entry_fill(trade_id, symbol, **kw):
            logged.update(kw)
            return True

        def _final_fill(_oid):
            return float(submitted.get("qty", 0)), fill_price

        pos = SimpleNamespace(symbol="UBER", current_price=70.0, qty=1, side="long", avg_entry_price=70.0)
        oco = SimpleNamespace(id="TP1", legs=[SimpleNamespace(id="ST1", order_type="stop", type="stop")])
        oco_submit = mock.Mock(return_value=oco)
        self.last_oco_submit = oco_submit
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "DRIVE", "entry_ref": 70.0,
                   "target": None, "wall_ref": 69.0}
        if trigger_override:
            trigger.update(trigger_override)
        size = {"size_ok": True, "shares": 1, "budget": 131.25}
        if track is not None:
            size["track"] = track

        def _flatten(*_args, **_kwargs):
            if pending_close:
                rec = next(v for k, v in state.items() if k.startswith("entry::"))
                rec["pending_exit_order_id"] = "PENDING_CLOSE"
            return flatten_ok

        def _record_emergency_stop(_target):
            if emergency_fill_qty > 0:
                rec = next(v for k, v in state.items() if k.startswith("entry::"))
                owned = int(float(rec.get("fill_qty") or rec.get("qty") or 0))
                rec["fill_qty"] = max(0, owned - int(emergency_fill_qty))
            return False

        flattened = mock.Mock(side_effect=_flatten)
        self.last_flatten = flattened
        emergency_obj = SimpleNamespace(id="EMERGENCY_STOP") if emergency_submit_status == "live" else None
        emergency_stop = mock.Mock(return_value=(emergency_submit_status, emergency_obj))
        self.last_emergency_stop = emergency_stop
        halt = mock.Mock()
        self.last_halt = halt
        cancel_confirmed = mock.Mock(return_value=emergency_cancelled)
        self.last_cancel_confirmed = cancel_confirmed
        patches = [
            mock.patch.object(dtm, "_enabled", return_value=True),
            mock.patch.object(dtm, "_load_state", side_effect=lambda: state),
            mock.patch.object(dtm, "_save_state", return_value=True),
            mock.patch.object(dtm, "_min_stop_room_ok", return_value=(True, "ok")),
            # 2026-10-06: place_entry widens via _room_stop and prices off live_price — both offline here
            mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _lim, stop: (stop, "room stop kept")),
            mock.patch("data.live_price.live_price", return_value=None),
            # 2026-10-07 touch pricing + terminal-entry wait — offline here (exercised in tests/test_day_tier_entry_fill.py)
            mock.patch("data.alpaca_data.get_latest_quote", return_value=None),
            mock.patch.object(dtm, "_await_entry_terminal", return_value=True),
            # the crossed-target safety tests below exercise the flatten machinery (fallback OFF); see
            # test_pin_fallback_turns_a_crossed_target_into_an_r_multiple_bracket for the default (ON) behaviour
            mock.patch.object(config, "DAYTRADE_FADE_PIN_FALLBACK", pin_fallback, create=True),
            mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None),
            mock.patch.object(dtm, "_confirm_fill", return_value=True),
            mock.patch.object(dtm, "_final_fill", side_effect=_final_fill),
            mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value=open_log or {}),
            mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(list(events or []), events_ok)),
            mock.patch("strategy.day_tier_logger.log_decision", return_value=True),
            mock.patch("strategy.day_tier_logger.log_entry_fill", side_effect=_log_entry_fill),
            mock.patch("strategy.day_tier_logger.log_stop_placed", return_value=True),
            mock.patch("strategy.day_tier_logger.log_target_placed", return_value=True),
            mock.patch("trade_logger.log_event", return_value=True),
            mock.patch.object(broker, "get_account", return_value=acct),
            mock.patch.object(broker, "get_open_positions", return_value=list(positions or [])),
            mock.patch.object(broker, "get_open_orders", return_value=[]),
            mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30),
            mock.patch.object(broker, "submit_limit_order", side_effect=_submit_limit),
            mock.patch.object(broker, "cancel_open_orders_for_symbol", return_value=0),
            mock.patch.object(broker, "get_open_position", return_value=pos),
            mock.patch.object(broker, "submit_oco_exit", oco_submit),
            mock.patch.object(dtm, "flatten_position", flattened),
            mock.patch.object(dtm, "_submit_verified_plain_stop", emergency_stop),
            mock.patch.object(dtm, "_durable_exit_recorded",
                              return_value=(flatten_ok if exit_recorded is None else exit_recorded)),
            mock.patch.object(dtm, "_halt_unresolved_exit", halt),
            mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=stop_state),
            mock.patch.object(dtm, "_cancel_daytrade_exit_legs", return_value=None),
            mock.patch.object(broker, "cancel_stop_confirmed", cancel_confirmed),
            mock.patch.object(dtm, "_confirmed_order_fill",
                              return_value=(emergency_readable, emergency_fill_qty, 338.92)),
            mock.patch.object(dtm, "_record_confirmed_stop_exit",
                              side_effect=_record_emergency_stop),
            mock.patch.object(dtm, "_live_net_covers_owned", return_value=live_net_covers),
        ]
        with ExitStack() as stack:  # >20 nested `with` items is a SyntaxError on the OCI py3.10 target
            for p in patches:
                stack.enter_context(p)
            ok = dtm.place_entry("UBER", {"would_consider": True, "track": decision_track or track}, trigger, size,
                                 bar_id="20260923-1000", equity=2500.0)
        rec = next((v for k, v in state.items() if k.startswith("entry::") and v.get("symbol") == "UBER"), {})
        return ok, submitted.get("qty"), logged.get("track"), rec.get("track")

    def test_track_b_submits_budget_capped_qty_and_stamps_b(self):
        ok, qty, log_track, state_track = self._run("B")
        self.assertTrue(ok)
        self.assertEqual(qty, 1)            # the budget share count, NOT the ~14-sh risk size
        self.assertEqual(log_track, "B")
        self.assertEqual(state_track, "B")

    def test_short_uses_alpacas_short_maintenance_rate(self):
        # $10 short, posted (long) rate 30% -> Alpaca short rule: greater of $5/share or 30% = 50% (board 2026-10-07)
        from execution import day_trade_manager as dtm
        spy = mock.Mock(wraps=dtm._bounded_entry_qty)
        with mock.patch.object(dtm, "_bounded_entry_qty", spy):
            self._run("A", fill_price=10.0, pin_fallback=True,
                      trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 10.0,
                                        "target": 9.5, "wall_ref": None})
        self.assertAlmostEqual(spy.call_args.args[8], 5.0 / 9.98, places=6)   # priced at the short limit (10 x 0.998)
        # a long at the same price keeps the posted rate
        spy.reset_mock()
        with mock.patch.object(dtm, "_bounded_entry_qty", spy):
            self._run("A", fill_price=10.0, pin_fallback=True,
                      trigger_override={"direction": "long", "mode": "FADE", "entry_ref": 10.0,
                                        "target": 10.5, "wall_ref": None})
        self.assertAlmostEqual(spy.call_args.args[8], 0.30)

    def test_short_under_two_fifty_is_skipped(self):
        from execution import day_trade_manager as dtm
        spy = mock.Mock(wraps=dtm._bounded_entry_qty)
        with mock.patch.object(dtm, "_bounded_entry_qty", spy):
            ok, qty, _, _ = self._run("A", fill_price=2.0, pin_fallback=True,
                                      trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 2.0,
                                                        "target": 1.9, "wall_ref": None})
        self.assertFalse(ok)
        spy.assert_not_called()

    def test_actual_fill_crossing_short_target_flattens_without_oco(self):
        # Mirrors the 2026-09-23 AAPL failure: signal geometry was valid at entry_ref=339, but the
        # short filled below its 338.76 target. The newly filled setup must close immediately.
        ok, _, _, _ = self._run(
            "A", fill_price=338.29,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.assertFalse(ok)
        self.last_flatten.assert_called_once()
        self.assertEqual(self.last_flatten.call_args.kwargs["reason"], "fill_invalidated_setup")
        self.assertEqual(self.last_emergency_stop.call_count, 1)  # valid stop before flatten
        self.last_oco_submit.assert_not_called()

    def test_pin_fallback_turns_a_crossed_target_into_an_r_multiple_bracket(self):
        # CEO order 2026-10-06 default: the fill passed the pin (short filled 338.29 below target 338.76) -> the
        # trade is KEPT with an R-multiple target on the profit side instead of being flattened.
        ok, _, _, _ = self._run(
            "A", fill_price=338.29, pin_fallback=True,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.assertTrue(ok)
        self.last_flatten.assert_not_called()
        self.last_oco_submit.assert_called_once()
        tp = self.last_oco_submit.call_args.args[3]
        self.assertLess(tp, 338.29)                      # a short target below the fill

    def test_failed_invalid_geometry_flatten_remains_reconcilable(self):
        self._run(
            "A", fill_price=338.29, flatten_ok=False,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        rec = next(v for k, v in self.last_state.items() if k.startswith("entry::"))
        self.assertEqual(rec["state"], "filled")  # _flatten_targets will retry it next tick
        self.assertIn("short target", rec["geometry_error"])
        self.assertEqual(self.last_emergency_stop.call_count, 2)  # before close + restored after failure
        self.last_halt.assert_called_once()

    def test_absent_position_without_durable_exit_never_terminalizes(self):
        # flatten_position can return True for an already-absent position. Without an exact
        # exit_fill that is unresolved ownership/P&L, not a successful close.
        self._run(
            "A", fill_price=338.29, flatten_ok=True, exit_recorded=False,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        rec = next(v for k, v in self.last_state.items() if k.startswith("entry::"))
        self.assertEqual(rec["state"], "filled")
        self.last_halt.assert_called_once()

    def test_stop_crossed_fills_flatten_without_submitting_invalid_emergency_stop(self):
        cases = [
            (340.0, {"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                     "target": 338.76, "wall_ref": None}),
            (98.5, {"direction": "long", "mode": "FADE", "entry_ref": 100.0,
                    "target": 101.0, "wall_ref": None}),
        ]
        for fill, trigger in cases:
            with self.subTest(direction=trigger["direction"]):
                self._run("A", fill_price=fill, flatten_ok=False, trigger_override=trigger)
                self.last_flatten.assert_called_once()
                self.last_emergency_stop.assert_not_called()
                self.last_oco_submit.assert_not_called()
                self.last_halt.assert_called_once()

    def test_live_first_emergency_stop_prevents_duplicate_restore(self):
        self._run(
            "A", fill_price=338.29, flatten_ok=False, stop_state=True,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.assertEqual(self.last_emergency_stop.call_count, 1)
        self.last_oco_submit.assert_not_called()

    def test_unconfirmed_emergency_stop_cancel_defers_market_close(self):
        self._run(
            "A", fill_price=338.29, emergency_cancelled=False,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.last_cancel_confirmed.assert_called_once_with("UBER", "EMERGENCY_STOP")
        self.last_flatten.assert_not_called()
        self.last_oco_submit.assert_not_called()
        self.last_halt.assert_called_once()

    def test_ambiguous_emergency_stop_submit_never_adds_market_reducer(self):
        self._run(
            "A", fill_price=338.29, emergency_submit_status="unknown",
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.last_emergency_stop.assert_called_once()
        self.last_cancel_confirmed.assert_not_called()
        self.last_flatten.assert_not_called()
        self.last_halt.assert_called_once()

    def test_unreadable_terminal_stop_fill_never_creates_second_reducer(self):
        self._run(
            "A", fill_price=338.29, emergency_readable=False,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.assertEqual(self.last_emergency_stop.call_count, 1)
        self.last_flatten.assert_not_called()
        self.last_halt.assert_called_once()

    def test_pending_close_prevents_reverse_capable_stop_restore(self):
        self._run(
            "A", fill_price=338.29, flatten_ok=False, pending_close=True,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.assertEqual(self.last_emergency_stop.call_count, 1)  # pre-close only; no competing restore
        self.last_halt.assert_called_once()

    def test_partial_emergency_stop_fill_protects_only_proven_residual(self):
        state = {}
        self._run(
            "A", state=state, fill_price=338.29, emergency_fill_qty=1.0,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.assertEqual(self.last_emergency_stop.call_count, 2)
        first_qty = self.last_emergency_stop.call_args_list[0].args[1]
        self.assertEqual(self.last_emergency_stop.call_args.args[1], first_qty - 1)
        self.last_flatten.assert_not_called()
        self.last_halt.assert_called_once()

    def test_restore_is_suppressed_when_live_net_no_longer_reduces(self):
        self._run(
            "A", fill_price=338.29, flatten_ok=False, live_net_covers=False,
            trigger_override={"direction": "short", "mode": "FADE", "entry_ref": 339.0,
                              "target": 338.76, "wall_ref": None},
        )
        self.assertEqual(self.last_emergency_stop.call_count, 1)  # pre-close only
        self.last_halt.assert_called_once()

    def test_state_only_b_lot_consumes_budget_through_place_entry(self):
        # Risk seat R3: a same-day FILLED Track-B NFLX lot whose log write failed (not in open_trades_from_log)
        # must use up the budget -> the $70 UBER Track-B entry wires 0 (no order submitted).
        today = datetime.now(ET).strftime("%Y%m%d")
        st = {f"entry::NFLX::{today}-0945": {"symbol": "NFLX", "track": "B", "state": "filled", "coid": "DT-NFLX-x",
                                             "bar_id": f"{today}-0945", "fill_qty": 1, "fill_px": 72.0, "side": "long"}}
        ok, qty, _, _ = self._run("B", state=st)
        self.assertTrue(ok)          # CEO order 2026-10-06: the budget shrinks to ONE share, never to a skip
        self.assertEqual(qty, 1)

    def test_logged_b_lot_is_counted_once_not_twice(self):
        # The same lot present in BOTH the log and state is counted once. Order price = round(70*1.002,2) = 70.14.
        # Once: (131.25 - 40) / 70.14 = 1.30 -> 1 share. Twice: (131.25 - 80) / 70.14 = 0.73 -> 0 shares.
        today = datetime.now(ET).strftime("%Y%m%d")
        st = {f"entry::SMCI::{today}-0945": {"symbol": "SMCI", "track": "B", "state": "protected", "coid": "DT-SMCI-x",
                                             "bar_id": f"{today}-0945", "fill_qty": 1, "fill_px": 40.0, "side": "long"}}
        log = {"DT-SMCI-x": {"trade_id": "DT-SMCI-x", "symbol": "SMCI", "track": "B", "fill_qty": 1,
                             "entry_price": 40.0, "side": "long"}}
        ok, qty, _, _ = self._run("B", state=st, open_log=log)
        self.assertTrue(ok)
        self.assertEqual(qty, 1)

    def test_exited_b_lot_no_longer_consumes_the_budget(self):
        # Cold-2nd R1: a Track-B NFLX lot that already EXITED today (entry_fill + exit_fill logged, so not in
        # the open set) keeps a "protected" state record; it must NOT use the budget -> UBER wires 1.
        today = datetime.now(ET).strftime("%Y%m%d")
        st = {f"entry::NFLX::{today}-0945": {"symbol": "NFLX", "track": "B", "state": "protected", "coid": "DT-NFLX-x",
                                             "bar_id": f"{today}-0945", "fill_qty": 1, "fill_px": 100.0, "side": "long"}}
        now = datetime.now(ET).isoformat()
        ev = [{"event": "entry_fill", "trade_id": "DT-NFLX-x", "ts": now},
              {"event": "exit_fill", "trade_id": "DT-NFLX-x", "ts": now, "realized_pnl": 0.0}]
        ok, qty, _, _ = self._run("B", state=st, events=ev)
        self.assertTrue(ok)
        self.assertEqual(qty, 1)

    def test_unreadable_log_counts_state_lots_fail_closed(self):
        today = datetime.now(ET).strftime("%Y%m%d")
        st = {f"entry::NFLX::{today}-0945": {"symbol": "NFLX", "track": "B", "state": "protected", "coid": "DT-NFLX-x",
                                             "bar_id": f"{today}-0945", "fill_qty": 1, "fill_px": 100.0, "side": "long"}}
        # Even with the lot's entry_fill+exit_fill present, an UNREADABLE log means exits cannot be proven ->
        # the same-day record is counted (over-count, fail-closed) -> the $70 UBER entry wires 0.
        ev = [{"event": "entry_fill", "trade_id": "DT-NFLX-x"}, {"event": "exit_fill", "trade_id": "DT-NFLX-x"}]
        ok, qty, _, _ = self._run("B", state=st, events=ev, events_ok=False)
        self.assertFalse(ok)
        self.assertIsNone(qty)

    def test_size_track_b_alone_still_caps(self):
        # T2: size dict says "B", decision says "A" -> still the Track-B cap + stamp (either dict routes to B).
        ok, qty, log_track, state_track = self._run("B", decision_track="A")
        self.assertTrue(ok)
        self.assertEqual(qty, 1)
        self.assertEqual(log_track, "B")
        self.assertEqual(state_track, "B")

    def test_decision_track_b_alone_still_caps(self):
        # Defense in depth: a size dict missing "track" but a Track-B decision still routes to the B cap.
        ok, qty, log_track, _ = self._run(None, decision_track="B")
        self.assertTrue(ok)
        self.assertEqual(qty, 1)
        self.assertEqual(log_track, "B")

    def test_same_side_co_hold_with_another_tier_is_skipped(self):
        # 2026-10-07 interim: a live same-side position the day tier does not own -> no entry (no co-hold)
        other = SimpleNamespace(symbol="UBER", side="long", qty="3", current_price=70.0, market_value=210.0)
        ok, qty, _, _ = self._run("A", positions=[other])
        self.assertFalse(ok)
        self.assertIsNone(qty)

    def test_track_a_unchanged(self):
        ok, qty, log_track, state_track = self._run("A")
        self.assertTrue(ok)
        self.assertGreater(qty, 1)          # Track A keeps risk-basis sizing
        self.assertEqual(log_track, "A")
        self.assertEqual(state_track, "A")


class DailyContext(unittest.TestCase):
    @staticmethod
    def _daily(closes, vols, last_day):
        """Consecutive CALENDAR-day daily bars ending on `last_day` (midnight ET stamps, as Alpaca returns)."""
        idx = pd.date_range(end=pd.Timestamp(last_day, tz="America/New_York"), periods=len(closes),
                            freq="D").tz_convert("UTC")
        return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": vols},
                            index=idx)

    def setUp(self):
        run_day_tier._daily_ctx_cache.clear()
        self._td = TemporaryDirectory()
        self._file = Path(self._td.name) / "ctx.json"
        self._p = mock.patch.object(run_day_tier, "_DAILY_CTX_FILE", self._file)
        self._p.start()
        # Inject a controllable fake data.fetcher (like tests/test_day_tier_track_b.py) so these tests never
        # import the alpaca SDK — order-independent even when another module stubbed alpaca in sys.modules.
        self._orig_fetcher = sys.modules.get("data.fetcher")
        self._fake = types.ModuleType("data.fetcher")
        self._fake.fetch_bars_window = lambda *a, **k: pd.DataFrame()
        sys.modules["data.fetcher"] = self._fake
        self.now = datetime.now(ET).replace(hour=10, minute=15, second=0, microsecond=0)
        self.today = self.now.strftime("%Y-%m-%d")
        self.prev = (self.now - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        self._cal = mock.patch.object(run_day_tier, "_prev_session_date", return_value=self.prev)
        self._cal.start()

    def tearDown(self):
        self._cal.stop()
        if self._orig_fetcher is not None:
            sys.modules["data.fetcher"] = self._orig_fetcher
        else:
            sys.modules.pop("data.fetcher", None)
        self._p.stop()
        self._td.cleanup()
        run_day_tier._daily_ctx_cache.clear()

    def _fetch(self, fn):
        self._fake.fetch_bars_window = fn

    def _serve(self, sip, iex, calls=None):
        def _fbw(symbol, tf, start, end, feed="sip", adjustment="raw"):
            if calls is not None:
                calls.append((feed, adjustment, end))
            return sip if feed == "sip" else iex
        self._fetch(_fbw)

    def test_split_adjusted_sip_close_iex_adv_today_dropped_and_cached(self):
        # 18 prior days + TODAY's forming bar (last row) — today must be dropped from both.
        sip = self._daily([100.0] * 16 + [101.0, 102.0, 999.0], [1e8] * 19, self.today)
        iex = self._daily([100.1] * 18 + [999.0], [2e6] * 6 + [3e6] * 6 + [4e6] * 6 + [9e9], self.today)
        calls: list = []
        self._serve(sip, iex, calls)
        pc, adv = run_day_tier._track_b_daily_context("UBER", self.now)
        self.assertEqual(pc, 102.0)                       # SIP settled prior close, not today's 999
        self.assertAlmostEqual(adv, 3e6)                  # mean of the IEX prior days, not the SIP 1e8
        self.assertEqual({c[0] for c in calls}, {"sip", "iex"})
        self.assertEqual({c[1] for c in calls}, {"split"})  # today's share basis (split-consistent)
        for _feed, _adj, end in calls:
            self.assertEqual((end.hour, end.minute), (0, 0))  # settled history only (never a recent-SIP window)
        self.assertEqual(json.loads(self._file.read_text())["ctx"]["UBER"], [102.0, 3e6])
        self.assertEqual(json.loads(self._file.read_text())["basis"], run_day_tier._DAILY_CTX_BASIS)
        # A FRESH process (in-memory cache cleared) reads the day's file — no refetch.
        run_day_tier._daily_ctx_cache.clear()

        def _no_refetch(*a, **k):
            raise AssertionError("refetch")
        self._fetch(_no_refetch)
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (102.0, 3e6))

    def test_missing_previous_session_bar_is_rejected_and_not_cached(self):
        # Last settled bar is D-2 (D-1 missing) -> a two-day "gap" would be measured -> skip, never cache.
        d2 = (self.now - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        self._serve(self._daily([100.0] * 18, [1e8] * 18, d2), self._daily([100.0] * 18, [2e6] * 18, d2))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))
        self.assertFalse(self._file.exists())

    def test_only_sip_missing_previous_session_is_rejected(self):
        d2 = (self.now - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        self._serve(self._daily([100.0] * 18, [1e8] * 18, d2), self._daily([100.0] * 18, [2e6] * 18, self.prev))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_only_iex_missing_previous_session_is_rejected(self):
        d2 = (self.now - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        self._serve(self._daily([100.0] * 18, [1e8] * 18, self.prev), self._daily([100.0] * 18, [2e6] * 18, d2))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_adv_minimum_days_boundary(self):
        # 14 prior days -> rejected; 15 -> accepted (both feeds ending on the previous session).
        self._serve(self._daily([100.0] * 14, [1e8] * 14, self.prev), self._daily([100.0] * 14, [2e6] * 14, self.prev))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))
        run_day_tier._daily_ctx_cache.clear()
        self._serve(self._daily([100.0] * 15, [1e8] * 15, self.prev), self._daily([100.0] * 15, [2e6] * 15, self.prev))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (100.0, 2e6))

    def test_too_few_adv_days_rejected(self):
        self._serve(self._daily([100.0] * 10, [1e8] * 10, self.prev), self._daily([100.0] * 10, [2e6] * 10, self.prev))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_unreadable_calendar_fails_closed(self):
        self._serve(self._daily([100.0] * 18, [1e8] * 18, self.prev), self._daily([100.0] * 18, [2e6] * 18, self.prev))
        with mock.patch.object(run_day_tier, "_prev_session_date", return_value=None):
            self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_fetch_failure_is_fail_safe(self):
        self._fetch(lambda *a, **k: pd.DataFrame())
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

        def _down(*a, **k):
            raise RuntimeError("down")
        self._fetch(_down)
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_same_day_cache_on_another_basis_is_ignored(self):
        # T3: today's date but an OLD basis tag (e.g. a raw-close / SIP-volume draft) -> a miss, never served.
        today = self.now.strftime("%Y%m%d")
        self._file.write_text(json.dumps({"date": today, "basis": "old", "ctx": {"UBER": [1.0, 1.0]}}))
        self._fetch(lambda *a, **k: pd.DataFrame())
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_stale_day_cache_is_ignored(self):
        # A PRIOR day's file on the CURRENT basis (the realistic case) -> a miss on the date alone.
        self._file.write_text(json.dumps({"date": "19990101", "basis": run_day_tier._DAILY_CTX_BASIS,
                                          "ctx": {"UBER": [1.0, 1.0]}}))
        self._fetch(lambda *a, **k: pd.DataFrame())
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_asof_replay_never_writes_the_live_cache(self):
        past = datetime(2026, 9, 21, 10, 15, tzinfo=ET)
        with mock.patch.object(run_day_tier, "_prev_session_date", return_value="2026-09-20"):
            self._serve(self._daily([100.0] * 17 + [101.0], [1e8] * 18, "2026-09-20"),
                        self._daily([100.0] * 18, [2e6] * 18, "2026-09-20"))
            self.assertEqual(run_day_tier._track_b_daily_context("UBER", past), (101.0, 2e6))
        self.assertFalse(self._file.exists())


class PrevSession(unittest.TestCase):
    def test_last_calendar_date_before_today(self):
        run_day_tier._prev_session_cache.clear()
        cal = [{"date": "2026-09-18"}, {"date": "2026-09-21"}, {"date": "2026-09-22"}]
        with mock.patch("reporting.pnl_ledger._get_json", return_value=cal):
            self.assertEqual(run_day_tier._prev_session_date(datetime(2026, 9, 23, 10, 0, tzinfo=ET)), "2026-09-22")
        run_day_tier._prev_session_cache.clear()

    def test_calendar_error_is_none_and_remembered_for_the_process(self):
        run_day_tier._prev_session_cache.clear()
        with mock.patch("reporting.pnl_ledger._get_json", side_effect=RuntimeError("down")) as gj:
            self.assertIsNone(run_day_tier._prev_session_date(datetime(2026, 9, 23, 10, 0, tzinfo=ET)))
            self.assertIsNone(run_day_tier._prev_session_date(datetime(2026, 9, 23, 10, 5, tzinfo=ET)))
        self.assertEqual(gj.call_count, 1)  # a failure is cached for the tick, not retried per symbol
        run_day_tier._prev_session_cache.clear()


class FetchWindowFeed(unittest.TestCase):
    def setUp(self):
        # tests/test_day_tier_shadow.py stubs the alpaca SDK in sys.modules at import time; a real
        # fetcher import is impossible after that in the same process. Skip LOUDLY (never a silent
        # pass) — run this module on its own to exercise it: python3 -m unittest tests.test_day_tier_track_b_live
        try:
            import alpaca.data as _ad
        except ImportError:
            self.skipTest("alpaca SDK not installed on this host (run on the OCI py3.10 venv)")
        if not hasattr(_ad, "__path__"):
            self.skipTest("alpaca SDK stubbed in-process by another test module — run this module alone")
    def test_iex_maps_to_iex_feed_and_default_stays_sip(self):
        from alpaca.data.enums import Adjustment, DataFeed
        from data import fetcher
        captured = {}
        client = mock.MagicMock()

        def _get(req):
            captured["feed"] = req.feed
            captured["adj"] = req.adjustment
            return SimpleNamespace(df=pd.DataFrame())
        client.get_stock_bars.side_effect = _get
        s = datetime(2026, 9, 21, 9, 30, tzinfo=ET)
        with mock.patch.object(fetcher, "get_client", return_value=client), \
             mock.patch.object(fetcher, "_rate_gate"):
            fetcher.fetch_bars_window("UBER", config.TF_5M, s, s.replace(hour=10), feed="iex")
            self.assertEqual(captured["feed"], DataFeed.IEX)
            fetcher.fetch_bars_window("UBER", config.TF_5M, s, s.replace(hour=10))
            self.assertEqual(captured["feed"], DataFeed.SIP)  # default unchanged for research callers
            self.assertEqual(captured["adj"], Adjustment.RAW)  # default adjustment unchanged
            fetcher.fetch_bars_window("UBER", config.TF_DAILY, s, s.replace(hour=10), adjustment="split")
            self.assertEqual(captured["adj"], Adjustment.SPLIT)

    def test_unknown_feed_is_honest_empty_without_a_call(self):
        from data import fetcher
        client = mock.MagicMock()
        s = datetime(2026, 9, 21, 9, 30, tzinfo=ET)
        with mock.patch.object(fetcher, "get_client", return_value=client):
            df = fetcher.fetch_bars_window("UBER", config.TF_5M, s, s.replace(hour=10), feed="otc")
            df2 = fetcher.fetch_bars_window("UBER", config.TF_5M, s, s.replace(hour=10), adjustment="all")
        self.assertTrue(df.empty)
        self.assertTrue(df2.empty)
        client.get_stock_bars.assert_not_called()


class DurableTrackStamp(unittest.TestCase):
    def test_entry_fill_stamp_and_open_set_default(self):
        from strategy import day_tier_logger
        with TemporaryDirectory() as td:
            path = Path(td) / "ev.jsonl"
            with mock.patch.object(day_tier_logger, "_JSONL", path):
                kw = dict(order_id="o", decision_id="d", side="long", requested_limit=70.2, fill_price=70.1,
                          fill_qty=1.0, market_price_at_fill=70.1, equity_at_entry=2500.0, budget=131.25,
                          notional=70.1)
                self.assertTrue(day_tier_logger.log_entry_fill("DT-B", "UBER", track="B", **kw))
                self.assertTrue(day_tier_logger.log_entry_fill("DT-A", "MSFT", **kw))              # default -> A
                self.assertTrue(day_tier_logger.log_entry_fill("DT-X", "SMCI", track="zz", **kw))  # junk -> A
                with path.open("a") as f:  # a pre-stamp (legacy) row with no track field
                    f.write(json.dumps({"event": "entry_fill", "trade_id": "DT-OLD", "symbol": "AAPL",
                                        "fill_qty": 1, "fill_price": 1}) + "\n")
                opened = day_tier_logger.open_trades_from_log()
        self.assertEqual(opened["DT-B"]["track"], "B")
        self.assertEqual(opened["DT-A"]["track"], "A")
        self.assertEqual(opened["DT-X"]["track"], "A")
        self.assertEqual(opened["DT-OLD"]["track"], "A")


class RunnerWindowGate(unittest.TestCase):
    """Outside the window the runner does NO Track-B fetch; inside it routes an ENTER to place_entry(track B)."""

    def _tick(self, in_window, state=None, extra=None, pe_side_effect=None, mom_over=None, side="LONG"):
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="4000")
        frame = mock.MagicMock(name="frame")
        mom = {"symbol": "UBER", "trigger": "ENTER", "direction": "long", "mode": "DRIVE",
               "entry_ref": 70.0, "structural_level": 69.0, "vol_confirmed": True, **(mom_over or {})}
        patches = [
            mock.patch.object(run_day_tier, "_clock_state", return_value=("open", 300.0)),
            mock.patch.object(run_day_tier, "_touch_heartbeat"),
            mock.patch.object(run_day_tier, "_maybe_sample_prices"),
            mock.patch.object(run_day_tier, "_track_b_daily_context", return_value=(100.0, 1e6)),
            mock.patch("execution.broker.get_account", return_value=acct),
            mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 0}),
            mock.patch("execution.day_trade_manager.tier_kill_check", return_value=False),
            mock.patch("execution.day_trade_manager._account_entry_halt_reason", return_value=None),
            mock.patch("strategy.day_tier_track_b.track_b_in_window", return_value=in_window),
            mock.patch("strategy.day_tier_track_b.track_b_universe", return_value=["UBER"]),
            mock.patch("strategy.day_tier_track_b.screen_mover", return_value={"is_mover": True, "gap_direction": "up"}),
            mock.patch("strategy.day_tier_momentum_trigger.compute_momentum_trigger", return_value=mom),
            mock.patch.object(config, "DAYTRADE_ENABLED", True),
            mock.patch.object(config, "DAYTRADE_TRACK_B_ENABLED", True),
            mock.patch.object(config, "DAYTRADE_UNIVERSE", []),
            mock.patch("execution.day_trade_manager._load_state", return_value=state if state is not None else {}),
            mock.patch("execution.day_trade_manager._save_state", return_value=True),
            mock.patch.object(run_day_tier, "_prev_session_date", return_value="2026-09-22"),
            # Owner rule 2026-10-03: Track B must agree with the symbol's Layer-A trend side. These tests
            # exercise the window/budget/shot mechanics, so the trend side agrees with the long ENTER.
            mock.patch.object(run_day_tier, "_side_for", return_value=side),
            mock.patch.object(run_day_tier, "_day_tier_exposure", return_value={}),
            mock.patch.object(run_day_tier, "_watch_day_ok", return_value=(True, "watch day (test)")),
            mock.patch.object(run_day_tier, "_alignment_for", return_value={"aligned": True, "checks": {}, "reason": "ok"}),
            # leveraged pivot (2026-10-06) is exercised in test_pivot_routes_the_order_to_the_etf
            mock.patch("strategy.day_tier_leverage.etf_for_order", return_value=None),
            mock.patch.object(run_day_tier, "_held_by_other_tiers", return_value=set()),
        ] + list(extra or [])
        with ExitStack() as stack:  # >20 nested `with` items is a SyntaxError on the OCI py3.10 target
            for p in patches:
                stack.enter_context(p)
            rm = stack.enter_context(mock.patch("execution.risk_manager.RiskManager"))
            bsf = stack.enter_context(mock.patch("strategy.day_tier_track_b.build_session_frame", return_value=frame))
            pe = stack.enter_context(mock.patch("execution.day_trade_manager.place_entry", return_value=True,
                                                side_effect=pe_side_effect))
            rm.return_value.check_kill_switch.return_value = False
            result = run_day_tier.run_tick()
        return result, bsf, pe

    def test_outside_window_fetches_nothing(self):
        result, bsf, pe = self._tick(False)
        self.assertEqual(result["phase"], "scan")
        self.assertFalse(result["track_b_window"])
        bsf.assert_not_called()
        pe.assert_not_called()

    def test_inside_window_routes_enter_to_place_entry_as_track_b(self):
        result, bsf, pe = self._tick(True)
        self.assertTrue(result["track_b_window"])
        self.assertEqual(result["entered_b"], 1)
        bsf.assert_called_once()
        size = pe.call_args.args[3]
        self.assertEqual(size["track"], "B")
        self.assertTrue(size["size_ok"])

    def test_track_m_router_denial_does_not_block_track_b(self):
        extra = [
            mock.patch.object(config, "DAYTRADE_TRACK_M_ENABLED", True),
            mock.patch.object(run_day_tier, "_run_track_m", return_value=(0, "router_denied")),
        ]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertEqual(result["track_m_note"], "router_denied")
        self.assertEqual(result["entered_b"], 1)
        bsf.assert_called_once()
        pe.assert_called_once()

    def test_enter_signal_persists_the_daily_shot_before_place_entry(self):
        import copy
        state: dict = {}
        today = datetime.now(ET).strftime("%Y%m%d")
        saves: list = []   # DEEP copies of what was actually handed to _save_state (not the aliased dict)
        events: list = []

        def _save(st):
            saves.append(copy.deepcopy(st))
            events.append("save")
            return True

        def _pe(*a, **k):
            events.append("place_entry")
            return True
        extra = [mock.patch("execution.day_trade_manager._save_state", side_effect=_save)]
        result, bsf, pe = self._tick(True, state=state, extra=extra, pe_side_effect=_pe)
        persisted = [s for s in saves if s.get(run_day_tier._SIGNAL_DAY_KEY)]
        self.assertTrue(persisted, "the marker was never SAVED")
        self.assertEqual(persisted[-1][run_day_tier._SIGNAL_DAY_KEY], {"date": today, "symbols": ["UBER"]})
        self.assertLess(events.index("save"), events.index("place_entry"))  # saved BEFORE place_entry
        # Next tick (a fresh process) starts from the SAVED copy only: the symbol is not re-evaluated.
        result2, bsf2, pe2 = self._tick(True, state=copy.deepcopy(persisted[-1]))
        bsf2.assert_not_called()
        pe2.assert_not_called()

    def test_failed_marker_write_does_not_abort_the_tick(self):
        extra = [mock.patch("execution.day_trade_manager._save_state", return_value=False)]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertEqual(result["phase"], "scan")
        self.assertEqual(result["entered_b"], 1)  # the tick continues; the in-process set still blocks re-eval

    def test_api_budget_cap_defers_without_using_the_daily_shot(self):
        state: dict = {}
        with mock.patch.object(config, "DAYTRADE_MAX_API_CALLS_PER_RUN", 0):
            result, bsf, pe = self._tick(True, state=state)
        self.assertEqual(result["track_b_note"], "api_budget")
        bsf.assert_not_called()
        self.assertNotIn(run_day_tier._SIGNAL_DAY_KEY, state)  # the shot is NOT burned -> next tick re-evaluates

    def test_calendar_unavailable_skips_track_b(self):
        extra = [mock.patch.object(run_day_tier, "_prev_session_date", return_value=None)]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertEqual(result["track_b_note"], "calendar_unavailable")
        bsf.assert_not_called()

    @staticmethod
    def _seq_clock(values):
        """Monotonic reads return `values` in order, then repeat the last one."""
        it = {"i": 0}

        def _mono():
            v = values[min(it["i"], len(values) - 1)]
            it["i"] += 1
            return v
        return SimpleNamespace(monotonic=_mono)

    def test_place_entry_reserve_threshold_both_sides(self):
        # Reads: t0, pre-loop, loop-top, pre-context, RESERVE. With the real 120 s cadence and 35 s reserve:
        # 90 s elapsed (30 s left) -> deferred; 80 s elapsed (40 s left) -> place_entry IS called.
        extra = [mock.patch.object(run_day_tier, "time", self._seq_clock([0.0, 0.0, 0.0, 0.0, 90.0]))]
        result, bsf, pe = self._tick(True, extra=extra)
        pe.assert_not_called()
        self.assertEqual(result["track_b_note"], "tick_budget")
        extra = [mock.patch.object(run_day_tier, "time", self._seq_clock([0.0, 0.0, 0.0, 0.0, 80.0]))]
        result, bsf, pe = self._tick(True, extra=extra)
        pe.assert_called_once()
    def test_one_track_b_entry_per_symbol_per_day(self):
        today = datetime.now(ET).strftime("%Y%m%d")
        state = {f"entry::UBER::{today}-1000": {"symbol": "UBER", "track": "B", "bar_id": f"{today}-1000",
                                                "state": "flattened_no_stop"}}
        result, bsf, pe = self._tick(True, state=state)
        bsf.assert_not_called()   # already had its Track-B entry today -> not even evaluated
        pe.assert_not_called()
        self.assertEqual(result["entered_b"], 0)

    def test_a_prior_day_track_b_record_does_not_block_today(self):
        prior = (datetime.now(ET) - pd.Timedelta(days=1)).strftime("%Y%m%d")
        state = {f"entry::UBER::{prior}-1000": {"symbol": "UBER", "track": "B", "bar_id": f"{prior}-1000",
                                                "state": "flattened_no_stop"},
                 run_day_tier._SIGNAL_DAY_KEY: {"date": prior, "symbols": ["UBER"]}}
        result, bsf, pe = self._tick(True, state=state)
        bsf.assert_called_once()

    def test_a_track_a_record_does_not_block_track_b(self):
        today = datetime.now(ET).strftime("%Y%m%d")
        state = {f"entry::UBER::{today}-1000": {"symbol": "UBER", "track": "A", "bar_id": f"{today}-1000",
                                                "state": "flattened_no_stop"}}
        result, bsf, pe = self._tick(True, state=state)
        bsf.assert_called_once()

    @staticmethod
    def _clock(over_after_calls):
        """A controlled monotonic clock: 0.0 for the first `over_after_calls` reads, then 1000 s (over budget).
        Read order in run_tick: _tick_t0, the pre-loop check, the loop-top check, the pre-daily-context check."""
        calls = {"n": 0}

        def _mono():
            calls["n"] += 1
            return 0.0 if calls["n"] <= over_after_calls else 1000.0
        return SimpleNamespace(monotonic=_mono)

    def test_budget_crossed_at_loop_top_stops_before_any_fetch(self):
        # Pre-loop check passes (reads 1-2 in budget); the IN-LOOP top check (read 3) is over -> break.
        ctx = mock.MagicMock(return_value=(100.0, 1e6))
        extra = [mock.patch.object(run_day_tier, "time", self._clock(2)),
                 mock.patch.object(run_day_tier, "_track_b_daily_context", ctx)]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertTrue(result["track_b_window"])   # proves the PRE-loop check did NOT fire
        bsf.assert_not_called()
        ctx.assert_not_called()
        self.assertEqual(result["track_b_note"], "tick_budget")

    def test_budget_crossed_before_daily_context_skips_the_fetch(self):
        # Reads 1-3 in budget (the frame is built); the re-check before the daily context (read 4) is over.
        ctx = mock.MagicMock(return_value=(100.0, 1e6))
        extra = [mock.patch.object(run_day_tier, "time", self._clock(3)),
                 mock.patch.object(run_day_tier, "_track_b_daily_context", ctx)]
        result, bsf, pe = self._tick(True, extra=extra)
        bsf.assert_called_once()
        ctx.assert_not_called()
        pe.assert_not_called()
        self.assertEqual(result["track_b_note"], "tick_budget")

    def test_track_b_window_or_import_error_does_not_escape_run_tick(self):
        extra = [mock.patch("strategy.day_tier_track_b.track_b_in_window", side_effect=RuntimeError("boom"))]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertEqual(result["phase"], "scan")
        self.assertEqual(result["track_b_note"], "import_error")
        bsf.assert_not_called()

    def test_pivot_routes_the_order_to_the_etf(self):
        from data.live_price import LivePrice
        lp = {"UBER": LivePrice(70.0, "iex_trade", 1.0), "UBRL": LivePrice(20.0, "iex_trade", 1.0)}
        # 10/10 whose max-size budget buys only 1 share of the stock (single-name cap shrunk to $75 vs UBER $70)
        # -> the 2x ETF at max size (Rafael 2026-10-06: ETF only when the stock gives 0 or 1 share)
        extra = [mock.patch("strategy.day_tier_leverage.etf_for_order", return_value="UBRL"),
                 mock.patch("strategy.day_tier_leverage.is_ten_of_ten", return_value=(True, "10/10")),
                 mock.patch.object(config, "DAYTRADE_MAX_SINGLE_NAME_NOTIONAL_PCT", 0.03),
                 mock.patch("data.live_price.live_price", side_effect=lambda s, **k: lp.get(s)),
                 mock.patch("execution.broker.get_open_positions", return_value=[]),
                 mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={})]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertEqual(result["entered_b"], 1)
        sym, _dec, trg, size = pe.call_args.args[:4]
        self.assertEqual(sym, "UBRL")
        self.assertEqual((trg["underlying"], trg["leverage"]), ("UBER", 2.0))
        # exact daily-reset stop with the harness's prior close (100): UBER 70 -> 69 moves UBRL by -5%, so 19.00
        from strategy import day_tier_leverage as lev
        self.assertAlmostEqual(trg["wall_ref"], lev.etf_stop_level(20.0, 70.0, 69.0, 2.0, False, prior_close=100.0),
                               places=3)
        self.assertAlmostEqual(trg["wall_ref"], 19.0, places=3)
        self.assertEqual(size["track"], "B")
        self.assertTrue(size.get("max_size"))

    # ── inverse-ETF short route (Rafael 2026-10-07) ─────────────────────────────────────────────────────────
    _SHORT = {"direction": "short", "entry_ref": 70.0, "structural_level": 71.0}
    _INV_OK = (({"symbol": "UBRD", "underlying": "UBER", "signal_direction": "short"},
                {"symbol": "UBRD", "trigger": "ENTER", "direction": "long", "signal_direction": "short",
                 "instrument": "inverse_etf", "underlying": "UBER", "entry_ref": 9.0, "wall_ref": 8.8},
                {"size_ok": True, "shares": 20, "budget": 200.0, "track": "B"}, "UBRD"), "inverse route -> UBRD")

    def test_short_on_a_coheld_stock_buys_the_inverse_etf(self):
        extra = [mock.patch.object(run_day_tier, "_held_by_other_tiers", return_value={"UBER"}),
                 mock.patch.object(run_day_tier, "_inverse_pivot", return_value=self._INV_OK)]
        result, _bsf, pe = self._tick(True, extra=extra, mom_over=self._SHORT, side="SHORT")
        self.assertEqual(result["entered_b"], 1)
        sym, _dec, trg = pe.call_args.args[:3]
        self.assertEqual((sym, trg["direction"], trg["signal_direction"]), ("UBRD", "long", "short"))

    def test_short_on_a_coheld_stock_with_no_route_is_skipped(self):
        extra = [mock.patch.object(run_day_tier, "_held_by_other_tiers", return_value={"UBER"}),
                 mock.patch.object(run_day_tier, "_inverse_pivot", return_value=(None, "no free liquid inverse ETF"))]
        result, _bsf, pe = self._tick(True, extra=extra, mom_over=self._SHORT, side="SHORT")
        self.assertEqual(result["entered_b"], 0)
        pe.assert_not_called()

    def test_short_not_held_with_budget_for_two_shares_shorts_the_stock(self):
        inv = mock.Mock(return_value=self._INV_OK)
        extra = [mock.patch.object(run_day_tier, "_inverse_pivot", inv),
                 mock.patch("strategy.day_tier_sizing.compute_day_tier_size",
                            return_value={"size_ok": True, "shares": 3, "budget": 300.0, "track": "B"})]
        result, _bsf, pe = self._tick(True, extra=extra, mom_over=self._SHORT, side="SHORT")
        inv.assert_not_called()
        sym, _dec, trg = pe.call_args.args[:3]
        self.assertEqual((sym, trg["direction"]), ("UBER", "short"))

    def test_short_not_held_with_budget_for_one_share_buys_the_inverse_etf(self):
        extra = [mock.patch.object(run_day_tier, "_inverse_pivot", return_value=self._INV_OK),
                 mock.patch("strategy.day_tier_sizing.compute_day_tier_size",
                            return_value={"size_ok": True, "shares": 1, "budget": 100.0, "track": "B"})]
        result, _bsf, pe = self._tick(True, extra=extra, mom_over=self._SHORT, side="SHORT")
        self.assertEqual(pe.call_args.args[0], "UBRD")

    def test_short_budget_route_failure_keeps_the_stock_short(self):
        extra = [mock.patch.object(run_day_tier, "_inverse_pivot", return_value=(None, "spread too wide")),
                 mock.patch("strategy.day_tier_sizing.compute_day_tier_size",
                            return_value={"size_ok": True, "shares": 1, "budget": 100.0, "track": "B"})]
        result, _bsf, pe = self._tick(True, extra=extra, mom_over=self._SHORT, side="SHORT")
        self.assertEqual((pe.call_args.args[0], pe.call_args.args[2]["direction"]), ("UBER", "short"))

    def test_ten_of_ten_short_on_the_stock_gets_max_size(self):
        extra = [mock.patch("strategy.day_tier_leverage.is_ten_of_ten", return_value=(True, "10/10")),
                 mock.patch("strategy.day_tier_sizing.compute_day_tier_size",
                            return_value={"size_ok": True, "shares": 3, "budget": 300.0, "track": "B"})]
        result, _bsf, pe = self._tick(True, extra=extra, mom_over=self._SHORT, side="SHORT")
        sym, _dec, trg, size = pe.call_args.args[:4]
        self.assertEqual((sym, trg["direction"]), ("UBER", "short"))
        self.assertTrue(size.get("max_size"))

    def test_ten_of_ten_short_routed_to_the_inverse_etf_gets_max_size(self):
        extra = [mock.patch("strategy.day_tier_leverage.is_ten_of_ten", return_value=(True, "10/10")),
                 mock.patch.object(run_day_tier, "_held_by_other_tiers", return_value={"UBER"}),
                 mock.patch.object(run_day_tier, "_inverse_pivot", return_value=self._INV_OK)]
        result, _bsf, pe = self._tick(True, extra=extra, mom_over=self._SHORT, side="SHORT")
        sym, _dec, _trg, size = pe.call_args.args[:4]
        self.assertEqual(sym, "UBRD")
        self.assertTrue(size.get("max_size"))

    def test_short_not_ten_of_ten_is_not_max_size(self):
        extra = [mock.patch("strategy.day_tier_leverage.is_ten_of_ten", return_value=(False, "no")),
                 mock.patch("strategy.day_tier_sizing.compute_day_tier_size",
                            return_value={"size_ok": True, "shares": 3, "budget": 300.0, "track": "B"})]
        result, _bsf, pe = self._tick(True, extra=extra, mom_over=self._SHORT, side="SHORT")
        self.assertFalse(pe.call_args.args[3].get("max_size"))

    def test_ten_of_ten_buying_two_plus_shares_trades_the_stock_at_max_size(self):
        extra = [mock.patch("strategy.day_tier_leverage.etf_for_order", return_value="UBRL"),
                 mock.patch("strategy.day_tier_leverage.is_ten_of_ten", return_value=(True, "10/10")),
                 mock.patch("execution.broker.get_open_positions", return_value=[]),
                 mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={})]
        _r, _b, pe = self._tick(True, extra=extra)
        self.assertEqual(pe.call_args.args[0], "UBER")       # $1,300 thin cap / $70 = 18 shares -> stock
        self.assertTrue(pe.call_args.args[3].get("max_size"))

    def test_affordable_non_ten_does_not_pivot(self):
        extra = [mock.patch("strategy.day_tier_leverage.etf_for_order", return_value="UBRL"),
                 mock.patch("execution.broker.get_open_positions", return_value=[]),
                 mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={})]
        _r, _b, pe = self._tick(True, extra=extra)
        self.assertEqual(pe.call_args.args[0], "UBER")
        self.assertFalse(pe.call_args.args[3].get("max_size"))

    def test_track_b_flag_off_is_a_no_op(self):
        # T4: with the window, universe and calendar all forced ON, only the FLAG can keep Track B off.
        extra = [mock.patch.object(config, "DAYTRADE_TRACK_B_ENABLED", False)]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertFalse(result["track_b"])
        self.assertFalse(result["track_b_window"])
        self.assertEqual(result["track_b_note"], "")
        bsf.assert_not_called()
        pe.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
