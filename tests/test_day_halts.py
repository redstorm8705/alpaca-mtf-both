#!/usr/bin/env python3
# ruff: noqa: E501
"""HALT-1019 (owner approval 2026-10-19): every Day-tier halt lasts only while its cause lasts.
Design record: logs/design_records/day_halt_audit_2026-10-19.md. No test reaches the broker: every broker call is mocked."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from execution import day_trade_manager as dtm
from strategy import day_tier_logger

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 20, 11, 0, tzinfo=ET)
TODAY = "20261020"


class _State:
    """In-memory state file for dtm._load_state/_save_state."""

    def __init__(self, d=None):
        self.d = d or {}

    def load(self):
        return json.loads(json.dumps(self.d))

    def save(self, s):
        self.d = json.loads(json.dumps(s))
        return True


def _patch_state(st):
    return [mock.patch.object(dtm, "_load_state", side_effect=st.load),
            mock.patch.object(dtm, "_save_state", side_effect=st.save),
            mock.patch.object(dtm, "_now_et", return_value=NOW),
            mock.patch.object(dtm, "_page")]


class UnresolvedMarkers(unittest.TestCase):
    def _run(self, st, fn):
        import contextlib
        with contextlib.ExitStack() as es:
            for p in _patch_state(st):
                es.enter_context(p)
            es.enter_context(mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}))
            return fn()

    def test_marker_is_per_trade_and_never_latches_the_day(self):
        st = _State()
        self._run(st, lambda: dtm._halt_unresolved_exit("AAPL", "test", trade_id="DT-1"))
        self.assertNotIn(dtm._HALT_KEY, st.d)
        self.assertEqual(st.d["_unresolved::AAPL::DT-1"]["trade_ids"], ["DT-1"])
        self.assertFalse(dtm._tier_killed_today(st.d))

    def test_clears_only_when_the_exit_is_booked(self):
        st = _State({"_unresolved::AAPL::DT-1": {"date": TODAY, "symbol": "AAPL", "trade_ids": ["DT-1"]}})
        with mock.patch.object(dtm, "_booked_closed", return_value=False):
            self._run(st, dtm._clear_resolved_markers)
        self.assertIn("_unresolved::AAPL::DT-1", st.d)
        with mock.patch.object(dtm, "_booked_closed", return_value=True):
            self._run(st, dtm._clear_resolved_markers)
        self.assertNotIn("_unresolved::AAPL::DT-1", st.d)

    def test_earlier_day_marker_dropped(self):
        st = _State({"_unresolved::AAPL::DT-1": {"date": "20261019", "symbol": "AAPL", "trade_ids": ["DT-1"]}})
        with mock.patch.object(dtm, "_booked_closed", return_value=False):
            self._run(st, dtm._clear_resolved_markers)
        self.assertEqual(st.d, {})

    def test_worst_case_charge_in_daily_budget(self):
        st = {"_unresolved::AAPL::DT-1": {"date": TODAY, "symbol": "AAPL", "trade_ids": ["DT-1"]}}
        ot = {"DT-1": {"trade_id": "DT-1", "symbol": "AAPL", "fill_qty": 10, "entry_price": 100.0}}
        jr = [{"event": "stop_placed", "trade_id": "DT-1", "stop_price": 99.0},
              {"event": "entry_fill", "trade_id": "DT-1"}]
        with mock.patch.object(dtm, "_realized_loss_today", return_value=(0.0, True)), \
                mock.patch.object(dtm, "_now_et", return_value=NOW), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(jr, True)), \
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value=ot):
            _rl, open_risk, _d = dtm._daily_risk_used(ot, st, 48.0)
        self.assertAlmostEqual(open_risk, 1000.0)            # full notional, not the $10 stop risk

    def test_unbounded_marker_blocks_budget(self):
        st = {"_unresolved::AAPL::unknown": {"date": TODAY, "symbol": "AAPL", "trade_ids": []}}
        with mock.patch.object(dtm, "_realized_loss_today", return_value=(0.0, True)), \
                mock.patch.object(dtm, "_now_et", return_value=NOW), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)):
            _rl, open_risk, _d = dtm._daily_risk_used({}, st, 48.0)
        self.assertEqual(open_risk, float("inf"))


class KillCheckNoLatch(unittest.TestCase):
    def _kill(self, st, **patches):
        import contextlib
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(dtm, "_enabled", return_value=True))
            for p in _patch_state(st):
                es.enter_context(p)
            es.enter_context(mock.patch.object(dtm, "_page_once_today"))
            es.enter_context(mock.patch.object(dtm, "force_flat_all", return_value=0))
            for name, val in patches.items():
                es.enter_context(mock.patch.object(dtm, name, **val))
            return dtm.tier_kill_check(2500.0, day_start_equity=2500.0)

    def test_unreadable_journal_blocks_tick_without_latch(self):
        st = _State()
        res = self._kill(st, _realized_loss_today={"return_value": (0.0, False)},
                         _flatten_targets={"return_value": {}})
        self.assertTrue(res)
        self.assertNotIn(dtm._HALT_KEY, st.d)

    def test_unreadable_baseline_blocks_tick_without_latch(self):
        st = _State()
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_load_state", side_effect=st.load), \
                mock.patch.object(dtm, "_save_state", side_effect=st.save), \
                mock.patch.object(dtm, "_now_et", return_value=NOW), \
                mock.patch.object(dtm, "_page_once_today"):
            self.assertTrue(dtm.tier_kill_check(2500.0, day_start_equity=float("nan")))
        self.assertNotIn(dtm._HALT_KEY, st.d)

    def test_error_blocks_tick(self):
        st = _State()
        res = self._kill(st, _realized_loss_today={"side_effect": RuntimeError("429")})
        self.assertTrue(res)
        self.assertNotIn(dtm._HALT_KEY, st.d)

    def test_clean_day_does_not_block(self):
        st = _State()
        res = self._kill(st, _realized_loss_today={"return_value": (0.0, True)},
                         _flatten_targets={"return_value": {}})
        self.assertFalse(res)

    def test_unresolved_worst_case_blocks_entries_not_flatten(self):
        st = _State({"_unresolved::AAPL::DT-1": {"date": TODAY, "symbol": "AAPL", "trade_ids": ["DT-1"]}})
        res = self._kill(st, _realized_loss_today={"return_value": (0.0, True)},
                         _flatten_targets={"return_value": {}},
                         _lot_notional={"return_value": 1000.0})     # > 5% of 2500 = 125
        self.assertTrue(res)
        self.assertNotIn(dtm._KILL_KEY, st.d)                         # no kill latch, no flatten


class JournalTodayCheck(unittest.TestCase):
    def _read(self, lines):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "day_tier_events.jsonl"
            p.write_text("\n".join(lines) + "\n")
            with mock.patch.object(day_tier_logger, "_JSONL", p):
                return day_tier_logger.read_events_today_checked(NOW.date())

    def test_old_torn_line_ignored(self):
        ev, ok, old = self._read(['{"event": "entry_fill", "ts": "2026-10-01T07:00:00-07:00"',
                                  '{"event": "exit_fill", "ts": "2026-10-02T07:00:00-07:00"}'])
        self.assertTrue(ok)
        self.assertEqual(old, 1)
        self.assertEqual(len(ev), 1)

    def test_torn_line_at_end_counts_against_today(self):
        _ev, ok, _old = self._read(['{"event": "exit_fill", "ts": "2026-10-02T07:00:00-07:00"}',
                                    '{"event": "exit_fill", "ts": "2026-10-20T07:'])
        self.assertFalse(ok)

    def test_torn_line_dated_yesterday_ignored_even_before_today_record(self):
        ev, ok, old = self._read(['{"ts": "2026-10-19T12:59:00-07:00", "event": "price_sample"}',
                                  '{"ts": "2026-10-19T13:00:01-07:00", "event": "exit_fi',
                                  '{"ts": "2026-10-20T06:31:00-07:00", "event": "decision"}'])
        self.assertTrue(ok)
        self.assertEqual(old, 1)
        self.assertEqual(len(ev), 2)

    def test_torn_non_exit_line_today_ignored(self):
        _ev, ok, old = self._read(['{"ts": "2026-10-20T07:00:00-07:00", "event": "price_sample", "seq'])
        self.assertTrue(ok)
        self.assertEqual(old, 1)

    def test_torn_exit_line_today_counts_against_today(self):
        _ev, ok, _old = self._read(['{"ts": "2026-10-20T07:00:00-07:00", "event": "exit_fill", "realiz'])
        self.assertFalse(ok)

    def test_torn_line_followed_by_today_counts_against_today(self):
        _ev, ok, _old = self._read(['{"event": "exit_fill", "realized_pnl": -5',
                                    '{"event": "entry_fill", "ts": "2026-10-20T07:00:00-07:00"}'])
        self.assertFalse(ok)


class ScopedStrictReader(unittest.TestCase):
    """HALT-1019 cold review r3: the per-trade and budget readers skip a torn line that cannot be the trade's."""
    ENTRY = json.dumps({"ts": "2026-10-20T07:00:00-07:00", "event": "entry_fill", "trade_id": "DT-5",
                        "symbol": "AAPL", "fill_qty": 10, "fill_price": 100.0})

    def _read(self, lines, **kw):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "day_tier_events.jsonl"
            p.write_text("\n".join(lines) + "\n")
            with mock.patch.object(day_tier_logger, "_JSONL", p):
                return day_tier_logger.read_events_checked(**kw)

    def test_strict_default_unchanged(self):
        _ev, ok = self._read(['{"ts": "2026-10-01T07:00:00-07:00", "event": "exit_fi', self.ENTRY])
        self.assertFalse(ok)

    def test_trade_scope_skips_line_before_entry(self):
        ev, ok = self._read(['{"ts": "2026-10-01T07:00:00-07:00", "event": "exit_fi', self.ENTRY],
                            trade_id="DT-5", scope="trade")
        self.assertTrue(ok)
        self.assertEqual(len(ev), 1)

    def test_trade_scope_keeps_same_day_fill_line_strict(self):
        _ev, ok = self._read([self.ENTRY, '{"ts": "2026-10-20T08:00:00-07:00", "event": "exit_fill", "trade_id": "DT'],
                             trade_id="DT-5", scope="trade")
        self.assertFalse(ok)

    def test_harmless_event_skipped_any_scope(self):
        _ev, ok = self._read([self.ENTRY, '{"ts": "2026-10-20T08:00:00-07:00", "event": "price_sample", "tr'],
                             trade_id="DT-5", scope="trade")
        self.assertTrue(ok)

    def test_trade_scope_without_entry_is_strict(self):
        _ev, ok = self._read(['{"ts": "2026-10-01T07:00:00-07:00", "event": "exit_fi'], trade_id="DT-9", scope="trade")
        self.assertFalse(ok)

    def test_glued_record_never_skipped(self):
        _ev, ok = self._read(['{"ts": "2026-10-01T07:00:00-07:00", "event": "deci{"ts": "2026-10-20T08:00:00-07:00", '
                              '"event": "exit_fill"}', self.ENTRY], trade_id="DT-5", scope="trade")
        self.assertFalse(ok)

    def test_today_scope_glued_record_counts_against_today(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "day_tier_events.jsonl"
            p.write_text('{"ts": "2026-10-19T13:00:00-07:00", "event": "deci{"ts": "2026-10-20T08:00:00-07:00", '
                         '"event": "exit_fill", "realized_pnl": -50}\n')
            with mock.patch.object(day_tier_logger, "_JSONL", p):
                _ev, ok, _old = day_tier_logger.read_events_today_checked(NOW.date())
        self.assertFalse(ok)


class ConvSizingReaderStaysStrict(unittest.TestCase):
    def test_conv_entry_reads_the_journal_strictly(self):
        # A torn old line must send CONV-1018 back to today's sizing, never to a band record missing a loss.
        calls = []

        def _rec(trade_id=None, scope=None):
            calls.append(scope)
            return [], False
        with mock.patch("strategy.day_tier_logger.read_events_checked", side_effect=_rec):
            plan, sized = dtm._conv_entry("AAPL", "long", {"conviction": 0.6}, {}, 100.0, 99.0, 100.0, 2500.0, 2500.0,
                                          {}, {}, {}, 5000.0, 0.0, 0.25, [])
        self.assertEqual((plan, sized), (None, None))
        self.assertEqual(calls, [None])


class ExternalClose(unittest.TestCase):
    def _book(self, orders, flat=True, legs_clear=True, journal_extra=()):
        tgt = {"trade_id": "DT-1", "symbol": "AAPL", "side": "long", "qty": 10, "entry_price": 100.0}
        journal = [{"event": "entry_fill", "trade_id": "DT-1", "ts": "2026-10-20T07:00:00-07:00", "order_id": "E1"},
                   *journal_extra]
        exits = []
        with mock.patch("execution.broker.get_open_position", return_value=None if flat else object()), \
                mock.patch.object(dtm, "_cancel_recorded_exit_legs_confirmed", return_value=legs_clear), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False), \
                mock.patch("strategy.day_tier_logger.read_events", return_value=journal), \
                mock.patch.object(dtm, "_closed_orders_since", return_value=orders), \
                mock.patch("strategy.day_tier_logger.log_exit_fill",
                           side_effect=lambda *a, **k: exits.append(k) or True), \
                mock.patch("trade_logger.log_event", return_value=True):
            return dtm._book_external_close(tgt), exits

    @staticmethod
    def _o(oid, side, qty, px, coid="owner-uuid"):
        return SimpleNamespace(id=oid, side=side, filled_qty=str(qty), filled_avg_price=str(px), client_order_id=coid)

    def test_books_hand_close_at_worst_prices(self):
        ok, exits = self._book([self._o("O1", "sell", 5, 97.0), self._o("O2", "sell", 10, 99.0)])
        self.assertTrue(ok)
        self.assertEqual(exits[0]["fill_qty"], 10.0)
        self.assertAlmostEqual(exits[0]["fill_price"], 98.0)      # 5 @ 97 then 5 @ 99 (worst first)
        self.assertAlmostEqual(exits[0]["realized_pnl"], -20.0)

    def test_excludes_journaled_orders(self):
        ok, _ex = self._book([self._o("TP1", "sell", 5, 104.0), self._o("O3", "sell", 5, 97.0)],
                             journal_extra=[{"event": "partial_exit_fill", "trade_id": "DT-1", "order_id": "TP1"}])
        self.assertFalse(ok)                                       # only 5 eligible shares for a 10-share lot

    def test_other_tier_close_after_entry_never_books(self):
        # Another tier sold this symbol after entry; an untagged co-held leg must not be booked as the Day exit.
        ok, exits = self._book([self._o("SW1", "sell", 10, 90.0, coid="IN-AAPL-s-1-x"),
                                self._o("LEG", "sell", 10, 105.0)])
        self.assertFalse(ok)
        self.assertEqual(exits, [])

    def test_not_flat_or_leg_live_never_books(self):
        self.assertFalse(self._book([self._o("O1", "sell", 10, 99.0)], flat=False)[0])
        self.assertFalse(self._book([self._o("O1", "sell", 10, 99.0)], legs_clear=False)[0])

    def test_unreadable_orders_never_books(self):
        self.assertFalse(self._book(None)[0])

    def test_unbooked_day_leg_is_not_excluded(self):
        # A stop_placed row is a record, not a booked fill: the Day leg's own fill counts at its own price.
        ok, exits = self._book([self._o("S1", "sell", 10, 90.0)],
                               journal_extra=[{"event": "stop_placed", "trade_id": "DT-1", "order_id": "S1"}])
        self.assertTrue(ok)
        self.assertAlmostEqual(exits[0]["realized_pnl"], -100.0)


