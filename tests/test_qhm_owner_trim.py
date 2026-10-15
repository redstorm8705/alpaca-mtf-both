# ruff: noqa: E501
"""QHM owner trim (Claude 2026-10-14): an operator-armed partial sale of a quarterly hold, executed by the bot with the
earnings-trim discipline (reserve -> cancel stop -> sell -> confirm -> restore stop -> ledger). Broker fully mocked."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  — test guard (blank broker keys, MTF_TEST_MODE)
from execution import quarterly_hold_manager as qm
from execution import broker  # noqa: F401  — patched below, never called for real
from execution import ownership_guard  # noqa: F401
import trade_logger  # noqa: F401



class _Broker:
    def __init__(self, qty):
        self.qty = qty

    def get_position(self, sym):
        return mock.Mock(qty=str(self.qty)) if self.qty else None


class _Dispatcher:
    def __init__(self):
        self.stops = []

    def submit_gtc_stop(self, _broker, sym, qty, side, px):
        self.stops.append((sym, qty, side, px))
        return mock.Mock(id=f"stop-{len(self.stops)}")


def _mgr(qty=2, price=1225.0):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m._positions = {}
    m.broker = _Broker(qty)
    m._dispatcher = _Dispatcher()
    m._get_live_price = lambda sym: price  # type: ignore[method-assign]
    m.live = price
    m._now_et = lambda: datetime(2026, 10, 15, 11, 0)  # type: ignore[method-assign]
    m._save_state = lambda: None  # type: ignore[method-assign]
    m._resync_from_alpaca = lambda pos: None  # type: ignore[method-assign]
    m.alerts = []
    m._alert = m.alerts.append  # type: ignore[method-assign]
    pos = qm.HoldPosition(symbol="LLY", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                          qty_filled=qty, avg_entry_price=1222.115, stop_price=1058.13, stop_order_id="old-stop",
                          entry_day="2026-08-24")
    m._positions["LLY"] = pos
    return m, pos


class OwnerTrim(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "qhm_owner_trims.json"
        self.p = mock.patch.object(qm, "_OWNER_TRIM_PATH", self.tmp)
        self.p.start()
        self.mo = mock.patch("execution.broker.is_market_open", return_value=True)
        self.mo.start()
        self.sl = mock.patch("time.sleep", lambda s: None)
        self.sl.start()
        _clock = iter(range(0, 10 ** 6, 5))                  # simulated clock: the 15 s poll ends in a few steps
        self.mn = mock.patch("time.monotonic", lambda: float(next(_clock)))
        self.mn.start()

    def tearDown(self):
        for x in (self.p, self.mo, self.sl, self.mn):
            x.stop()

    def _arm(self, **kw):
        req = {"qty": 1, "trigger_price": 1222.0, "deadline": "2026-10-28", "status": "armed", "entry_day": "2026-08-24"}
        req.update(kw)
        self.tmp.write_text(json.dumps({"LLY": req}))

    def _state(self):
        return json.loads(self.tmp.read_text())["LLY"]

    def _run(self, m, pos, fill=True):
        def _sell(sym, qty, tier=None):
            if fill:
                m.broker.qty -= qty
            return True
        with mock.patch("data.live_price.live_price", return_value=mock.Mock(price=m.live) if m.live else None), \
                mock.patch("execution.broker.cancel_order", return_value=True) as cancel, \
                mock.patch("execution.broker.partial_close_position", side_effect=_sell) as sell, \
                mock.patch("execution.broker.get_open_orders", return_value=[]), \
                mock.patch("execution.ownership_guard.apply_authorized_tier_reduction", return_value=True) as led, \
                mock.patch("trade_logger.log_event"):
            ran = m._maybe_owner_trim(pos)
        return ran, cancel, sell, led

    def test_no_request_does_nothing(self):
        m, pos = _mgr()
        ran, _c, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        sell.assert_not_called()

    def test_below_trigger_before_deadline_waits(self):
        self._arm()
        m, pos = _mgr(price=1209.0)
        ran, _c, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        sell.assert_not_called()
        self.assertEqual(self._state()["status"], "armed")

    def test_trigger_sells_one_keeps_one_with_stop(self):
        self._arm()
        m, pos = _mgr(price=1223.0)
        ran, cancel, sell, led = self._run(m, pos)
        self.assertTrue(ran)
        cancel.assert_called_once_with("old-stop")
        sell.assert_called_once_with("LLY", 1, tier="qhm")
        led.assert_called_once_with("LLY", "qhm", 1.0, "owner_trim")
        self.assertEqual(m._dispatcher.stops, [("LLY", 1, "sell", 1058.13)])
        self.assertEqual(pos.stop_order_id, "stop-1")
        st = self._state()
        self.assertEqual((st["status"], st["sold_qty"], st["remaining_qty"]), ("done", 1, 1))

    def test_deadline_sells_at_market_below_trigger(self):
        self._arm(deadline="2026-10-15")
        m, pos = _mgr(price=1150.0)
        ran, _c, sell, _l = self._run(m, pos)
        self.assertTrue(ran)
        sell.assert_called_once()
        self.assertEqual(self._state()["status"], "done")

    def test_done_request_never_refires(self):
        self._arm(status="done")
        m, pos = _mgr(price=1300.0)
        ran, _c, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        sell.assert_not_called()

    def test_must_leave_a_share(self):
        self._arm(qty=2)
        m, pos = _mgr(qty=2, price=1300.0)
        ran, _c, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        sell.assert_not_called()
        self.assertEqual(self._state()["status"], "rejected")

    def test_market_closed_does_nothing(self):
        self._arm(deadline="2026-10-01")
        m, pos = _mgr()
        with mock.patch("execution.broker.is_market_open", return_value=False):
            ran, _c, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        sell.assert_not_called()

    def test_unfilled_sell_restores_stop_and_stays_reserved(self):
        self._arm()
        m, pos = _mgr(price=1230.0)
        ran, _c, _s, led = self._run(m, pos, fill=False)
        self.assertTrue(ran)
        led.assert_not_called()
        self.assertEqual(m._dispatcher.stops, [("LLY", 2, "sell", 1058.13)])
        self.assertEqual(self._state()["status"], "reserved")

    def test_stop_fired_during_cancel_never_trims(self):
        self._arm()
        m, pos = _mgr(price=1230.0)
        with mock.patch("data.live_price.live_price", return_value=mock.Mock(price=1230.0)), \
                mock.patch("execution.broker.cancel_order", side_effect=lambda oid: setattr(m.broker, "qty", 1) or True), \
                mock.patch("execution.broker.partial_close_position") as sell, \
                mock.patch("execution.broker.get_open_orders", return_value=[]):
            m._maybe_owner_trim(pos)
        sell.assert_not_called()
        self.assertEqual(m._dispatcher.stops, [("LLY", 1, "sell", 1058.13)])

    def test_reservation_write_failure_sells_nothing(self):
        self._arm()
        m, pos = _mgr(price=1230.0)
        with mock.patch.object(m, "_set_owner_trim", return_value=False):
            ran, cancel, sell, _l = self._run(m, pos)
        cancel.assert_not_called()
        sell.assert_not_called()

    def test_coheld_symbol_waits_and_alerts(self):
        self._arm()
        m, pos = _mgr(qty=2, price=1300.0)
        m.broker.qty = 5                                      # a Day lot of 3 co-holds the symbol
        ran, cancel, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        cancel.assert_not_called()
        sell.assert_not_called()
        self.assertEqual(self._state()["status"], "armed")
        self.assertTrue(any("waiting" in a for a in m.alerts))

    def test_unconfirmed_cancels_only_qhm_sells(self):
        self._arm()
        m, pos = _mgr(price=1230.0)
        day = mock.Mock(id="day-oco", side="sell", client_order_id="DT-LLY-s-1-abc")
        ours = mock.Mock(id="qh-sell", side="sell", client_order_id="QH-LLY-s-1-abc")
        with mock.patch("data.live_price.live_price", return_value=mock.Mock(price=1230.0)), \
                mock.patch("execution.broker.cancel_order", return_value=True) as cancel, \
                mock.patch("execution.broker.partial_close_position", return_value=True), \
                mock.patch("execution.broker.get_open_orders", return_value=[day, ours]), \
                mock.patch("execution.ownership_guard.apply_authorized_tier_reduction"), \
                mock.patch("trade_logger.log_event"):
            m._maybe_owner_trim(pos)
        cancelled = [c.args[0] for c in cancel.call_args_list]
        self.assertIn("qh-sell", cancelled)
        self.assertNotIn("day-oco", cancelled)

    def test_rearmed_request_not_overwritten_by_done(self):
        self._arm()
        m, pos = _mgr(price=1230.0)
        self.tmp.write_text(json.dumps({"LLY": {"status": "armed", "qty": 1}}))
        self.assertFalse(m._set_owner_trim("LLY", expect_status="reserved", status="done"))
        self.assertEqual(self._state()["status"], "armed")

    def test_request_for_a_different_hold_is_rejected(self):
        self._arm(entry_day="2026-07-01")
        m, pos = _mgr(price=1300.0)
        ran, _c, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        sell.assert_not_called()
        self.assertEqual(self._state()["status"], "rejected")

    def test_no_fresh_price_no_trigger(self):
        self._arm()
        m, pos = _mgr(price=None)
        ran, _c, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        sell.assert_not_called()

    def test_error_after_stop_cancel_restores_the_stop(self):
        self._arm()
        m, pos = _mgr(price=1230.0)
        with mock.patch("data.live_price.live_price", return_value=mock.Mock(price=1230.0)), \
                mock.patch("execution.broker.cancel_order", return_value=True), \
                mock.patch("execution.broker.partial_close_position", side_effect=RuntimeError("boom")), \
                mock.patch("execution.broker.get_open_orders", return_value=[]):
            m._maybe_owner_trim(pos)
        self.assertEqual(m._dispatcher.stops, [("LLY", 2, "sell", 1058.13)])

    def test_busy_lock_never_blocks(self):
        import fcntl
        self._arm()
        m, _pos = _mgr()
        with open(str(self.tmp) + ".lock", "w") as other:
            fcntl.flock(other, fcntl.LOCK_EX)                 # another process holds the lock
            self.assertFalse(m._set_owner_trim("LLY", status="reserved"))
        self.assertEqual(self._state()["status"], "armed")

    def test_bad_request_rejected(self):
        self._arm(deadline="not-a-date")
        m, pos = _mgr()
        ran, _c, sell, _l = self._run(m, pos)
        self.assertFalse(ran)
        self.assertEqual(self._state()["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
