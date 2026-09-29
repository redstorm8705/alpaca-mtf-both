# ruff: noqa: E501
"""QHM tranches 2-3 (2026-09-28). Before this fix a tranche-1 fill placed the stop and set ACTIVE, and
maybe_enter_positions only looked at PENDING_ENTRY/AWAITING_FILL, so tranches 2-3 never ran (live: LLY sat at
tranche 2 / tranches_filled 1 since 2026-08-24). ACTIVE holds with tranches owed now buy them through the
stop-safe add, with every check before the stop is cancelled."""
import unittest
from datetime import datetime
from types import SimpleNamespace

from execution import quarterly_hold_manager as qm

NOW = datetime(2026, 10, 1, 10, 30, tzinfo=qm.ET)  # entry 2026-09-29 -> Day 3
TRIM: dict = {}  # served by the stubbed _load_earnings_trim_state


def _pos(**kw):
    d = dict(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE, qty_filled=1,
             entry_day="2026-09-29", stop_price=200.0, stop_order_id="stop-1", tranche=2, tranches_filled=1,
             tranche1_price=231.0, avg_entry_price=231.0)
    d.update(kw)
    return qm.HoldPosition(**d)


class Broker:
    def __init__(self, equity=10000.0, bp=10000.0):
        self.equity, self.bp = equity, bp

    def get_account(self):
        return SimpleNamespace(equity=self.equity, regt_buying_power=self.bp)


def _mgr(pos, price=235.0, room=10, fill=None, broker=None):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m._clock = lambda: NOW
    m.broker = broker or Broker()
    m._positions = {pos.symbol: pos}
    m._thesis_config = {}
    m._save_state = lambda: None  # type: ignore[method-assign]
    m.alerts = []
    m._alert = m.alerts.append  # type: ignore[method-assign]
    m._get_live_price = lambda s: price  # type: ignore[method-assign]
    m._qhm_cap_room_shares = lambda s, p, e: room  # type: ignore[method-assign]
    m._get_quarterly_notional_excl = lambda s: 0.0  # type: ignore[method-assign]
    m.calls = []

    m._design_stop_price = lambda sym, avg: None  # type: ignore[method-assign]
    m._load_earnings_trim_state = lambda: TRIM  # type: ignore[method-assign]

    def _ssa(p, qty, px, label, raise_stop_to=None):
        m.calls.append((p.symbol, qty, label))
        got = qty if fill is None else fill
        p.qty_filled += got
        return got
    m._stop_safe_add = _ssa  # type: ignore[method-assign]
    return m


