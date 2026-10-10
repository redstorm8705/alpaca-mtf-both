#!/usr/bin/env python3
# ruff: noqa: E501
"""Co-hold P1 (2026-10-10, board McKinney/Taleb): the close-fill lookup must never price a Swing exit at a Day-tier
fill. With a co-held Day lot the most recent same-side fill on the symbol can be the Day tier's OCO stop leg (an
untagged child of a DT- parent). Both lookup paths (external-close and submitted_after) drop DT- orders with their
legs. Design record: logs/design_records/same_direction_cohold_2026-10-10.md (P1).
Run: PYTHONPATH=. python3 -m pytest -q tests/test_fill_helpers_day_exclusion.py
"""
from __future__ import annotations

import time
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from execution import fill_helpers as fh

PT = ZoneInfo("America/Los_Angeles")


def _o(coid, side, price, filled_at="2026-10-12T15:00:00Z", created_at="2026-10-12T14:00:00Z", legs=None, otype="market"):
    return types.SimpleNamespace(client_order_id=coid, side=side, filled_avg_price=price, filled_at=filled_at,
                                 created_at=created_at, id=coid, type=otype, stop_price=None, legs=legs)


def _trade(direction="long", entry=100.0):
    return {"direction": direction, "entry_price": entry,
            "entry_time": (datetime.now(PT) - timedelta(hours=2)).isoformat()}


class NonDayOrders(unittest.TestCase):
    def test_day_parent_dropped_with_its_legs(self):
        leg = _o("leg-uuid", "sell", 95.0)
        out = fh._non_day_orders([_o("DT-NVDA-b-1-aaaa", "buy", 100.0, legs=[leg]), _o("IN-NVDA-s-1-bbbb", "sell", 97.0)])
        self.assertEqual([x.client_order_id for x in out], ["IN-NVDA-s-1-bbbb"])

    def test_other_parent_kept_with_its_legs(self):
        leg = _o("leg-uuid", "sell", 95.0)
        out = fh._non_day_orders([_o("IN-NVDA-s-1-bbbb", "sell", 0, legs=[leg]), _o("manual", "sell", 96.0)])
        self.assertEqual([x.client_order_id for x in out], ["IN-NVDA-s-1-bbbb", "leg-uuid", "manual"])

    def test_empty_and_none(self):
        self.assertEqual(fh._non_day_orders(None), [])
        self.assertEqual(fh._non_day_orders([]), [])


class RecoverFillSkipsDayFills(unittest.TestCase):
    def _recover(self, orders, **kw):
        client = mock.Mock()
        client.get_orders.return_value = orders
        with mock.patch.object(fh, "get_trading_client", return_value=client), \
                mock.patch.object(fh.time, "sleep"), \
                mock.patch.object(fh, "_log_fill_latency"):
            out = fh._recover_fill("NVDA", kw.pop("trade", _trade()), no_retry=True, **kw)
        return out, client

    def test_external_close_ignores_a_later_day_stop_leg(self):
        # Swing stop sold at $97 at 15:00; the Day OCO stop leg sold at $95 later (15:30) — the Swing exit is $97.
        day_leg = _o("leg-uuid", "sell", 95.0, filled_at="2026-10-12T15:30:00Z", otype="stop")
        orders = [_o("DT-NVDA-s-1-aaaa", "sell", None, legs=[day_leg]),
                  _o("DT-NVDA-b-1-cccc", "buy", 99.0, filled_at="2026-10-12T15:10:00Z"),
                  _o("IN-NVDA-s-1-bbbb", "sell", 97.0, filled_at="2026-10-12T15:00:00Z", otype="stop")]
        price, client = self._recover(orders)
        self.assertEqual(price, 97.0)
        self.assertTrue(client.get_orders.call_args.kwargs["filter"].nested)

    def test_external_close_only_day_fills_is_unverified(self):
        day_leg = _o("leg-uuid", "sell", 95.0, otype="stop")
        price, _ = self._recover([_o("DT-NVDA-s-1-aaaa", "sell", None, legs=[day_leg])])
        self.assertIsNone(price)

    def test_external_close_swing_oco_leg_still_counts(self):
        own_leg = _o("leg-uuid", "sell", 96.0, otype="stop")
        price, _ = self._recover([_o("IN-NVDA-s-1-bbbb", "sell", None, legs=[own_leg])])
        self.assertEqual(price, 96.0)

    def test_submitted_after_skips_a_day_entry_created_after_our_close(self):
        # Our market close (created first) has not filled yet; a Day buy created after it has — not our fill.
        orders = [_o("IN-NVDA-s-1-bbbb", "sell", None, created_at="2026-10-12T15:00:00Z"),
                  _o("DT-NVDA-b-1-cccc", "buy", 99.0, created_at="2026-10-12T15:00:01Z")]
        price, client = self._recover(orders, submitted_after=time.time() - 5)
        self.assertIsNone(price)
        self.assertTrue(client.get_orders.call_args.kwargs["filter"].nested)

    def test_submitted_after_returns_our_fill(self):
        orders = [_o("IN-NVDA-s-1-bbbb", "sell", 98.0, created_at="2026-10-12T15:00:00Z")]
        price, _ = self._recover(orders, submitted_after=time.time() - 5)
        self.assertEqual(price, 98.0)


if __name__ == "__main__":
    unittest.main()
