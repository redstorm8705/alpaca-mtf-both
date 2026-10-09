#!/usr/bin/env python3
# ruff: noqa: E501
"""
Tier-safety stage 1 (2026-10-08): a stop/close recovery in execution/broker.py cancels ONLY the
calling tier's own orders, and a wash-trade reject cancels nothing.

Replay of the 2026-10-07 EWY/AAPL incident shape: the day tier's protective stop (DT-) and the
swing tier's stop (IN-) rest on one symbol; the swing tier's GTC stop is rejected 40310000 with
related_orders naming BOTH. Before this fix the recovery cancelled every order on the symbol,
stripping the day tier's stop. Now the DT- order must survive.
Run: PYTHONPATH=. python3 -m pytest -q tests/test_broker_tier_safety.py
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

for _mod in ("alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums"):
    sys.modules.setdefault(_mod, mock.MagicMock())

from execution import broker as bk  # noqa: E402

DT_ID = "11111111-1111-1111-1111-111111111111"
IN_ID = "22222222-2222-2222-2222-222222222222"
UN_ID = "33333333-3333-3333-3333-333333333333"

HELD = ('{"code":40310000,"existing_qty":"5","held_for_orders":"5","message":"insufficient qty '
        'available for order (requested: 5, available: 0)","related_orders":["%s","%s"]}' % (DT_ID, IN_ID))
WASH = ('{"code":40310000,"message":"potential wash trade detected. use complex orders",'
        '"reject_reason":"opposite side market/stop order exists"}')


def _ord(oid, coid, side="sell", status="new"):
    return types.SimpleNamespace(id=oid, client_order_id=coid, symbol="EWY", side=side,
                                 type="stop", qty=5, status=status)


BOOK = {
    DT_ID: _ord(DT_ID, "DT-EWY-s-1-aaaa"),
    IN_ID: _ord(IN_ID, "IN-EWY-s-1-bbbb"),
    UN_ID: _ord(UN_ID, "manual-uuid-no-tag"),
}


class _Env:
    """Patches the broker's Alpaca touch points; records every cancel."""

    def __init__(self, submit_side_effect, book=None):
        self.cancelled: list[str] = []
        self.book = dict(BOOK if book is None else book)
        self.client = mock.Mock()
        self.client.submit_order.side_effect = submit_side_effect
        self.client.get_open_position.return_value = types.SimpleNamespace(side="long")
        self.alert = mock.Mock()

    def __enter__(self):
        def _cancel(oid):
            self.cancelled.append(str(oid))
            return True
        self._ps = [
            mock.patch.object(bk, "_get_trading_client", return_value=self.client),
            mock.patch.object(bk, "get_open_orders", side_effect=lambda *a, **k: list(self.book.values())),
            mock.patch.object(bk, "get_order", side_effect=lambda oid: self.book.get(oid)),
            mock.patch.object(bk, "cancel_order", side_effect=_cancel),
            mock.patch.object(bk, "_floor_bound_stop_qty", side_effect=lambda s, q, sd, t: q),
            mock.patch.object(bk.time, "sleep"),
            mock.patch.dict(sys.modules, {"alerts": types.SimpleNamespace(alert_gtc_failed=self.alert)}),
        ]
        for p in self._ps:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._ps):
            p.stop()


class WashTradeDetection(unittest.TestCase):
    def test_detects_both_message_forms(self):
        self.assertTrue(bk._is_wash_trade_reject(WASH))
        self.assertTrue(bk._is_wash_trade_reject("reject_reason: Opposite side market/stop order exists"))

    def test_held_for_orders_is_not_wash_trade(self):
        self.assertFalse(bk._is_wash_trade_reject(HELD))
        self.assertFalse(bk._is_wash_trade_reject(None))  # type: ignore[arg-type]


