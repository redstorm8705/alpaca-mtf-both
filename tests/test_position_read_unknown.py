#!/usr/bin/env python3
# ruff: noqa: E501
"""SAFE-1024: a raised position read (429 / 5xx / timeout) is UNKNOWN — never "flat", never "size from the tracker".

broker.get_open_position returns None only on a confirmed 404 and raises on any other error. Each test drives the real
function with a raising broker mock and proves: no "closed" result, no order sent, and the confirmed-404 meaning
unchanged. Every broker call is mocked."""
from __future__ import annotations

import unittest
from unittest import mock

from execution import day_trade_manager as dtm
from execution import exit_logic


class _ReadError(Exception):
    pass


def _raise(*_a, **_k):
    raise _ReadError("HTTP 429 too many requests")


class DayTierFlatten(unittest.TestCase):
    def test_unreadable_position_is_not_reported_closed(self):
        with mock.patch("execution.broker.get_open_position", side_effect=_raise), \
                mock.patch("execution.broker.partial_close_position") as close, \
                mock.patch.object(dtm, "_page") as page:
            ok = dtm.flatten_position("AAA", 10, "long", entry_price=100.0, trade_id="DT-AAA-1",
                                      reason="stop_unconfirmed_flatten")
        self.assertFalse(ok)
        close.assert_not_called()
        page.assert_called_once()

    def test_confirmed_404_is_still_flat(self):
        with mock.patch("execution.broker.get_open_position", return_value=None), \
                mock.patch("execution.broker.partial_close_position") as close:
            self.assertTrue(dtm.flatten_position("AAA", 10, "long", entry_price=100.0, trade_id="DT-AAA-1"))
        close.assert_not_called()

    def test_force_flat_retries_on_unreadable_position(self):
        tgt = {"symbol": "AAA", "side": "long", "qty": 10, "entry_price": 100.0, "trade_id": "DT-AAA-1", "order_id": "o"}
        with mock.patch.object(dtm, "_closed_in_log", return_value=False), \
                mock.patch("execution.broker.get_open_position", side_effect=_raise), \
                mock.patch.object(dtm, "flatten_position") as fp:
            self.assertEqual(dtm._force_flat_one(tgt, "eod_force_flat", confirm_cancel=True), (False, False))
        fp.assert_not_called()

    def test_force_flat_confirmed_404_is_done(self):
        tgt = {"symbol": "AAA", "side": "long", "qty": 10, "entry_price": 100.0, "trade_id": "DT-AAA-1", "order_id": "o"}
        with mock.patch.object(dtm, "_closed_in_log", return_value=False), \
                mock.patch("execution.broker.get_open_position", return_value=None):
            self.assertEqual(dtm._force_flat_one(tgt, "eod_force_flat", confirm_cancel=True), (True, False))


class ExitLogicStopSizing(unittest.TestCase):
    def test_unreadable_position_sends_no_stop(self):
        trade = {"overnight": False}
        with mock.patch.object(exit_logic, "get_open_position", side_effect=_raise), \
                mock.patch.object(exit_logic, "submit_day_stop_order") as day, \
                mock.patch.object(exit_logic, "submit_gtc_stop_order") as gtc:
            ok = exit_logic._submit_new_stop("AAA", trade, mock.MagicMock(), 95.0, 10, "long", None, "trail_phase")
        self.assertFalse(ok)
        day.assert_not_called()
        gtc.assert_not_called()

    def test_live_position_caps_the_stop(self):
        trade = {"overnight": False}
        pos = mock.MagicMock(qty="6", side="long")
        order = mock.MagicMock(id="S1")
        with mock.patch.object(exit_logic, "get_open_position", return_value=pos), \
                mock.patch.object(exit_logic, "submit_day_stop_order", return_value=order) as day, \
                mock.patch.object(exit_logic, "_log_trade_event"):
            ok = exit_logic._submit_new_stop("AAA", trade, mock.MagicMock(), 95.0, 10, "long", None, "trail_phase")
        self.assertTrue(ok)
        self.assertEqual(day.call_args.kwargs["qty"], 6)


