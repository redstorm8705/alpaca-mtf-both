#!/usr/bin/env python3
# ruff: noqa: E501
"""Risk-path tests for the day-tier OCO bracket-exit build (2026-09-19).

Covers the new logic: OCO geometry validation + market-stop leg (broker.submit_oco_exit); the
recorded-leg cancel that the tier-scoped cancel cannot reach (dtm._cancel_daytrade_exit_legs);
reconcile recognizing an OCO stop leg by explicit id, not by DT- coid (dtm._has_live_daytrade_stop);
and the take-profit heal-labeling so a harvested winner books P&L as take_profit, not protective_stop
(dtm._record_confirmed_stop_exit)."""
from __future__ import annotations

import unittest
from unittest import mock
from types import SimpleNamespace

from execution import broker
from execution import day_trade_manager as dtm


class _Leg:
    def __init__(self, oid, otype):
        self.id = oid
        self.order_type = otype
        self.type = otype


class _ParentOrder:
    """Models a submitted Alpaca OCO order: the PARENT order is the take-profit; the STOP is the
    single child leg (docs orders-at-alpaca). `oid` is therefore the take-profit order id."""
    def __init__(self, oid="TPPARENT", legs=None):
        self.id = oid
        self.client_order_id = "DT-SPY-s-1-x"
        self.legs = legs if legs is not None else [_Leg("STOPCHILD", "stop")]


class OcoGeometry(unittest.TestCase):
    """submit_oco_exit must reject an inverted/degenerate geometry BEFORE any broker call."""

    @staticmethod
    def _client_must_not_be_called():
        m = mock.MagicMock()
        m.submit_order.side_effect = AssertionError("broker client called on a geometry reject")
        return m

    def test_long_requires_stop_below_tp(self):
        with mock.patch.object(broker, "_get_trading_client", self._client_must_not_be_called):
            # long exit: need stop < tp; here stop 110 > tp 105 → reject
            self.assertIsNone(broker.submit_oco_exit("SPY", 2, "long", take_profit_price=105.0, stop_price=110.0))

    def test_short_requires_tp_below_stop(self):
        with mock.patch.object(broker, "_get_trading_client", self._client_must_not_be_called):
            # short exit: need tp < stop; here tp 110 > stop 105 → reject
            self.assertIsNone(broker.submit_oco_exit("SPY", 2, "short", take_profit_price=110.0, stop_price=105.0))

    def test_bad_side_rejected(self):
        with mock.patch.object(broker, "_get_trading_client", self._client_must_not_be_called):
            self.assertIsNone(broker.submit_oco_exit("SPY", 2, "sideways", take_profit_price=110.0, stop_price=90.0))

    def test_nonpositive_qty_or_price_rejected(self):
        with mock.patch.object(broker, "_get_trading_client", self._client_must_not_be_called):
            self.assertIsNone(broker.submit_oco_exit("SPY", 0, "long", 110.0, 90.0))
            self.assertIsNone(broker.submit_oco_exit("SPY", 2, "long", -1.0, 90.0))
            self.assertIsNone(broker.submit_oco_exit("SPY", 2, "long", 110.0, 0.0))

    def test_valid_long_builds_market_stop_oco(self):
        from alpaca.trading.enums import OrderClass, OrderSide
        captured = {}
        fake_client = mock.MagicMock()

        def _submit(order_data):
            captured["req"] = order_data
            return _ParentOrder()  # real OCO shape: parent=TP, legs=[stop child]

        fake_client.submit_order.side_effect = _submit
        with mock.patch.object(broker, "_get_trading_client", lambda: fake_client):
            order = broker.submit_oco_exit("SPY", 3, "long", take_profit_price=110.0, stop_price=90.0, tier="daytrade")
        self.assertIsNotNone(order)
        req = captured["req"]
        self.assertEqual(req.order_class, OrderClass.OCO)
        self.assertEqual(req.side, OrderSide.SELL)  # long exit = sell
        self.assertEqual(float(req.take_profit.limit_price), 110.0)
        self.assertEqual(float(req.stop_loss.stop_price), 90.0)
        # MARKET stop (guaranteed exit): the stop leg carries NO limit_price
        self.assertIsNone(getattr(req.stop_loss, "limit_price", None))

    def test_valid_short_exit_is_buy(self):
        from alpaca.trading.enums import OrderSide
        captured = {}
        fake_client = mock.MagicMock()

        def _submit(order_data):
            captured["req"] = order_data
            return _ParentOrder()

        fake_client.submit_order.side_effect = _submit
        with mock.patch.object(broker, "_get_trading_client", lambda: fake_client):
            order = broker.submit_oco_exit("SPY", 2, "short", take_profit_price=90.0, stop_price=110.0)
        self.assertIsNotNone(order)
        self.assertEqual(captured["req"].side, OrderSide.BUY)  # short exit = buy


