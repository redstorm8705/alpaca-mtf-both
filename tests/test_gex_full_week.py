#!/usr/bin/env python3
# ruff: noqa: E501
"""GEX-1017 full-week coverage (logs/design_records/gex_full_week_coverage_2026-10-17.md).

Pure logic + mocked contract/snapshot data — no network, no broker:
  * window end only widens: Mon-Wed with >=2 expiries left = coming Friday (unchanged); Thu/Fri reach next week;
  * a fetch that hit the page cap drops its last (partial) expiry;
  * contracts inside the 1-hour floor are removed before gating;
  * per-expiry admission: a censored expiry drops out, a liquid one sets the label;
  * none admitted -> pool of non-same-day expiries; pool fails -> UNKNOWN with a reason; empty -> no exception."""
import math
import unittest
from datetime import date, datetime
from unittest import mock

from data import gex

ET = gex.ET


def occ(sym, exp, cp, strike):
    return f"{sym}{exp:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def chain(sym, exp, spot, n, oi=500.0):
    """n call+put strikes around spot (1-point apart), each with OI."""
    out = {}
    for i in range(-n, n + 1):
        k = round(spot + i, 2)
        out[occ(sym, exp, "C", k)] = oi
        out[occ(sym, exp, "P", k)] = oi
    return out


def quotes(oi_map, spot, now, iv=0.3, censor=False):
    """Indicative-style quotes priced off Black-Scholes at `iv`; `censor` -> zero bids (dying strip)."""
    snaps = {}
    for s in oi_map:
        exp, is_call, k = gex._parse_occ(s)
        T = max((exp - now).total_seconds() / (365.0 * 24 * 3600), gex._MIN_T_YEARS)
        px = gex._bs_price(spot, k, T, iv, is_call)
        if censor:
            snaps[s] = {"latestQuote": {"bp": 0, "ap": px}}
        else:
            snaps[s] = {"latestQuote": {"bp": px * 0.99, "ap": px * 1.01}}
    return snaps


class _Now(datetime):
    NOW = datetime(2026, 10, 15, 11, 0, tzinfo=ET)   # Thursday 11:00 ET

    @classmethod
    def now(cls, tz=None):
        return cls.NOW.astimezone(tz) if tz else cls.NOW


class TestWindowEnd(unittest.TestCase):
    def test_mon_wed_unchanged_when_two_expiries_left(self):
        mon = date(2026, 10, 19)
        listed = {date(2026, 10, d) for d in (19, 20, 21, 22, 23)}           # SPY dailies
        self.assertEqual(gex._window_end_for(mon, listed), date(2026, 10, 23))
        tue = date(2026, 10, 20)
        listed_daily = {date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)}
        self.assertEqual(gex._window_end_for(tue, listed_daily), date(2026, 10, 23))

    def test_thursday_reaches_next_week(self):
        thu = date(2026, 10, 15)
        listed = {date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 21)}
        self.assertEqual(gex._window_end_for(thu, listed), date(2026, 10, 19))

    def test_monthly_opex_friday_reaches_next_week(self):
        fri = date(2026, 10, 16)
        listed = {date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 21), date(2026, 10, 23)}
        self.assertEqual(gex._window_end_for(fri, listed), date(2026, 10, 21))

    def test_weekly_only_chain(self):
        mon = date(2026, 10, 19)
        listed = {date(2026, 10, 23), date(2026, 10, 30)}                      # EWY: Fridays only
        self.assertEqual(gex._window_end_for(mon, listed), date(2026, 10, 30))

    def test_holiday_shifted_weekly_expiry(self):
        thu = date(2027, 3, 25)                                                # Good Friday week: expiry on Thursday
        self.assertEqual(gex._window_end_for(thu, {date(2027, 3, 25), date(2027, 4, 2)}), date(2027, 4, 2))

    def test_nothing_listed_is_friday(self):
        self.assertEqual(gex._window_end_for(date(2026, 10, 19), set()), date(2026, 10, 23))