class LateEntryFillHeal(unittest.TestCase):
    """SAFE-1024 item 5: shares a pending-cancel entry fills after the stop was sized are closed from the ENTRY ORDER's
    fill, never from the broker net; no second close after a crash; nothing traded after hours."""

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.path.write_text(json.dumps({"entry::AAA::b1": {
            "symbol": "AAA", "side": "long", "state": "protected", "order_id": "E1", "coid": "DT-AAA-1",
            "fill_qty": 6, "entry_unterminal": True, "unterminal_recorded_qty": 6}}))
        for p in (mock.patch.object(dtm, "_STATE", self.path), mock.patch.object(dtm, "_page")):
            p.start()
            self.addCleanup(p.stop)

    def rec(self):
        import json
        return json.loads(self.path.read_text())["entry::AAA::b1"]

    @staticmethod
    def _order(status, filled, px=100.0, oid="E1"):
        return mock.MagicMock(id=oid, status=status, filled_qty=str(filled), filled_avg_price=str(px))

    def test_unreadable_entry_order_takes_no_action(self):
        with mock.patch("execution.broker.get_order", return_value=None), \
                mock.patch("execution.broker.partial_close_position") as close:
            dtm._heal_late_entry_fills(allow_trade=True)
        close.assert_not_called()
        self.assertTrue(self.rec()["entry_unterminal"])

    def test_not_terminal_waits(self):
        with mock.patch("execution.broker.get_order", return_value=self._order("pending_cancel", 6)), \
                mock.patch("execution.broker.partial_close_position") as close:
            out = dtm._heal_late_entry_fills(allow_trade=True)
        close.assert_not_called()
        self.assertEqual(out["pending"], 1)

    def test_no_late_shares_clears_the_mark(self):
        with mock.patch("execution.broker.get_order", return_value=self._order("canceled", 6)), \
                mock.patch("execution.broker.partial_close_position") as close:
            dtm._heal_late_entry_fills(allow_trade=True)
        close.assert_not_called()
        self.assertFalse(self.rec()["entry_unterminal"])

    def test_late_shares_closed_then_booked_then_cleared(self):
        pos = mock.MagicMock(qty="10", side="long")
        entry = self._order("canceled", 10)
        with mock.patch("execution.broker.get_order", return_value=entry), \
                mock.patch("execution.broker.get_open_position", return_value=pos), \
                mock.patch("execution.broker.partial_close_position", return_value=mock.MagicMock(id="C1")) as close:
            dtm._heal_late_entry_fills(allow_trade=True)
        close.assert_called_once_with("AAA", 4, tier="daytrade", _return_order=True, position_side="long")
        self.assertEqual(self.rec()["late_close_order_id"], "C1")
        closed = self._order("filled", 4, px=99.0, oid="C1")
        with mock.patch("execution.broker.get_order", side_effect=lambda oid, *a, **k: closed if oid == "C1" else entry), \
                mock.patch("execution.broker.partial_close_position") as close2, \
                mock.patch("trade_logger.log_event") as ev:
            dtm._heal_late_entry_fills(allow_trade=True)
        close2.assert_not_called()
        self.assertEqual(ev.call_args.kwargs["realized_pnl"], -4.0)
        self.assertEqual(self.rec()["late_closed_qty"], 4)
        with mock.patch("execution.broker.get_order", return_value=entry), \
                mock.patch("execution.broker.partial_close_position") as close3:
            dtm._heal_late_entry_fills(allow_trade=True)
        close3.assert_not_called()
        self.assertFalse(self.rec()["entry_unterminal"])

    def test_close_never_exceeds_the_live_same_side_net(self):
        pos = mock.MagicMock(qty="2", side="long")
        with mock.patch("execution.broker.get_order", return_value=self._order("canceled", 10)), \
                mock.patch("execution.broker.get_open_position", return_value=pos), \
                mock.patch("execution.broker.partial_close_position", return_value=mock.MagicMock(id="C1")) as close:
            dtm._heal_late_entry_fills(allow_trade=True)
        self.assertEqual(close.call_args.args[1], 2)

    def test_opposite_side_net_is_never_touched(self):
        pos = mock.MagicMock(qty="-5", side="short")
        with mock.patch("execution.broker.get_order", return_value=self._order("canceled", 10)), \
                mock.patch("execution.broker.get_open_position", return_value=pos), \
                mock.patch("execution.broker.partial_close_position") as close:
            dtm._heal_late_entry_fills(allow_trade=True)
        close.assert_not_called()

    def test_after_hours_pages_and_trades_nothing(self):
        with mock.patch("execution.broker.get_order", return_value=self._order("canceled", 10)), \
                mock.patch("execution.broker.partial_close_position") as close:
            dtm._heal_late_entry_fills(allow_trade=False)
        close.assert_not_called()
        self.assertTrue(self.rec()["entry_unterminal"])

    def test_crash_after_submit_adopts_the_order_never_sends_a_second(self):
        import json
        st = json.loads(self.path.read_text())
        st["entry::AAA::b1"].update(late_close_started="2026-10-26T14:00:00+00:00", late_close_qty=4)
        self.path.write_text(json.dumps(st))
        entry, working = self._order("canceled", 10), self._order("new", 0, oid="C9")
        with mock.patch("execution.broker.get_order", side_effect=lambda oid, *a, **k: working if oid == "C9" else entry), \
                mock.patch.object(dtm, "_late_close_adopt", return_value=(True, "C9")), \
                mock.patch("execution.broker.partial_close_position") as close:
            dtm._heal_late_entry_fills(allow_trade=True)
        close.assert_not_called()
        self.assertEqual(self.rec()["late_close_order_id"], "C9")

    def test_crash_with_unreadable_orders_sends_nothing(self):
        import json
        st = json.loads(self.path.read_text())
        st["entry::AAA::b1"].update(late_close_started="2026-10-26T14:00:00+00:00", late_close_qty=4)
        self.path.write_text(json.dumps(st))
        with mock.patch("execution.broker.get_order", return_value=self._order("canceled", 10)), \
                mock.patch.object(dtm, "_late_close_adopt", return_value=(False, "")), \
                mock.patch("execution.broker.partial_close_position") as close:
            dtm._heal_late_entry_fills(allow_trade=True)
        close.assert_not_called()


