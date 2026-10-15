# ruff: noqa: E501
"""QHM pre-market / after-hours dip-adds (Claude 2026-10-15). Session edges from the Alpaca calendar, a liquidity check
against each stock's own typical daily move, and the unchanged dip-add path with extended=True. Broker fully mocked."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import tests  # noqa: F401  — test guard (blank broker keys, MTF_TEST_MODE)
from execution import broker
from execution import dip_zones as dz
from execution import quarterly_hold_manager as qm

ET = ZoneInfo("America/New_York")
_ROW = {"date": "2026-10-15", "open": "09:30", "close": "16:00", "session_open": "0400", "session_close": "2000"}


def _client(is_open=False, rows=None):
    c = mock.Mock()
    c.get_clock.return_value = mock.Mock(is_open=is_open)
    c.get.return_value = [_ROW] if rows is None else rows
    return c


class Session(unittest.TestCase):
    def _at(self, hh, mm, **kw):
        with mock.patch.object(broker, "_get_trading_client", return_value=_client(**kw)):
            return broker.extended_session_now(datetime(2026, 10, 15, hh, mm, tzinfo=ET))

    def test_premarket_and_after_hours_inside(self):
        self.assertTrue(self._at(4, 0))
        self.assertTrue(self._at(9, 29))
        self.assertTrue(self._at(16, 0))
        self.assertTrue(self._at(19, 59))

    def test_outside_sessions(self):
        self.assertFalse(self._at(3, 59))      # overnight session: off
        self.assertFalse(self._at(20, 0))
        self.assertFalse(self._at(12, 0, is_open=True))
        self.assertFalse(self._at(9, 45))      # regular hours even if the clock read says closed (halt): not extended

    def test_premarket_only(self):
        with mock.patch.object(broker, "_get_trading_client", return_value=_client()):
            at = lambda hh, mm: broker.extended_session_now(datetime(2026, 10, 15, hh, mm, tzinfo=ET), premarket_only=True)  # noqa: E731
            self.assertTrue(at(4, 0))
            self.assertTrue(at(9, 29))
            self.assertFalse(at(16, 0))
            self.assertFalse(at(19, 59))

    def test_early_close_day_uses_the_real_close(self):
        row = dict(_ROW, close="13:00", session_close="1700")
        self.assertTrue(self._at(13, 5, rows=[row]))
        self.assertFalse(self._at(17, 0, rows=[row]))

    def test_non_trading_day_and_unreadable(self):
        self.assertFalse(self._at(10, 0, rows=[]))
        self.assertIsNone(self._at(10, 0, rows=[dict(_ROW, session_open="")]))
        with mock.patch.object(broker, "_get_trading_client", side_effect=RuntimeError("down")):
            self.assertIsNone(broker.extended_session_now(datetime(2026, 10, 15, 5, 0, tzinfo=ET)))


class TypicalMove(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "dip_zones.json"
        self.p = mock.patch.object(dz, "_STATE", self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_reader(self):
        self.assertIsNone(dz.typical_move_for("NVDA"))
        self.tmp.write_text(json.dumps({"zones": {"NVDA": {"rung_a": -0.04, "rung_b": -0.08, "median_move": 0.0154}}}))
        self.assertEqual(dz.typical_move_for("nvda"), 0.0154)
        for bad in (0, -0.01, "nan", "inf", None):
            self.tmp.write_text(json.dumps({"zones": {"NVDA": {"median_move": bad}}}))
            self.assertIsNone(dz.typical_move_for("NVDA"), bad)

    def test_compute_writes_median_move(self):
        import numpy as np
        import pandas as pd
        idx = pd.bdate_range("2025-08-01", periods=300)
        rng = np.random.default_rng(3)
        f = pd.DataFrame({"A": 100 * np.cumprod(1 + rng.normal(0, 0.01, 300)),
                          "B": 100 * np.cumprod(1 + rng.normal(0, 0.03, 300))}, index=idx)
        z = dz.compute_zones(f, 2)
        win = f.index > f.index.max() - pd.DateOffset(years=1)
        self.assertAlmostEqual(z["A"]["median_move"], (f["A"] / f["A"].shift(1) - 1).abs()[win].median(), places=12)
        self.assertLess(z["A"]["median_move"], z["B"]["median_move"])


def _mgr(price=200.0):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m._positions = {}
    m.broker = mock.Mock()
    m.broker.get_account.side_effect = RuntimeError("no broker in tests")
    m._get_live_price = lambda sym: 226.0  # type: ignore[method-assign]  # stale mark: must NOT be used when extended
    m._get_account_equity = mock.Mock(return_value=2700.0)  # type: ignore[method-assign]
    m._save_state = lambda: True  # type: ignore[method-assign]
    m._now_et = lambda: datetime(2026, 10, 15, 17, 0)  # type: ignore[method-assign]
    pos = qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                          qty_filled=1, avg_entry_price=226.36, tranche1_price=226.36, stop_price=192.41,
                          stop_order_id="stop-1")
    m._positions["NVDA"] = pos
    return m, pos


class QuoteCheck(unittest.TestCase):
    def _ok(self, quote, move=0.0154, px=200.0):
        m, _ = _mgr()
        with mock.patch("data.alpaca_data.get_latest_quote", return_value=quote), \
                mock.patch("execution.dip_zones.typical_move_for", return_value=move):
            return m._extended_quote_ok("NVDA", px)

    def test_tight_quote_passes(self):
        self.assertTrue(self._ok({"bid": 199.9, "ask": 200.1}))

    def test_wide_quote_or_missing_inputs_fail_closed(self):
        self.assertFalse(self._ok({"bid": 190.0, "ask": 210.0}))         # 10% spread vs 1.54% typical move
        self.assertFalse(self._ok(None))
        self.assertFalse(self._ok({"bid": 199.9, "ask": 200.1}, move=None))
        self.assertFalse(self._ok({"bid": 200.1, "ask": 199.9}))          # crossed
        self.assertFalse(self._ok({"bid": 0, "ask": 200.1}))
        self.assertFalse(self._ok({"bid": 199.9, "ask": 200.1}, px=201.0))  # print outside the quote


class ExtendedPass(unittest.TestCase):
    def test_outside_session_does_nothing(self):
        m, _ = _mgr()
        m._maybe_dip_add = mock.Mock()  # type: ignore[method-assign]
        for v in (False, None):
            with mock.patch("execution.broker.extended_session_now", side_effect=lambda v=v, **kw: v):
                self.assertEqual(m.run_extended_hours_check(), 0)
        m._maybe_dip_add.assert_not_called()

    def test_inside_session_runs_proof_then_dip_add_extended(self):
        m, pos = _mgr()
        m._get_open_orders = mock.Mock(return_value=[])  # type: ignore[method-assign]
        m._reconcile_stop_coverage = mock.Mock(return_value=(False, False))  # type: ignore[method-assign]
        m._detect_external_close = mock.Mock(return_value=False)  # type: ignore[method-assign]
        m._strict_resync_qty = mock.Mock(return_value=1)  # type: ignore[method-assign]
        m._maybe_dip_add = mock.Mock()  # type: ignore[method-assign]
        calls = []
        with mock.patch("execution.broker.extended_session_now",
                        side_effect=lambda **kw: calls.append(kw) or True):
            self.assertEqual(m.run_extended_hours_check(), 1)
        self.assertEqual(calls, [{"premarket_only": True}])
        m._maybe_dip_add.assert_called_once_with(pos, "2026-10-15", extended=True)

    def test_blocked_coverage_unreadable_book_or_unproven_qty_skip(self):
        for setup in ("blocked", "book", "qty", "closed", "recovery"):
            m, pos = _mgr()
            m._get_open_orders = mock.Mock(return_value=None if setup == "book" else [])  # type: ignore[method-assign]
            m._reconcile_stop_coverage = mock.Mock(return_value=(True, setup == "blocked"))  # type: ignore[method-assign]
            m._detect_external_close = mock.Mock(return_value=setup == "closed")  # type: ignore[method-assign]
            m._strict_resync_qty = mock.Mock(return_value=None if setup == "qty" else 1)  # type: ignore[method-assign]
            if setup == "recovery":
                pos.ambiguous_add_client_order_id = "QH-x"
            m._maybe_dip_add = mock.Mock()  # type: ignore[method-assign]
            with mock.patch("execution.broker.extended_session_now", return_value=True):
                m.run_extended_hours_check()
            m._maybe_dip_add.assert_not_called()


class ExtendedDipAdd(unittest.TestCase):
    def test_uses_fresh_print_not_the_stale_mark(self):
        m, pos = _mgr()
        from data.live_price import LivePrice
        # fresh print $220.0 (-2.8%) is ABOVE a -3.9% zone; the stale $226 mark would also be above. Use $216 (-4.6%).
        with mock.patch("data.live_price.live_price", return_value=LivePrice(216.0, "iex_trade", 2.0)), \
                mock.patch("execution.dip_zones.zones_for", return_value=(-0.039, -0.085)):
            m._maybe_dip_add(pos, "2026-10-15", extended=True)
        m._get_account_equity.assert_called()                 # reached sizing on the fresh price

    def test_no_fresh_print_no_add(self):
        m, pos = _mgr()
        with mock.patch("data.live_price.live_price", return_value=None), \
                mock.patch("execution.dip_zones.zones_for", return_value=(-0.039, -0.085)):
            m._maybe_dip_add(pos, "2026-10-15", extended=True)
        m._get_account_equity.assert_not_called()

    def test_stop_safe_add_outside_session_never_touches_the_stop(self):
        m, pos = _mgr()
        with mock.patch("execution.broker.extended_session_now", return_value=None), \
                mock.patch("execution.broker.cancel_order") as cancel, \
                mock.patch("execution.tier_capital_allocator.live_admit") as admit:
            self.assertEqual(m._stop_safe_add(pos, 1, 216.0, "dip-add", extended=True), 0)
        cancel.assert_not_called()
        admit.assert_not_called()
        self.assertEqual(pos.stop_order_id, "stop-1")


if __name__ == "__main__":
    unittest.main()