class TestFetchWindow(unittest.TestCase):
    def _run(self, today, pages):
        calls = []

        def fake(symbol, gte, lte):
            calls.append((gte, lte))
            return pages.pop(0)
        with mock.patch.object(gex, "_fetch_contracts_meta", side_effect=fake), \
                mock.patch.object(gex, "datetime", _Now):
            out = gex._fetch_window_contracts("SPY", today)
        return out, calls

    def test_mon_single_fetch_same_as_before(self):
        _Now.NOW = datetime(2026, 10, 19, 11, 0, tzinfo=ET)
        a = {}
        for d in (19, 20, 21, 22, 23):
            a.update(chain("SPY", date(2026, 10, d), 100.0, 2))
        (oi, meta), calls = self._run(date(2026, 10, 19), [(a, False)])
        self.assertEqual(calls, [("2026-10-19", "2026-10-23")])               # exactly the old window, one fetch
        self.assertEqual(meta["window_end"], "2026-10-23")
        self.assertFalse(meta["extended"])

    def test_friday_weekly_only_chain_reaches_next_friday(self):
        _Now.NOW = datetime(2026, 10, 23, 10, 0, tzinfo=ET)                  # Friday, EWY-style chain
        a = chain("EWY", date(2026, 10, 23), 50.0, 2)
        b = chain("EWY", date(2026, 10, 30), 50.0, 2)
        (oi, meta), calls = self._run(date(2026, 10, 23), [(a, False), (b, False)])
        exps = {gex._expiry_of(s) for s in oi}
        self.assertEqual(meta["window_end"], "2026-10-30")
        self.assertIn(date(2026, 10, 30), exps)                                 # never collapses onto today's strip

    def test_wednesday_mwf_chain_extends(self):
        _Now.NOW = datetime(2026, 10, 21, 10, 0, tzinfo=ET)
        a = chain("AAPL", date(2026, 10, 21), 100.0, 2)
        a.update(chain("AAPL", date(2026, 10, 23), 100.0, 2))
        b = chain("AAPL", date(2026, 10, 26), 100.0, 2)
        (oi, meta), calls = self._run(date(2026, 10, 21), [(a, False), (b, False)])
        self.assertTrue(meta["extended"])
        self.assertEqual(meta["window_end"], "2026-10-26")

    def test_capped_monday_fetch_with_two_complete_expiries_does_not_extend(self):
        _Now.NOW = datetime(2026, 10, 19, 10, 0, tzinfo=ET)                  # Monday, dense chain hits the cap
        a = chain("QQQ", date(2026, 10, 19), 100.0, 2)
        a.update(chain("QQQ", date(2026, 10, 20), 100.0, 2))
        a.update(chain("QQQ", date(2026, 10, 21), 100.0, 2))
        a.update(chain("QQQ", date(2026, 10, 22), 100.0, 2))                   # partial (dropped)
        (oi, meta), calls = self._run(date(2026, 10, 19), [(a, True)])
        self.assertEqual(len(calls), 1)                                         # two complete expiries left: no extension
        self.assertFalse(meta["extended"])
        self.assertEqual(meta["window_end"], "2026-10-21")                      # ends at the last COMPLETE expiry
        self.assertNotIn(date(2026, 10, 22), {gex._expiry_of(s) for s in oi})

    def test_capped_monday_fetch_with_one_complete_expiry_resumes(self):
        _Now.NOW = datetime(2026, 10, 19, 10, 0, tzinfo=ET)
        a = chain("QQQ", date(2026, 10, 19), 100.0, 2)
        a.update(chain("QQQ", date(2026, 10, 20), 100.0, 2))
        a.update(chain("QQQ", date(2026, 10, 21), 100.0, 2))                   # partial (dropped, then resumed)
        b = chain("QQQ", date(2026, 10, 21), 100.0, 3)
        (oi, meta), calls = self._run(date(2026, 10, 19), [(a, True), (b, False)])
        self.assertEqual(calls[1][0], "2026-10-21")
        self.assertIn(date(2026, 10, 21), {gex._expiry_of(s) for s in oi})
        self.assertEqual(meta["window_end"], "2026-10-23")

    def test_capped_thursday_fetch_is_resumed_not_narrowed(self):
        _Now.NOW = datetime(2026, 10, 15, 10, 0, tzinfo=ET)                  # Thursday, dense chain hits the cap
        a = chain("QQQ", date(2026, 10, 15), 100.0, 2)
        a.update(chain("QQQ", date(2026, 10, 16), 100.0, 2))                   # Friday cut in half by the cap
        b = chain("QQQ", date(2026, 10, 16), 100.0, 3)                         # resumed: Friday complete
        b.update(chain("QQQ", date(2026, 10, 19), 100.0, 2))
        (oi, meta), calls = self._run(date(2026, 10, 15), [(a, True), (b, False)])
        self.assertEqual(calls[1][0], "2026-10-16")                             # resumes at the cut expiry
        exps = {gex._expiry_of(s) for s in oi}
        self.assertIn(date(2026, 10, 16), exps)
        self.assertIn(date(2026, 10, 19), exps)
        self.assertEqual(meta["window_end"], "2026-10-19")

    def test_thursday_extends_and_drops_partial_and_dead(self):
        _Now.NOW = datetime(2026, 10, 15, 15, 30, tzinfo=ET)                  # 30 min before close
        a = chain("SPY", date(2026, 10, 15), 100.0, 2)                         # same-day, inside 1-hour floor
        a.update(chain("SPY", date(2026, 10, 16), 100.0, 2))
        b = chain("SPY", date(2026, 10, 19), 100.0, 2)
        b.update(chain("SPY", date(2026, 10, 20), 100.0, 2))                   # last expiry of a truncated fetch
        (oi, meta), calls = self._run(date(2026, 10, 15), [(a, False), (b, True)])
        exps = {gex._expiry_of(s) for s in oi}
        self.assertTrue(meta["extended"])
        self.assertTrue(meta["truncated"])
        self.assertNotIn(date(2026, 10, 15), exps)                             # 1-hour floor
        self.assertNotIn(date(2026, 10, 20), exps)                             # partial expiry dropped
        self.assertIn(date(2026, 10, 19), exps)


