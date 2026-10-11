#!/usr/bin/env python3
# ruff: noqa: E501
"""Co-hold D3 (2026-10-10/11; CEO rule "the Day tier may trade a stock another tier holds in the same direction";
board Harris/Taleb + Gro + GAI). With DAYTRADE_NO_COHOLD lifted, a Day entry on a stock another tier holds in the
SAME direction is submitted as an Alpaca BRACKET (entry + take-profit + stop) — Alpaca's wash-trade rule rejects a
plain limit buy while that tier's sell stop rests, and names bracket/OCO orders as exceptions. A complete fill makes
the bracket legs the protection; a partial fill cancels the group and the normal post-fill OCO protects the filled
shares; a wash reject marks the skip "cohold_wash" for the runner's ETF fallback.
Design record: logs/design_records/same_direction_cohold_2026-10-10.md (D3).
Run: PYTHONPATH=. python3 -m pytest -q tests/test_day_tier_cohold_bracket.py
"""
from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import broker
from execution import day_trade_manager as dtm


def _leg(lid, otype, status="new", filled_qty=0):
    return SimpleNamespace(id=lid, order_type=otype, type=otype, status=status, filled_qty=filled_qty)


def _bracket(legs=None):
    return SimpleNamespace(id="ENTRY1", client_order_id="DT-x", legs=legs if legs is not None
                           else [_leg("TP1", "limit"), _leg("SL1", "stop", "held")])


class BracketTakeProfit(unittest.TestCase):
    def test_ride_uses_r_multiple_off_the_limit(self):
        with mock.patch.object(dtm, "_cfg", side_effect=lambda k, d=None: 2.0 if k == "DAYTRADE_RIDE_TARGET_R" else d):
            self.assertEqual(dtm._bracket_take_profit({"target": None}, "long", 100.0, 98.0), 104.0)
            self.assertEqual(dtm._bracket_take_profit({"target": None}, "short", 100.0, 102.0), 96.0)

    def test_fade_pin_kept_only_on_the_profit_side(self):
        self.assertEqual(dtm._bracket_take_profit({"target": 103.0}, "long", 100.0, 98.0), 103.0)
        with mock.patch.object(dtm, "_cfg", side_effect=lambda k, d=None: 2.0 if k == "DAYTRADE_RIDE_TARGET_R" else d):
            self.assertEqual(dtm._bracket_take_profit({"target": 99.0}, "long", 100.0, 98.0), 104.0)

    def test_no_target_or_bad_geometry_is_none(self):
        self.assertIsNone(dtm._bracket_take_profit({"no_target": True}, "long", 100.0, 98.0))
        self.assertIsNone(dtm._bracket_take_profit({"target": None}, "long", 100.0, 100.0))


