#!/usr/bin/env python3
# ruff: noqa: E501
"""
Regression harness for execution.broker._hold_state and the allow_cancel_blocking opt-out.

PRE-WIRE-BLOCKER-3 (found by cold-2nd 2026-07-20): _hold_state previously returned
_HOLD_STABLE for ANY order list without a pending_cancel — INCLUDING AN EMPTY LIST — and never
checked side, type or qty. Because a STABLE answer becomes PROTECTION_ALREADY_HELD, which a
protection-asserting caller reads as "protected, do not page", that was an UNVERIFIED CLAIM OF
PROTECTION: the exact never-mask-a-loss violation the sentinel was built to prevent.

The tests that BEHAVIOURALLY catch this bug (would return the wrong answer under the old logic,
not merely a TypeError from the old 1-arg signature) are the empty-book / resting-limit /
wrong-side / partial-cover / unparseable-qty cases. The remainder are invariant guards
(covering-stop → STABLE, pending_cancel → UNSTABLE, unreadable → UNKNOWN, default-path-unchanged)
that lock in correct behaviour so a future edit cannot regress it.
Run: PYTHONPATH=. python3 -m unittest tests.test_broker_hold_state
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

# Stub the Alpaca SDK — broker.py imports it at module load; every call we exercise is patched.
for _mod in ("alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums"):
    sys.modules.setdefault(_mod, mock.MagicMock())

from execution import broker as bk  # noqa: E402  (after the SDK stub)


def _o(side="sell", otype="stop", qty=3, status="new", oid="o1"):
    return types.SimpleNamespace(id=oid, symbol="X", side=side, type=otype, qty=qty, status=status)


class HoldStateProvesProtection(unittest.TestCase):
    """_hold_state must only say STABLE when a reducing-side stop actually covers the qty."""

    def _state(self, orders, side="sell", qty=3):
        with mock.patch.object(bk, "get_open_orders", mock.Mock(return_value=orders)):
            return bk._hold_state("X", side, qty)

    # THE REGRESSION: an empty book is NOT protection.
    def test_empty_book_is_not_stable(self):
        self.assertEqual(self._state([]), bk._HOLD_NO_COVER)

    def test_covering_reducing_stop_is_stable(self):
        self.assertEqual(self._state([_o(side="sell", otype="stop", qty=3)]), bk._HOLD_STABLE)

    def test_multiple_stops_summing_to_cover_is_stable(self):
        self.assertEqual(
            self._state([_o(qty=1, oid="a"), _o(qty=2, oid="b")]), bk._HOLD_STABLE)

    # A limit order can hold the qty (explaining the 40310000) but is NOT protection.
    def test_reducing_limit_is_not_protection(self):
        self.assertEqual(self._state([_o(otype="limit", qty=3)]), bk._HOLD_NO_COVER)

    # Wrong side: a buy order does not protect a long.
    def test_wrong_side_stop_is_not_protection(self):
        self.assertEqual(self._state([_o(side="buy", otype="stop", qty=3)]), bk._HOLD_NO_COVER)

    # Partial cover is not cover.
    def test_partial_cover_is_not_stable(self):
        self.assertEqual(self._state([_o(qty=1)], qty=3), bk._HOLD_NO_COVER)

    def test_short_position_buy_stop_is_stable(self):
        self.assertEqual(
            self._state([_o(side="buy", otype="stop", qty=2)], side="buy", qty=2),
            bk._HOLD_STABLE)

    # pending_cancel still wins over everything — the hold is about to release.
    def test_pending_cancel_is_unstable_even_when_covered(self):
        self.assertEqual(
            self._state([_o(qty=3, status="pending_cancel")]), bk._HOLD_UNSTABLE)

    def test_unreadable_book_is_unknown(self):
        self.assertEqual(self._state(None), bk._HOLD_UNKNOWN)

    def test_raising_book_is_unknown(self):
        def _boom(_s=None):
            raise RuntimeError("api down")
        with mock.patch.object(bk, "get_open_orders", _boom):
            self.assertEqual(bk._hold_state("X", "sell", 3), bk._HOLD_UNKNOWN)

    # Enum-style side/type (Alpaca SDK returns enums with .value) must be handled.
    def test_enum_style_side_and_type(self):
        _enum_o = types.SimpleNamespace(
            id="e1", symbol="X", status="new", qty=3,
            side=types.SimpleNamespace(value="sell"),
            type=types.SimpleNamespace(value="stop"))
        self.assertEqual(self._state([_enum_o]), bk._HOLD_STABLE)

    # An unparseable qty must not be counted as cover (fail loud, never assume).
    def test_unparseable_qty_does_not_count_as_cover(self):
        self.assertEqual(self._state([_o(qty="abc")]), bk._HOLD_NO_COVER)

    # stop_limit and trailing_stop both contain "stop" and both genuinely defend the position,
    # so they must count toward cover (deliberate — see the in-code note).
    def test_stop_limit_counts_as_cover(self):
        self.assertEqual(self._state([_o(otype="stop_limit", qty=3)]), bk._HOLD_STABLE)

    def test_trailing_stop_counts_as_cover(self):
        self.assertEqual(self._state([_o(otype="trailing_stop", qty=3)]), bk._HOLD_STABLE)

    # Epsilon boundary: exact cover is STABLE; one share short is NO_COVER.
    def test_exact_cover_is_stable(self):
        self.assertEqual(self._state([_o(qty=3)], qty=3), bk._HOLD_STABLE)

    def test_one_share_short_is_no_cover(self):
        self.assertEqual(self._state([_o(qty=2)], qty=3), bk._HOLD_NO_COVER)


class OptOutMapsHoldStateToSentinels(unittest.TestCase):
    """End-to-end: submit_*_stop_order(allow_cancel_blocking=False) must map hold state to the
    right sentinel, and must NEVER cancel anything."""

    def setUp(self):
        self.cancels = []
        self._p = [
            mock.patch.object(bk, "cancel_open_orders_for_symbol",
                              mock.Mock(side_effect=lambda s: self.cancels.append(s) or 1)),
            mock.patch.object(bk, "_floor_bound_stop_qty",
                              mock.Mock(side_effect=lambda symbol, qty, side, tier: qty)),
            mock.patch.object(bk.time, "sleep", mock.Mock()),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _submit(self, fn, orders, err='{"code":40310000,"message":"insufficient qty available"}'):
        class _C:
            def submit_order(_self, _od):
                raise Exception(err)
        with mock.patch.object(bk, "_get_trading_client", mock.Mock(return_value=_C())), \
             mock.patch.object(bk, "get_open_orders", mock.Mock(return_value=orders)):
            return fn("X", 3, "sell", 100.0, allow_cancel_blocking=False)

    def test_gtc_empty_book_returns_none_not_held(self):
        for fn in (bk.submit_gtc_stop_order, bk.submit_day_stop_order):
            with self.subTest(fn=fn.__name__):
                r = self._submit(fn, [])
                self.assertIsNone(r, "empty book must NOT be reported as protected")
                self.assertIsNot(r, bk.PROTECTION_ALREADY_HELD)
        self.assertEqual(self.cancels, [], "opt-out must never cancel")

    def test_covering_stop_returns_already_held(self):
        for fn in (bk.submit_gtc_stop_order, bk.submit_day_stop_order):
            with self.subTest(fn=fn.__name__):
                self.assertIs(self._submit(fn, [_o(qty=3)]), bk.PROTECTION_ALREADY_HELD)
        self.assertEqual(self.cancels, [], "opt-out must never cancel")

    def test_resting_limit_returns_none(self):
        for fn in (bk.submit_gtc_stop_order, bk.submit_day_stop_order):
            with self.subTest(fn=fn.__name__):
                self.assertIsNone(self._submit(fn, [_o(otype="limit", qty=3)]))
        self.assertEqual(self.cancels, [])

    def test_unreadable_book_returns_unknown(self):
        for fn in (bk.submit_gtc_stop_order, bk.submit_day_stop_order):
            with self.subTest(fn=fn.__name__):
                self.assertIs(self._submit(fn, None), bk.PROTECTION_UNKNOWN)
        self.assertEqual(self.cancels, [])

    def test_cover_proven_against_requested_qty_not_floor_reduced(self):
        """PRE-WIRE-BLOCKER-3 hardening: _hold_state must prove cover against the qty ORIGINALLY
        REQUESTED, never the floor-reduced one. Here the floor bounds 10→3 but only a 3-share
        stop rests. Proving against the reduced 3 would (wrongly) say STABLE while 7 shares are
        naked; proving against the requested 10 correctly says NO_COVER → None → caller pages."""
        seen = {}
        def _capture_hold_state(symbol, side, qty):
            seen["qty"] = qty
            # a 3-share covering stop is on the book
            with mock.patch.object(bk, "get_open_orders", mock.Mock(return_value=[_o(qty=3)])):
                return _real_hold_state(symbol, side, qty)
        _real_hold_state = bk._hold_state
        class _C:
            def submit_order(_self, _od):
                raise Exception('{"code":40310000,"message":"insufficient qty available"}')
        with mock.patch.object(bk, "_floor_bound_stop_qty", mock.Mock(return_value=3)), \
             mock.patch.object(bk, "_hold_state", _capture_hold_state), \
             mock.patch.object(bk, "_get_trading_client", mock.Mock(return_value=_C())):
            r = bk.submit_day_stop_order("X", 10, "sell", 100.0, allow_cancel_blocking=False)
        self.assertEqual(seen["qty"], 10, "must prove cover against the REQUESTED qty (10), not floor-reduced (3)")
        self.assertIsNone(r, "3-share stop does NOT cover a 10-share request → None (page), not HELD")

    def test_default_path_still_cancels_and_is_unchanged(self):
        """The MSTR recovery (board 26-0) must be untouched when the caller does NOT opt out."""
        class _C:
            def submit_order(_self, _od):
                raise Exception('{"code":40310000,"message":"insufficient qty available"}')
        with mock.patch.object(bk, "_get_trading_client", mock.Mock(return_value=_C())), \
             mock.patch.object(bk, "get_open_orders", mock.Mock(return_value=[])):
            r = bk.submit_day_stop_order("X", 3, "sell", 100.0)      # default allow_cancel_blocking=True
        self.assertIsNone(r)
        self.assertEqual(self.cancels, ["X"], "default path MUST still cancel blocking orders")


if __name__ == "__main__":
    unittest.main(verbosity=2)