class TrancheAdds(unittest.TestCase):
    def test_due_tranche_2_buys_and_advances(self):
        pos = _pos()
        m = _mgr(pos)
        self.assertEqual(m.maybe_enter_positions(), ["NVDA"])
        want = max(int(10000 * 0.2 / 3 / 235.0), 1)
        self.assertEqual(m.calls, [("NVDA", want, "tranche-2")])
        self.assertEqual((pos.tranche, pos.tranches_filled, pos.qty_filled), (3, 2, 1 + want))
        self.assertEqual(len(m.alerts), 1)

    def test_tranche_3_on_day_5_then_done(self):
        pos = _pos(tranche=3, tranches_filled=2, entry_day="2026-09-27")
        m = _mgr(pos)
        self.assertEqual(m.maybe_enter_positions(), ["NVDA"])
        self.assertEqual((pos.tranche, pos.tranches_filled), (4, 3))
        m.calls.clear()
        self.assertEqual(m.maybe_enter_positions(), [])  # all three filled -> nothing more
        self.assertEqual(m.calls, [])

    def test_not_due_yet(self):
        pos = _pos(entry_day="2026-09-30")  # Day 2 -> tranche 2 due on Day 3
        m = _mgr(pos)
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_reconfirm_fail_skips(self):
        pos = _pos()
        m = _mgr(pos, price=220.0)  # < 231 * 0.98
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_cap_blocks_before_touching_the_stop(self):
        # the live LLY case: a grandfathered hold far over the 20% per-name cap -> room 0
        pos = _pos(symbol="LLY", entry_day="2026-08-24", tranche1_price=1100.0, stop_price=1058.13, qty_filled=2)
        m = _mgr(pos, price=1183.5, room=0)
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])
        self.assertEqual((pos.tranche, pos.tranches_filled), (2, 1))

    def test_cap_unreadable_fails_closed(self):
        pos = _pos()
        m = _mgr(pos, room=None)
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_unaffordable_skips(self):
        pos = _pos()
        m = _mgr(pos, broker=Broker(bp=100.0))
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_price_at_or_below_stop_skips(self):
        pos = _pos(stop_price=236.0, tranche1_price=236.0)
        m = _mgr(pos, price=235.0)
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_dip_added_today_defers(self):
        pos = _pos(last_dip_add_date="2026-10-01")
        m = _mgr(pos)
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_no_resting_stop_skips(self):
        pos = _pos(stop_order_id=None)
        m = _mgr(pos)
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_adds_into_earnings_no_blackout(self):
        # nothing in the add path looks at the earnings calendar any more (Rafael 2026-09-28)
        pos = _pos(earnings_gate_date="2026-10-03")
        m = _mgr(pos)
        self.assertEqual(m.maybe_enter_positions(), ["NVDA"])

    def test_no_re_add_after_trim_for_this_print(self):
        pos = _pos()
        m = _mgr(pos)
        TRIM["NVDA"] = {"gate_date": "2026-10-20", "tier1": "done"}
        try:
            self.assertEqual(m.maybe_enter_positions(), [])
            self.assertEqual(m.calls, [])
        finally:
            TRIM.clear()

    def test_trim_for_a_past_print_does_not_block(self):
        pos = _pos()
        m = _mgr(pos)
        TRIM["NVDA"] = {"gate_date": "2026-08-26", "tier1": "done", "tier2": "done"}
        try:
            self.assertEqual(m.maybe_enter_positions(), ["NVDA"])
        finally:
            TRIM.clear()

    def test_unreadable_trim_state_fails_closed(self):
        pos = _pos()
        m = _mgr(pos)

        def _boom():
            raise qm._EarningsTrimStateUnreadable("corrupt")
        m._load_earnings_trim_state = _boom  # type: ignore[method-assign]
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_skipped_names_do_not_use_the_cycle_budget(self):
        m = _mgr(_pos())
        m._positions["GOOGL"] = _pos(symbol="GOOGL")
        m._positions["GE"] = _pos(symbol="GE")
        TRIM.update({"NVDA": {"gate_date": "2026-10-20", "tier1": "reserved"},
                     "GOOGL": {"gate_date": "2026-10-28", "tier2": "done"}})
        try:
            self.assertEqual(m.maybe_enter_positions(), ["GE"])
        finally:
            TRIM.clear()

    def test_within_one_percent_of_stop_skips(self):
        pos = _pos(stop_price=233.0, tranche1_price=233.0)
        m = _mgr(pos, price=235.0)  # 0.86% above the stop
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_no_fill_retries_capped_per_day(self):
        pos = _pos()
        m = _mgr(pos, fill=0)
        for _ in range(5):
            m.maybe_enter_positions()
        self.assertEqual(len(m.calls), qm._TRANCHE_MAX_TRIES_PER_DAY)

    def test_at_most_two_adds_per_cycle(self):
        m = _mgr(_pos())
        for sym in ("GOOGL", "GE", "AMZN"):
            m._positions[sym] = _pos(symbol=sym)
        m.maybe_enter_positions()
        self.assertEqual(len(m.calls), qm._TRANCHE_MAX_ADDS_PER_CYCLE)

    def test_short_hold_skips(self):
        pos = _pos(direction="short")
        m = _mgr(pos)
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.calls, [])

    def test_no_fill_does_not_advance(self):
        pos = _pos()
        m = _mgr(pos, fill=0)
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual((pos.tranche, pos.tranches_filled), (2, 1))

    def test_fill_counted_from_alpaca_when_poll_missed_it(self):
        pos = _pos()
        m = _mgr(pos)

        def _ssa(p, qty, px, label, raise_stop_to=None):  # poll saw nothing, resync shows the share
            p.qty_filled += 1
            return 0
        m._stop_safe_add = _ssa  # type: ignore[method-assign]
        self.assertEqual(m.maybe_enter_positions(), ["NVDA"])
        self.assertEqual((pos.tranche, pos.tranches_filled), (3, 2))

    def test_awaiting_fill_tranche1_fill_does_not_submit_limit_tranche_2(self):
        pos = _pos(state=qm.HoldState.AWAITING_FILL, tranche=1, tranches_filled=0, entry_day="2026-09-27",
                   qty_filled=0, entry_order_id="o1", stop_order_id=None)
        m = _mgr(pos)
        submitted = []
        m._submit_tranche = lambda p, t: submitted.append(p.tranche) or True  # type: ignore[method-assign]

        def _fill(p):  # tranche-1 fill: stop placed -> ACTIVE (as _compute_and_submit_stop does)
            p.tranches_filled, p.tranche, p.qty_filled, p.state = 1, 2, 1, qm.HoldState.ACTIVE
            return True
        m._check_fill_and_advance = _fill  # type: ignore[method-assign]
        m.maybe_enter_positions()
        self.assertEqual(submitted, [])