class BracketProtection(unittest.TestCase):
    def _run(self, statuses, filled, requested=5, cancel_ok=True, order=None):
        """statuses: {leg_id: status} returned by broker.get_order(leg_id)."""
        def _get(oid, nested=False):
            return _leg(oid, "stop" if oid == "SL1" else "limit", statuses.get(oid, "")) if oid in statuses else None
        with mock.patch("execution.broker.get_order", side_effect=_get), \
                mock.patch("execution.broker.cancel_stop_confirmed", return_value=cancel_ok) as c, \
                mock.patch.object(dtm.time, "sleep"):
            return dtm._bracket_protection("NVDA", order or _bracket(), filled, requested), c

    def test_complete_fill_with_live_stop_leg(self):
        out, _ = self._run({"SL1": "held", "TP1": "new"}, 5)
        self.assertEqual(out, ("live", "SL1", "TP1"))

    def test_take_profit_filled_seconds_after_entry_is_never_gone(self):
        # board 2026-10-11 race: TP filled -> Alpaca cancelled the stop leg. Must be "live" (an exit happened, booked by
        # the reconcile from tp_order_id), never "gone" (which would place a second exit on the other tier's shares).
        out, _ = self._run({"SL1": "canceled", "TP1": "filled"}, 5)
        self.assertEqual(out[0], "live")

    def test_partly_filled_then_cancelled_take_profit_is_an_exit(self):
        # cold-2nd round 2: TP filled 3 of 10 then cancelled reads "canceled" with filled_qty 3 -> an exit, not "gone"
        def _get(oid, nested=False):
            return _leg("SL1", "stop", "rejected") if oid == "SL1" else _leg("TP1", "limit", "canceled", 3)
        with mock.patch("execution.broker.get_order", side_effect=_get), \
                mock.patch("execution.broker.cancel_stop_confirmed", return_value=True), \
                mock.patch.object(dtm.time, "sleep"):
            self.assertEqual(dtm._bracket_protection("NVDA", _bracket(), 10, 10)[0], "live")
            self.assertEqual(dtm._bracket_protection("NVDA", _bracket(), 7, 10)[0], "unknown")

    def test_replaced_leg_cannot_prove_nothing_filled(self):
        out, _ = self._run({"SL1": "canceled", "TP1": "replaced"}, 5)
        self.assertEqual(out[0], "unknown")

    def test_partial_fill_confirms_the_group_cancelled(self):
        out, c = self._run({"SL1": "canceled", "TP1": "canceled"}, 3)
        self.assertEqual(out[0], "gone")
        self.assertEqual(c.call_count, 2)

    def test_partial_fill_with_a_filled_leg_is_unknown(self):
        out, _ = self._run({"SL1": "canceled", "TP1": "filled"}, 3)
        self.assertEqual(out[0], "unknown")

    def test_partial_fill_unconfirmed_cancel_is_unknown(self):
        out, _ = self._run({"SL1": "held", "TP1": "new"}, 3, cancel_ok=False)
        self.assertEqual(out[0], "unknown")

    def test_complete_fill_both_legs_cancelled_falls_back(self):
        out, _ = self._run({"SL1": "canceled", "TP1": "canceled"}, 5)
        self.assertEqual(out[0], "gone")

    def test_unreadable_leg_status_after_cancel_is_unknown(self):
        out, _ = self._run({"SL1": "canceled"}, 5)          # TP1 unreadable
        self.assertEqual(out[0], "unknown")

    def test_missing_legs_reread_nested(self):
        parent = _bracket([_leg("TP1", "limit"), _leg("SL1", "stop", "held")])

        def _get(oid, nested=False):
            if oid == "ENTRY1" and nested:
                return parent
            return _leg(oid, "stop", "held") if oid == "SL1" else None
        with mock.patch("execution.broker.get_order", side_effect=_get), mock.patch.object(dtm.time, "sleep"):
            self.assertEqual(dtm._bracket_protection("NVDA", _bracket([]), 5, 5)[0], "live")

    def test_complete_fill_without_any_stop_leg_is_unknown(self):
        with mock.patch("execution.broker.get_order", return_value=None):
            self.assertEqual(dtm._bracket_protection("NVDA", _bracket([_leg("TP1", "limit")]), 5, 5)[0], "unknown")


