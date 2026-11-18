# ruff: noqa: E501
"""FIFO-CARRY-1117: carried FIFO lots are checked against Alpaca positions on the first tracker pass of a new PT day.

Design record: logs/design_records/fifo_carry_reconcile_2026-11-17.md. Every broker call is mocked; nothing reaches the
network.
"""
import json
import logging
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from execution import fifo_pnl
from execution import portfolio_tracker as pt

TAG = "[change=fifo-carry-1117]"
TODAY = "2026-11-16"


def _pos(sym, qty):
    return SimpleNamespace(symbol=sym, qty=qty)


def _acct(lmv="0", smv="0"):
    return SimpleNamespace(long_market_value=lmv, short_market_value=smv)


def _lot(qty, price, side):
    return {"qty": qty, "price": price, "side": side}


def _order(oid, sym, filled_qty, filled_at, submitted_at):
    return SimpleNamespace(id=oid, symbol=sym, filled_qty=filled_qty, filled_at=filled_at, submitted_at=submitted_at,
                           created_at=submitted_at)


class _Broker:
    """Patches the broker reads the check uses: positions (a list per call, or an exception), account, orders."""

    def __init__(self, test, positions_seq, account=None, orders=None, qhm=()):
        self.positions_seq = list(positions_seq)
        self.account = account if account is not None else _acct()
        self.orders = orders
        client = mock.MagicMock()
        if isinstance(orders, Exception):
            client.get_orders.side_effect = orders
        else:
            client.get_orders.side_effect = lambda filter=None: list(orders or [])
        self.client = client
        test.addCleanup(mock.patch.stopall)
        mock.patch("execution.broker.get_open_positions", side_effect=self._positions).start()
        mock.patch("execution.broker.get_account", side_effect=self._account).start()
        mock.patch("execution.broker.get_trading_client", return_value=client).start()
        mock.patch("execution.broker.submit_market_order", side_effect=AssertionError("no orders")).start()
        mock.patch("execution.quarterly_hold_manager.get_quarterly_hold_symbols", return_value=list(qhm)).start()

    def _positions(self):
        item = self.positions_seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _account(self):
        if isinstance(self.account, Exception):
            raise self.account
        return self.account


def _run(prior_lots, day_fills=(), today=TODAY):
    """The tracker's sequence: P1 before the fill fetch, then P2 + orders + reconcile."""
    p1 = pt._carry_read_positions("P1")
    if p1 is None:
        return prior_lots, None
    return pt._fifo_carry_reconcile(prior_lots, p1, list(day_fills), today)


class TestPureReconcile(unittest.TestCase):
    def test_unchanged_when_equal_or_alpaca_larger_same_sign(self):
        lots = {"A": [_lot(3, 10.0, "long")], "B": [_lot(2, 5.0, "short")], "C": [_lot(1, 7.0, "long")]}
        p = {"A": 3.0, "B": -4.0, "C": 5.0}
        out, changes, skipped = fifo_pnl._reconcile_carried_lots(lots, p, p, set(), set(), set())
        self.assertEqual(out, lots)
        self.assertEqual(changes, [])
        self.assertEqual(skipped, [])

    def test_input_not_mutated(self):
        lots = {"AAPL": [_lot(1, 180.0, "long"), _lot(2, 185.0, "long")]}
        snap = json.loads(json.dumps(lots))
        fifo_pnl._reconcile_carried_lots(lots, {"AAPL": 1.0}, {"AAPL": 1.0}, set(), set(), set())
        self.assertEqual(lots, snap)

    def test_fractional_quantities_truncate_like_fifo(self):
        lots = {"L": [_lot(5, 10.0, "long")], "S": [_lot(2, 20.0, "short")]}
        p = {"L": 2.7, "S": -0.5}
        out, changes, _ = fifo_pnl._reconcile_carried_lots(lots, p, p, set(), set(), set())
        self.assertEqual(out, {"L": [_lot(2, 10.0, "long")]})
        by = {c["symbol"]: c for c in changes}
        self.assertEqual((by["L"]["start_of_day"], by["L"]["action"], by["L"]["shares"]), (2, "trimmed", 3))
        self.assertEqual((by["S"]["start_of_day"], by["S"]["action"], by["S"]["shares"]), (0, "dropped", 2))

    def test_positions_qty_map_reads_objects_and_dicts(self):
        m = fifo_pnl._positions_qty_map([_pos("A", "-3"), {"symbol": "B", "qty": "2.5"}])
        self.assertEqual(m, {"A": -3.0, "B": 2.5})


