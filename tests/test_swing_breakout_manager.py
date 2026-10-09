# ruff: noqa: E501
"""C2 swing breakout tier (2026-09-29): decision functions + order lifecycle with a fake broker."""
import contextlib
import importlib
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

from execution import swing_breakout_manager as sb


def _bars(closes, vol=1e6, start="2025-01-02"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": vol}, index=idx)


def _breakout_df(n=260):
    closes = list(np.linspace(100, 120, n - 1)) + [125.0]   # last close above the prior 55-day high and SMA200
    return _bars(closes)


@contextlib.contextmanager
def _mods(fakes):
    """Install fake modules in sys.modules AND as attributes on their parent packages: `from execution import
    broker` reads the package attribute first, which another test may have set to a different fake."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(sys.modules, fakes))
        for name, mod in fakes.items():
            parent, _, child = name.rpartition(".")
            if parent:
                stack.enter_context(mock.patch.object(importlib.import_module(parent), child, mod, create=True))
        yield



def _fresh(px):
    """A (price, time) last-trade print stamped now — fresh for the age check; None stays None."""
    from datetime import timezone
    return None if px is None else (px, datetime.now(timezone.utc))

class Decisions(unittest.TestCase):
    def test_events_carry_versioned_c2_family_identity(self):
        fake = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"trade_logger": fake}):
            sb._log("signal", "AAPL", note="test")
        kwargs = fake.log_event.call_args.kwargs
        self.assertEqual(kwargs["family_id"], "swing_breakout_55d_v1")
        self.assertEqual(kwargs["hypothesis_version"], "c2-swing-breakout-2026-09-29")

    def test_signal_fires_on_new_55d_high_above_sma200(self):
        sig = sb.breakout_signal(_breakout_df())
        self.assertIsNotNone(sig)
        self.assertEqual(sig["close"], 125.0)
        self.assertGreater(sig["atr"], 0)

    def test_no_signal_when_not_a_new_high(self):
        closes = list(np.linspace(100, 130, 259)) + [120.0]
        self.assertIsNone(sb.breakout_signal(_bars(closes)))

    def test_no_signal_below_sma200(self):
        closes = list(np.linspace(200, 100, 200)) + [95.0] * 55 + [99.0]  # new 55d high but far below SMA200
        self.assertIsNone(sb.breakout_signal(_bars(closes)))

    def test_insufficient_history(self):
        self.assertIsNone(sb.breakout_signal(_bars([100.0] * 150)))

    def test_rank_by_dollar_volume(self):
        bars = {"A": _bars([10.0] * 60, vol=1e6), "B": _bars([100.0] * 60, vol=1e6), "C": _bars([50.0] * 60, vol=1e6)}
        self.assertEqual(sb.rank_universe(bars, 2), ["B", "C"])

    def test_exit_rules(self):
        self.assertEqual(sb.exit_reason(99.0, 100.0, 5, 30), "trend_break")
        self.assertEqual(sb.exit_reason(101.0, 100.0, 30, 30), "time")
        self.assertIsNone(sb.exit_reason(101.0, 100.0, 29, 30))
        self.assertIsNone(sb.exit_reason(float("nan"), 100.0, 3, 30))   # unknown price never forces an exit
        self.assertIsNone(sb.exit_reason(99.0, None, None, 30))

    def test_sessions_held_counts_entry_day_as_one(self):
        df = _bars([100.0] * 10, start="2026-09-14")    # completed sessions 09-14 .. 09-25
        self.assertEqual(sb.sessions_held(df, "2026-09-24"), 3)   # 09-24, 09-25 completed + today

    def test_size_order_bounds(self):
        with mock.patch.object(sb.config, "SWING_BREAKOUT_MAX_RISK_PCT", 0.02, create=True):
            q, _ = sb.size_order(2500.0, 230.0, 5.0, 0.0, 0.0)          # slot $500 → 2 sh; risk $50/$12.5 → 4
            self.assertEqual(q, 2)
            q, _ = sb.size_order(2500.0, 230.0, 5.0, 2400.0, 0.0)       # others use 96% → room $100 → 0
            self.assertEqual(q, 0)
            q, _ = sb.size_order(2500.0, 100.0, 20.0, 0.0, 0.0)         # risk cap: $50 / $50 → 1 sh
            self.assertEqual(q, 1)
            q, _ = sb.size_order(2500.0, 100.0, 1.0, 0.0, 1900.0)       # tier already $1,900 of $2,000 → 1 sh
            self.assertEqual(q, 1)
            q, _ = sb.size_order(float("nan"), 100.0, 1.0, 0.0, 0.0)
            self.assertEqual(q, 0)
            q, why = sb.size_order(2520.0, 511.0, 9.5, 0.0, 0.0)       # MSFT: 1 share > $504 slot → floor → 1 sh
            self.assertEqual(q, 1)
            self.assertIn("1-share floor", why)
            q, _ = sb.size_order(2520.0, 640.0, 9.5, 0.0, 0.0)         # one share > 25% of equity ($630) → 0
            self.assertEqual(q, 0)
            q, _ = sb.size_order(2520.0, 600.0, 21.0, 0.0, 0.0)        # 2.5×ATR $52.5 > $50.4 risk cap → 0
            self.assertEqual(q, 0)
            q, _ = sb.size_order(2520.0, 511.0, 9.5, 0.0, 2000.0)      # budget room $16 → no floor → 0
            self.assertEqual(q, 0)

    def test_size_order_invariant11_amended_two_limbs(self):
        # 2026-10-02 live book: equity $2,441, swing $256, QHM $3,175. QHM no longer blocks the 100% limb; the
        # total limb 1.75 × 2441 − 3431 = $841 binds → slot $488 → 2 sh at $230 (risk cap 48.8/12.5 → 3).
        with mock.patch.multiple(sb.config, SWING_BREAKOUT_MAX_RISK_PCT=0.02, SWING_BREAKOUT_TOTAL_OVERNIGHT_K=1.75,
                                 create=True):
            q, why = sb.size_order(2441.0, 230.0, 5.0, 256.0, 0.0, 3175.0)
            self.assertEqual(q, 2)
            self.assertIn("total-limb $841", why)
            q, _ = sb.size_order(2441.0, 230.0, 5.0, 256.0, 700.0, 3175.0)    # tier already $700 → room $141 → 0
            self.assertEqual(q, 0)
            q, _ = sb.size_order(2441.0, 230.0, 5.0, 256.0, 0.0, 4100.0)      # QHM fills the K limb → 0
            self.assertEqual(q, 0)
            q, _ = sb.size_order(2441.0, 230.0, 5.0, 3431.0, 0.0, 0.0)        # same book, ledger unreadable → 0
            self.assertEqual(q, 0)
            q, _ = sb.size_order(2441.0, 230.0, 5.0, 256.0, 0.0, -1.0)        # bad input → 0
            self.assertEqual(q, 0)

    def test_protected_notional_from_ledger_and_fail_safe(self):
        led = {"positions": {
            "LLY": {"tiers": {"qhm": {"qty": 2.0}, "forever6": {"qty": 0.0}, "intraday": {"qty": 0.0}}},
            "GE": {"tiers": {"qhm": {"qty": 1.0}, "forever6": {"qty": 1.0}, "intraday": {"qty": 1.0}}},
        }}
        pos = [SimpleNamespace(symbol="LLY", qty="2", market_value="2284"),
               SimpleNamespace(symbol="GE", qty="3", market_value="-930"),   # 2 of 3 shares protected
               SimpleNamespace(symbol="PLTR", qty="1", market_value="190")]
        import execution.ownership_guard as og
        with mock.patch.object(og, "load_ledger", return_value=led):
            self.assertAlmostEqual(sb._protected_notional(pos), 2284 + 620, places=6)
        with mock.patch.object(og, "load_ledger", side_effect=og.LedgerError("corrupt")):
            self.assertEqual(sb._protected_notional(pos), 0.0)


class FakeBroker:
    def __init__(self, fill=True, stop_ok=True, positions=None):
        self.fill, self.stop_ok = fill, stop_ok
        self.positions = positions or []
        self.orders, self.stops, self.closes, self.cancels = {}, [], [], []

    def get_open_positions(self):
        return self.positions

    def get_open_orders(self, symbol=None):
        return []


    def get_account(self):
        return SimpleNamespace(equity=2500.0, buying_power=10000.0)

    def submit_limit_order(self, sym, qty, side, limit, tier="intraday", client_order_id=None):
        o = SimpleNamespace(id=f"ord-{sym}", status="filled" if self.fill else "new",
                            filled_qty=qty if self.fill else 0, filled_avg_price=limit if self.fill else 0)
        self.orders[o.id] = o
        return o

    def get_order(self, oid):
        return self.orders.get(oid, SimpleNamespace(status="new", filled_qty=0, filled_avg_price=0))

    def cancel_order(self, oid):
        self.cancels.append(oid)
        o = self.orders.get(oid)
        if o is not None and o.status not in ("filled", "canceled"):
            o.status = "canceled"                            # a cancel takes effect (terminal) at once
        return True

    def get_order_by_client_order_id(self, coid):
        return None

    def submit_gtc_stop_order(self, sym, qty, side, px, tier="intraday", allow_cancel_blocking=True):
        self.stops.append((sym, qty, side, px, tier))
        if not self.stop_ok:
            return None
        o = SimpleNamespace(id=f"stop-{sym}", status="new", filled_qty=0, filled_avg_price=0)
        self.orders[o.id] = o
        return o

    def cancel_stop_confirmed(self, sym, oid):
        self.cancels.append(oid)
        return True

    def partial_close_position(self, sym, qty, tier="intraday", _return_order=False):
        self.closes.append((sym, qty, tier))
        o = SimpleNamespace(id=f"close-{sym}", status="filled", filled_qty=qty, filled_avg_price=130.0)
        self.orders[o.id] = o
        return o

    def get_open_position(self, sym):
        # shares bought through this fake for `sym`; a lot seeded directly into state (exit tests) holds 2
        bought = sum(int(o.filled_qty) for k, o in self.orders.items() if k == f"ord-{sym}")
        return SimpleNamespace(qty=bought or 2)

    market_open = None                              # None → clock raises (15:50 ET fallback); True/False → is_open

    def get_clock(self):
        if self.market_open is None:
            raise RuntimeError("no clock in tests")   # exercises the 15:50 ET fallback
        return {"is_open": self.market_open, "next_close": None}


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(sb, "_STATE", Path(self.tmp.name) / "sb.json")
        self.p.start()
        self.logs = []
        self.p2 = mock.patch.object(sb, "_log", lambda ev, sym, **kw: self.logs.append((ev, sym, kw)))
        self.p2.start()
        self.p3 = mock.patch.object(sb, "_page", lambda m: self.logs.append(("page", "", {"m": m})))
        self.p3.start()
        self.cfg = mock.patch.multiple(sb.config, SWING_BREAKOUT_ENABLED=True, SWING_BREAKOUT_POOL=["AAA", "BBB"],
                                       SWING_BREAKOUT_TOP_N=10, SWING_BREAKOUT_FILL_WAIT_S=0.0,
                                       SWING_BREAKOUT_MAX_RISK_PCT=0.02, create=True)
        self.cfg.start()
        self.admits = []
        self.alloc_ok = True

        def _admit(tier, owner, sym, side, qty, px, **kw):
            self.admits.append((tier, owner, sym, side, qty, kw.get("stop_price")))
            lease = SimpleNamespace(client_order_id=f"IN-{sym}-lease") if self.alloc_ok else None
            return SimpleNamespace(approved=self.alloc_ok, reason="ok" if self.alloc_ok else "tier capital cap", lease=lease)
        self.alloc = SimpleNamespace(live_admit=_admit, live_order_id=lambda lease: lease.client_order_id if lease else None,
                                     live_bind=lambda lease, order: True, live_release=lambda lease, reason: None)

    def tearDown(self):
        for p in (self.cfg, self.p3, self.p2, self.p):
            p.stop()
        self.tmp.cleanup()

    def _run_entries(self, broker, bars):
        fakes = {"execution.broker": broker,
                 "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: {"bid": 124.9, "ask": 125.0}, get_latest_trade_with_time=lambda s: _fresh(124.9)),
                 "execution.quarterly_hold_manager": SimpleNamespace(get_quarterly_hold_symbols=lambda: []),
                 "execution.tier_capital_allocator": self.alloc}
        m = sb.SwingBreakoutManager()
        with _mods(fakes), mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: bars[s])), \
                mock.patch("time.sleep"):
            return m, m.run_entries(datetime(2026, 9, 30, 10, 10, tzinfo=sb.ET))

    def test_entry_places_protective_stop(self):
        b = FakeBroker()
        m, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, ["AAA"])
        st = sb._load_state()["positions"]["AAA"]
        self.assertEqual(st["status"], "open")
        self.assertEqual(len(b.stops), 1)
        sym, qty, side, px, tier = b.stops[0]
        self.assertEqual((sym, side, tier), ("AAA", "sell", "intraday"))
        self.assertEqual(self.admits[0][:4], ("swing", "intraday", "AAA", "buy"))
        self.assertLess(px, st["entry_px"])
        self.assertEqual(qty, st["qty"])

    def test_once_per_day_latch(self):
        b = FakeBroker()
        bars = {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)}
        self._run_entries(b, bars)
        _, got = self._run_entries(b, bars)
        self.assertEqual(got, [])

    # ── revision 2026-10-02 (board risk + execution seats, cold-2nd) ──────────────────────────────
    def test_allocator_off_mints_and_stores_own_client_id(self):
        self.alloc_ok = True
        self.alloc.live_order_id = lambda lease: None          # allocator OFF: approved, no lease
        b = FakeBroker()
        sent = []
        orig = b.submit_limit_order
        b.submit_limit_order = lambda *a, **kw: (sent.append(kw.get("client_order_id")), orig(*a, **kw))[1]
        self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        coid = sb._load_state()["positions"]["AAA"]["coid"]
        self.assertTrue(coid.startswith("IN-AAA-b-"))
        self.assertEqual(sent, [coid])

    def test_forever6_universe_is_excluded(self):
        with mock.patch.object(sb.config, "FOREVER6_UNIVERSE", ["AAA"], create=True):
            _, got = self._run_entries(FakeBroker(), {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, [])
        self.assertTrue(any(ev == "skip" and s == "AAA" for ev, s, _ in self.logs))

    def test_broker_read_failure_unlatches_the_day(self):
        b = FakeBroker()
        b.get_account = lambda: (_ for _ in ()).throw(RuntimeError("503"))
        self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertIsNone(sb._load_state().get("last_entry_scan"))

    def test_lot_notional_claims_request_until_filled(self):
        self.assertEqual(sb._lot_notional({"qty": 2, "entry_px": 100.0, "req_qty": 3, "limit": 101.0}), 200.0)
        self.assertEqual(sb._lot_notional({"qty": 0, "req_qty": 3, "limit": 101.0}), 303.0)
        self.assertEqual(sb._lot_notional({"qty": "x"}), 0.0)

    def test_zero_position_read_is_not_gone_until_corroborated(self):
        m = sb.SwingBreakoutManager()
        reads = iter([0, 0])
        with mock.patch.object(sb.SwingBreakoutManager, "_broker_qty", staticmethod(lambda s: next(reads))), \
                mock.patch("time.sleep"):
            self.assertFalse(m._confirmed_gone("AAA", {"opened_ts": sb.time.time()}))   # fresh lot: never gone
            self.assertTrue(m._confirmed_gone("AAA", {"opened_ts": 1.0}))               # old lot, 0 then 0
        reads2 = iter([0, 3])
        with mock.patch.object(sb.SwingBreakoutManager, "_broker_qty", staticmethod(lambda s: next(reads2))), \
                mock.patch("time.sleep"):
            self.assertFalse(m._confirmed_gone("AAA", {}))                               # second read non-zero

    def test_estimated_entry_books_unknown_pnl(self):
        state = {"positions": {"AAA": {"status": "open", "qty": 2, "entry_px": 125.0, "entry_px_estimated": True}}}
        sb.SwingBreakoutManager()._book_exit("AAA", state["positions"]["AAA"], state, "time", 130.0, 2)
        self.assertIsNone(state["positions"]["AAA"]["realized_pnl"])

    def test_book_gone_uses_pending_close_fill_after_restart(self):
        b = FakeBroker()
        b.orders["close-AAA"] = SimpleNamespace(id="close-AAA", status="filled", filled_qty=2, filled_avg_price=131.0)
        rec = {"status": "open", "qty": 2, "entry_px": 125.0, "stop_order_id": "", "close_order_id": "close-AAA",
               "exit_reason_pending": "trend_break"}
        state = {"positions": {"AAA": rec}}
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            sb.SwingBreakoutManager()._book_gone("AAA", rec, state)
        self.assertEqual((rec["status"], rec["exit_reason"], rec["realized_pnl"]), ("closed", "trend_break", 12.0))

    def test_book_gone_exit_event_has_a_loggable_price(self):
        rec = {"status": "open", "qty": 2, "entry_px": 125.0, "stop_order_id": "", "close_order_id": ""}
        state = {"positions": {"AAA": rec}}
        with _mods({"execution.broker": FakeBroker()}):
            sb.SwingBreakoutManager()._book_gone("AAA", rec, state)
        ev = [kw for e, s, kw in self.logs if e == "exit" and s == "AAA"][0]
        self.assertEqual((ev["price"], ev["price_unknown"], ev["realized_pnl"]), (0.0, True, None))

    def test_existing_position_is_skipped(self):
        b = FakeBroker(positions=[SimpleNamespace(symbol="AAA", market_value=300.0)])
        _, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, [])
        self.assertTrue(any(ev == "skip" and s == "AAA" for ev, s, _ in self.logs))

    def test_stop_failure_flattens(self):
        b = FakeBroker(stop_ok=False)
        _, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, [])
        self.assertEqual(b.closes[0][0], "AAA")
        self.assertEqual(b.closes[0][2], "intraday")
        self.assertEqual(sb._load_state()["positions"]["AAA"]["status"], "closed")

    def test_allocator_denial_is_a_logged_skip(self):
        b = FakeBroker()
        self.alloc_ok = False
        _, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, [])
        self.assertEqual(b.orders, {})
        self.assertTrue(any(ev == "skip" and "allocator" in kw.get("reason", "") for ev, _, kw in self.logs))

    def test_unfilled_entry_records_nothing_open(self):
        b = FakeBroker(fill=False)
        _, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, [])
        self.assertEqual(sb._load_state()["positions"]["AAA"]["status"], "unfilled")
        self.assertEqual(b.stops, [])

    def test_exit_trend_break_closes_own_qty(self):
        sb._save_state({"positions": {"AAA": {"status": "open", "qty": 2, "entry_px": 125.0, "stop_px": 110.0,
                                             "stop_order_id": "stop-AAA", "entry_date": "2026-09-20", "coid": "BO-x"}}})
        b = FakeBroker()
        df = _bars([130.0] * 30, start="2026-08-17")          # prior 20-day low = 128.7
        fakes = {"execution.broker": b, "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: {"bid": 120.0, "ask": 120.2}, get_latest_trade_with_time=lambda s: _fresh(120.0))}
        with _mods(fakes), mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: df)), \
                mock.patch("time.sleep"):
            got = sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 15, 52, tzinfo=sb.ET))
        self.assertEqual(got, ["AAA"])
        self.assertEqual(b.closes, [("AAA", 2, "intraday")])
        self.assertIn("stop-AAA", b.cancels)
        self.assertEqual(sb._load_state()["positions"]["AAA"]["exit_reason"], "trend_break")

    def test_partial_stop_fill_closes_only_remainder(self):
        sb._save_state({"positions": {"AAA": {"status": "open", "qty": 3, "entry_px": 125.0, "stop_px": 110.0,
                                             "stop_order_id": "stop-AAA", "entry_date": "2026-09-20", "coid": "BO-x"}}})
        b = FakeBroker()
        b.orders["stop-AAA"] = SimpleNamespace(id="stop-AAA", status="canceled", filled_qty=1, filled_avg_price=110.0)
        st = sb._load_state()
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            sb.SwingBreakoutManager()._close_lot("AAA", st["positions"]["AAA"], st, "trend_break")
        self.assertEqual(b.closes, [("AAA", 2, "intraday")])

    def test_before_1550_only_reconciles(self):
        sb._save_state({"positions": {"AAA": {"status": "open", "qty": 2, "entry_px": 125.0, "stop_order_id": "stop-AAA",
                                             "entry_date": "2026-09-20"}}})
        b = FakeBroker()
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            got = sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 12, 0, tzinfo=sb.ET))
        self.assertEqual((got, b.closes), ([], []))

    def test_stop_fill_is_booked_by_reconcile(self):
        sb._save_state({"positions": {"AAA": {"status": "open", "qty": 2, "entry_px": 125.0, "stop_order_id": "stop-AAA",
                                             "entry_date": "2026-09-20"}}})
        b = FakeBroker()
        b.orders["stop-AAA"] = SimpleNamespace(id="stop-AAA", status="filled", filled_qty=2, filled_avg_price=111.0)
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 12, 0, tzinfo=sb.ET))
        rec = sb._load_state()["positions"]["AAA"]
        self.assertEqual((rec["status"], rec["exit_reason"], rec["realized_pnl"]), ("closed", "protective_stop", -28.0))

    def test_half_day_close_uses_clock(self):
        from datetime import timedelta
        now = datetime(2026, 11, 27, 12, 52, tzinfo=sb.ET)
        clock = SimpleNamespace(get_clock=lambda: {"next_close": datetime(2026, 11, 27, 13, 0, tzinfo=sb.ET)})
        with _mods({"execution.broker": clock}):
            self.assertTrue(sb.SwingBreakoutManager._near_close(now))
            self.assertFalse(sb.SwingBreakoutManager._near_close(now - timedelta(minutes=30)))

    def test_disabled_is_inert(self):
        with mock.patch.object(sb.config, "SWING_BREAKOUT_ENABLED", False):
            self.assertEqual(sb.SwingBreakoutManager().run_entries(datetime(2026, 9, 30, 10, 10, tzinfo=sb.ET)), [])

    def test_unreadable_state_blocks_entries(self):
        sb._STATE.write_text("{not json")
        self.assertEqual(sb.SwingBreakoutManager().run_entries(datetime(2026, 9, 30, 10, 10, tzinfo=sb.ET)), [])
        self.assertTrue(any(ev == "page" for ev, _, _ in self.logs))

    # ── self-audit fixes (2026-09-29) ─────────────────────────────────────────
    def test_partially_filled_is_not_terminal(self):
        self.assertFalse(sb._is_terminal("partially_filled"))
        self.assertFalse(sb._is_terminal("OrderStatus.PARTIALLY_FILLED"))
        self.assertTrue(sb._is_terminal("OrderStatus.FILLED"))
        self.assertTrue(sb._is_terminal("canceled"))

    def test_unreadable_entry_order_falls_back_to_position(self):
        b = FakeBroker()
        b.get_order = lambda oid: None                        # order unreadable after the fill
        b.get_open_position = lambda sym: SimpleNamespace(qty=3)
        _, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, ["AAA"])
        rec = sb._load_state()["positions"]["AAA"]
        self.assertEqual((rec["status"], rec["qty"]), ("open", 3))
        self.assertEqual(b.stops[0][1], 3)

    def test_unreadable_entry_order_and_position_is_unverified(self):
        b = FakeBroker()
        b.get_order = lambda oid: None

        def _raise(sym):
            raise RuntimeError("api down")
        b.get_open_position = _raise
        _, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, [])
        self.assertEqual(sb._load_state()["positions"]["AAA"]["status"], "entry_unverified")
        self.assertEqual(b.stops, [])
        self.assertIn("AAA", sb.get_breakout_symbols())       # still excluded from orphan adoption
        self.assertTrue(any(ev == "page" for ev, _, _ in self.logs))

    def test_same_scan_second_entry_sees_first_lot(self):
        b = FakeBroker()
        with mock.patch.multiple(sb.config, SWING_BREAKOUT_BUDGET_PCT=0.20, create=True):
            _, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _breakout_df()})
        self.assertEqual(len(got), 1)                          # the 20% budget is used by the first lot
        self.assertTrue(any(ev == "skip" and kw.get("reason") == "no size" for ev, _, kw in self.logs))

    def test_unreadable_stop_fill_halts_exit_then_reprotects(self):
        sb._save_state({"positions": {"AAA": {"status": "open", "qty": 2, "entry_px": 125.0, "stop_px": 110.0,
                                             "stop_order_id": "stop-AAA", "entry_date": "2026-09-20", "coid": "IN-x"}}})
        b = FakeBroker()
        real_get = b.get_order
        b.get_order = lambda oid: None
        st = sb._load_state()
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            ok = sb.SwingBreakoutManager()._close_lot("AAA", st["positions"]["AAA"], st, "trend_break")
        self.assertFalse(ok)
        self.assertEqual(b.closes, [])                         # never sells blind
        self.assertEqual(sb._load_state()["positions"]["AAA"]["status"], "exit_unverified")
        b.get_order = real_get
        b.orders["stop-AAA"] = SimpleNamespace(id="stop-AAA", status="canceled", filled_qty=0, filled_avg_price=0)
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 12, 0, tzinfo=sb.ET))
        rec = sb._load_state()["positions"]["AAA"]
        self.assertEqual((rec["status"], rec["qty"]), ("open", 2))
        self.assertEqual(b.stops[-1][:3], ("AAA", 2, "sell"))

    def test_working_close_is_cancelled_before_restop(self):
        sb._save_state({"positions": {"AAA": {"status": "open", "qty": 3, "entry_px": 125.0, "stop_px": 110.0,
                                             "stop_order_id": "stop-AAA", "entry_date": "2026-09-20", "coid": "IN-x"}}})
        b = FakeBroker()

        def _pc(sym, qty, tier="intraday", _return_order=False):
            b.closes.append((sym, qty, tier))
            o = SimpleNamespace(id="close-AAA", status="partially_filled", filled_qty=1, filled_avg_price=120.0)
            b.orders[o.id] = o
            return o
        b.partial_close_position = _pc
        st = sb._load_state()
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            ok = sb.SwingBreakoutManager()._close_lot("AAA", st["positions"]["AAA"], st, "trend_break")
        self.assertFalse(ok)
        self.assertIn("close-AAA", b.cancels)                  # working close cancelled first
        self.assertEqual(b.stops[-1][:2], ("AAA", 2))          # re-stop covers only the unsold 2 shares
        self.assertEqual(sb._load_state()["positions"]["AAA"]["qty"], 2)

    # ── board / cold-2nd findings (2026-09-29) ────────────────────────────────
    def _lot(self, **kw):
        rec = {"status": "open", "qty": 2, "entry_px": 125.0, "stop_px": 110.0, "stop_order_id": "stop-AAA",
               "entry_date": "2026-09-20", "coid": "IN-x", "signal": {"close": 125.0, "atr": 5.0}}
        rec.update(kw)
        sb._save_state({"positions": {"AAA": rec}})

    def _reconcile(self, b, quote=None):
        fakes = {"execution.broker": b,
                 "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: quote or {"bid": 120.0, "ask": 120.2},
                                                     get_latest_trade_with_time=lambda s: _fresh((quote or {"bid": 120.0})["bid"]))}
        with _mods(fakes), mock.patch("time.sleep"):
            sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 12, 0, tzinfo=sb.ET))
        return sb._load_state()["positions"]["AAA"]

    def test_externally_cancelled_stop_is_replaced(self):
        self._lot()
        b = FakeBroker()
        b.orders["stop-AAA"] = SimpleNamespace(id="stop-AAA", status="canceled", filled_qty=0, filled_avg_price=0)
        rec = self._reconcile(b)
        self.assertEqual(b.stops[-1][:4], ("AAA", 2, "sell", 110.0))
        self.assertEqual((rec["status"], rec["stop_order_id"], rec["unprotected"]), ("open", "stop-AAA", False))

    def test_lot_without_stop_id_is_protected(self):            # restart between "open" and the stop save
        self._lot(stop_order_id="")
        b = FakeBroker()
        rec = self._reconcile(b)
        self.assertEqual(len(b.stops), 1)
        self.assertEqual(rec["stop_order_id"], "stop-AAA")

    def test_failed_restop_flags_unprotected_and_retries(self):
        self._lot(stop_order_id="")
        b = FakeBroker(stop_ok=False)
        rec = self._reconcile(b)
        self.assertTrue(rec["unprotected"])
        self.assertTrue(any(ev == "page" and "UNPROTECTED" in kw["m"] for ev, _, kw in self.logs))
        b.stop_ok = True
        rec = self._reconcile(b)                                  # next cycle retries
        self.assertFalse(rec["unprotected"])
        self.assertEqual(rec["stop_order_id"], "stop-AAA")

    def test_failed_restop_with_price_through_stop_closes(self):
        self._lot(stop_order_id="")
        b = FakeBroker(stop_ok=False)
        b.market_open = True
        rec = self._reconcile(b, quote={"bid": 105.0, "ask": 105.2})
        self.assertEqual(b.closes, [("AAA", 2, "intraday")])
        self.assertEqual(rec["exit_reason"], "stop_breached_unprotected")

    def test_wide_bid_alone_is_not_a_breach(self):            # CEO 2026-10-09: last trade, not the IEX bid/ask
        self._lot(stop_order_id="")
        b = FakeBroker(stop_ok=False)
        b.market_open = True
        fakes = {"execution.broker": b,
                 "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: {"bid": 95.0, "ask": 130.0},
                                                     get_latest_trade_with_time=lambda s: _fresh(121.0))}
        with _mods(fakes), mock.patch("time.sleep"):
            sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 12, 0, tzinfo=sb.ET))
        rec = sb._load_state()["positions"]["AAA"]
        self.assertEqual(b.closes, [])                          # bid $95 is below the $110 stop; the last trade is not
        self.assertEqual((rec["status"], rec["unprotected"]), ("open", True))

    def test_missing_last_trade_is_not_a_breach(self):
        self._lot(stop_order_id="")
        b = FakeBroker(stop_ok=False)
        b.market_open = True
        fakes = {"execution.broker": b,
                 "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: {"bid": 105.0, "ask": 105.2},
                                                     get_latest_trade_with_time=lambda s: _fresh(None))}
        with _mods(fakes), mock.patch("time.sleep"):
            sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 12, 0, tzinfo=sb.ET))
        self.assertEqual(b.closes, [])

    def test_stale_last_trade_is_not_a_breach_and_does_not_latch(self):   # board Harris+Taleb 2026-10-09
        from datetime import timedelta, timezone
        old = lambda s: (100.0, datetime.now(timezone.utc) - timedelta(hours=2))   # noqa: E731
        sb._save_state({"positions": {"AAA": {"status": "open", "qty": 2, "entry_px": 125.0, "stop_px": 110.0,
                                             "stop_order_id": "stop-AAA", "entry_date": "2026-09-20", "coid": "BO-x"}}})
        b = FakeBroker()
        df = _bars([130.0] * 30, start="2026-08-17")
        fakes = {"execution.broker": b,
                 "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: {"bid": 99.0, "ask": 99.2},
                                                     get_latest_trade_with_time=old)}
        with _mods(fakes), mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: df)), \
                mock.patch("time.sleep"):
            got = sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 15, 52, tzinfo=sb.ET))
            self.assertFalse(sb.SwingBreakoutManager._price_through_stop("AAA", 110.0))
        self.assertEqual(got, [])
        self.assertNotIn("exit_checked", sb._load_state()["positions"]["AAA"])   # retried next cycle

    def test_trend_break_uses_last_trade_not_midpoint(self):
        sb._save_state({"positions": {"AAA": {"status": "open", "qty": 2, "entry_px": 125.0, "stop_px": 110.0,
                                             "stop_order_id": "stop-AAA", "entry_date": "2026-09-20", "coid": "BO-x"}}})
        b = FakeBroker()
        df = _bars([130.0] * 30, start="2026-08-17")          # prior 20-day low = 128.7
        # midpoint (100+170)/2 = 135 would hold; the last trade 120 is below the 20-day low -> trend break
        fakes = {"execution.broker": b,
                 "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: {"bid": 100.0, "ask": 170.0},
                                                     get_latest_trade_with_time=lambda s: _fresh(120.0))}
        with _mods(fakes), mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: df)), \
                mock.patch("time.sleep"):
            got = sb.SwingBreakoutManager().run_exit_check(datetime(2026, 9, 30, 15, 52, tzinfo=sb.ET))
        self.assertEqual(got, ["AAA"])

    def test_after_hours_breach_does_not_close(self):
        self._lot(stop_order_id="")
        b = FakeBroker(stop_ok=False)
        b.market_open = False
        rec = self._reconcile(b, quote={"bid": 105.0, "ask": 105.2})
        self.assertEqual(b.closes, [])
        self.assertEqual((rec["status"], rec["unprotected"]), ("open", True))

    def test_close_with_no_position_places_no_stop(self):
        self._lot()
        b = FakeBroker()
        b.partial_close_position = lambda sym, qty, tier="intraday", _return_order=False: True
        b.get_open_position = lambda sym: None
        st = sb._load_state()
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            sb.SwingBreakoutManager()._close_lot("AAA", st["positions"]["AAA"], st, "trend_break")
        self.assertEqual(b.stops, [])
        rec = sb._load_state()["positions"]["AAA"]
        self.assertEqual((rec["status"], rec["exit_reason"]), ("closed", "external_close_unverified"))

    def test_partial_stop_then_close_books_full_pnl(self):
        self._lot(qty=3)
        b = FakeBroker()
        b.orders["stop-AAA"] = SimpleNamespace(id="stop-AAA", status="canceled", filled_qty=1, filled_avg_price=110.0)
        st = sb._load_state()
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            sb.SwingBreakoutManager()._close_lot("AAA", st["positions"]["AAA"], st, "trend_break")
        rec = sb._load_state()["positions"]["AAA"]
        self.assertEqual(rec["realized_pnl"], -15.0 + 2 * 5.0)   # 1 @ 110 (-15) + 2 @ 130 (+10)
        self.assertTrue(any(ev == "partial_exit" and kw["realized_pnl"] == -15.0 for ev, _, kw in self.logs))

    def test_restart_mid_entry_promotes_filled_order(self):
        self._lot(status="submitted", qty=0, entry_order_id="ord-AAA", stop_order_id="", entry_px=0, stop_px=0,
                  limit=125.25)
        b = FakeBroker()
        b.orders["ord-AAA"] = SimpleNamespace(id="ord-AAA", status="filled", filled_qty=3, filled_avg_price=125.1)
        rec = self._reconcile(b)
        self.assertEqual((rec["status"], rec["qty"], rec["entry_px"]), ("open", 3, 125.1))
        self.assertEqual(b.stops[-1][:2], ("AAA", 3))
        self.assertAlmostEqual(b.stops[-1][3], round(125.1 - 2.5 * 5.0, 2))

    def test_ambiguous_submit_resolved_by_client_id(self):
        self._lot(status="submit_unknown", qty=0, stop_order_id="", entry_px=0, stop_px=0, limit=125.25)
        b = FakeBroker()
        o = SimpleNamespace(id="ord-AAA", status="filled", filled_qty=2, filled_avg_price=125.0)
        b.orders["ord-AAA"] = o
        b.get_order_by_client_order_id = lambda coid: o
        rec = self._reconcile(b)
        self.assertEqual((rec["status"], rec["qty"]), ("open", 2))
        self.assertIn("AAA", sb.get_breakout_symbols())

    def test_ambiguous_submit_that_never_happened_is_released(self):
        self._lot(status="submit_unknown", qty=0, stop_order_id="", entry_px=0, stop_px=0)
        b = FakeBroker()
        b.get_open_position = lambda sym: None
        rec = self._reconcile(b)
        self.assertEqual(rec["status"], "unfilled")
        self.assertNotIn("AAA", sb.get_breakout_symbols())

    def test_fill_during_cancel_is_counted(self):
        b = FakeBroker(fill=False)

        def _cancel(oid):                                     # the order fills while the cancel is pending
            b.cancels.append(oid)
            o = b.orders.get(oid)
            if o is not None and oid.startswith("ord-"):
                o.status, o.filled_qty, o.filled_avg_price = "filled", 4, 125.2
            return True
        b.cancel_order = _cancel
        _, got = self._run_entries(b, {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)})
        self.assertEqual(got, ["AAA"])
        self.assertEqual(sb._load_state()["positions"]["AAA"]["qty"], 4)
        self.assertEqual(b.stops[-1][1], 4)

    def test_gone_lot_cancels_its_resting_stop(self):
        self._lot()
        b = FakeBroker()
        b.get_open_position = lambda sym: None
        rec = self._reconcile(b)
        self.assertIn("stop-AAA", b.cancels)
        self.assertEqual(rec["exit_reason"], "external_close_unverified")

    def test_deferred_exit_retries_next_cycle(self):
        self._lot()
        b = FakeBroker()
        b.cancel_stop_confirmed = lambda sym, oid: False       # 15:50: stop cancel not confirmed → deferred
        df = _bars([130.0] * 30, start="2026-08-17")
        fakes = {"execution.broker": b, "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: {"bid": 120.0, "ask": 120.2}, get_latest_trade_with_time=lambda s: _fresh(120.0))}
        m = sb.SwingBreakoutManager()
        with _mods(fakes), mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: df)), mock.patch("time.sleep"):
            self.assertEqual(m.run_exit_check(datetime(2026, 9, 30, 15, 51, tzinfo=sb.ET)), [])
            b.cancel_stop_confirmed = lambda sym, oid: True
            self.assertEqual(m.run_exit_check(datetime(2026, 9, 30, 15, 56, tzinfo=sb.ET)), ["AAA"])

    def test_no_entries_before_1005(self):
        b = FakeBroker()
        fakes = {"execution.broker": b}
        with _mods(fakes):
            self.assertEqual(sb.SwingBreakoutManager().run_entries(datetime(2026, 9, 30, 10, 0, tzinfo=sb.ET)), [])
        self.assertEqual(b.orders, {})

    def test_state_unreadable_keeps_last_good_symbols(self):
        self._lot()
        self.assertEqual(sb.get_breakout_symbols(), {"AAA"})
        sb._STATE.write_text("{broken")
        self.assertEqual(sb.get_breakout_symbols(), {"AAA"})

    # ── round-2 findings (2026-09-29) ─────────────────────────────────────────
    def test_entry_stop_fail_with_breach_books_once(self):     # risk-seat probe P5 / cold-2nd R2-1
        b = FakeBroker(stop_ok=False)
        b.market_open = True
        state = {"n": 0}
        real_close = b.partial_close_position

        def _pc(sym, qty, tier="intraday", _return_order=False):
            if state["n"]:
                return True                                    # position already flat
            state["n"] += 1
            o = real_close(sym, qty, tier, _return_order)
            o.filled_avg_price = 100.0                          # sold through the stop
            return o
        b.partial_close_position = _pc
        flat = {"v": False}
        orig_pos = b.get_open_position
        b.get_open_position = lambda sym: None if flat["v"] else orig_pos(sym)
        quotes = iter([{"bid": 124.9, "ask": 125.0}] + [{"bid": 99.0, "ask": 99.2}] * 20)
        fakes = {"execution.broker": b,
                 "data.alpaca_data": SimpleNamespace(get_latest_quote=lambda s: next(quotes), get_latest_trade_with_time=lambda s: _fresh(99.0)),
                 "execution.quarterly_hold_manager": SimpleNamespace(get_quarterly_hold_symbols=lambda: []),
                 "execution.tier_capital_allocator": self.alloc}
        m = sb.SwingBreakoutManager()

        def _book_exit_then_flat(*a, **k):
            flat["v"] = True
            return real_book(*a, **k)
        real_book = m._book_exit
        m._book_exit = _book_exit_then_flat
        bars = {"AAA": _breakout_df(), "BBB": _bars([100.0] * 260)}
        with _mods(fakes), mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: bars[s])), \
                mock.patch("time.sleep"):
            got = m.run_entries(datetime(2026, 9, 30, 10, 10, tzinfo=sb.ET))
        rec = sb._load_state()["positions"]["AAA"]
        exits = [kw for ev, _, kw in self.logs if ev == "exit"]
        self.assertEqual(got, [])
        self.assertEqual(len(exits), 1)                          # booked exactly once
        self.assertEqual(rec["exit_reason"], "stop_breached_unprotected")
        self.assertIsNotNone(rec["realized_pnl"])
        self.assertLess(rec["realized_pnl"], 0)
        self.assertEqual(len(b.closes), 1)

    def test_unreadable_order_with_working_buy_is_not_promoted(self):
        self._lot(status="submitted", qty=0, entry_order_id="ord-AAA", stop_order_id="", entry_px=0, stop_px=0,
                  req_qty=100)
        b = FakeBroker()
        b.get_order = lambda oid: None
        b.get_open_position = lambda sym: SimpleNamespace(qty=40)
        b.get_open_orders = lambda symbol=None: [SimpleNamespace(symbol="AAA", side="buy")]
        rec = self._reconcile(b)
        self.assertEqual(rec["status"], "entry_unverified")
        self.assertEqual(b.stops, [])
        self.assertIn("ord-AAA", b.cancels)                      # _settle cancels even when the read failed

    def test_unreadable_order_promotes_at_most_requested_qty(self):
        self._lot(status="submitted", qty=0, entry_order_id="ord-AAA", stop_order_id="", entry_px=0, stop_px=0,
                  req_qty=4, limit=125.25)
        b = FakeBroker()
        b.get_order = lambda oid: None
        b.get_open_position = lambda sym: SimpleNamespace(qty=7)  # 3 extra shares belong to another tier
        rec = self._reconcile(b)
        self.assertEqual((rec["status"], rec["qty"]), ("open", 4))
        self.assertEqual(b.stops[-1][1], 4)
        self.assertTrue(rec["entry_px_estimated"])

    def test_closed_lot_is_never_closed_again(self):
        self._lot(status="closed", realized_pnl=-20.0, exit_reason="trend_break")
        b = FakeBroker()
        st = sb._load_state()
        with _mods({"execution.broker": b}):
            self.assertTrue(sb.SwingBreakoutManager()._close_lot("AAA", st["positions"]["AAA"], st, "time"))
        self.assertEqual(b.closes, [])
        self.assertEqual(sb._load_state()["positions"]["AAA"]["realized_pnl"], -20.0)


if __name__ == "__main__":
    unittest.main()
