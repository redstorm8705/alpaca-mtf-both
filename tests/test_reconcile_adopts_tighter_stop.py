#!/usr/bin/env python3
# ruff: noqa: E501
"""GTC reconcile never loosens a stop (Claude 2026-10-13, PLTR).

The operator cancelled the bot's PLTR sell stop ($181.72) and placed a tighter one ($205.40) in Alpaca. The startup /
pre-market reconcile read the stored (cancelled) id, cleared it and re-submitted a stop at the tracker's $181.72; an
in-place replace was compared against the tracker and cancelled as "stale". Now: a cancelled/missing stored stop adopts
the single live operator/Swing stop for the same shares, and a live stop tighter than the tracker's becomes the
tracker's stop, so every later path (adoption, pre-RTH cancel + DAY-stop resubmit) uses the tighter level.
Runs where the Alpaca SDK is installed (OCI / CI); skipped otherwise.
"""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from execution import orphan_manager as om
except ModuleNotFoundError:  # pragma: no cover — local Mac without alpaca-py
    om = None


def _order(oid, status, px, qty=1, side="sell", coid="df772278-aaed-4e55-8303-884d50dc59f4", tif="gtc"):
    return SimpleNamespace(id=oid, status=status, stop_price=px, qty=qty, side=side, type="stop",
                           time_in_force=tif, client_order_id=coid, filled_avg_price=None)


class _Tracker:
    def __init__(self, trade):
        self.open_trades = {"PLTR": trade}

    def get_overnight_gtc_positions(self):
        return [(s, t) for s, t in self.open_trades.items() if t.get("gtc_stop_order_id")]

    def set_gtc_stop_order_id(self, sym, oid):
        self.open_trades[sym]["gtc_stop_order_id"] = oid

    def clear_gtc_stop_order_id(self, sym):
        self.open_trades[sym]["gtc_stop_order_id"] = None

    def _save_log(self):
        pass


def _pltr(**kw):
    t = {"status": "open", "direction": "long", "qty": 2, "qty_remaining": 1, "entry_price": 181.72,
         "stop": 181.72, "trail_stop": None, "gtc_stop_order_id": "old-id", "overnight": True}
    t.update(kw)
    return t