class TestStartOfDayCheck(unittest.TestCase):
    def _warnings(self, cm):
        return [r.getMessage() for r in cm.records if r.levelno == logging.WARNING]

    def test_phantom_long_dropped(self):
        _Broker(self, [[_pos("NVDA", "4")], [_pos("NVDA", "4")]])
        prior = {"PLTR": [_lot(2, 150.0, "long")], "NVDA": [_lot(4, 100.0, "long")]}
        with self.assertLogs("execution.portfolio_tracker", level="INFO") as cm:
            out, n = _run(prior)
        self.assertEqual(out, {"NVDA": [_lot(4, 100.0, "long")]})
        self.assertEqual(n, 1)
        self.assertIn(f"{TAG} PLTR carried 2, P1 0, P2 0, start-of-day 0: dropped 2", self._warnings(cm))

    def test_long_trimmed_oldest_first(self):
        _Broker(self, [[_pos("AAPL", "1")], [_pos("AAPL", "1")]])
        prior = {"AAPL": [_lot(1, 180.0, "long"), _lot(2, 185.0, "long")]}
        with self.assertLogs("execution.portfolio_tracker", level="INFO") as cm:
            out, n = _run(prior)
        self.assertEqual(out, {"AAPL": [_lot(1, 185.0, "long")]})
        self.assertEqual(n, 1)
        self.assertIn(f"{TAG} AAPL carried 3, P1 1, P2 1, start-of-day 1: trimmed 2", self._warnings(cm))

    def test_short_trimmed_from_string_quantity(self):
        _Broker(self, [[_pos("TSLA", "-3")], [_pos("TSLA", "-3")]])
        prior = {"TSLA": [_lot(2, 400.0, "short"), _lot(3, 410.0, "short")]}
        out, n = _run(prior)
        self.assertEqual(out, {"TSLA": [_lot(3, 410.0, "short")]})
        self.assertEqual(n, 1)

    def test_short_partial_lot_trim(self):
        _Broker(self, [[_pos("TSLA", "-3")], [_pos("TSLA", "-3")]])
        prior = {"TSLA": [_lot(5, 400.0, "short")]}
        out, _ = _run(prior)
        self.assertEqual(out, {"TSLA": [_lot(3, 400.0, "short")]})

    def test_opposite_sign_drops_all(self):
        _Broker(self, [[_pos("UBER", "-3")], [_pos("UBER", "-3")]])
        prior = {"UBER": [_lot(2, 70.0, "long"), _lot(3, 71.0, "long")]}
        with self.assertLogs("execution.portfolio_tracker", level="INFO") as cm:
            out, n = _run(prior)
        self.assertEqual(out, {})
        self.assertEqual(n, 1)
        self.assertIn(f"{TAG} UBER carried 5, P1 -3, P2 -3, start-of-day -3: dropped 5", self._warnings(cm))

    def test_mixed_sign_dropped(self):
        _Broker(self, [[], []])
        prior = {"HOOD": [_lot(5, 30.0, "long"), _lot(5, 31.0, "short")]}
        with self.assertLogs("execution.portfolio_tracker", level="INFO") as cm:
            out, n = _run(prior)
        self.assertEqual(out, {})
        self.assertEqual(n, 1)
        self.assertIn(f"{TAG} HOOD carried 0, P1 0, P2 0, start-of-day 0: dropped 10 (mixed long/short lots)",
                      self._warnings(cm))

    def test_symbol_with_fill_today_skipped(self):
        _Broker(self, [[], []])
        prior = {"MARA": [_lot(29, 15.0, "long")]}
        fills = [{"symbol": "MARA", "side": "buy", "qty": "1", "price": "15", "id": "f1",
                  "transaction_time": "2026-11-16T14:31:00Z"}]
        with self.assertLogs("execution.portfolio_tracker", level="INFO") as cm:
            out, n = _run(prior, fills)
        self.assertEqual(out, prior)
        self.assertEqual(n, 0)
        self.assertIn(f"{TAG} MARA skipped (fill today): carried 29, P1 0, P2 0", self._warnings(cm))

    def test_qhm_symbol_untouched(self):
        b = _Broker(self, [[], []], qhm=("LLY",))
        prior = {"LLY": [_lot(1, 800.0, "long")]}
        out, n = _run(prior)
        self.assertEqual(out, prior)
        self.assertEqual(n, 0)
        b.client.get_orders.assert_not_called()

    def test_positions_read_failure_changes_nothing(self):
        _Broker(self, [RuntimeError("503")])
        prior = {"PLTR": [_lot(2, 150.0, "long")]}
        with self.assertLogs("execution.portfolio_tracker", level="WARNING") as cm:
            out, n = _run(prior)
        self.assertEqual((out, n), (prior, None))
        self.assertEqual(len(cm.records), 1)
        self.assertIn("positions read P1 (or its account check) failed (503)", cm.records[0].getMessage())

    def test_second_positions_read_failure_changes_nothing(self):
        _Broker(self, [[], RuntimeError("503")])
        prior = {"PLTR": [_lot(2, 150.0, "long")]}
        with self.assertLogs("execution.portfolio_tracker", level="WARNING") as cm:
            out, n = _run(prior)
        self.assertEqual((out, n), (prior, None))
        self.assertEqual(len(cm.records), 1)
        self.assertIn("positions read P2 (or its account check) failed (503)", cm.records[0].getMessage())

    def test_positions_read_timeout_changes_nothing(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def _hang():
            release.wait()
            return []
        with mock.patch("execution.manual_sale_proof._REQUEST_TIMEOUT_SECONDS", 0.05), \
                mock.patch("execution.broker.get_open_positions", side_effect=_hang):
            prior = {"PLTR": [_lot(2, 150.0, "long")]}
            with self.assertLogs("execution.portfolio_tracker", level="WARNING") as cm:
                out, n = _run(prior)
        self.assertEqual((out, n), (prior, None))
        self.assertEqual(len(cm.records), 1)
        self.assertIn("timed out", cm.records[0].getMessage())

    def test_empty_read_with_market_value_changes_nothing(self):
        for lmv, smv in (("1234.5", "0"), ("0", "-99.0")):
            with self.subTest(lmv=lmv, smv=smv):
                _Broker(self, [[], []], account=_acct(lmv, smv))
                prior = {"PLTR": [_lot(2, 150.0, "long")]}
                with self.assertLogs("execution.portfolio_tracker", level="WARNING") as cm:
                    out, n = _run(prior)
                self.assertEqual((out, n), (prior, None))
                self.assertEqual(len(cm.records), 1)
                self.assertIn("returned no positions while long market value", cm.records[0].getMessage())
                mock.patch.stopall()

    def test_empty_read_account_failure_changes_nothing(self):
        _Broker(self, [[], []], account=RuntimeError("down"))
        prior = {"PLTR": [_lot(2, 150.0, "long")]}
        out, n = _run(prior)
        self.assertEqual((out, n), (prior, None))

    def test_fill_between_reads_skips_symbol(self):
        # AAPL 1 @ 180 + 2 @ 185 carried; a sell of 1 lands between the two reads (not yet in the fetched fills).
        _Broker(self, [[_pos("AAPL", "3")], [_pos("AAPL", "2")]])
        prior = {"AAPL": [_lot(1, 180.0, "long"), _lot(2, 185.0, "long")]}
        with self.assertLogs("execution.portfolio_tracker", level="INFO") as cm:
            out, n = _run(prior)
        self.assertEqual(out, prior)
        self.assertEqual(n, 0)
        self.assertIn(f"{TAG} AAPL skipped (positions changed between the two reads): carried 3, P1 3, P2 2",
                      self._warnings(cm))

    def test_order_filled_today_skips_symbol(self):
        sod = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)   # 00:00 PT on 11-16
        orders = [
            # GTC stop submitted yesterday, filled today -> counts
            _order("o1", "SOFI", "4", datetime(2026, 11, 16, 14, 31, tzinfo=timezone.utc),
                   datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)),
            # filled before today's PT midnight -> does not count
            _order("o2", "RIVN", "9", datetime(2026, 11, 16, 7, 59, tzinfo=timezone.utc),
                   datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)),
            # cancelled, nothing filled -> does not count
            _order("o3", "RIVN", "0", None, sod),
        ]
        b = _Broker(self, [[], []], orders=orders)
        prior = {"SOFI": [_lot(4, 9.0, "short")], "RIVN": [_lot(9, 12.0, "long")]}
        with self.assertLogs("execution.portfolio_tracker", level="INFO") as cm:
            out, n = _run(prior)
        self.assertEqual(out, {"SOFI": [_lot(4, 9.0, "short")]})
        self.assertEqual(n, 1)
        self.assertIn(f"{TAG} SOFI skipped (order filled today): carried -4, P1 0, P2 0", self._warnings(cm))
        req = b.client.get_orders.call_args.kwargs["filter"]
        self.assertEqual(sorted(req.symbols), ["RIVN", "SOFI"])

    def test_orders_read_pages_through_every_result(self):
        t0 = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
        page1 = [_order("a", "RIVN", "0", None, datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)),
                 _order("b", "RIVN", "0", None, datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc))]
        page2 = [_order("c", "RIVN", "9", datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc), t0)]
        pages = [page1, page2]
        b = _Broker(self, [[], []])
        b.client.get_orders.side_effect = lambda filter=None: pages.pop(0)
        with mock.patch("execution.manual_sale_proof._PAGE_SIZE", 2):
            out, n = _run({"RIVN": [_lot(9, 12.0, "long")]})
        self.assertEqual(out, {"RIVN": [_lot(9, 12.0, "long")]})   # the page-2 order filled today -> skipped
        self.assertEqual(n, 0)
        self.assertEqual(b.client.get_orders.call_count, 2)

    def test_orders_read_past_its_time_budget_changes_nothing(self):
        """One deadline bounds the whole paged read (cold review T1): a history that is still paging when the
        budget runs out fails the read, and the carried lots stay unchanged with one WARNING."""
        full = [_order("a", "PLTR", "0", None, datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)),
                _order("b", "PLTR", "0", None, datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc))]
        b = _Broker(self, [[], []])
        b.client.get_orders.side_effect = lambda filter=None: list(full)
        clock = iter([0.0, 0.0, 1e9, 1e9, 1e9])
        prior = {"PLTR": [_lot(2, 150.0, "long")]}
        with mock.patch("execution.manual_sale_proof._PAGE_SIZE", 2), \
                mock.patch("time.monotonic", side_effect=lambda: next(clock)), \
                self.assertLogs("execution.portfolio_tracker", level="WARNING") as cm:
            out, n = _run(prior)
        self.assertEqual((out, n), (prior, None))
        self.assertEqual(len(cm.records), 1)
        self.assertIn("time budget", cm.records[0].getMessage())
        self.assertEqual(b.client.get_orders.call_count, 1)

    def test_page_started_inside_the_budget_gets_only_the_time_left(self):
        """A page that starts just inside the budget is waited on for the time LEFT, not a fresh full wait
        (adversarial review C4'): a slow page past the deadline fails the read; lots stay unchanged."""
        import threading
        release = threading.Event()
        full = [_order("a", "PLTR", "0", None, datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)),
                _order("b", "PLTR", "0", None, datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc))]
        calls = {"n": 0}

        def _slow(filter=None):
            calls["n"] += 1
            if calls["n"] > 1:
                release.wait()          # the second page never answers inside the budget
            return list(full)
        b = _Broker(self, [[], []])
        b.client.get_orders.side_effect = _slow
        prior = {"PLTR": [_lot(2, 150.0, "long")]}
        try:
            with mock.patch("execution.manual_sale_proof._PAGE_SIZE", 2), \
                    mock.patch("execution.manual_sale_proof._REQUEST_TIMEOUT_SECONDS", 0.2), \
                    self.assertLogs("execution.portfolio_tracker", level="WARNING") as cm:
                import time as _t
                t0 = _t.monotonic()
                out, n = _run(prior)
                took = _t.monotonic() - t0
        finally:
            release.set()
        self.assertEqual((out, n), (prior, None))
        self.assertLess(took, 0.2 * 1.9)        # one budget, not one budget per page
        self.assertIn("orders read failed", cm.records[-1].getMessage())

    def test_orders_read_failure_changes_nothing(self):
        _Broker(self, [[], []], orders=RuntimeError("429"))
        prior = {"PLTR": [_lot(2, 150.0, "long")]}
        with self.assertLogs("execution.portfolio_tracker", level="WARNING") as cm:
            out, n = _run(prior)
        self.assertEqual((out, n), (prior, None))
        self.assertEqual(len(cm.records), 1)
        self.assertIn("orders read failed", cm.records[0].getMessage())

    def test_no_orders_read_when_nothing_to_change(self):
        b = _Broker(self, [[_pos("NVDA", "4")], [_pos("NVDA", "4")]])
        out, n = _run({"NVDA": [_lot(4, 100.0, "long")]})
        self.assertEqual(n, 0)
        b.client.get_orders.assert_not_called()

    def test_qhm_lookup_failure_changes_nothing(self):
        _Broker(self, [[], []])
        with mock.patch("execution.quarterly_hold_manager.get_quarterly_hold_symbols",
                        side_effect=RuntimeError("state unreadable")):
            prior = {"PLTR": [_lot(2, 150.0, "long")]}
            out, n = _run(prior)
        self.assertEqual((out, n), (prior, None))