class TornLineClearsMarker(unittest.TestCase):
    def test_old_torn_line_plus_booked_exit_clears(self):
        lines = ['{"event": "entry_fill", "ts": "2026-10-01T07:00:00-07:00"',
                 json.dumps({"event": "entry_fill", "trade_id": "DT-1", "symbol": "AAPL", "side": "long",
                             "fill_qty": 10, "fill_price": 100.0, "ts": "2026-10-20T07:00:00-07:00"}),
                 json.dumps({"event": "exit_fill", "trade_id": "DT-1", "symbol": "AAPL", "fill_qty": 10,
                             "fill_price": 99.0, "realized_pnl": -10.0, "ts": "2026-10-20T08:00:00-07:00"})]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "day_tier_events.jsonl"
            p.write_text("\n".join(lines) + "\n")
            with mock.patch.object(day_tier_logger, "_JSONL", p):
                self.assertTrue(dtm._booked_closed("DT-1"))
                self.assertFalse(dtm._booked_closed("DT-2"))


class ShortWorstCase(unittest.TestCase):
    def _n(self, side):
        ot = {"DT-1": {"trade_id": "DT-1", "symbol": "TSLA", "side": side, "fill_qty": 10, "entry_price": 100.0}}
        with mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value=ot):
            return dtm._lot_notional("DT-1", {})

    def test_unresolved_short_is_unbounded(self):
        # It may already be covered at an unknown price: today's live price is not a bound.
        self.assertEqual(self._n("short"), float("inf"))

    def test_long_is_notional(self):
        self.assertAlmostEqual(self._n("long"), 1000.0)

    def test_state_only_short_is_unbounded(self):
        st = {"entry::TSLA::b": {"coid": "DT-1", "side": "short", "fill_px": 100.0, "fill_qty": 10}}
        with mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}):
            self.assertEqual(dtm._lot_notional("DT-1", st), float("inf"))


if __name__ == "__main__":
    unittest.main()
