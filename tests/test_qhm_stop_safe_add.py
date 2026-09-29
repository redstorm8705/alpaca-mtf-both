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

    def submit_limit(self, broker, sym, qty, side, price, client_order_id=None):
        self.adds.append((sym, qty, side, price))
        return SimpleNamespace(id="add-1") if self.add_ok else None


class FakeBroker:
    def __init__(self, held):
        self.held = held

    def get_position(self, sym):
        return SimpleNamespace(qty=self.held) if self.held else None


class UnreadablePositionBroker(FakeBroker):
    def get_position(self, sym):
        raise RuntimeError("position API unavailable")


def _mgr(pos, broker, disp, resync_qty=None):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m.broker = broker
    m._clock = None
    m._dispatcher = disp
    m._positions = {pos.symbol: pos}
    m.alerts = []
    m._alert = m.alerts.append  # type: ignore[method-assign]
    m._save_state = lambda: True  # type: ignore[method-assign]
    m._grandfathered_notional_for_allocator = lambda: 0.0  # type: ignore[method-assign]
    m._now_et = lambda: __import__("datetime").datetime.now()  # type: ignore[method-assign]
    def _arm(p, client_id):
        p.ambiguous_add_client_order_id = client_id
        p.ambiguous_add_baseline_qty = p.qty_filled
        return True
    m._arm_ambiguous_add_recovery = _arm  # type: ignore[method-assign]

    def _resync(p):
        if resync_qty is not None:
            p.qty_filled = resync_qty
    m._resync_from_alpaca = _resync  # type: ignore[method-assign]
    def _strict(p, *_args):
        _resync(p)
        return p.qty_filled
    m._strict_resync_qty = _strict  # type: ignore[method-assign]
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
    def _get_order(oid):
        if oid == "add-1" and "add-1" in cancels:
            return SimpleNamespace(status="canceled", filled_qty=order.filled_qty)
        return order
    fake_broker_mod = SimpleNamespace(
        is_market_open=lambda: market_open,
        cancel_order=_cancel,
        get_order=_get_order,
        get_order_by_client_order_id=lambda _coid: None,
    )
    lease = SimpleNamespace(id="lease-1")
    fake_allocator_mod = SimpleNamespace(
        live_admit=lambda *_a, **_kw: SimpleNamespace(approved=True, lease=lease),
        live_bind=lambda *_a, **_kw: True,
        live_release=lambda *_a, **_kw: True,
        live_order_id=lambda _lease: "QH-NVDA-test",
    )
    with mock.patch.dict(sys.modules, {"execution.broker": fake_broker_mod}), \
            mock.patch.dict(sys.modules, {"execution.tier_capital_allocator": fake_allocator_mod}), \
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

    def test_pre_cancel_recovery_write_failure_leaves_stop_intact(self):
        pos, d = _pos(), FakeDispatcher()
        manager = _mgr(pos, FakeBroker(2), d)
        manager._arm_ambiguous_add_recovery = lambda *_args: False
        got, cancels = _run(manager, pos)
        self.assertEqual((got, cancels, d.adds), (0, [], []))
        self.assertEqual(pos.stop_order_id, "stop-old")

    def test_final_recovery_clear_write_failure_stays_pending(self):
        pos, d = _pos(qty=2), FakeDispatcher()
        manager = _mgr(pos, FakeBroker(2), d, resync_qty=3)
        manager._save_state = mock.Mock(side_effect=[True, True, False])
        got, _ = _run(manager, pos, add_qty=1)
        self.assertEqual(got, 0)
        self.assertEqual(pos.state, qm.HoldState.PENDING_STOP_REPLACE)
        self.assertEqual(pos.stop_recovery_canceled_order_id, "stop-old")
        self.assertTrue(pos.stop_recovery_add_submitted)

    def test_pre_submit_write_failure_restores_verified_original_stop(self):
        pos, d = _pos(qty=2), FakeDispatcher()
        manager = _mgr(pos, FakeBroker(2), d)
        manager._save_state = mock.Mock(side_effect=[True, False, True])

        def _arm(p, client_id):
            p.ambiguous_add_client_order_id = client_id
            p.ambiguous_add_baseline_qty = p.qty_filled
            return manager._save_state()

        manager._arm_ambiguous_add_recovery = _arm
        got, cancels = _run(manager, pos)
        self.assertEqual(got, 0)
        self.assertEqual(cancels, ["stop-old"])
        self.assertEqual(d.adds, [])
        self.assertEqual(d.stops, [("NVDA", 2, "sell", 200.0)])
        self.assertEqual(pos.stop_order_id, "stop-new")
        self.assertEqual(pos.state, qm.HoldState.PENDING_STOP_REPLACE)

    def test_position_unreadable_after_stop_cancel_blocks_add(self):
        pos, d = _pos(), FakeDispatcher()
        manager = _mgr(pos, UnreadablePositionBroker(2), d)
        got, cancels = _run(manager, pos)
        self.assertEqual(got, 0)
        self.assertEqual(cancels, ["stop-old"])
        self.assertEqual(d.adds, [])
        self.assertEqual(d.stops, [])
        self.assertEqual(pos.state, qm.HoldState.PENDING_STOP_REPLACE)
        self.assertIsNone(pos.stop_order_id)
        self.assertTrue(pos.stop_recovery_requires_qty_proof)
        self.assertEqual(pos.stop_recovery_canceled_order_id, "stop-old")
        self.assertEqual(pos.stop_recovery_baseline_qty, 2)
        self.assertTrue(any("position unreadable" in alert for alert in manager.alerts))

        # A later cycle must prove the QHM-owned quantity before replacing the stop.
        def _prove_owner(recovery_pos, owner, baseline):
            recovery_pos.qty_filled = owner
            return owner
        manager._strict_resync_qty = _prove_owner
        recovered_broker = SimpleNamespace(
            get_order=lambda _oid: SimpleNamespace(status="canceled", filled_qty=1)
        )
        with mock.patch.dict(sys.modules, {"execution.broker": recovered_broker}):
            self.assertTrue(manager.resubmit_stop_if_needed(pos.symbol))
        self.assertEqual(d.stops, [("NVDA", 1, "sell", 200.0)])
        self.assertFalse(pos.stop_recovery_requires_qty_proof)
        self.assertIsNone(pos.stop_recovery_canceled_order_id)

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
        # A submit that returns no order is ambiguous; recovery remains armed even
        # when a provisional stop was restored successfully.
        self.assertEqual(pos.state, qm.HoldState.PENDING_STOP_REPLACE)

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
        self.assertEqual(m.alerts, [":rotating_light: QHM NVDA dip-add post-add stop resubmit — stop pending resubmit"])

    def test_no_resting_stop_skips_cancel_and_stops_after_fill(self):
        pos, d = _pos(qty=2, stop_id=None), FakeDispatcher()
        got, cancels = _run(_mgr(pos, FakeBroker(2), d, resync_qty=3), pos, add_qty=1)
        self.assertEqual((got, cancels), (1, []))
        self.assertEqual(d.stops, [("NVDA", 3, "sell", 200.0)])

    def test_dip_add_label_keeps_original_alert_text(self):
        pos, d = _pos(qty=2), FakeDispatcher(stop_ok=False, add_ok=False)
        m = _mgr(pos, FakeBroker(2), d)
        _run(m, pos, label="dip-add")
        self.assertEqual(
            m.alerts,
            [
                ":rotating_light: QHM NVDA dip-add ambiguous-add-current-qty — stop pending resubmit",
                ":rotating_light: QHM NVDA dip-add outcome AMBIGUOUS — current position protected where verifiable; exact order recovery pending.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