class SubmitBracketEntry(unittest.TestCase):
    def _submit(self, side, lim, tp, stop, raise_with=None):
        client = mock.Mock()
        client.submit_order.side_effect = Exception(raise_with) if raise_with else None
        client.submit_order.return_value = _bracket()
        with mock.patch.object(broker, "_get_trading_client", return_value=client), \
                mock.patch.object(broker.time, "sleep"):
            return broker.submit_bracket_entry("NVDA", 5, side, lim, tp, stop, client_order_id="DT-x"), client

    def test_valid_long_and_short(self):
        out, client = self._submit("buy", 100.0, 104.0, 98.0)
        self.assertIsNotNone(out)
        client.submit_order.assert_called_once()
        out, _ = self._submit("sell", 100.0, 96.0, 102.0)
        self.assertIsNotNone(out)

    def test_one_cent_stop_is_valid_despite_float_drift(self):
        out, client = self._submit("buy", 99.99, 100.50, 99.98)
        self.assertIsNotNone(out)

    def test_inverted_geometry_rejected_before_submit(self):
        for args in (("buy", 100.0, 99.0, 98.0), ("buy", 100.0, 104.0, 100.0), ("sell", 100.0, 101.0, 102.0)):
            out, client = self._submit(*args)
            self.assertIsNone(out, args)
            client.submit_order.assert_not_called()

    def test_wash_reject_returns_the_sentinel(self):
        out, _ = self._submit("buy", 100.0, 104.0, 98.0,
                              raise_with='{"code":40310000,"message":"potential wash trade detected. use complex orders"}')
        self.assertIs(out, broker.WASH_REJECTED)