@unittest.skipIf(om is None, "alpaca-py not installed")
class NeverLoosen(unittest.TestCase):
    def _run(self, trade, stored, open_orders, phase="closed"):
        tracker = _Tracker(trade)
        sent, cancelled = [], []

        def _submit(sym, qty, side, px):
            sent.append((sym, qty, side, px))
            return SimpleNamespace(id="bot-new")

        def _cancel(oid):
            cancelled.append(oid)
            return True
        fake_data = SimpleNamespace(get_latest_trade=lambda s: 208.7)
        with mock.patch.object(om, "get_tod_phase", return_value=phase), \
                mock.patch.object(om, "_is_trading_day_today", return_value=True), \
                mock.patch.object(om, "resolve_live_order", return_value=(stored, "old-id")), \
                mock.patch.object(om, "get_open_orders", return_value=open_orders), \
                mock.patch.object(om, "get_open_position", return_value=SimpleNamespace(qty=1)), \
                mock.patch.object(om, "submit_gtc_stop_order", side_effect=_submit), \
                mock.patch.object(om, "cancel_order", side_effect=_cancel), \
                mock.patch.object(om, "send_slack"), mock.patch.object(om, "alert_gtc_failed"), \
                mock.patch("trade_logger.log_event"), \
                mock.patch("pathlib.Path.exists", return_value=True), \
                mock.patch("pathlib.Path.read_text", return_value="{}"), \
                mock.patch.dict(sys.modules, {"data.alpaca_data": fake_data}):
            om.cancel_and_reconcile_gtc_stops(tracker)     # QHM state readable and empty
        return tracker.open_trades["PLTR"], sent, cancelled

    def test_operator_cancel_and_new_tighter_stop_is_adopted(self):
        manual = _order("e6b2e72e", "accepted", 205.40)
        trade, sent, cancelled = self._run(_pltr(), _order("old-id", "canceled", 181.72), [manual])
        self.assertEqual(trade["gtc_stop_order_id"], "e6b2e72e")
        self.assertEqual(trade["stop"], 205.40)
        self.assertEqual(sent, [])                       # no second, looser stop
        self.assertEqual(cancelled, [])                  # closed phase: the adopted stop stays

    def test_premarket_cancel_then_resubmit_level_is_tighter(self):
        manual = _order("e6b2e72e", "accepted", 205.40)
        trade, _sent, cancelled = self._run(_pltr(), _order("old-id", "canceled", 181.72), [manual], phase="premarket")
        self.assertEqual(trade["stop"], 205.40)          # the RTH DAY stop is placed from this level
        self.assertEqual(cancelled, ["e6b2e72e"])

    def test_live_tighter_stop_on_stored_id_is_adopted(self):
        trade, sent, _ = self._run(_pltr(gtc_stop_order_id="old-id"), _order("old-id", "accepted", 205.40), [])
        self.assertEqual(trade["stop"], 205.40)
        self.assertEqual(sent, [])

    def test_trail_field_is_raised_when_set(self):
        trade, _s, _c = self._run(_pltr(trail_stop=200.00), _order("old-id", "accepted", 205.40), [])
        self.assertEqual((trade["trail_stop"], trade["stop"]), (205.40, 181.72))

    def test_looser_live_stop_is_not_adopted(self):
        trade, _s, cancelled = self._run(_pltr(stop=205.40), _order("old-id", "accepted", 181.72), [])
        self.assertEqual(trade["stop"], 205.40)
        self.assertEqual(cancelled, ["old-id"])          # stale (looser) stop still cancelled + resubmitted

    def test_short_side_tighter_is_lower(self):
        t = _pltr(direction="short", stop=181.46, qty=1, qty_remaining=1)
        trade, _s, _c = self._run(t, _order("old-id", "accepted", 172.80, side="buy"), [])
        self.assertEqual(trade["stop"], 172.80)

    def test_ambiguous_or_foreign_replacement_is_not_adopted(self):
        two = [_order("a", "accepted", 205.40), _order("b", "accepted", 200.00)]
        trade, _s, _c = self._run(_pltr(), _order("old-id", "canceled", 181.72), two)
        self.assertNotIn(trade["gtc_stop_order_id"], ("a", "b"))
        qhm_tagged = [_order("q", "accepted", 205.40, coid="QH-PLTR-1")]
        trade2, _s2, _c2 = self._run(_pltr(), _order("old-id", "canceled", 181.72), qhm_tagged)
        self.assertNotEqual(trade2["gtc_stop_order_id"], "q")

    def test_adopted_stop_past_entry_blocks_breakeven_pulldown(self):
        manual = _order("e6b2e72e", "accepted", 205.40)
        trade, _s, _c = self._run(_pltr(), _order("old-id", "canceled", 181.72), [manual])
        self.assertTrue(trade["be_stop_promoted"])
        below = _order("old-id", "accepted", 179.00)
        trade2, _s2, _c2 = self._run(_pltr(stop=170.0), below, [])
        self.assertEqual(trade2["stop"], 179.00)
        self.assertFalse(trade2.get("be_stop_promoted", False))   # still below entry: promotion stays available

    def test_stop_limit_or_trailing_operator_order_is_not_adopted(self):
        sl = _order("sl", "accepted", 205.40)
        sl.type = "stop_limit"
        trade, _s, _c = self._run(_pltr(), _order("old-id", "canceled", 181.72), [sl])
        self.assertNotEqual(trade["gtc_stop_order_id"], "sl")

    def test_original_stop_frozen_before_adoption(self):
        trade, _s, _c = self._run(_pltr(), _order("old-id", "accepted", 205.40), [])
        self.assertEqual((trade["original_stop"], trade["stop"]), (181.72, 205.40))
        kept, _s2, _c2 = self._run(_pltr(original_stop=162.0), _order("old-id", "accepted", 205.40), [])
        self.assertEqual(kept["original_stop"], 162.0)

    def test_wrong_share_count_is_not_adopted(self):
        trade, _s, _c = self._run(_pltr(), _order("old-id", "accepted", 205.40, qty=2), [])
        self.assertEqual(trade["stop"], 181.72)


if __name__ == "__main__":
    unittest.main()