class TestAdmission(unittest.TestCase):
    def setUp(self):
        _Now.NOW = datetime(2026, 10, 15, 11, 0, tzinfo=ET)
        self.p = mock.patch.object(gex, "datetime", _Now)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_censored_front_drops_out_liquid_next_sets_label(self):
        now = _Now.NOW
        front = chain("SPY", date(2026, 10, 16), 100.0, 8)
        nxt = chain("SPY", date(2026, 10, 19), 100.0, 8)
        snaps = quotes(front, 100.0, now, censor=True)
        snaps.update(quotes(nxt, 100.0, now))
        out = gex._compute_gex_admitted(snaps, {**front, **nxt}, 100.0, date(2026, 10, 15))
        self.assertFalse(out["expiries_used"]["2026-10-16"]["admitted"])
        self.assertTrue(out["expiries_used"]["2026-10-19"]["admitted"])
        self.assertEqual(out["admission"], "per_expiry")
        self.assertNotEqual(out["label"], "UNKNOWN")
        self.assertEqual(out["pin"]["expiry"], "2026-10-19")                   # pin = nearest ADMITTED expiry

    def test_pool_fallback_when_none_passes_alone(self):
        now = _Now.NOW
        a = chain("SPY", date(2026, 10, 19), 100.0, 1)                         # 6 windowed each: too few alone (<10), 12 pooled
        b = chain("SPY", date(2026, 10, 20), 100.0, 1)
        snaps = quotes(a, 100.0, now)
        snaps.update(quotes(b, 100.0, now))
        out = gex._compute_gex_admitted(snaps, {**a, **b}, 100.0, date(2026, 10, 15))
        self.assertFalse(any(v["admitted"] for v in out["expiries_used"].values()))
        self.assertEqual(out["admission"], "pool_non_same_day")
        self.assertTrue(out["quality_ok"])

    def test_same_day_excluded_from_pool_and_reason_given(self):
        now = _Now.NOW
        today = chain("SPY", date(2026, 10, 15), 100.0, 8)
        snaps = quotes(today, 100.0, now, censor=True)
        out = gex._compute_gex_admitted(snaps, today, 100.0, date(2026, 10, 15))
        self.assertEqual(out["label"], "UNKNOWN")
        self.assertEqual(out["admission"], "none")
        self.assertEqual(out["unknown_reason"], "no_contracts_outside_same_day")

    def test_pool_fails_unknown_with_reason(self):
        now = _Now.NOW
        a = chain("SPY", date(2026, 10, 19), 100.0, 1)
        out = gex._compute_gex_admitted(quotes(a, 100.0, now, censor=True), a, 100.0, date(2026, 10, 15))
        self.assertEqual(out["label"], "UNKNOWN")
        self.assertEqual(out["unknown_reason"], "no_expiry_admitted_and_pool_failed")

    def test_empty_never_raises(self):
        out = gex._compute_gex_admitted({}, {}, 100.0, date(2026, 10, 15))
        self.assertEqual(out["label"], "UNKNOWN")
        self.assertEqual(out["expiries_used"], {})

    def test_label_is_plain_sum_of_admitted(self):
        now = _Now.NOW
        a = chain("SPY", date(2026, 10, 19), 100.0, 8)
        out = gex._compute_gex_admitted(quotes(a, 100.0, now), a, 100.0, date(2026, 10, 15))
        direct = gex._compute_gex(quotes(a, 100.0, now), a, 100.0)
        self.assertTrue(math.isclose(out["raw_gex_m"], direct["raw_gex_m"]))
        self.assertEqual(out["label"], direct["label"])


if __name__ == "__main__":
    unittest.main()
