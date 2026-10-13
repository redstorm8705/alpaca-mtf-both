#!/usr/bin/env python3
# ruff: noqa: E501
"""Unmanaged-position alarm (design record logs/design_records/unmanaged_position_alarm_2026-10-12.md): page when the
account holds shares no live manager manages; page-only; an unreadable source pages, never reads as zero.
Run: PYTHONPATH=. python3 -m pytest -q tests/test_unmanaged_positions.py
"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from execution import unmanaged_positions as um

ET = ZoneInfo("America/New_York")


def _pos(sym, qty, mv=None):
    return SimpleNamespace(symbol=sym, qty=str(qty), market_value=str(mv if mv is not None else 70.0 * qty))


class Alarm(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = mock.patch.object(um, "_STATE", Path(d.name) / "alarm.json")
        p.start()
        self.addCleanup(p.stop)
        self.pages = []
        pm = mock.patch.dict(um._MEM, clear=True)
        pm.start()
        self.addCleanup(pm.stop)
        p2 = mock.patch.object(um, "_page", side_effect=lambda m: self.pages.append(m))
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch("trade_logger.log_event")
        self.events = p3.start()
        self.addCleanup(p3.stop)

    def _run(self, positions, managed=None, errors=None, tracker=None, now=None, broker_raises=False, per=None):
        broker = mock.patch("execution.broker.get_open_positions",
                            side_effect=RuntimeError("down") if broker_raises else None,
                            return_value=positions)
        mq = mock.patch.object(um, "_managed_qty", return_value=(managed or {}, errors or [], per or {}))
        orders = [mock.patch(f"execution.broker.{n}") for n in ("submit_limit_order", "submit_market_order",
                                                                 "close_position", "submit_bracket_entry")]
        with broker, mq:
            ms = [o.start() for o in orders]
            try:
                out = um.check_unmanaged(tracker or SimpleNamespace(open_trades={}), now_et=now)
            finally:
                for o in orders:
                    o.stop()
        for m in ms:
            m.assert_not_called()                     # page-only: never trades
        return out

    def test_uber_case_pages_once(self):
        out = self._run([_pos("UBER", 72)])
        self.assertEqual(out, {"UBER": 72.0})
        self.assertEqual(self.pages, [])                 # first sighting: wait one pass (fill-to-book races)
        self._run([_pos("UBER", 72)])
        self.assertEqual(len(self.pages), 1)
        self.assertIn("[UBER] 72 share(s)", self.pages[0])
        self._run([_pos("UBER", 72)])
        self.assertEqual(len(self.pages), 1)             # same day: no repeat page
        self.assertEqual(self.events.call_count, 3)      # but an event every pass

    def test_transient_residual_never_pages(self):
        self._run([_pos("NVDA", 3)])                     # Day fill seen before the Day tier booked it
        self._run([_pos("NVDA", 3)], managed={"NVDA": 3.0})
        self.assertEqual(self.pages, [])

    def test_growth_repages_and_next_day_rearms(self):
        self._run([_pos("UBER", 18)])
        self._run([_pos("UBER", 18)])
        self._run([_pos("UBER", 72)])
        self.assertEqual(len(self.pages), 1)            # growth confirmed on the next pass, not this one
        self._run([_pos("UBER", 72)])
        self.assertEqual(len(self.pages), 2)
        nxt = datetime(2030, 1, 2, 10, 0, tzinfo=ET)
        self._run([_pos("UBER", 72)], now=nxt)
        self._run([_pos("UBER", 72)], now=nxt)
        self.assertEqual(len(self.pages), 3)

    def test_cleared_then_reappearing_pages_again(self):
        for positions in ([_pos("MSFT", 10)], [_pos("MSFT", 10)], [], [], [_pos("MSFT", 10)], [_pos("MSFT", 10)]):
            self._run(positions)
        self.assertEqual(len(self.pages), 2)

    def test_kind_flip_and_back_pages_again(self):
        for managed in (None, None, {"UBER": 200.0}, {"UBER": 200.0}, None, None):
            self._run([_pos("UBER", 72)], managed=managed)
        self.assertEqual(len(self.pages), 3)            # long, over, long again

    def test_midnight_keeps_two_pass_memory(self):
        self._run([_pos("UBER", 72)], now=datetime(2030, 1, 2, 23, 45, tzinfo=ET))
        self._run([_pos("UBER", 72)], now=datetime(2030, 1, 3, 0, 15, tzinfo=ET))
        self.assertEqual(len(self.pages), 1)            # no extra overnight delay after the daily reset

    def test_growth_needs_confirmation_too(self):
        self._run([_pos("AAPL", 5)])
        self._run([_pos("AAPL", 5)])
        self.assertEqual(len(self.pages), 1)
        self._run([_pos("AAPL", 15)])                   # Day fill not yet booked
        self._run([_pos("AAPL", 15)], managed={"AAPL": 10.0})
        self.assertEqual(len(self.pages), 1)
        self._run([_pos("AAPL", 12)])
        self._run([_pos("AAPL", 12)])
        self.assertEqual(len(self.pages), 2)            # real growth still pages

    def test_alternating_residual_still_pages(self):
        for i in range(4):
            self._run([_pos("UBER", 72)], managed=None if i % 2 == 0 else {"UBER": 72.0})
        self.assertEqual(len(self.pages), 1)

    def test_two_day_races_on_one_stock_never_page(self):
        self._run([_pos("NVDA", 5)])                                   # lot 1 filled, not booked yet
        self._run([_pos("NVDA", 10)], managed={"NVDA": 5.0})           # lot 1 booked, lot 2 mid-race
        self._run([_pos("NVDA", 10)], managed={"NVDA": 10.0})
        self.assertEqual(self.pages, [])
        self._run([_pos("NVDA", 5)])
        self._run([_pos("NVDA", 5)], managed={"NVDA": 0.0})            # nothing ever booked: real -> pages
        self.assertEqual(len(self.pages), 1)

    def test_unwritable_state_still_pages(self):
        with mock.patch.object(um, "_STATE", Path("/nonexistent-dir/x/alarm.json")):
            self._run([_pos("UBER", 72)])
            self._run([_pos("UBER", 72)])
            self._run([_pos("UBER", 72)])
        self.assertTrue(any("cannot be written" in p for p in self.pages))
        self.assertEqual(sum("[UBER]" in p for p in self.pages), 1)

    def test_flapping_residual_pages_once(self):
        for i in range(6):
            self._run([_pos("UBER", 72)] if i % 2 == 0 else [])
        self.assertEqual(len(self.pages), 1)

    def test_pending_qhm_entry_is_not_told_to_close(self):
        with mock.patch.object(um, "_qhm_pending_entries", return_value={"NVDA"}):
            self._run([_pos("NVDA", 2)])
            self._run([_pos("NVDA", 2)])
        self.assertEqual(len(self.pages), 1)
        self.assertIn("Do NOT close", self.pages[0])
        self.assertNotIn("close them", self.pages[0])

    def test_option_positions_are_ignored(self):
        opt = SimpleNamespace(symbol="NVDA261120C00200000", qty="1", market_value="300", asset_class="us_option")
        self.assertEqual(self._run([opt]), {})

    def test_shrink_then_regrow_pages_again(self):
        for n in (72, 72, 7, 7, 47, 47):
            self._run([_pos("UBER", n)])
        self.assertEqual(len(self.pages), 2)
        self.assertIn("[UBER] 47 share(s)", self.pages[1])

    def test_one_pass_dip_does_not_repage(self):
        for n in (72, 72, 7, 72, 72):
            self._run([_pos("UBER", n)])
        self.assertEqual(len(self.pages), 1)

    def test_other_tier_booking_does_not_repeat_page(self):
        self._run([_pos("UBER", 72)])
        self._run([_pos("UBER", 72)])
        self._run([_pos("UBER", 82)], managed={"UBER": 10.0})
        self._run([_pos("UBER", 82)], managed={"UBER": 10.0})
        self.assertEqual(len(self.pages), 1)

    def test_fully_managed_is_quiet(self):
        out = self._run([_pos("LLY", 2), _pos("EWY", -1)], managed={"LLY": 2.0, "EWY": -1.0})
        self.assertEqual((out, self.pages), ({}, []))

    def test_partial_and_short_residuals_are_signed(self):
        out = self._run([_pos("NVDA", 5), _pos("EWY", -3)], managed={"NVDA": 2.0, "EWY": -1.0})
        self.assertEqual(out, {"NVDA": 3.0, "EWY": -2.0})

    def test_unreadable_source_without_history_pages_with_caveat(self):
        self._run([_pos("UBER", 72)], errors=["qhm:LedgerError"])
        self._run([_pos("UBER", 72)], errors=["qhm:LedgerError"])
        self.assertTrue(any("ownership source unreadable: qhm:LedgerError" in p for p in self.pages))
        uber = [p for p in self.pages if "[UBER]" in p]
        self.assertEqual(len(uber), 1)
        self.assertIn("verify before acting", uber[0])

    def test_unreadable_source_uses_last_good_reading(self):
        # pass 1: QHM readable (LLY 2); pass 2: QHM unreadable -> LLY must NOT page as unmanaged
        self._run([_pos("LLY", 2)], managed={"LLY": 2.0}, per={"qhm": {"LLY": 2.0}})
        self.assertEqual(self.pages, [])
        out = self._run([_pos("LLY", 2)], errors=["qhm:JSONDecodeError"])
        self.assertEqual(out, {})
        self.assertEqual(len(self.pages), 1)
        self.assertIn("last good reading", self.pages[0])
        self._run([_pos("LLY", 2)], errors=["qhm:JSONDecodeError"])
        self.assertEqual(len(self.pages), 1)                     # source page once per day

    def test_day_log_fallback_is_same_day_only(self):
        d1 = datetime(2030, 1, 2, 11, 0, tzinfo=ET)
        d2 = datetime(2030, 1, 3, 11, 0, tzinfo=ET)
        self._run([_pos("NVDA", 3)], managed={"NVDA": 3.0}, per={"day_tier_log": {"NVDA": 3.0}}, now=d1)
        self.assertEqual(self._run([_pos("NVDA", 3)], errors=["day_tier_log:ValueError"], now=d1), {})
        self._run([_pos("NVDA", 3)], errors=["day_tier_log:ValueError"], now=d2)
        out = self._run([_pos("NVDA", 3)], errors=["day_tier_log:ValueError"], now=d2)
        self.assertEqual(out, {"NVDA": 3.0})
        self.assertTrue(any("[NVDA]" in p and "verify before acting" in p for p in self.pages))

    def test_over_claim_is_not_called_short(self):
        self._run([_pos("UBER", 7)], managed={"UBER": 72.0})
        out = self._run([_pos("UBER", 7)], managed={"UBER": 72.0})
        self.assertEqual(out, {"UBER": -65.0})
        self.assertIn("records are ahead", self.pages[0])
        self.assertNotIn("short", self.pages[0])

    def test_broker_unreadable_pages_once(self):
        self._run([], broker_raises=True)
        self._run([], broker_raises=True)
        self.assertEqual(len(self.pages), 1)

    def test_premarket_pass_runs(self):
        out = self._run([_pos("UBER", 72)], now=datetime(2030, 1, 3, 4, 5, tzinfo=ET))
        self.assertIn("UBER", out)

    def test_never_raises(self):
        with mock.patch.object(um, "_load", side_effect=RuntimeError("boom")):
            self.assertEqual(um.check_unmanaged(SimpleNamespace(open_trades={})), {})


class ManagedSources(unittest.TestCase):
    NOW = datetime(2030, 1, 3, 11, 0, tzinfo=ET)

    def _patches(self, day=None, sbm=None, promo=None, qhm=None, led=None):
        return [mock.patch("execution.swing_breakout_manager._load_state", return_value=sbm or {"positions": {}}),
                mock.patch.object(um, "_day_tier_claims", return_value=day or {}),
                mock.patch("execution.day_promotion._load", return_value=promo if promo is not None else {}),
                mock.patch("execution.quarterly_hold_manager.get_quarterly_hold_quantities", return_value=qhm or {}),
                mock.patch("execution.ownership_guard.load_ledger", return_value=led or {"positions": {}})]

    def _managed(self, tracker=None, **kw):
        ps = self._patches(**kw)
        for p in ps:
            p.start()
        try:
            return um._managed_qty(tracker or SimpleNamespace(open_trades={}), self.NOW)
        finally:
            for p in ps:
                p.stop()

    def test_every_manager_counts(self):
        tracker = SimpleNamespace(open_trades={"PLTR": {"qty": 2, "qty_remaining": 1, "direction": "long"},
                                               "EWY": {"qty": 1, "direction": "short"}})
        managed, errors, _per = self._managed(
            tracker, sbm={"positions": {"MSFT": {"status": "open", "qty": 1},
                                        "AMD": {"status": "submit_unknown", "qty": 0, "req_qty": 4}}},
            day={"NVDA": 3.0}, promo={"t1": {"status": "ready", "symbol": "META", "qty": 2},
                                      "t2": {"status": "adopted", "symbol": "META", "qty": 2}},
            qhm={"LLY": 2}, led={"positions": {"TSLA": {"tiers": {"forever6": {"qty": 1}}}}})
        self.assertEqual(errors, [])
        self.assertEqual(managed, {"PLTR": 1.0, "EWY": -1.0, "MSFT": 1.0, "AMD": 4.0, "META": 2.0, "NVDA": 3.0,
                                   "LLY": 2.0, "TSLA": 1.0})

    def test_unreadable_handoff_is_an_error(self):
        ps = self._patches()
        ps[2] = mock.patch("execution.day_promotion._load", return_value=None)
        for p in ps:
            p.start()
        try:
            _m, errors, _p = um._managed_qty(SimpleNamespace(open_trades={}), self.NOW)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(errors, ["promotions:ValueError"])

    def _day(self, trades, complete=True, now=None):
        with mock.patch("strategy.day_tier_logger.open_trades_from_log_checked", return_value=(trades, complete)):
            return um._day_tier_claims(now or self.NOW)

    def test_day_carry_window(self):
        trades = {"a": {"symbol": "NVDA", "entry_ts": "2030-01-02T15:30:00-05:00", "fill_qty": 3, "side": "long"},
                  "b": {"symbol": "AMD", "entry_ts": "2030-01-03T09:50:00-05:00", "fill_qty": 2, "side": "short"}}
        sessions = [date(2030, 1, 2), date(2030, 1, 3)]
        with mock.patch.object(um, "_session_dates", return_value=sessions[:1]):     # before the 01-03 open
            self.assertEqual(self._day({"a": trades["a"]}, now=datetime(2030, 1, 3, 4, 0, tzinfo=ET)), {"NVDA": 3.0})
        with mock.patch.object(um, "_session_dates", return_value=sessions):
            self.assertEqual(self._day(trades, now=datetime(2030, 1, 3, 10, 0, tzinfo=ET)), {"AMD": -2.0})
        with self.assertRaises(ValueError):
            self._day(trades, complete=False)
        with self.assertRaises(ValueError):
            self._day({"x": {"symbol": "NVDA", "entry_ts": "2030-01-03T09:50:00-05:00", "fill_qty": 1, "side": "?"}})

    def test_carry_spans_weekend_and_holiday(self):
        fri = date(2030, 1, 4)
        sat10 = datetime(2030, 1, 5, 10, 0, tzinfo=ET)
        sun12 = datetime(2030, 1, 6, 12, 0, tzinfo=ET)
        mon04 = datetime(2030, 1, 7, 4, 0, tzinfo=ET)
        mon10 = datetime(2030, 1, 7, 10, 0, tzinfo=ET)
        before_mon = [date(2030, 1, 3), fri]
        for now in (sat10, sun12, mon04):
            self.assertTrue(um._carried(fri, now, before_mon))
        self.assertFalse(um._carried(fri, mon10, before_mon + [date(2030, 1, 7)]))     # flattened at Monday's open
        # Monday holiday (no Monday session): still carried Monday 10:00 and Tuesday 04:00, not Tuesday 10:00
        self.assertTrue(um._carried(fri, mon10, before_mon))
        self.assertTrue(um._carried(fri, datetime(2030, 1, 8, 4, 0, tzinfo=ET), before_mon))
        self.assertFalse(um._carried(fri, datetime(2030, 1, 8, 10, 0, tzinfo=ET), before_mon + [date(2030, 1, 8)]))
        # a full session already passed since entry -> not carried
        self.assertFalse(um._carried(date(2030, 1, 2), datetime(2030, 1, 4, 4, 0, tzinfo=ET),
                                     [date(2030, 1, 2), date(2030, 1, 3)]))
        # calendar unreadable -> weekday fallback still spans the weekend
        self.assertTrue(um._carried(fri, sun12, []))
        self.assertFalse(um._carried(fri, mon10, []))

    def test_a_failing_source_is_reported(self):
        ps = self._patches()
        ps[3] = mock.patch("execution.quarterly_hold_manager.get_quarterly_hold_quantities", side_effect=ValueError("x"))
        for p in ps:
            p.start()
        try:
            _m, errors, per = um._managed_qty(SimpleNamespace(open_trades={}), self.NOW)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(errors, ["qhm:ValueError"])
        self.assertNotIn("qhm", per)


if __name__ == "__main__":
    unittest.main()