class OcoLegIds(unittest.TestCase):
    """The OCO PARENT is the take-profit; the STOP is the child leg. _oco_leg_ids must return the
    parent id as the TP id — the pre-fix bug left tp_id empty, which halted every harvested winner."""

    def test_parent_is_take_profit_stop_is_child(self):
        oco = _ParentOrder(oid="TP_PARENT", legs=[_Leg("STOP_CHILD", "stop")])
        stop_id, tp_id, oco_id = dtm._oco_leg_ids(oco)
        self.assertEqual(tp_id, "TP_PARENT")      # parent = take-profit (NOT empty)
        self.assertEqual(stop_id, "STOP_CHILD")   # child leg = stop
        self.assertEqual(oco_id, "TP_PARENT")

    def test_missing_legs_falls_back_to_parent(self):
        oco = _ParentOrder(oid="ONLY_PARENT", legs=[])
        stop_id, tp_id, oco_id = dtm._oco_leg_ids(oco)
        self.assertEqual(tp_id, "ONLY_PARENT")
        self.assertEqual(stop_id, "ONLY_PARENT")  # fail-closed fallback, never empty

    def test_explicit_limit_leg_used_as_tp(self):
        oco = _ParentOrder(oid="PARENT", legs=[_Leg("STOP1", "stop"), _Leg("TP1", "limit")])
        stop_id, tp_id, _ = dtm._oco_leg_ids(oco)
        self.assertEqual(stop_id, "STOP1")
        self.assertEqual(tp_id, "TP1")            # an explicit limit leg wins over the parent


class CancelExitLegs(unittest.TestCase):
    def test_cancels_recorded_leg_ids_for_symbol_only(self):
        state = {
            "entry::SPY::b1": {"symbol": "SPY", "oco_order_id": "OCO1", "stop_order_id": "ST", "tp_order_id": "TP"},
            "entry::AAPL::b1": {"symbol": "AAPL", "stop_order_id": "OTHER"},
        }
        cancelled = []
        with mock.patch.object(dtm, "_load_state", lambda: state), \
             mock.patch.object(broker, "cancel_order", lambda oid: cancelled.append(oid) or True):
            dtm._cancel_daytrade_exit_legs("SPY")
        self.assertEqual(set(cancelled), {"OCO1", "ST", "TP"})   # SPY's three ids
        self.assertNotIn("OTHER", cancelled)                     # never another symbol's

    def test_never_raises_on_bad_state(self):
        with mock.patch.object(dtm, "_load_state", side_effect=RuntimeError("boom")):
            dtm._cancel_daytrade_exit_legs("SPY")  # must not raise


class HasLiveDaytradeStop(unittest.TestCase):
    def test_recognizes_oco_leg_by_recorded_id(self):
        state = {"entry::SPY::b1": {"symbol": "SPY", "oco_order_id": "OCO1", "stop_order_id": "ST", "tp_order_id": "TP"}}
        # A live order whose id matches the recorded OCO stop leg — its coid is NOT DT-tagged.
        leg = SimpleNamespace(id="ST", order_type="stop", type="stop", client_order_id="broker-generated-xyz")
        with mock.patch.object(dtm, "_load_state", lambda: state), \
             mock.patch.object(broker, "get_open_orders", lambda s: [leg]):
            self.assertIs(dtm._has_live_daytrade_stop("SPY"), True)

    def test_none_when_orderbook_unreadable(self):
        with mock.patch.object(broker, "get_open_orders", lambda s: None):
            self.assertIsNone(dtm._has_live_daytrade_stop("SPY"))

    def test_false_when_only_entry_limit_rests(self):
        state = {"entry::SPY::b1": {"symbol": "SPY", "stop_order_id": "ST"}}
        entry = SimpleNamespace(id="ENTRY", order_type="limit", type="limit", client_order_id="x")
        with mock.patch.object(dtm, "_load_state", lambda: state), \
             mock.patch.object(broker, "get_open_orders", lambda s: [entry]):
            self.assertIs(dtm._has_live_daytrade_stop("SPY"), False)