class PlaceEntryCohold(unittest.TestCase):
    """place_entry on a stock another tier holds LONG (net +7)."""

    def _run(self, *, no_cohold, held_side="long", direction="long", bracket_ret=None, final_fill=(5, 100.0),
             legs_status="live"):
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)
        held = SimpleNamespace(symbol="NVDA", side=held_side, qty="7" if held_side == "long" else "-7",
                               current_price=100.0, avg_entry_price=99.0)
        swing_stop = SimpleNamespace(id="SW1", symbol="NVDA", side="sell", type="stop", qty="7",
                                     client_order_id="IN-NVDA-s-1-aaaa")
        state: dict = {}
        sub_lim = mock.Mock(return_value=None)
        sub_br = mock.Mock(return_value=_bracket() if bracket_ret is None else bracket_ret)
        oco = mock.Mock(return_value=None)
        stop_tr = 98.0 if direction == "long" else 102.0
        trigger = {"trigger": "ENTER", "direction": direction, "mode": "RIDE", "entry_ref": 100.0,
                   "target": None, "wall_ref": stop_tr}
        cfg = {"DAYTRADE_NO_COHOLD": no_cohold, "DAYTRADE_RIDE_TARGET_R": 2.0}
        real_cfg = dtm._cfg
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(dtm, "_enabled", return_value=True))
            es.enter_context(mock.patch.object(dtm, "_cfg", side_effect=lambda k, d=None: cfg[k] if k in cfg else real_cfg(k, d)))
            es.enter_context(mock.patch.object(dtm, "_load_state", return_value=state))
            es.enter_context(mock.patch.object(dtm, "_save_state", return_value=True))
            es.enter_context(mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _l, st: (st, "kept")))
            es.enter_context(mock.patch.object(dtm, "_compute_stop_price", return_value=stop_tr))
            es.enter_context(mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None))
            es.enter_context(mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")))
            es.enter_context(mock.patch.object(dtm, "_bounded_entry_qty", side_effect=lambda q, *a, **k: (q, "rooms=ok")))
            es.enter_context(mock.patch.object(dtm, "_confirm_fill", return_value=True))
            es.enter_context(mock.patch.object(dtm, "_await_entry_terminal", return_value=True))
            es.enter_context(mock.patch.object(dtm, "_final_fill", return_value=final_fill))
            es.enter_context(mock.patch.object(dtm, "_bracket_protection",
                                  return_value=(legs_status, "SL1", "TP1")))
            halt = es.enter_context(mock.patch.object(dtm, "_halt_unresolved_exit"))
            es.enter_context(mock.patch("data.live_price.live_price", return_value=None))
            es.enter_context(mock.patch("data.alpaca_data.get_latest_quote", return_value=None))
            es.enter_context(mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}))
            es.enter_context(mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)))
            es.enter_context(mock.patch("strategy.day_tier_logger.log_decision", return_value=True))
            es.enter_context(mock.patch("strategy.day_tier_logger.log_entry_fill", return_value=True))
            es.enter_context(mock.patch("strategy.day_tier_logger.log_stop_placed", return_value=True))
            es.enter_context(mock.patch("strategy.day_tier_logger.log_target_placed", return_value=True))
            es.enter_context(mock.patch("trade_logger.log_event", return_value=None))
            es.enter_context(mock.patch.object(broker, "get_account", return_value=acct))
            es.enter_context(mock.patch.object(broker, "get_open_positions", return_value=[held]))
            es.enter_context(mock.patch.object(broker, "get_open_position", return_value=held))
            es.enter_context(mock.patch.object(broker, "get_open_orders", return_value=[swing_stop]))
            es.enter_context(mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30))
            es.enter_context(mock.patch.object(broker, "cancel_open_orders_for_symbol", return_value=0))
            es.enter_context(mock.patch("execution.tier_capital_allocator.live_admit",
                           return_value=SimpleNamespace(approved=True, lease=None, reason="")))
            es.enter_context(mock.patch("execution.tier_capital_allocator.live_order_id", return_value="DT-x"))
            es.enter_context(mock.patch("execution.tier_capital_allocator.live_bind", return_value=True))
            es.enter_context(mock.patch("execution.tier_capital_allocator.live_release", return_value=None))
            es.enter_context(mock.patch.object(broker, "submit_limit_order", sub_lim))
            es.enter_context(mock.patch.object(broker, "submit_bracket_entry", sub_br))
            es.enter_context(mock.patch.object(broker, "submit_oco_exit", oco))
            es.enter_context(mock.patch.object(broker, "_get_trading_client", side_effect=AssertionError("real broker")))
            es.enter_context(mock.patch.object(broker, "submit_day_stop_order", return_value=SimpleNamespace(id="PS1")))
            es.enter_context(mock.patch.object(dtm, "flatten_position", return_value=True))
            dtm._LAST_ENTRY_SKIP.clear()
            ok = dtm.place_entry("NVDA", {"would_consider": True}, trigger, {"size_ok": True, "shares": 5},
                                 bar_id="20261012-0930", equity=2500.0)
        return ok, sub_lim, sub_br, oco, state, halt

    def test_guard_on_still_skips(self):
        ok, sub_lim, sub_br, *_ = self._run(no_cohold=True)
        self.assertFalse(ok)
        sub_lim.assert_not_called()
        sub_br.assert_not_called()

    def test_same_direction_cohold_uses_a_bracket_and_its_legs_protect(self):
        ok, sub_lim, sub_br, oco, state, _ = self._run(no_cohold=False)
        self.assertTrue(ok)
        sub_lim.assert_not_called()
        args = sub_br.call_args.args
        self.assertEqual((args[0], args[1], args[2]), ("NVDA", 5, "buy"))
        self.assertEqual(args[5], 98.0)                                   # stop
        self.assertGreater(args[4], args[3])                               # take-profit above the limit
        oco.assert_not_called()
        rec = next(iter(state.values()))
        self.assertEqual((rec["state"], rec["stop_order_id"], rec["tp_order_id"]), ("protected", "SL1", "TP1"))
        self.assertTrue(rec["cohold_bracket"])

    def test_opposite_direction_is_still_blocked(self):
        ok, sub_lim, sub_br, *_ = self._run(no_cohold=False, held_side="short")
        self.assertFalse(ok)
        sub_br.assert_not_called()

    def test_wash_reject_marks_the_etf_fallback(self):
        ok, sub_lim, sub_br, *_ = self._run(no_cohold=False, bracket_ret=broker.WASH_REJECTED)
        self.assertFalse(ok)
        sub_lim.assert_not_called()
        self.assertEqual(dtm.last_entry_skip("NVDA")["reason"], "cohold_wash")

    def test_partial_fill_falls_back_to_the_normal_exit(self):
        ok, sub_lim, sub_br, oco, state, _ = self._run(no_cohold=False, final_fill=(3, 100.0), legs_status="gone")
        oco.assert_called_once()                                           # normal post-fill OCO for the 3 filled
        self.assertEqual(oco.call_args.args[1], 3)

    def test_unknown_legs_halt_without_a_second_exit(self):
        ok, sub_lim, sub_br, oco, state, halt = self._run(no_cohold=False, legs_status="unknown")
        self.assertFalse(ok)
        oco.assert_not_called()
        halt.assert_called_once()