class GtcStopRecovery(unittest.TestCase):
    def test_replay_swing_recovery_leaves_day_tier_stop(self):
        """EWY replay: IN stop rejected 40310000 naming DT+IN orders → only IN cancelled, then retry OK."""
        ok = types.SimpleNamespace(id="new")
        with _Env([Exception(HELD), ok]) as env:
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIs(out, ok)
        self.assertIn(IN_ID, env.cancelled)
        self.assertNotIn(DT_ID, env.cancelled)
        self.assertNotIn(UN_ID, env.cancelled)

    def test_wash_trade_reject_cancels_nothing_and_pages(self):
        with _Env([Exception(WASH)]) as env:
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        self.assertEqual(env.cancelled, [])
        self.assertEqual(env.client.submit_order.call_count, 1)   # no poll
        env.alert.assert_called_once()

    def test_wash_trade_reject_with_opt_out_does_not_claim_protection(self):
        with _Env([Exception(WASH)]) as env:
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="intraday",
                                           allow_cancel_blocking=False)
        self.assertIsNone(out)
        self.assertEqual(env.cancelled, [])

    def test_only_foreign_blockers_no_cancel_no_poll(self):
        """Only another tier's / an untagged order holds the qty → nothing cancelled, no 63s poll, page."""
        book = {DT_ID: BOOK[DT_ID], UN_ID: BOOK[UN_ID]}
        err = HELD.replace(IN_ID, UN_ID)
        with _Env([Exception(err)], book=book) as env:
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        self.assertEqual(env.cancelled, [])
        self.assertEqual(env.client.submit_order.call_count, 1)
        env.alert.assert_called_once()

    def test_partial_foreign_hold_protects_free_shares(self):
        """Board C1: the day tier's stop holds 5 of 10 → the swing tier stops the 5 free shares, cancels nothing."""
        ok = types.SimpleNamespace(id="rem")
        book = {DT_ID: BOOK[DT_ID]}
        err = ('{"code":40310000,"existing_qty":"10","held_for_orders":"5","message":"insufficient qty '
               'available for order (requested: 10, available: 5)","related_orders":["%s"]}' % DT_ID)
        with _Env([Exception(err), ok], book=book) as env:
            out = bk.submit_gtc_stop_order("EWY", 10, "sell", 100.0, tier="intraday")
        self.assertIs(out, ok)
        self.assertEqual(env.cancelled, [])
        self.assertEqual(env.client.submit_order.call_count, 2)
        env.alert.assert_not_called()

    def test_foreign_profit_limit_is_not_protection(self):
        """Cold-2nd N1: a manual sell LIMIT holds 5 of 10 → no free-share stop claim; page + None."""
        lim = types.SimpleNamespace(id=UN_ID, client_order_id="manual", symbol="EWY", side="sell",
                                    type="limit", qty=5, status="new")
        err = ('{"code":40310000,"existing_qty":"10","held_for_orders":"5","message":"insufficient qty '
               'available for order (requested: 10, available: 5)"}')
        with _Env([Exception(err)], book={UN_ID: lim}) as env:
            out = bk.submit_gtc_stop_order("EWY", 10, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        self.assertEqual(env.cancelled, [])
        self.assertEqual(env.client.submit_order.call_count, 1)
        env.alert.assert_called_once()

    def test_enum_side_foreign_stop(self):
        """Cold-2nd N4: alpaca-py returns enum sides/types ("OrderSide.SELL"-style objects with .value)."""
        E = lambda v: types.SimpleNamespace(value=v)  # noqa: E731
        dt = types.SimpleNamespace(id=DT_ID, client_order_id="DT-EWY-s-1-a", symbol="EWY", side=E("sell"),
                                   type=E("stop"), qty=5, status="new")
        ok = types.SimpleNamespace(id="rem")
        err = ('{"code":40310000,"existing_qty":"10","held_for_orders":"5","message":"insufficient qty '
               'available for order (requested: 10, available: 5)"}')
        with _Env([Exception(err), ok], book={DT_ID: dt}) as env:
            out = bk.submit_gtc_stop_order("EWY", 10, "sell", 100.0, tier="intraday")
        self.assertIs(out, ok)
        self.assertEqual(env.cancelled, [])

    def test_inconsistent_qty_body_pages(self):
        """held_for_orders=0 with available < existing is not a clean foreign hold → page, no partial stop."""
        book = {DT_ID: BOOK[DT_ID]}
        err = ('{"code":40310000,"existing_qty":"5","held_for_orders":"0","available":"3",'
               '"message":"insufficient qty available for order (requested: 5, available: 3)"}')
        with _Env([Exception(err)], book=book) as env:
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        self.assertEqual(env.client.submit_order.call_count, 1)
        env.alert.assert_called_once()

    def test_exact_prod_wash_trade_string(self):
        prod = ('{"code":40310000,"message":"potential wash trade detected. use complex orders",'
                '"reject_reason":"opposite side market/stop order exists"}')
        self.assertTrue(bk._is_wash_trade_reject(prod))

    def test_lingering_reservation_keeps_legacy_poll(self):
        """MSTR case: our order was already cancelled, nothing rests on the book, Alpaca still holds the
        qty for a moment → the 63s poll must still run (it is what releases the stop)."""
        ok = types.SimpleNamespace(id="new")
        err = '{"code":40310000,"held_for_orders":"5","message":"insufficient qty available"}'
        with _Env([Exception(err), Exception(err), ok], book={}) as env:
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIs(out, ok)
        self.assertEqual(env.client.submit_order.call_count, 3)
        env.alert.assert_not_called()

    def test_own_related_order_already_cancelled_keeps_poll(self):
        """related_orders names our own (already-cancelled) order; a foreign order also rests → we still
        poll, because the own reservation is what is releasing."""
        ok = types.SimpleNamespace(id="new")
        book = {DT_ID: BOOK[DT_ID]}
        lookup = dict(BOOK)
        with _Env([Exception(HELD), ok], book=book) as env, \
                mock.patch.object(bk, "get_order", side_effect=lambda oid: lookup.get(oid)):
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIs(out, ok)
        self.assertNotIn(DT_ID, env.cancelled)

    def test_wash_trade_during_poll_stops_polling(self):
        with _Env([Exception(HELD), Exception(WASH), types.SimpleNamespace(id="x")]) as env:
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        self.assertEqual(env.client.submit_order.call_count, 2)
        self.assertNotIn(DT_ID, env.cancelled)
        env.alert.assert_called_once()

    def test_qhm_recovery_never_cancels_swing_stop(self):
        ok = types.SimpleNamespace(id="new")
        book = dict(BOOK)
        qh = "44444444-4444-4444-4444-444444444444"
        book[qh] = _ord(qh, "QH-EWY-s-1-cccc")
        err = HELD.replace(DT_ID, qh)
        with _Env([Exception(err), ok], book=book) as env:
            out = bk.submit_gtc_stop_order("EWY", 5, "sell", 100.0, tier="qhm")
        self.assertIs(out, ok)
        self.assertEqual(sorted(set(env.cancelled)), [qh])


class DayStopRecovery(unittest.TestCase):
    def test_day_stop_retry_cancels_own_tier_only(self):
        ok = types.SimpleNamespace(id="new")
        with _Env([Exception(HELD), ok]) as env:
            out = bk.submit_day_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIs(out, ok)
        self.assertEqual(env.cancelled, [IN_ID])

    def test_day_stop_wash_trade_cancels_nothing(self):
        with _Env([Exception(WASH)]) as env:
            out = bk.submit_day_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        env.alert.assert_called_once()
        self.assertEqual(env.cancelled, [])
        self.assertEqual(env.client.submit_order.call_count, 1)


    def test_day_stop_foreign_hold_protects_free_shares(self):
        """Adversarial C4: DAY path — the day tier's stop holds 5 of 10 → the swing tier's DAY stop covers the 5 free."""
        ok = types.SimpleNamespace(id="rem")
        err = ('{"code":40310000,"existing_qty":"10","held_for_orders":"5","message":"insufficient qty '
               'available for order (requested: 10, available: 5)"}')
        with _Env([Exception(err), ok], book={DT_ID: BOOK[DT_ID]}) as env:
            out = bk.submit_day_stop_order("EWY", 10, "sell", 100.0, tier="intraday")
        self.assertIs(out, ok)
        self.assertEqual(env.cancelled, [])
        env.alert.assert_not_called()

    def test_day_stop_foreign_limit_hold_pages(self):
        lim = types.SimpleNamespace(id=UN_ID, client_order_id="manual", symbol="EWY", side="sell",
                                    type="limit", qty=5, status="new")
        err = ('{"code":40310000,"existing_qty":"10","held_for_orders":"5","message":"insufficient qty '
               'available for order (requested: 10, available: 5)"}')
        with _Env([Exception(err)], book={UN_ID: lim}) as env:
            out = bk.submit_day_stop_order("EWY", 10, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        self.assertEqual(env.cancelled, [])
        self.assertEqual(env.client.submit_order.call_count, 1)
        env.alert.assert_called_once()

    def test_day_stop_failed_retry_pages(self):
        with _Env([Exception(HELD), Exception(HELD)]) as env:
            out = bk.submit_day_stop_order("EWY", 5, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        self.assertEqual(env.cancelled, [IN_ID])
        env.alert.assert_called_once()

    def test_partly_filled_foreign_stop_counts_only_unfilled(self):
        """A foreign stop for 5 with 3 already filled covers only 2 → held 5 not proven covered → page."""
        dt = types.SimpleNamespace(id=DT_ID, client_order_id="DT-EWY-s-1-a", symbol="EWY", side="sell",
                                   type="stop", qty=5, filled_qty=3, status="partially_filled")
        err = ('{"code":40310000,"existing_qty":"10","held_for_orders":"5","message":"insufficient qty '
               'available for order (requested: 10, available: 5)"}')
        with _Env([Exception(err)], book={DT_ID: dt}) as env:
            out = bk.submit_gtc_stop_order("EWY", 10, "sell", 100.0, tier="intraday")
        self.assertIsNone(out)
        env.alert.assert_called_once()


class PartialCloseRecovery(unittest.TestCase):
    def test_swing_partial_close_cancels_own_tier_only(self):
        ok = types.SimpleNamespace(id="new")
        with _Env([Exception(HELD), ok]) as env:
            out = bk.partial_close_position("EWY", 2, tier="intraday")
        self.assertTrue(out)
        self.assertEqual(env.cancelled, [IN_ID])

    def test_partial_close_wash_trade_cancels_nothing(self):
        with _Env([Exception(WASH)]) as env:
            out = bk.partial_close_position("EWY", 2, tier="intraday")
        self.assertFalse(out)
        self.assertEqual(env.cancelled, [])


class MarketOrderWashTrade(unittest.TestCase):
    def test_wash_trade_is_not_cached_as_short_block(self):
        bk._short_blocked_symbols.discard("EWY")
        with _Env([Exception(WASH)]):
            out = bk.submit_market_order("EWY", 1, "sell", tier="intraday")
        self.assertIsNone(out)
        self.assertNotIn("EWY", bk._short_blocked_symbols)

    def test_real_short_block_still_cached(self):
        bk._short_blocked_symbols.discard("ZZZ")
        with _Env([Exception('{"code":40310000,"message":"account is not allowed to short"}')]):
            bk.submit_market_order("ZZZ", 1, "sell", tier="intraday")
        self.assertIn("ZZZ", bk._short_blocked_symbols)
        bk._short_blocked_symbols.discard("ZZZ")

    def test_buy_to_cover_reject_is_not_cached_as_short_block(self):
        # EWY 2026-10-08: a BUY covering a short, rejected because a resting buy stop holds the share.
        bk._short_blocked_symbols.discard("EWY")
        held = ('{"available":"0","code":40310000,"existing_qty":"1","held_for_orders":"1",'
                '"message":"insufficient qty available for order (requested: 1, available: 0)"}')
        with _Env([Exception(held)]):
            out = bk.submit_market_order("EWY", 1, "buy", tier="intraday")
        self.assertIsNone(out)
        self.assertNotIn("EWY", bk._short_blocked_symbols)

    def test_sell_held_for_orders_is_not_cached_as_short_block(self):
        bk._short_blocked_symbols.discard("YYY")
        held = ('{"available":"0","code":40310000,"held_for_orders":"2",'
                '"message":"insufficient qty available for order (requested: 2, available: 0)"}')
        with _Env([Exception(held)]):
            bk.submit_market_order("YYY", 2, "sell", tier="intraday")
        self.assertNotIn("YYY", bk._short_blocked_symbols)


if __name__ == "__main__":
    unittest.main()