class TestFirstPassGateAndState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "open_lots_prior_day.json"
        p = mock.patch("execution.fifo_pnl._LOTS_STATE_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, data):
        self.path.write_text(json.dumps(data))

    def test_missing_file_skips_check(self):
        self.assertFalse(pt._carry_is_first_pass(TODAY))

    def test_missing_or_unparseable_date_skips_check(self):
        for d in ({"open_lots": {}}, {"date": "", "open_lots": {}}, {"date": "11/15/2026", "open_lots": {}}):
            with self.subTest(d=d):
                self._write(d)
                self.assertFalse(pt._carry_is_first_pass(TODAY))

    def test_same_day_second_pass_does_not_run_check(self):
        self._write({"date": TODAY, "open_lots": {}})
        self.assertFalse(pt._carry_is_first_pass(TODAY))

    def test_prior_day_file_runs_check(self):
        self._write({"date": "2026-11-13", "open_lots": {}})
        self.assertTrue(pt._carry_is_first_pass(TODAY))

    def test_changed_count_written_and_kept_same_day(self):
        fifo_pnl._save_open_lots_state({}, TODAY, set(), carry_changed=29)
        self.assertEqual(json.loads(self.path.read_text())["carry_reconciled_symbols"], 29)
        fifo_pnl._save_open_lots_state({}, TODAY, set())            # a later same-day pass: count kept
        self.assertEqual(json.loads(self.path.read_text())["carry_reconciled_symbols"], 29)
        fifo_pnl._save_open_lots_state({}, "2026-11-17", set())     # a new day without a check: no count
        self.assertNotIn("carry_reconciled_symbols", json.loads(self.path.read_text()))


class TestPltrReplay(unittest.TestCase):
    """The real 11-16 PLTR Day-tier fills, with the 2-share phantom PLTR long carried into the day."""

    FILLS = [
        {"id": "20261116152633372::285daf2d-7602-4018-9288-f36c6c3855b2",
         "order_id": "1a9e5b9d-028f-4c6a-a8bf-76245ab65195", "symbol": "PLTR", "side": "sell_short",
         "qty": "6", "price": "159.44", "transaction_time": "2026-11-16T20:26:33.37228Z"},
        {"id": "20261116152634400::b87f9930-7f85-457c-9925-cbeeb1fb171b",
         "order_id": "1a9e5b9d-028f-4c6a-a8bf-76245ab65195", "symbol": "PLTR", "side": "sell_short",
         "qty": "2", "price": "159.44", "transaction_time": "2026-11-16T20:26:34.400596Z"},
        {"id": "20261116155810583::f51331f1-8edb-4fb1-a716-7c2c3e857635",
         "order_id": "971153dc-c3ef-4e25-8314-7c227aa53a74", "symbol": "PLTR", "side": "buy",
         "qty": "8", "price": "160.04", "transaction_time": "2026-11-16T20:58:10.583993Z"},
    ]

    def test_without_check_the_phantom_long_corrupts_the_trade(self):
        with mock.patch("execution.quarterly_hold_manager.get_quarterly_hold_symbols", return_value=[]), \
                self.assertLogs("execution.fifo_pnl", level="CRITICAL") as cm:
            fifo_pnl._fifo_reconstruct(self.FILLS, {"PLTR": [_lot(2, 150.0, "long")]}, set())
        self.assertIn("buy_to_cover with no open short lots (net_qty=-6, qty=8", cm.records[0].getMessage())

    def test_morning_check_then_day_fills_give_one_8_share_short(self):
        # First pass at the 11-16 open: Alpaca holds no PLTR; no PLTR fill or order yet today.
        _Broker(self, [[_pos("NVDA", "4")], [_pos("NVDA", "4")]], orders=[])
        prior = {"PLTR": [_lot(2, 150.0, "long")], "NVDA": [_lot(4, 100.0, "long")]}
        lots, n = _run(prior, day_fills=[])
        self.assertEqual(n, 1)
        self.assertNotIn("PLTR", lots)
        # Later passes fold in the day's fills.
        with self.assertNoLogs("execution.fifo_pnl", level="CRITICAL"):
            pnl, remaining, per_trade, seen = fifo_pnl._fifo_reconstruct(self.FILLS, lots, set())
        self.assertEqual(pnl, -4.80)
        self.assertNotIn("PLTR", remaining)
        self.assertEqual(remaining, {"NVDA": [_lot(4, 100.0, "long")]})
        # One 8-share short trade: the FIFO records it per opening lot (6 + 2), all 159.44 -> 160.04.
        self.assertEqual(sum(t["qty"] for t in per_trade), 8)
        self.assertTrue(all(t["side"] == "short" and t["entry"] == 159.44 and t["exit"] == 160.04
                            for t in per_trade))
        self.assertAlmostEqual(sum(t["pnl"] for t in per_trade), -4.80, places=6)
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