class OnePerDayAndStopRatchet(unittest.TestCase):
    def test_overdue_hold_buys_one_tranche_per_day(self):
        pos = _pos(entry_day="2026-09-20")  # tranches 2 and 3 both overdue
        m = _mgr(pos)
        m.maybe_enter_positions()
        m.maybe_enter_positions()
        self.assertEqual([c[2] for c in m.calls], ["tranche-2"])
        self.assertEqual((pos.tranche, pos.tranches_filled), (3, 2))

    def test_design_stop_passed_for_projected_average(self):
        pos = _pos(qty_filled=1, avg_entry_price=231.0)
        m = _mgr(pos)
        seen = {}
        m._design_stop_price = lambda sym, avg: seen.setdefault("avg", avg) and 210.0  # type: ignore[method-assign]
        got = {}

        def _ssa(p, qty, px, label, raise_stop_to=None):
            got["raise"] = raise_stop_to
            p.qty_filled += qty
            return qty
        m._stop_safe_add = _ssa  # type: ignore[method-assign]
        m.maybe_enter_positions()
        q = max(int(10000 * 0.2 / 3 / 235.0), 1)
        self.assertAlmostEqual(seen["avg"], (231.0 + 235.0 * q) / (1 + q))
        self.assertEqual(got["raise"], 210.0)


def _helper_run(pos, fill_qty, raise_to, add_ok=True):
    import sys
    from unittest import mock
    m = _mgr(pos)
    del m._stop_safe_add
    stops = []
    m._dispatcher = SimpleNamespace(
        submit_limit=lambda *a: SimpleNamespace(id="add-1") if add_ok else None,
        submit_gtc_stop=lambda b, s, q, side, px: stops.append((q, px)) or SimpleNamespace(id="stop-2"))
    m.broker.get_position = lambda s: SimpleNamespace(qty=pos.qty_filled)

    def _resync(p):
        p.qty_filled += fill_qty
    m._resync_from_alpaca = _resync  # type: ignore[method-assign]
    fake = SimpleNamespace(is_market_open=lambda: True, cancel_order=lambda oid: True,
                           get_order=lambda oid: SimpleNamespace(status="filled" if fill_qty else "new", filled_qty=fill_qty))
    clock = iter(range(1000))
    with mock.patch.dict(sys.modules, {"execution.broker": fake}), mock.patch("time.sleep"), \
            mock.patch("time.monotonic", side_effect=lambda: float(next(clock))):
        m._stop_safe_add(pos, 1, 235.0, "tranche-2", raise_stop_to=raise_to)
    return stops


