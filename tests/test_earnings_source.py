# ruff: noqa: E501
"""EARN-SOURCE-1029: Finnhub-first per-symbol earnings dates with FMP fallback (data/fmp_client.py).

Every HTTP call is mocked; no test reaches Finnhub, FMP or the broker. Design:
logs/design_records/earnings_source_finnhub_2026-10-29.md.
"""
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import data.fmp_client as fc


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


TODAY = date(2026, 10, 29)


def _fh_rows(sym, dates, hour="amc"):
    return {"earningsCalendar": [{"symbol": sym, "date": d.isoformat(), "hour": hour} for d in dates]}


class EarningsSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(fc, "_CACHE_DIR", d),
            mock.patch.object(fc, "_EARNINGS_SYMBOL_CACHE", d / "earnings_symbol_cache.json"),
            mock.patch.object(fc, "_EARNINGS_CACHE", d / "earnings_week.json"),
            mock.patch.object(fc, "_EARNINGS_LOCK", d / ".lock"),
            mock.patch.object(fc, "_et_today", return_value=TODAY),
            mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "k", "FMP_API_KEY": "f"}),
            mock.patch.object(fc, "_in_trading_process", return_value=False),
            mock.patch.object(fc.time, "sleep"),
        ]
        for p in self.patches:
            p.start()
        self.dir = d

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _get(self, finnhub, fmp=None):
        """requests.get mock: finnhub/fmp are callables(sym) -> _Resp."""
        def fake(url, params=None, timeout=None):
            if "finnhub" in url:
                return finnhub(params["symbol"])
            return (fmp or (lambda s: _Resp(402)))(params["symbol"])
        return mock.patch.object(fc.requests, "get", side_effect=fake)

    def test_finnhub_dated_nearest_first(self):
        # Finnhub lists newest first incl. two future quarters (NOW measured 10-29).
        rows = _fh_rows("NOW", [date(2027, 4, 20), date(2027, 1, 26), date(2026, 10, 28)])
        with self._get(lambda s: _Resp(200, rows)):
            info = fc.get_earnings_info("NOW")
        self.assertEqual(info["state"], fc.EARNINGS_DATED)
        self.assertEqual(info["source"], "finnhub_symbol")
        self.assertEqual(info["dates"][0], date(2026, 10, 28))     # nearest first: yesterday's report
        self.assertEqual(fc.get_cached_earnings_dates("NOW")[:2], [date(2026, 10, 28), date(2027, 1, 26)])

    def test_tie_puts_earlier_date_first(self):
        self.assertEqual(fc._nearest_first([TODAY + timedelta(days=1), TODAY - timedelta(days=1)], TODAY),
                         [TODAY - timedelta(days=1), TODAY + timedelta(days=1)])

    def test_empty_finnhub_falls_back_and_unknown_never_none(self):
        with self._get(lambda s: _Resp(200, {"earningsCalendar": []}), lambda s: _Resp(402)):
            info = fc.get_symbol_earnings_info("SNDK")
        self.assertEqual(info["state"], fc.EARNINGS_UNKNOWN)
        e = json.loads((self.dir / "earnings_symbol_cache.json").read_text())["SNDK"]
        self.assertEqual(e["ttl_h"], fc._FAILED_TTL_H)                  # 1 h, not 24 h
        self.assertEqual(e["providers_tried"], ["finnhub", "fmp"])

    def test_fmp_fallback_answer(self):
        fmp_body = [{"date": "2026-11-05"}, {"date": "2026-08-01"}]
        with self._get(lambda s: _Resp(429, headers={"Retry-After": "5"}), lambda s: _Resp(200, fmp_body)):
            info = fc.get_earnings_info("XOM")
        self.assertEqual(info["state"], fc.EARNINGS_DATED)
        self.assertEqual(info["source"], "fmp_symbol")
        e = json.loads((self.dir / "earnings_symbol_cache.json").read_text())["XOM"]
        self.assertEqual(e["ttl_h"], fc._FAILED_TTL_H)                  # Finnhub failed -> 1 h whatever FMP said

    def test_foreign_symbol_rows_dropped_alias_kept(self):
        with self._get(lambda s: _Resp(200, _fh_rows("ZZZ", [TODAY + timedelta(days=3)]))):
            self.assertEqual(fc.get_earnings_info("AAPL")["state"], fc.EARNINGS_UNKNOWN)
        with self._get(lambda s: _Resp(200, _fh_rows("BRK.A", [TODAY + timedelta(days=3)]))):
            self.assertEqual(fc.get_earnings_info("BRK.B")["state"], fc.EARNINGS_DATED)

    def test_v1_cache_entry_is_stale(self):
        (self.dir / "earnings_symbol_cache.json").write_text(json.dumps(
            {"NOW": {"status": 402, "dates": [], "fetched_at_utc": datetime.now(timezone.utc).isoformat()}}))
        with self._get(lambda s: _Resp(200, _fh_rows("NOW", [TODAY + timedelta(days=5)]))):
            self.assertEqual(fc.get_earnings_info("NOW")["state"], fc.EARNINGS_DATED)

    def test_cached_dates_never_call_provider(self):
        with mock.patch.object(fc.requests, "get") as g:
            self.assertEqual(fc.get_cached_earnings_dates("AAPL"), [])
            g.assert_not_called()

    def test_precedence_dated_batch_beats_symbol_unknown(self):
        now = datetime.now(timezone.utc).isoformat()
        (self.dir / "earnings_week.json").write_text(json.dumps({"symbols": {
            "LLY": {"dates": [(TODAY + timedelta(days=4)).isoformat()], "scanned_to": "2026-12-01",
                    "fetched_at_utc": now}}}))
        with self._get(lambda s: _Resp(500)):
            info = fc.get_earnings_info("LLY")
        self.assertEqual(info["state"], fc.EARNINGS_DATED)
        self.assertEqual(info["source"], "fmp_batch")

    def test_batch_none_never_answers(self):
        now = datetime.now(timezone.utc).isoformat()
        (self.dir / "earnings_week.json").write_text(json.dumps({"symbols": {
            "NOW": {"dates": [], "scanned_to": "2026-12-01", "fetched_at_utc": now}}}))
        with self._get(lambda s: _Resp(500)):
            self.assertEqual(fc.get_earnings_info("NOW")["state"], fc.EARNINGS_UNKNOWN)

    def test_none_in_window_only_from_covered_answer(self):
        with self._get(lambda s: _Resp(200, _fh_rows("GE", [TODAY - timedelta(days=9)]))):
            info = fc.get_earnings_info("GE")
        self.assertEqual(info["state"], fc.EARNINGS_NONE_IN_WINDOW)

    def test_trading_process_budget_one_lookup(self):
        rows = _fh_rows("A", [TODAY + timedelta(days=2)])
        with mock.patch.object(fc, "_in_trading_process", return_value=True), \
                mock.patch.object(fc, "_last_live_ts", 0.0), \
                self._get(lambda s: _Resp(200, {"earningsCalendar": [dict(r, symbol=s) for r in
                                                                     rows["earningsCalendar"]]})) as g:
            barrier = threading.Barrier(2)
            out = []

            def call(sym):
                barrier.wait()
                out.append(fc.get_symbol_earnings_info(sym)["state"])
            ts = [threading.Thread(target=call, args=(s,)) for s in ("AAA", "BBB")]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            finnhub_calls = [c for c in g.call_args_list if "finnhub" in c.args[0]]
            self.assertEqual(len(finnhub_calls), 1)
            self.assertEqual(sorted(out), [fc.EARNINGS_DATED, fc.EARNINGS_UNKNOWN])

    def test_warm_429_sequence(self):
        calls = []

        def fh(sym):
            calls.append(sym)
            return _Resp(429, headers={"Retry-After": "7"})
        with self._get(fh, lambda s: _Resp(402)):
            counts = fc.warm_symbol_earnings(["A", "B", "C"])
        self.assertEqual(calls, ["A", "B"])          # second 429 stops Finnhub for the run
        self.assertEqual(counts["r429"], 2)
        self.assertEqual(counts["unknown"], 3)
        fc.time.sleep.assert_any_call(7)

    def test_warm_refreshes_entry_expiring_before_close(self):
        """Cold-2nd T1: a fresh entry that would expire before `refresh_until` is refreshed; one that outlives it is
        skipped; without refresh_until only expired entries are refreshed."""
        now = datetime.now(timezone.utc)
        cache = {"XOM": {"v": 2, "provider": "fmp", "covered": True, "dates": ["2026-10-30"], "ttl_h": 1.0,
                         "fetched_at_utc": (now - timedelta(minutes=45)).isoformat()},
                 "AAPL": {"v": 2, "provider": "finnhub", "covered": True, "dates": ["2026-11-02"], "ttl_h": 24.0,
                          "fetched_at_utc": (now - timedelta(hours=1)).isoformat()}}
        (self.dir / "earnings_symbol_cache.json").write_text(json.dumps(cache))
        asked = []

        def fh(sym):
            asked.append(sym)
            return _Resp(200, _fh_rows(sym, [TODAY + timedelta(days=1)]))
        with self._get(fh):
            counts = fc.warm_symbol_earnings(["XOM", "AAPL"])
            self.assertEqual((asked, counts["fresh_skipped"]), ([], 2))
            counts = fc.warm_symbol_earnings(["XOM", "AAPL"], refresh_until=now + timedelta(hours=7))
        self.assertEqual(asked, ["XOM"])
        self.assertEqual(counts["fresh_skipped"], 1)
        stored = json.loads((self.dir / "earnings_symbol_cache.json").read_text())["XOM"]
        self.assertEqual((stored["provider"], stored["ttl_h"]), ("finnhub", fc._EARNINGS_SYMBOL_TTL_H))

    def test_warm_spacing_applies_after_finnhub_off(self):
        with self._get(lambda s: _Resp(429, headers={"Retry-After": "3"}), lambda s: _Resp(402)):
            fc.warm_symbol_earnings(["A", "B", "C", "D"])
        spacing = [c for c in fc.time.sleep.call_args_list if c.args == (fc._WARM_SPACING_S,)]
        self.assertEqual(len(spacing), 3)    # after B's second 429 (Finnhub off), then after C and D

    def test_token_never_logged_on_error(self):
        def boom(sym):
            raise RuntimeError("GET https://finnhub.io/...&token=SECRET")
        with self._get(boom), self.assertLogs(fc.logger, level="DEBUG") as cm:
            fc._fetch_finnhub_earnings("AAPL")
        self.assertFalse(any("SECRET" in m for m in cm.output))