class CoholdLift(unittest.TestCase):
    """The lift (config.DAYTRADE_NO_COHOLD = False): the runner lets a SAME-direction co-hold reach the stock path; an
    opposite holder keeps the ETF route / skip; a bracket wash reject routes to the ETF."""

    def test_cohold_ok_same_direction_only(self):
        import run_day_tier as rdt
        with mock.patch("config.DAYTRADE_NO_COHOLD", False, create=True):
            self.assertTrue(rdt._cohold_ok("EWY", "short", {"EWY": -1.0}))
            self.assertTrue(rdt._cohold_ok("NVDA", "long", {"NVDA": 7.0}))
            self.assertFalse(rdt._cohold_ok("UBER", "short", {"UBER": 1.0}))     # opposite -> blocked
            self.assertFalse(rdt._cohold_ok("NVDA", "long", None))                # unreadable -> blocked
            self.assertFalse(rdt._cohold_ok("NVDA", "long", {"NVDA": 0.0}))
        with mock.patch("config.DAYTRADE_NO_COHOLD", True, create=True):
            self.assertFalse(rdt._cohold_ok("NVDA", "long", {"NVDA": 7.0}))      # guard on

    def test_config_lifts_the_guard(self):
        import config
        self.assertIs(getattr(config, "DAYTRADE_NO_COHOLD", True), False)

    def test_foreign_signed_qty(self):
        import run_day_tier as rdt
        snap = SimpleNamespace(errors=(), broker_qty={"NVDA": 10.0, "EWY": -1.0},
                               claimed_qty=lambda s, t: 3.0 if s == "NVDA" else 0.0,
                               foreign_symbols=lambda t: frozenset({"NVDA", "EWY"}))
        with mock.patch("execution.ownership_snapshot.build_ownership_snapshot", return_value=snap):
            self.assertEqual(rdt._foreign_signed_qty(), {"NVDA": 7.0, "EWY": -1.0})
        bad = SimpleNamespace(errors=("daytrade_log:ValueError",), broker_qty={}, claimed_qty=lambda s, t: 0.0,
                              foreign_symbols=lambda t: frozenset())
        with mock.patch("execution.ownership_snapshot.build_ownership_snapshot", return_value=bad):
            self.assertIsNone(rdt._foreign_signed_qty())

    def test_wash_reject_routes_to_the_etf(self):
        import run_day_tier as rdt
        calls, skips = [], {}

        class _Fake:
            def place_entry(self, sym, decision, trigger, size, *, bar_id, equity, min_qty=1, **kw):
                calls.append(sym)
                if sym == "NVDA":
                    skips[sym] = {"reason": "cohold_wash", "wired": 5, "min": min_qty}
                    return False
                return True

            def last_entry_skip(self, sym):
                return skips.get(sym)

        route = (({}, {"direction": "long", "symbol": "NVDL"}, {"size_ok": True, "shares": 5}, "NVDL"), "ok")
        with mock.patch.object(rdt, "_bull_pivot", return_value=route):
            ok, sym, _d, tries = rdt._place_affordable(
                _Fake(), "NVDA", {}, {"trigger": "ENTER", "direction": "long", "entry_ref": 100.0}, {"size_ok": True},
                bar_id="b", equity=2500.0, buying_power=9000.0, held={"NVDA"}, track="A", exposure={})
        self.assertEqual((ok, sym, calls), (True, "NVDL", ["NVDA", "NVDL"]))


if __name__ == "__main__":
    unittest.main()
