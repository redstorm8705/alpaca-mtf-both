#!/usr/bin/env python3
# ruff: noqa: E501
"""QHM earnings dates (design record logs/design_records/qhm_earnings_dates_fix_2026-10-12.md; board Thorp + McKinney,
GAI): three states (DATED / NONE_IN_WINDOW / UNKNOWN), a per-symbol scan window, a locked per-symbol merge write, a 24 h
per-symbol cache including negatives, HTTP 402 -> UNKNOWN; QHM uses FMP, else a today-or-later config date, else
UNKNOWN; a config-only date never cancels a stop; the pre-earnings stop cancel is off unless the flag is True.
Run: PYTHONPATH=. python3 -m pytest -q tests/test_qhm_earnings_dates.py
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import config
from data import fmp_client as fc
from execution import quarterly_hold_manager as qhm

TODAY = date.today()


def _resp(status, payload):
    return SimpleNamespace(status_code=status, json=lambda: payload)


class _TmpCache(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        d = Path(self._d.name)
        for name, val in (("_CACHE_DIR", d), ("_EARNINGS_CACHE", d / "earnings_week.json"),
                          ("_EARNINGS_SYMBOL_CACHE", d / "earnings_symbol_cache.json"),
                          ("_EARNINGS_LOCK", d / ".lock")):
            p = mock.patch.object(fc, name, val)
            p.start()
            self.addCleanup(p.stop)
        k = mock.patch.object(fc, "_key", return_value="k")
        k.start()
        self.addCleanup(k.stop)

    def _preload(self, symbols, rows, lookahead=35):
        with mock.patch.object(fc.requests, "get", return_value=_resp(200, rows)):
            return fc.preload_earnings_week(symbols, lookahead_days=lookahead)


class EarningsStates(_TmpCache):
    def test_scanned_empty_is_none_and_unscanned_looks_up(self):
        ge = (TODAY + timedelta(days=8)).isoformat()
        self._preload(["AAPL", "GE"], [{"symbol": "GE", "date": ge}])
        with mock.patch.object(fc, "_fetch_symbol_earnings", return_value=(200, [])) as f:
            self.assertEqual(fc.get_earnings_info("AAPL")["state"], fc.EARNINGS_NONE_IN_WINDOW)
            self.assertEqual(fc.get_earnings_info("GE")["state"], fc.EARNINGS_DATED)
            f.assert_not_called()                                       # scanned symbols: no network
            fc.get_earnings_info("LLY")                                 # never scanned -> per-symbol lookup
            f.assert_called_once_with("LLY")

    def test_short_scan_is_unknown_for_a_longer_horizon(self):
        self._preload(["GE"], [], lookahead=10)
        with mock.patch.object(fc, "_fetch_symbol_earnings", return_value=(200, [])) as f:
            self.assertEqual(fc.get_earnings_info("GE", min_horizon_days=5)["source"], "fmp_batch")
            fc.get_earnings_info("GE", min_horizon_days=21)
            f.assert_called_once()                                      # 10-day scan cannot answer 21 days

    def test_second_writer_merges_not_narrows(self):
        d1 = (TODAY + timedelta(days=9)).isoformat()
        self._preload(["GE", "NFLX"], [{"symbol": "GE", "date": d1}], lookahead=35)
        self._preload(["AAPL"], [], lookahead=10)                       # preflight-style narrower scan
        syms = json.loads(fc._EARNINGS_CACHE.read_text())["symbols"]
        self.assertEqual(set(syms), {"GE", "NFLX", "AAPL"})
        self.assertEqual(syms["GE"]["dates"], [d1])
        self.assertEqual(fc.get_cached_earnings_dates("GE"), [date.fromisoformat(d1)])

    def test_plan_blocked_symbol_is_unknown_and_cached(self):
        with mock.patch.object(fc.requests, "get", return_value=_resp(402, {})) as g, \
                self.assertLogs(fc.logger, "WARNING") as cm:
            a = fc.get_earnings_info("LLY")
            b = fc.get_earnings_info("LLY")
        self.assertEqual((a["state"], b["state"]), (fc.EARNINGS_UNKNOWN, fc.EARNINGS_UNKNOWN))
        self.assertEqual(g.call_count, 1)                               # <= 1 live call per symbol per 24 h
        self.assertEqual(sum("plan restriction" in m for m in cm.output), 1)
        self.assertEqual(fc.get_cached_earnings_dates("LLY"), [])      # legacy list contract

    def test_negative_result_is_cached(self):
        with mock.patch.object(fc, "_fetch_symbol_earnings", return_value=(200, [])) as f:
            fc.get_earnings_info("GEV")
            fc.get_earnings_info("GEV")
        f.assert_called_once()

    def test_write_is_atomic_json(self):
        self._preload(["GE"], [])
        self.assertIsInstance(json.loads(fc._EARNINGS_CACHE.read_text()), dict)
        self.assertEqual(list(Path(self._d.name).glob("*.tmp")), [])


class QhmNextEarnings(unittest.TestCase):
    def _run(self, info, cfg_date, sym_info=None):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "cfg.json"
            cfgp.write_text(json.dumps({"picks": {"LLY": {"earnings_exit_before": cfg_date}}}))
            alert = mock.Mock()
            me = SimpleNamespace(_now_et=lambda: datetime.now(ZoneInfo("America/New_York")), _alert=alert)
            qhm._EARNINGS_SRC_LOGGED.clear()
            with mock.patch.object(qhm, "_CONFIG_PATH", cfgp), \
                    mock.patch("data.fmp_client.get_earnings_info", return_value=info), \
                    mock.patch("data.fmp_client.get_symbol_earnings_info",
                               return_value=sym_info if sym_info is not None else info) as self.sym_call:
                out = qhm.QuarterlyHoldManager._next_earnings(me, "LLY")
        return out, alert

    def test_fmp_date_wins(self):
        d = TODAY + timedelta(days=4)
        out, _ = self._run({"state": "DATED", "dates": [d]}, (TODAY + timedelta(days=9)).isoformat())
        self.assertEqual(out, (d, "fmp"))

    def test_future_config_date_when_fmp_unknown(self):
        cd = TODAY + timedelta(days=9)
        out, alert = self._run({"state": "UNKNOWN", "dates": []}, cd.isoformat())
        self.assertEqual(out, (cd, "config"))
        alert.assert_not_called()

    def test_past_config_date_is_unknown_and_pages(self):
        out, alert = self._run({"state": "UNKNOWN", "dates": []}, (TODAY - timedelta(days=30)).isoformat())
        self.assertEqual(out, (None, "unknown"))
        alert.assert_called_once()


    def test_affirmative_none_does_not_page(self):
        out, alert = self._run({"state": "NONE_IN_WINDOW", "dates": []}, (TODAY - timedelta(days=2)).isoformat())
        self.assertEqual(out, (None, "none_in_window"))
        alert.assert_not_called()

    def test_batch_none_asks_per_symbol_and_plan_block_uses_config(self):
        # LLY: absent from the sparse batch calendar, per-symbol 402 -> config date
        cd = TODAY + timedelta(days=17)
        out, _ = self._run({"state": "NONE_IN_WINDOW", "dates": [], "source": "fmp_batch"}, cd.isoformat(),
                           sym_info={"state": "UNKNOWN", "dates": []})
        self.assertEqual(out, (cd, "config"))

    def test_report_already_happened_beats_a_stale_config_date(self):
        # GE on the day after its report: per-symbol shows the past report and the next one months out ->
        # the config date (today) is NOT used.
        nxt = TODAY + timedelta(days=95)
        out, _ = self._run({"state": "NONE_IN_WINDOW", "dates": [], "source": "fmp_batch"}, TODAY.isoformat(),
                           sym_info={"state": "DATED", "dates": [nxt, TODAY - timedelta(days=1)]})
        self.assertEqual(out, (nxt, "fmp"))

    def test_batch_with_only_a_past_date_asks_per_symbol(self):
        nxt = TODAY + timedelta(days=90)
        out, alert = self._run({"state": "DATED", "dates": [TODAY - timedelta(days=1)], "source": "fmp_batch"},
                               (TODAY - timedelta(days=1)).isoformat(), sym_info={"state": "DATED", "dates": [nxt]})
        self.assertEqual(out, (nxt, "fmp"))
        alert.assert_not_called()

    def test_per_symbol_none_does_not_fall_back_to_config(self):
        out, _ = self._run({"state": "NONE_IN_WINDOW", "dates": [], "source": "fmp_batch"},
                           (TODAY + timedelta(days=3)).isoformat(),
                           sym_info={"state": "NONE_IN_WINDOW", "dates": [TODAY - timedelta(days=1)]})
        self.assertEqual(out, (None, "none_in_window"))


class QhmTrimWindowOneShare(unittest.TestCase):
    """A 1-share hold that has not trimmed reports the trim window closed (Tier 1 trims qty-1 = 0, so the trailing lock
    must keep running); a hold already trimmed to its last share keeps its pending Tier 2 (window active)."""

    def _run(self, trim_state, live_price=None):
        gate = TODAY + timedelta(days=5)
        pos = SimpleNamespace(state=qhm.HoldState.ACTIVE, qty_filled=1, avg_entry_price=300.0, direction="long",
                              symbol="GE")
        me = SimpleNamespace(dry_run=False, _next_earnings=lambda s: (gate, "fmp"),
                             _now_et=lambda: datetime.now(ZoneInfo("America/New_York")),
                             _load_earnings_trim_state=lambda: {"GE": {"gate_date": gate.isoformat(), **trim_state}},
                             _save_earnings_trim_state=lambda st: True, _alert=mock.Mock(),
                             _get_live_price=lambda s: live_price,
                             _execute_earnings_trim_tier2=mock.Mock())
        with mock.patch("execution.broker.is_market_open", return_value=True):
            return qhm.QuarterlyHoldManager._maybe_earnings_trim(me, pos, TODAY.isoformat()), me

    def test_untrimmed_one_share_hold_reports_the_window_closed(self):
        out, _ = self._run({"tier1": "none", "tier2": "none"})
        self.assertFalse(out)

    def test_last_share_after_tier1_keeps_tier2(self):
        out, me = self._run({"tier1": "done", "tier2": "none", "t1_fired_pct": 0.05}, live_price=340.0)
        self.assertTrue(out)                                   # window active: trailing lock stands down
        me._execute_earnings_trim_tier2.assert_called_once()   # +13% >= 2 x 5% -> tier 2 exits the last share


class MergeRestamp(_TmpCache):
    def test_narrow_rescan_restamps_and_keeps_the_wide_dates(self):
        far = (TODAY + timedelta(days=30)).isoformat()
        self._preload(["GE"], [{"symbol": "GE", "date": far}], lookahead=35)
        cache = json.loads(fc._EARNINGS_CACHE.read_text())
        cache["symbols"]["GE"]["fetched_at_utc"] = (datetime.now().astimezone() - timedelta(hours=11)).isoformat()
        fc._EARNINGS_CACHE.write_text(json.dumps(cache))
        self._preload(["GE"], [], lookahead=10)
        e = json.loads(fc._EARNINGS_CACHE.read_text())["symbols"]["GE"]
        self.assertEqual((e["dates"], e["scanned_to"]), ([far], (TODAY + timedelta(days=35)).isoformat()))
        self.assertTrue(fc._fresh(e["fetched_at_utc"], 1))                 # re-stamped now


class SymbolTimeoutRetry(_TmpCache):
    def test_timeout_retries_after_an_hour_not_a_day(self):
        with mock.patch.object(fc, "_fetch_symbol_earnings", return_value=(0, [])) as f:
            fc.get_earnings_info("GEV")
            cache = json.loads(fc._EARNINGS_SYMBOL_CACHE.read_text())
            cache["GEV"]["fetched_at_utc"] = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
            fc._EARNINGS_SYMBOL_CACHE.write_text(json.dumps(cache))
            fc.get_earnings_info("GEV")
        self.assertEqual(f.call_count, 2)


class QhmStopCancelGate(unittest.TestCase):
    def _run(self, src, flag, days=3):
        pos = SimpleNamespace(state=qhm.HoldState.ACTIVE, symbol="GE", stop_order_id="S1", earnings_gate_date=None,
                              updated_at=None)
        broker = mock.Mock()
        broker.get_position.return_value = object()
        me = SimpleNamespace(_now_et=lambda: datetime.now(ZoneInfo("America/New_York")), dry_run=False, broker=broker,
                             _alert=mock.Mock(), _save_state=mock.Mock(),
                             _next_earnings=lambda s: (TODAY + timedelta(days=days), src))
        qhm._EARNINGS_SRC_LOGGED.clear()
        with mock.patch.object(config, "QHM_EARNINGS_STOP_CANCEL_ENABLED", flag), \
                mock.patch("execution.broker.cancel_order") as cancel:
            qhm.QuarterlyHoldManager._maybe_enter_earnings_hold(me, pos)
        return pos, cancel

    def test_flag_off_keeps_the_stop(self):
        pos, cancel = self._run("fmp", False)
        cancel.assert_not_called()
        self.assertEqual((pos.state, pos.stop_order_id), (qhm.HoldState.ACTIVE, "S1"))

    def test_config_date_never_cancels(self):
        pos, cancel = self._run("config", True)
        cancel.assert_not_called()
        self.assertEqual(pos.state, qhm.HoldState.ACTIVE)

    def test_flag_on_with_fmp_date_cancels_as_before(self):
        pos, cancel = self._run("fmp", True)
        cancel.assert_called_once_with("S1")
        self.assertEqual(pos.state, qhm.HoldState.PENDING_EARNINGS)

    def test_far_earnings_does_nothing(self):
        pos, cancel = self._run("fmp", True, days=12)
        cancel.assert_not_called()

    def test_flag_default_is_off(self):
        self.assertIs(config.QHM_EARNINGS_STOP_CANCEL_ENABLED, False)


if __name__ == "__main__":
    unittest.main()