class StopRatchetInHelper(unittest.TestCase):
    def test_raised_on_fill(self):
        pos = _pos(qty_filled=1)
        self.assertEqual(_helper_run(pos, 1, 205.0), [(2, 205.0)])
        self.assertEqual(pos.stop_price, 205.0)

    def test_not_raised_without_fill(self):
        pos = _pos(qty_filled=1)
        self.assertEqual(_helper_run(pos, 0, 205.0), [(1, 200.0)])
        self.assertEqual(pos.stop_price, 200.0)

    def test_never_lowered(self):
        pos = _pos(qty_filled=1)
        self.assertEqual(_helper_run(pos, 1, 190.0), [(2, 200.0)])

    def test_never_at_or_above_add_price(self):
        pos = _pos(qty_filled=1)
        self.assertEqual(_helper_run(pos, 1, 235.0), [(2, 200.0)])

    def test_add_failed_restores_original_price(self):
        pos = _pos(qty_filled=1)
        self.assertEqual(_helper_run(pos, 0, 205.0, add_ok=False), [(1, 200.0)])


class LateFillAndFailedResync(unittest.TestCase):
    def test_late_fill_and_failed_resync_still_stops_all_shares(self):
        # the poll saw nothing, the add filled before the remainder cancel, and the resync failed:
        # the final order read must count the share and the stop must cover orig + filled.
        import sys
        from unittest import mock
        pos = _pos(qty_filled=1)
        m = _mgr(pos)
        del m._stop_safe_add
        stops = []
        m._dispatcher = SimpleNamespace(
            submit_limit=lambda *a: SimpleNamespace(id="add-1"),
            submit_gtc_stop=lambda b, s, q, side, px: stops.append(q) or SimpleNamespace(id="stop-2"))
        m.broker.get_position = lambda s: SimpleNamespace(qty=1)

        def _resync_fails(p):
            raise RuntimeError("alpaca down")
        m._resync_from_alpaca = _resync_fails  # type: ignore[method-assign]
        reads = iter([SimpleNamespace(status="new", filled_qty=0)] * 14 + [SimpleNamespace(status="filled", filled_qty=1)] * 5)
        fake = SimpleNamespace(is_market_open=lambda: True, cancel_order=lambda oid: True,
                               get_order=lambda oid: next(reads))
        clock = iter(range(1000))
        with mock.patch.dict(sys.modules, {"execution.broker": fake}), mock.patch("time.sleep"), \
                mock.patch("time.monotonic", side_effect=lambda: float(next(clock))):
            got = m._stop_safe_add(pos, 1, 235.0, "tranche-2")
        self.assertEqual(got, 1)
        self.assertEqual(stops, [2])


class RemainderCancelFailure(unittest.TestCase):
    def test_failed_remainder_cancel_alerts(self):
        import sys
        from unittest import mock
        pos = _pos(qty_filled=2)
        m = _mgr(pos)
        del m._stop_safe_add  # use the real helper
        m._dispatcher = SimpleNamespace(
            submit_limit=lambda *a: SimpleNamespace(id="add-1"),
            submit_gtc_stop=lambda *a: SimpleNamespace(id="stop-2"))
        m.broker.get_position = lambda s: SimpleNamespace(qty=2)
        m._resync_from_alpaca = lambda p: None  # type: ignore[method-assign]
        fake = SimpleNamespace(is_market_open=lambda: True, get_order=lambda oid: SimpleNamespace(status="new", filled_qty=0),
                               cancel_order=lambda oid: oid == "stop-1")
        clock = iter(range(1000))
        with mock.patch.dict(sys.modules, {"execution.broker": fake}), mock.patch("time.sleep"), \
                mock.patch("time.monotonic", side_effect=lambda: float(next(clock))):
            got = m._stop_safe_add(pos, 1, 235.0, "tranche-2")
        self.assertEqual(got, 0)
        self.assertEqual(pos.stop_order_id, "stop-2")
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("add remainder cancel failed", m.alerts[0])


if __name__ == "__main__":
    unittest.main()