class TakeProfitHeal(unittest.TestCase):
    def test_tp_fill_recorded_as_take_profit(self):
        from strategy import day_tier_logger
        import trade_logger
        events = [
            {"event": "entry_fill", "trade_id": "T1"},
            {"event": "stop_placed", "stop_order_id": "ST"},
            {"event": "target_placed", "tp_order_id": "TP"},
        ]
        target = {"trade_id": "T1", "symbol": "SPY", "side": "long", "entry_price": 100.0, "qty": 2}
        logged = {}

        def _fill(oid, expected):
            return (True, 2.0, 110.0) if oid == "TP" else (True, 0.0, 0.0)  # TP filled, stop auto-cancelled

        def _log_exit(trade_id, symbol, *, order_id, exit_reason, fill_price, fill_qty, market_price_at_exit, realized_pnl):
            logged["reason"] = exit_reason
            logged["pnl"] = realized_pnl
            return True

        with mock.patch.object(day_tier_logger, "read_events_checked", lambda t: (events, True)), \
             mock.patch.object(dtm, "_confirmed_order_fill", _fill), \
             mock.patch.object(day_tier_logger, "log_exit_fill", _log_exit), \
             mock.patch.object(trade_logger, "log_event", lambda *a, **k: True):
            result = dtm._record_confirmed_stop_exit(target)
        self.assertIs(result, True)
        self.assertEqual(logged["reason"], "take_profit")     # NOT "protective_stop"
        self.assertEqual(logged["pnl"], 20.0)                 # (110-100)*2

    def test_stop_fill_still_labeled_protective_stop(self):
        from strategy import day_tier_logger
        import trade_logger
        events = [
            {"event": "stop_placed", "stop_order_id": "ST"},
            {"event": "target_placed", "tp_order_id": "TP"},
        ]
        target = {"trade_id": "T1", "symbol": "SPY", "side": "long", "entry_price": 100.0, "qty": 2}
        logged = {}

        def _fill(oid, expected):
            return (True, 2.0, 96.0) if oid == "ST" else (True, 0.0, 0.0)  # stop filled, TP auto-cancelled

        def _log_exit(trade_id, symbol, *, order_id, exit_reason, fill_price, fill_qty, market_price_at_exit, realized_pnl):
            logged["reason"] = exit_reason
            logged["pnl"] = realized_pnl
            return True

        with mock.patch.object(day_tier_logger, "read_events_checked", lambda t: (events, True)), \
             mock.patch.object(dtm, "_confirmed_order_fill", _fill), \
             mock.patch.object(day_tier_logger, "log_exit_fill", _log_exit), \
             mock.patch.object(trade_logger, "log_event", lambda *a, **k: True):
            result = dtm._record_confirmed_stop_exit(target)
        self.assertIs(result, True)
        self.assertEqual(logged["reason"], "protective_stop")
        self.assertEqual(logged["pnl"], -8.0)                 # (96-100)*2

    def test_double_fill_books_the_loss_not_the_gain(self):
        # Broker OCO anomaly: BOTH legs report a terminal fill (sibling-cancel lost a fast-market
        # race). The heal must book the LOSS (protective_stop), never the gain (masked-loss seat A).
        from strategy import day_tier_logger
        import trade_logger
        events = [
            {"event": "stop_placed", "stop_order_id": "ST"},
            {"event": "target_placed", "tp_order_id": "TP_PARENT"},
        ]
        target = {"trade_id": "T1", "symbol": "SPY", "side": "long", "entry_price": 100.0, "qty": 2}
        logged = {}

        def _fill(oid, expected):
            if oid == "ST":
                return (True, 2.0, 96.0)    # stop filled → −$8
            return (True, 2.0, 110.0)       # TP ALSO filled → +$20 (the anomaly)

        def _log_exit(trade_id, symbol, *, order_id, exit_reason, fill_price, fill_qty, market_price_at_exit, realized_pnl):
            logged["reason"] = exit_reason
            logged["pnl"] = realized_pnl
            return True

        with mock.patch.object(day_tier_logger, "read_events_checked", lambda t: (events, True)), \
             mock.patch.object(dtm, "_confirmed_order_fill", _fill), \
             mock.patch.object(day_tier_logger, "log_exit_fill", _log_exit), \
             mock.patch.object(trade_logger, "log_event", lambda *a, **k: True):
            result = dtm._record_confirmed_stop_exit(target)
        self.assertIs(result, True)
        self.assertEqual(logged["reason"], "protective_stop")  # stops-first → loss booked
        self.assertEqual(logged["pnl"], -8.0)                  # the LOSS, never the +20 gain


if __name__ == "__main__":
    unittest.main(verbosity=2)