class FifoFillPaging(unittest.TestCase):
    """SAFE-1024 item 6: FILL paging uses page_token; a page that does not advance raises instead of looping."""

    @staticmethod
    def _page(ids, day="2026-10-21"):
        return [{"id": i, "symbol": "AAA", "side": "buy", "qty": "1", "price": "1",
                 "transaction_time": f"{day}T15:00:00Z"} for i in ids]

    def _run(self, pages):
        import json
        from execution import fifo_pnl
        seen_params: list = []

        def _open(req, **_k):
            seen_params.append(req.full_url)
            body = json.dumps(pages[min(len(seen_params) - 1, len(pages) - 1)]).encode()
            m = mock.MagicMock()
            m.__enter__.return_value.read.return_value = body
            return m
        with mock.patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}), \
                mock.patch("urllib.request.urlopen", side_effect=_open):
            return fifo_pnl._fetch_alpaca_fills_for_date("2026-10-21"), seen_params

    def test_two_pages_follow_page_token_and_dedupe_the_boundary(self):
        p1 = self._page([f"a{i}" for i in range(100)])
        p2 = self._page(["a99", "b1", "b2"])
        rows, urls = self._run([p1, p2])
        self.assertEqual(len(rows), 102)
        self.assertIn("page_token=a99", urls[1])
        self.assertNotIn("after_id", urls[1])

    def test_a_repeating_page_raises_instead_of_looping(self):
        from execution import fifo_pnl  # noqa: F401
        p1 = self._page([f"a{i}" for i in range(100)])
        with self.assertRaises(RuntimeError):
            self._run([p1, p1, p1])


if __name__ == "__main__":
    unittest.main()
