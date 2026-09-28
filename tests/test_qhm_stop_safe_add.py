# ruff: noqa: E501
"""Characterization tests for QuarterlyHoldManager._stop_safe_add (the Option C stop-safe add, extracted
verbatim from _maybe_dip_add 2026-09-28 so tranches 2-3 can reuse it). INVARIANT on every branch: the
position ends with a resting stop OR PENDING_STOP_REPLACE + an alert — never naked."""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import quarterly_hold_manager as qm


class FakeDispatcher:
    def __init__(self, stop_ok=True, add_ok=True):
        self.stop_ok, self.add_ok = stop_ok, add_ok
        self.stops, self.adds = [], []

    def submit_gtc_stop(self, broker, sym, qty, side, price):
        self.stops.append((sym, qty, side, price))
        return SimpleNamespace(id="stop-new") if self.stop_ok else None

    def submit_limit(self, broker, sym, qty, side, price):
        self.adds.append((sym, qty, side, price))
        return SimpleNamespace(id="add-1") if self.add_ok else None


class FakeBroker:
    def __init__(self, held):
        self.held = held

    def get_position(self, sym):
        return SimpleNamespace(qty=self.held) if self.held else None


def _mgr(pos, broker, disp, resync_qty=None):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m.broker = broker
    m._clock = None
    m._dispatcher = disp
    m._positions = {pos.symbol: pos}
    m.alerts = []
    m._alert = m.alerts.append  # type: ignore[method-assign]
    m._save_state = lambda: None  # type: ignore[method-assign]

    def _resync(p):
        if resync_qty is not None:
            p.qty_filled = resync_qty
    m._resync_from_alpaca = _resync  # type: ignore[method-assign]
    return m


def _pos(qty=2, stop_id="stop-old"):
    return qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                           qty_filled=qty, entry_day="2026-09-29", stop_price=200.0, stop_order_id=stop_id)


def _run(m, pos, add_qty=1, market_open=True, cancel_ok=True, order=None, label="tranche-2"):
    order = order or SimpleNamespace(status="filled", filled_qty=add_qty)
    cancels = []

    def _cancel(oid):
        cancels.append(oid)
        return cancel_ok if oid == "stop-old" else True
    clock = iter(range(0, 1000))
    # stand-in for execution.broker (the function imports these three names at call time)
    fake_broker_mod = SimpleNamespace(is_market_open=lambda: market_open, cancel_order=_cancel,
                                      get_order=lambda oid: order)
    with mock.patch.dict(sys.modules, {"execution.broker": fake_broker_mod}), \
            mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=lambda: float(next(clock))):
        got = m._stop_safe_add(pos, add_qty, 230.0, label)
    return got, cancels


class StopSafeAdd(unittest.TestCase):
    def test_market_closed_defers_untouched(self):
        pos, d = _pos(), FakeDispatcher()
        got, cancels = _run(_mgr(pos, FakeBroker(2), d), pos, market_open=False)
        self.assertEqual((got, cancels, d.adds), (0, [], []))
        self.assertEqual(pos.stop_order_id, "stop-old")

    def test_stop_cancel_fails_aborts_with_stop_intact(self):
        pos, d = _pos(), FakeDispatcher()
        got, _ = _run(_mgr(pos, FakeBroker(2), d), pos, cancel_ok=False)
        self.assertEqual((got, d.adds, d.stops), (0, [], []))
        self.assertEqual(pos.stop_order_id, "stop-old")

    def test_stop_fired_during_cancel_aborts_add_and_protects_remainder(self):
        pos, d = _pos(qty=2), FakeDispatcher()
        got, _ = _run(_mgr(pos, FakeBroker(1), d, resync_qty=1), pos)
        self.assertEqual((got, d.adds), (0, []))
        self.assertEqual(d.stops, [("NVDA", 1, "sell", 200.0)])
        self.assertEqual((pos.stop_order_id, pos.state), ("stop-new", qm.HoldState.ACTIVE))

    def test_stop_fired_flat_no_add_no_stop(self):
        pos, d = _pos(qty=2), FakeDispatcher()
        got, _ = _run(_mgr(pos, FakeBroker(0), d), pos)
        self.assertEqual((got, d.adds, d.stops), (0, [], []))

    def test_add_fails_restores_original_stop(self):
        pos, d = _pos(qty=2), FakeDispatcher(add_ok=False)
        got, _ = _run(_mgr(pos, FakeBroker(2), d), pos)
        self.assertEqual(got, 0)
        self.assertEqual(d.stops, [("NVDA", 2, "sell", 200.0)])
        self.assertEqual(pos.state, qm.HoldState.ACTIVE)

    def test_full_fill_restops_full_held_qty(self):
        pos, d = _pos(qty=2), FakeDispatcher()
        got, cancels = _run(_mgr(pos, FakeBroker(2), d, resync_qty=3), pos, add_qty=1)
        self.assertEqual(got, 1)
        self.assertEqual(d.adds, [("NVDA", 1, "buy", round(230.0 * 1.001, 2))])
        self.assertEqual(d.stops, [("NVDA", 3, "sell", 200.0)])
        self.assertEqual(cancels, ["stop-old"])  # a filled add is not cancelled

    def test_partial_fill_cancels_remainder_and_stops_held(self):
        pos, d = _pos(qty=2), FakeDispatcher()
        got, cancels = _run(_mgr(pos, FakeBroker(2), d, resync_qty=3), pos, add_qty=2,
                            order=SimpleNamespace(status="partially_filled", filled_qty=1))
        self.assertEqual(got, 1)
        self.assertEqual(cancels, ["stop-old", "add-1"])
        self.assertEqual(d.stops, [("NVDA", 3, "sell", 200.0)])

    def test_stop_resubmit_failure_goes_pending_with_alert(self):
        pos, d = _pos(qty=2), FakeDispatcher(stop_ok=False)
        m = _mgr(pos, FakeBroker(2), d, resync_qty=3)
        got, _ = _run(m, pos, add_qty=1)
        self.assertEqual(got, 1)
        self.assertEqual(pos.state, qm.HoldState.PENDING_STOP_REPLACE)
        self.assertIsNone(pos.stop_order_id)
        self.assertEqual(m.alerts, [":rotating_light: QHM NVDA tranche-2 post-add stop resubmit — stop pending resubmit"])

    def test_no_resting_stop_skips_cancel_and_stops_after_fill(self):
        pos, d = _pos(qty=2, stop_id=None), FakeDispatcher()
        got, cancels = _run(_mgr(pos, FakeBroker(2), d, resync_qty=3), pos, add_qty=1)
        self.assertEqual((got, cancels), (1, []))
        self.assertEqual(d.stops, [("NVDA", 3, "sell", 200.0)])

    def test_dip_add_label_keeps_original_alert_text(self):
        pos, d = _pos(qty=2), FakeDispatcher(stop_ok=False, add_ok=False)
        m = _mgr(pos, FakeBroker(2), d)
        _run(m, pos, label="dip-add")
        self.assertEqual(m.alerts, [":rotating_light: QHM NVDA dip-add add-failed — stop pending resubmit"])


if __name__ == "__main__":
    unittest.main()