class WarmRunnerSymbolsTest(unittest.TestCase):
    """run_earnings_warm.symbols(): held names first, zero / NaN / unreadable quantities dropped, deduped, upper-case."""

    def _run(self, ledger, universe=None, universe_err=None):
        import run_earnings_warm as rw
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "data" / "state").mkdir(parents=True)
        if ledger is not None:
            (root / "data" / "state" / "ownership_ledger.json").write_text(json.dumps(ledger))
        fake_dz = mock.MagicMock()
        if universe_err:
            fake_dz.universe.side_effect = universe_err
        else:
            fake_dz.universe.return_value = universe or []
        cfg = mock.MagicMock(WATCHLIST=["aapl", "SPY"], DAY_TIER_UNIVERSE=("NVDA",), DAYTRADE_UNIVERSE=[],
                             FOREVER6_UNIVERSE=["META"])
        with mock.patch.object(rw, "ROOT", root), mock.patch.object(rw, "config", cfg), \
                mock.patch.dict("sys.modules", {"execution.dip_zones": fake_dz}), \
                mock.patch("execution.dip_zones", fake_dz, create=True):
            import execution
            with mock.patch.object(execution, "dip_zones", fake_dz, create=True):
                out = rw.symbols()
        tmp.cleanup()
        return out

    def test_held_first_dedup_and_bad_qty_dropped(self):
        ledger = {"positions": {"GE": {"alpaca_net_qty": 3.0}, "XOM": {"alpaca_net_qty": 0.0},
                                "BAD": {"alpaca_net_qty": "nan"}, "OLD": {"alpaca_net_qty": "x"},
                                "SHORT": {"alpaca_net_qty": -2}, "NONE": None}}
        out = self._run(ledger, universe=["GE", "googl"])
        self.assertEqual(out, ["GE", "SHORT", "AAPL", "SPY", "NVDA", "GOOGL"])

    def test_missing_ledger_and_universe_failure_fall_back(self):
        out = self._run(None, universe_err=RuntimeError("no pandas"))
        self.assertEqual(out, ["AAPL", "SPY", "NVDA", "META"])

    def test_non_dict_ledger_does_not_crash(self):
        self.assertEqual(self._run(["not", "a", "dict"])[:1], ["AAPL"])

    def test_next_close(self):
        import run_earnings_warm as rw
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        self.assertEqual(rw.next_close_utc(datetime(2026, 10, 30, 8, 30, tzinfo=et)),
                         datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc))      # EDT: 16:00 ET = 20:00 UTC
        self.assertEqual(rw.next_close_utc(datetime(2026, 10, 30, 16, 0, tzinfo=et)),
                         datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc))      # at the close: tomorrow's
        self.assertEqual(rw.next_close_utc(datetime(2026, 11, 2, 9, 15, tzinfo=et)),
                         datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc))       # EST after 11-01


if __name__ == "__main__":
    unittest.main()
