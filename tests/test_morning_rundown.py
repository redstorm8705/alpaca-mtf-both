# ruff: noqa: E501
"""MORNING-1029 — scripts/morning_rundown.py machine tests (spec §9a 1–5, 7; v9 / v9.2 / v9.3).

Every data call is mocked (bars, latest trade, contracts, snapshots, calendar, Slack); a guard fails the test on any
unmocked network connection. Fixture: Monday 2026-11-09, the 06:46 PT send (14:46 UTC, EST), SPY around $100.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import scripts.morning_rundown as mr  # noqa: E402

UTC = timezone.utc
ET = mr.ET
TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 14, 46, 0, tzinfo=UTC)          # 06:46 PT / 09:46 ET
CUTOFF = datetime(2026, 11, 9, 14, 31, 0, tzinfo=UTC)       # last delayed SIP bar (06:31 PT)
M = timedelta(minutes=1)


def _no_network(*_a, **_k):
    raise AssertionError("unmocked network call in a test")


def _weekdays_before(d: date, n: int) -> list[date]:
    out: list[date] = []
    cur = d
    while len(out) < n:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur)
    return sorted(out)


SESSIONS = _weekdays_before(TODAY, 20)
_HIST_CACHE: dict = {}


def _cal(start, end):
    days = [d for d in SESSIONS + [TODAY] if start <= d <= end]
    return [{"date": d, "open": datetime.combine(d, datetime.min.time().replace(hour=9, minute=30), tzinfo=ET),
             "close": datetime.combine(d, datetime.min.time().replace(hour=16), tzinfo=ET)} for d in days]


def _frame(rows: list[tuple[datetime, float, float, float, float]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows],
                         "close": [r[4] for r in rows], "volume": [1000.0] * len(rows)}, index=idx)


def _occ(k: int, cp: str) -> str:
    return f"SPY261109{cp}{k * 1000:08d}"


class Fixture:
    """Configurable fake market for one build. Defaults give a complete, verified 06:46 setup on both sides."""

    def __init__(self):
        self.trade = (100.05, NOW - timedelta(seconds=4))
        self.drop_iex_minutes: set[datetime] = set()
        self.quote_t = NOW                       # option quote timestamp
        self.asks = {(101, "C"): 0.30, (102, "C"): 0.10, (99, "P"): 0.25, (98, "P"): 0.08,
                     (100, "C"): 0.60, (100, "P"): 0.55}
        self.last_trade_px: dict = {}
        self.daily = (101.0, 99.0, 100.0)
        self.gex = {"label": "POSITIVE", "pin": {"call_wall": 102.0, "put_wall": 98.0}, "flip_strike": None}
        self.today_sip_extra: list = []
        self.gap_override: list | None = None

    # bars
    def bars(self, symbol, start, end, feed):
        rows = []
        if start.astimezone(ET).date() < TODAY:          # 20-session history in every clock window
            key = (feed, start, end)
            if key in _HIST_CACHE:
                return _HIST_CACHE[key]
            for d in SESSIONS:
                t0 = datetime.combine(d, datetime.min.time(), tzinfo=ET).replace(hour=9).astimezone(UTC)
                for i in range(0, 7 * 60):
                    t = t0 + i * M
                    c = 100.0 + (0.02 if i % 2 else 0.0)
                    if feed == "sip":
                        rows.append((t, c, c + 0.05, c - 0.05, c))
                    else:
                        rows.append((t, c, c + 0.04, c - 0.04, c))
            _HIST_CACHE[key] = _frame([r for r in rows if start <= r[0] < end])
            return _HIST_CACHE[key]
        if feed == "sip":
            t = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)   # 01:00 PT
            while t <= CUTOFF:
                c = 100.0
                h, lo = c + 0.05, c - 0.05
                if t == datetime(2026, 11, 9, 10, 0, tzinfo=UTC):
                    h = 100.3
                if t == datetime(2026, 11, 9, 11, 0, tzinfo=UTC):
                    lo = 99.7
                rows.append((t, c, h, lo, c))
                t += M
            rows += self.today_sip_extra
            return _frame([r for r in rows if start <= r[0] < end])
        # IEX today: pre-market thin, complete from the open
        if self.gap_override is not None:
            return _frame([r for r in self.gap_override if start <= r[0] < end])
        t = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
        while t < NOW:
            if t not in self.drop_iex_minutes:
                rows.append((t, 100.05, 100.09, 100.01, 100.05))
            t += M
        return _frame([r for r in rows if start <= r[0] < end])

    def daily_bars(self, symbol, start, end):
        h, lo, c = self.daily
        return _frame([(datetime.combine(SESSIONS[-1], datetime.min.time(), tzinfo=UTC), c, h, lo, c)])

    def contracts(self, symbol, gte, lte):
        return {_occ(k, cp): 1000.0 for k in range(96, 106) for cp in ("C", "P")}

    def snapshots(self, symbols):
        out = {}
        for s in symbols:
            k, cp = int(s[-8:]) // 1000, s[-9]
            ask = self.asks.get((k, cp))
            if ask is None:
                continue
            out[s] = {"latestQuote": {"ap": ask, "bp": round(ask - 0.02, 2), "as": 10, "bs": 12, "ax": "C", "bx": "C",
                                      "c": "A", "t": self.quote_t.isoformat().replace("+00:00", "Z")},
                      "latestTrade": {"p": self.last_trade_px.get((k, cp), ask)}}
        return out

    def patches(self):
        return [mock.patch.object(mr, "get_bars", side_effect=self.bars),
                mock.patch.object(mr, "get_daily_bars", side_effect=self.daily_bars),
                mock.patch.object(mr, "latest_trade", side_effect=lambda s: self.trade),
                mock.patch.object(mr, "fetch_contracts", side_effect=self.contracts),
                mock.patch.object(mr, "fetch_snapshots", side_effect=self.snapshots),
                mock.patch.object(mr.gex, "_compute_gex", side_effect=lambda *a, **k: dict(self.gex)),
                mock.patch.object(mr.gex, "_expiry_range", return_value=("2026-11-09", "2026-11-13")),
                mock.patch.object(mr, "calendar", side_effect=_cal)]


class Base(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection"):
            p = mock.patch(target, side_effect=_no_network)
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        logs = Path(self.tmp.name)
        state = {"recs": logs / "recs.jsonl", "recommendations": logs / "0dte.jsonl", "heartbeat": logs / "hb.json",
                 "chain_dir": logs / "chain"}
        for p in (mock.patch.object(mr, "_LOGS", logs), mock.patch.dict(mr.STATE, state)):
            p.start()
            self.addCleanup(p.stop)
        self.logs = logs
        self.fx = Fixture()
        self.clock = [NOW]

    def now_fn(self):
        return self.clock[0]

    def sleep_fn(self, sec):
        self.clock[0] = self.clock[0] + timedelta(seconds=sec)

    def build(self, send="0646"):
        cal_today = _cal(TODAY, TODAY)[0]
        ps = self.fx.patches()
        for p in ps:
            p.start()
        try:
            return mr.build_symbol("SPY", send, NOW, cal_today, SESSIONS, now_fn=self.now_fn, sleep_fn=self.sleep_fn)
        finally:
            for p in ps:
                p.stop()

    def texts(self, rec):
        counts: dict = {}
        return {s: mr.finalize_side("SPY", rec["sides"][s], counts) for s in mr.SIDES}, counts


class TestBaselineFixture(Base):
    def test_complete_verified_watchlist(self):
        rec = self.build()
        self.assertTrue(rec["verified"])
        self.assertEqual(rec["sides"]["CALL"]["state"], "WATCHING")
        self.assertEqual(rec["sides"]["PUT"]["state"], "WATCHING")
        self.assertAlmostEqual(rec["stats"]["merge_distance"], 0.1, places=6)
        self.assertAlmostEqual(rec["stats"]["b_high"], 0.01, places=6)
        self.assertAlmostEqual(rec["stats"]["b_low"], 0.01, places=6)
        self.assertEqual(rec["stats"]["sessions"]["merge_distance"], 20)
        texts, counts = self.texts(rec)
        self.assertIn("If SPY closes above $100.30", texts["CALL"])
        self.assertIn("If SPY closes below $99.70", texts["PUT"])
        self.assertNotIn("Limit $", texts["CALL"] + texts["PUT"])
        self.assertFalse(any(counts.values()))

    def test_sigma_is_close_to_close_dollar_std(self):
        rec = self.build()
        # alternating 100.00 / 100.02 closes -> |change| 0.02 every minute, signs alternate: sample std ≈ 0.02
        self.assertAlmostEqual(rec["stats"]["sigma_1m"], 0.02, places=3)
        self.assertAlmostEqual(rec["stats"]["t_max_min"], (0.1 / rec["stats"]["sigma_1m"]) ** 2, places=6)

    def test_em_factor_is_computed_not_typed(self):
        self.assertAlmostEqual(mr.EM_FACTOR, (2 * 3.141592653589793) ** 0.5 / 2, places=12)
        rec = self.build()
        self.assertTrue(rec["em"]["available"])
        self.assertAlmostEqual(rec["em"]["em"], mr.EM_FACTOR * (0.60 + 0.55), places=9)


class TestTriggerState(Base):
    def test_1a_trade_older_than_t_max_is_reference_only(self):
        self.fx.trade = (100.05, NOW - timedelta(hours=3))
        rec = self.build()
        texts, _ = self.texts(rec)
        for side in mr.SIDES:
            self.assertEqual(rec["sides"][side]["state"], "REFERENCE ONLY")
            self.assertIn("REFERENCE ONLY", texts[side])
            self.assertNotRegex(texts[side], r"(?i)only if|closes (above|below)|breaks")

    def test_1a2_price_stale_after_slow_quote_polls_is_reference_only(self):
        """Codex pre-ship 11-08: the price is fresh at the start, but the quote polls run past t_max -> not WATCHING."""
        orig = mr.poll_quotes

        def slow_poll(*a, **k):
            out = orig(*a, **k)
            self.clock[0] = self.clock[0] + timedelta(hours=1)   # far beyond the fixture's t_max (~25 min)
            return out
        with mock.patch.object(mr, "poll_quotes", side_effect=slow_poll):
            rec = self.build()
        self.assertFalse(rec["verified"])
        self.assertFalse(rec["current_price"]["current_after_polls"])
        for side in mr.SIDES:
            self.assertNotEqual(rec["sides"][side]["state"], "WATCHING")
            self.assertIn("went stale during the quote polls", rec["sides"][side]["state_reason"] or "")

    def test_1a3_trigger_broken_during_quote_polls_is_reference_only(self):
        """Codex pre-ship 11-09: an IEX bar that completes while the quote polls run and reaches the call trigger
        makes the CALL side REFERENCE ONLY, not WATCHING; the PUT side is unaffected."""
        rows, t = [], datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
        while t < NOW + 10 * M:                         # both polls advance the clock 3 min each
            hi = 101.2 if t == NOW else 100.09          # completes only after the polls have advanced the clock
            rows.append((t, 100.05, hi, 100.01, 100.05))
            t += M
        self.fx.gap_override = rows
        orig = mr.poll_quotes
        n = {"polls": 0}

        def slow_poll(*a, **k):
            out = orig(*a, **k)
            n["polls"] += 1
            if n["polls"] == 2:                          # the candidate poll (the ATM poll runs first)
                self.clock[0] = self.clock[0] + 3 * M    # well inside t_max, so the price stays current
            return out
        with mock.patch.object(mr, "poll_quotes", side_effect=slow_poll):
            rec = self.build()
        self.assertTrue(rec["verified"])
        self.assertEqual(rec["sides"]["CALL"]["state"], "REFERENCE ONLY")
        self.assertIn("trigger already broken during the quote polls", rec["sides"]["CALL"]["state_reason"])
        self.assertEqual(rec["sides"]["PUT"]["state"], "WATCHING")

    def test_1a3b_broken_trigger_without_quotes_is_not_watched(self):
        """Cold review 11-09: with no call quotes the CALL side would be NO_QUOTE / NO_CONTRACT (both watched); a
        trigger that broke during the polls must still make it REFERENCE ONLY, so it is never watched."""
        rows, t = [], datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
        while t < NOW + 10 * M:                         # both polls advance the clock 3 min each
            hi = 101.2 if t == NOW else 100.09          # completes only after the polls have advanced the clock
            rows.append((t, 100.05, hi, 100.01, 100.05))
            t += M
        self.fx.gap_override = rows
        self.fx.asks = {k: v for k, v in self.fx.asks.items() if k[1] != "C" or k[0] == 100}   # no call candidates
        orig = mr.poll_quotes
        n = {"polls": 0}

        def slow_poll(*a, **k):
            out = orig(*a, **k)
            n["polls"] += 1
            if n["polls"] == 2:                          # the candidate poll (the ATM poll runs first)
                self.clock[0] = self.clock[0] + 3 * M    # well inside t_max, so the price stays current
            return out
        with mock.patch.object(mr, "poll_quotes", side_effect=slow_poll):
            rec = self.build()
        self.assertTrue(rec["verified"])
        self.assertEqual(rec["sides"]["CALL"]["state"], "REFERENCE ONLY")
        self.assertIn("trigger already broken during the quote polls", rec["sides"]["CALL"]["state_reason"])
        # build_plan watches only WATCHING / NO_QUOTE / NO_CONTRACT, so REFERENCE ONLY is never watched
        self.assertNotIn(rec["sides"]["CALL"]["state"], ("WATCHING", mr.NO_QUOTE, mr.NO_CONTRACT))

    def test_1a4_unreadable_post_poll_history_is_reference_only(self):
        orig = mr.poll_quotes

        def slow_poll(*a, **k):
            out = orig(*a, **k)
            self.clock[0] = self.clock[0] + 3 * M
            self.fx.drop_iex_minutes = {NOW - M}          # a minute is now missing from the re-fetched history
            return out
        with mock.patch.object(mr, "poll_quotes", side_effect=slow_poll):
            rec = self.build()
        self.assertFalse(rec["verified"])
        for side in mr.SIDES:
            self.assertNotEqual(rec["sides"][side]["state"], "WATCHING")

    def test_1b_price_above_nearest_resistance_uses_next_level(self):
        ladder = [{"price": p, "sources": [n]} for p, n in ((99.0, "pdl"), (100.3, "on high"), (101.0, "pdh"), (102.0, "cw"))]
        s = mr.build_setup(ladder, 100.5, "CALL", 0.1, {})
        self.assertEqual(s["trigger"]["price"], 101.0)
        self.assertEqual(s["invalidation"]["price"], 100.3)

    def test_1c_sip_high_since_level_time_breaks_level(self):
        sip = _frame([(datetime(2026, 11, 9, 12, 0, tzinfo=UTC), 100, 100.4, 99.9, 100)])
        lv = mr.level(100.3, "x", datetime(2026, 11, 9, 11, 0, tzinfo=UTC))
        self.assertTrue(mr.level_broken(lv, True, sip, _frame([]), {"b_high": 0.0})[0])
        lv_after = mr.level(100.3, "x", datetime(2026, 11, 9, 13, 0, tzinfo=UTC))
        self.assertFalse(mr.level_broken(lv_after, True, sip, _frame([]), {"b_high": 0.0})[0])

    def test_1d_level_within_merge_distance_is_not_a_trigger(self):
        ladder = [{"price": 100.08, "sources": ["a"]}, {"price": 100.5, "sources": ["b"]}, {"price": 101, "sources": ["c"]},
                  {"price": 99, "sources": ["d"]}]
        s = mr.build_setup(ladder, 100.05, "CALL", 0.1, {})
        self.assertEqual(s["trigger"]["price"], 100.5)
        self.assertTrue(any("merge distance" in k["why"] for k in s["skipped"]))

    def test_1e_missing_gap_minute_is_reference_only(self):
        self.fx.drop_iex_minutes = {datetime(2026, 11, 9, 14, 38, tzinfo=UTC)}
        rec = self.build()
        self.assertFalse(rec["iex_history"]["complete"])
        self.assertEqual(rec["iex_history"]["missing_minutes"], 1)
        texts, _ = self.texts(rec)
        for side in mr.SIDES:
            self.assertIn("trigger history not verified", texts[side])
            self.assertEqual(rec["sides"][side]["state"], "REFERENCE ONLY")

    def test_1f_gap_bar_touched_then_reversed_is_not_a_trigger(self):
        gap = _frame([(datetime(2026, 11, 9, 14, 35, tzinfo=UTC), 600, 601.0, 599.8, 600.9),
                      (datetime(2026, 11, 9, 14, 40, tzinfo=UTC), 600, 600.1, 598.9, 599.0)])
        lv = mr.level(601.0, "pdh", None)
        broken, why = mr.level_broken(lv, True, _frame([]), gap, {"b_high": 0.175})
        self.assertTrue(broken)
        ladder = [dict(lv, confluence=False), {"price": 603.0, "sources": ["cw"]}, {"price": 597.0, "sources": ["pw"]}]
        s = mr.build_setup(ladder, 599.0, "CALL", 0.1, {0: (broken, why)})
        self.assertEqual(s["trigger"]["price"], 603.0)

    def test_1g_reference_sections_carry_no_trigger_text_or_numbers(self):
        rec = self.build(send="0600")
        texts, counts = self.texts(rec)
        for side in mr.SIDES:
            self.assertNotEqual(rec["sides"][side]["state"], "WATCHING")
            self.assertNotRegex(texts[side], r"(?i)only if|closes (above|below)|breaks (above|below)|buy the")
            self.assertNotRegex(texts[side], r"Limit \$\d|sell-stop|Stop: *\$\d")
        self.assertFalse(any(counts.values()))


class TestSetupCompleteness(Base):
    def _no_rank_when(self, setup_fn):
        real = mr.select_strike
        with mock.patch.object(mr, "select_strike", side_effect=real) as sel:
            setup_fn()
            rec = self.build()
        return rec, sel

    def test_2a_no_level_above_price_missing_trigger(self):
        def cfg():
            self.fx.trade = (110.0, NOW - timedelta(seconds=4))
            self.fx.gap_override = [(t, 110, 110.02, 109.99, 110) for t in
                                    (CUTOFF + i * M for i in range(1, 15))]
        rec, sel = self._no_rank_when(cfg)
        call = rec["sides"]["CALL"]
        self.assertEqual(call["state"], mr.NO_SETUP)
        self.assertIn("trigger", call["setup"]["missing"])
        self.assertFalse(any(c.args[1]["side"] == "CALL" for c in sel.call_args_list))
        texts, _ = self.texts(rec)
        self.assertIn("NO ACTIONABLE SETUP — missing trigger", texts["CALL"])
        self.assertNotRegex(texts["CALL"], r"\d{2,3}C|Limit|[Ss]top")

    def test_2b_trigger_without_beyond_level_and_no_em_missing_target(self):
        ladder = [{"price": 99.0, "sources": ["pdl"]}, {"price": 100.5, "sources": ["pdh"]}]
        s = mr.build_setup(ladder, 100.0, "CALL", 0.1, {})
        self.assertEqual(s["missing"], ["target"])

    def test_2c_no_level_on_other_side_missing_invalidation(self):
        ladder = [{"price": 100.5, "sources": ["a"]}, {"price": 101.0, "sources": ["b"]}]
        s = mr.build_setup(ladder, 100.0, "CALL", 0.1, {})
        self.assertEqual(s["missing"], ["invalidation"])
        with mock.patch.object(mr, "rank_candidates") as rk:
            rec = {"side": "CALL", "setup": s, "strike": None, "state": mr.NO_SETUP}
            txt = mr.side_text("SPY", rec)
            rk.assert_not_called()
        self.assertIn("missing invalidation", txt)

    def test_2d_em_is_the_target_when_next_beyond_trigger(self):
        ladder = mr.merge_levels([mr.level(99.0, "pdl", None), mr.level(100.5, "pdh", None),
                                  mr.level(101.4, "EM high (approx.)", None)], 0.1)
        s = mr.build_setup(ladder, 100.0, "CALL", 0.1, {})
        self.assertEqual(s["target"]["sources"], ["EM high (approx.)"])


class TestStops(unittest.TestCase):
    def _pick(self, is_call=True, iv=0.2, k=101.0):
        return {"strike": k, "is_call": is_call, "iv": iv}

    def test_3a_call_and_put_stop_below_bid(self):
        T = mr.years_between(datetime(2026, 11, 9, 21, tzinfo=UTC), datetime(2026, 11, 9, 15, tzinfo=UTC))
        call = mr.model_stop(self._pick(True, 0.25, 101.0), 100.6, T, bid=0.9)
        put = mr.model_stop(self._pick(False, 0.25, 99.0), 99.4, T, bid=0.9)
        for st in (call, put):
            self.assertTrue(st["valid"], st)
            self.assertGreater(st["stop"], 0)
            self.assertLess(st["stop"], 0.9)

    def test_3b_no_iv_or_floor_or_above_bid_is_not_placeable(self):
        self.assertFalse(mr.model_stop(self._pick(iv=None), 100, 0.01, 0.9)["valid"])
        with mock.patch.object(mr, "model_premium", return_value=0.05):
            self.assertFalse(mr.model_stop(self._pick(), 100, 0.01, 0.9)["valid"])
        with mock.patch.object(mr, "model_premium", return_value=0.95):
            self.assertFalse(mr.model_stop(self._pick(), 100, 0.01, 0.9)["valid"])

    def test_v93_stop_inside_the_spread(self):
        with mock.patch.object(mr, "model_premium", return_value=1.00):
            st = mr.model_stop(self._pick(), 100, 0.01, bid=0.80)
        self.assertFalse(st["valid"])
        self.assertEqual(st["stop_check_bid"], 0.80)
        with mock.patch.object(mr, "model_premium", return_value=0.79):
            self.assertTrue(mr.model_stop(self._pick(), 100, 0.01, bid=0.80)["valid"])

    def test_floor_to_tick(self):
        self.assertEqual(mr.floor_cent(0.138), 0.13)
        self.assertEqual(mr.floor_cent(0.79), 0.79)


class TestQuotesAndCap(Base):
    def test_4_same_session_quote_before_window_is_not_current(self):
        self.fx.quote_t = datetime(2026, 11, 9, 12, 31, tzinfo=UTC)   # 07:31 ET, same session, before the window
        rec = self.build()
        for side in mr.SIDES:
            self.assertEqual(rec["sides"][side]["state"], mr.NO_QUOTE)
        texts, counts = self.texts(rec)
        self.assertIn("NO CURRENT EXECUTABLE QUOTE", texts["CALL"])
        self.assertFalse(any(counts.values()))

    def test_5_last_trade_under_cap_but_ask_over_cap(self):
        for key in ((101, "C"), (102, "C"), (99, "P"), (98, "P")):
            self.fx.asks[key] = 1.30
            self.fx.last_trade_px[key] = 1.00
        rec = self.build()
        for side in mr.SIDES:
            self.assertEqual(rec["sides"][side]["state"], mr.NO_CONTRACT)
            self.assertIsNone(rec["sides"][side]["strike"]["pick"])
        texts, _ = self.texts(rec)
        self.assertIn("NO ACTIONABLE CONTRACT UNDER $1.25", texts["PUT"])
        self.assertNotIn("1.00", texts["PUT"])

    def test_5_eligibility_never_uses_mid_or_trade(self):
        q = {"bid": 1.0, "ask": 1.26, "current": True}
        self.assertFalse(mr.eligibility(q)[0])
        for bad in ({"bid": 0.0, "ask": 0.5, "current": True}, {"bid": 0.5, "ask": 0.5, "current": True},
                    {"bid": 0.6, "ask": 0.5, "current": True}):
            self.assertFalse(mr.eligibility(bad)[0])

    def test_rank_reward_to_risk_ties_to_cheaper_ask(self):
        cands = [{"occ": "A", "strike": 101.0, "is_call": True, "bid": 0.5, "ask": 0.6},
                 {"occ": "B", "strike": 102.0, "is_call": True, "bid": 0.2, "ask": 0.3}]
        with mock.patch.object(mr, "implied_vol", return_value=(0.2, "ok")), \
                mock.patch.object(mr, "model_premium", side_effect=lambda S, K, T, iv, c: 0.0):
            pick, ranked = mr.rank_candidates(cands, 103.0, 99.0, 100.0, 0.001)
        # A: 2/0.6=3.33 ; B: 1/0.3=3.33 -> tie -> cheaper ask B
        self.assertEqual(pick["occ"], "B")
        self.assertTrue(pick["rank_kind"].startswith("reward_to_risk"))

    def test_rank_falls_back_without_iv(self):
        cands = [{"occ": "A", "strike": 101.0, "is_call": True, "bid": 0.5, "ask": 0.6}]
        with mock.patch.object(mr, "implied_vol", return_value=(None, "no IV root")):
            pick, _ = mr.rank_candidates(cands, 103.0, 99.0, 100.0, 0.001)
        self.assertTrue(pick["rank_kind"].startswith("intrinsic_per_ask"))

    def test_target_does_not_reach(self):
        cands = [{"occ": "A", "strike": 104.0, "is_call": True, "bid": 0.1, "ask": 0.12},
                 {"occ": "B", "strike": 103.5, "is_call": True, "bid": 0.1, "ask": 0.15}]
        with mock.patch.object(mr, "implied_vol", return_value=(None, "x")):
            pick, _ = mr.rank_candidates(cands, 103.0, 99.0, 100.0, 0.001)
        self.assertEqual(pick["occ"], "B")
        self.assertEqual(pick["note"], "target does not reach this strike")


class TestLoggingAndHeartbeat(Base):
    def _run(self, send="0646"):
        ps = self.fx.patches() + [mock.patch.object(mr, "send_slack_blocks", return_value=True),
                                  mock.patch.object(mr, "send_slack")]
        for p in ps:
            p.start()
        try:
            return mr.run(send, now=NOW, now_fn=self.now_fn, sleep_fn=self.sleep_fn)
        finally:
            for p in ps:
                p.stop()

    def test_7_rows_stamps_heartbeat_keys_archives(self):
        self.assertEqual(self._run(), 0)
        rows = [json.loads(x) for x in (self.logs / "recs.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        for r in rows:
            for k in ("ts_utc", "ts_local", "tz", "schema_version"):
                self.assertIn(k, r)
            self.assertTrue(r["ts_utc"].endswith("Z"))
            self.assertTrue(r["ts_local"].endswith("-08:00"))
        hb = json.loads((self.logs / "hb.json").read_text())
        for k in mr.VIOLATION_KEYS:
            self.assertEqual(hb["violations"][k], 0)
            self.assertIn(k, hb["violations_today"])
        for k in ("b_high", "b_low", "sigma_1m", "t_max", "merge_distance", "session_counts"):
            self.assertIn(k, hb["symbols"]["SPY"])
        self.assertEqual(hb["detected_et_offset"], "-05:00")
        self.assertTrue(hb["ok"])
        self.assertTrue(list((self.logs / "chain").glob("2026-11-09_0646.json.gz")))
        self.assertTrue((self.logs / "morning_rundown_2026-11-09.md").exists())
        plan = json.loads((self.logs / "morning_rundown_plan_2026-11-09.json").read_text())
        self.assertTrue(plan["symbols"]["SPY"]["sides"]["CALL"]["watch"])
        recs = [json.loads(x) for x in (self.logs / "0dte.jsonl").read_text().splitlines()]
        self.assertEqual({r["side"] for r in recs}, {"CALL", "PUT"})
        self.assertTrue(all(r["r"] == mr.gex._RISK_FREE_RATE and r["q"] == mr.gex._DIV_YIELD for r in recs))

    def test_7_jsonl_valid_with_two_overlapping_writers(self):
        path = self.logs / "x.jsonl"
        big = {"pad": "z" * 20000}

        def w(i):
            for _ in range(20):
                mr.locked_append_jsonl(path, [{**big, "i": i}])
        ts = [threading.Thread(target=w, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 40)
        for line in lines:
            json.loads(line)

    def test_7_injected_ask_over_cap_is_counted_and_replaced(self):
        rec = self.build()
        call = rec["sides"]["CALL"]
        call["strike"]["pick"]["ask"] = 1.30
        counts: dict = {}
        txt = mr.finalize_side("SPY", call, counts)
        self.assertEqual(counts.get("ask_over_cap"), 1)
        self.assertIn("REFERENCE ONLY", txt)
        self.assertNotIn("1.30", txt)

    def test_t3_recs_append_failure_still_writes_plan(self):
        real = mr.locked_append_jsonl

        def fail_recs(path, rows):
            if path == mr.STATE["recs"]:
                raise OSError("disk full")
            return real(path, rows)
        with mock.patch.object(mr, "locked_append_jsonl", side_effect=fail_recs):
            self._run()
        self.assertTrue((self.logs / "morning_rundown_plan_2026-11-09.json").exists())
        hb = json.loads((self.logs / "hb.json").read_text())
        self.assertTrue(hb["sends"]["watchlist"]["plan_ok"])

    def test_heartbeat_error_after_good_send_does_not_report_failed(self):
        with mock.patch.object(mr, "update_heartbeat", side_effect=OSError("hb")):
            ps = self.fx.patches() + [mock.patch.object(mr, "send_slack_blocks", return_value=True),
                                      mock.patch.object(mr, "send_slack")]
            for p in ps:
                p.start()
            try:
                rc = mr.run("0646", now=NOW, now_fn=self.now_fn, sleep_fn=self.sleep_fn)
                warned = [c.args[0] for c in mr.send_slack.call_args_list]
            finally:
                for p in ps:
                    p.stop()
        self.assertEqual(rc, 0)
        self.assertFalse(any("FAILED" in w for w in warned))

    def test_t2_trailing_unpublished_minute_is_waited_for(self):
        last = datetime(2026, 11, 9, 14, 45, tzinfo=UTC)
        orig = self.fx.bars

        def lagged(symbol, start, end, feed):
            df = orig(symbol, start, end, feed)
            if feed == "iex" and start.astimezone(ET).date() == TODAY and self.clock[0] < NOW + timedelta(seconds=10):
                df = df[df.index != pd.Timestamp(last)]
            return df
        self.fx.bars = lagged
        self.fx.quote_t = NOW + timedelta(seconds=30)
        rec = self.build()
        self.assertTrue(rec["iex_history"]["complete"])
        self.assertEqual(rec["sides"]["CALL"]["state"], "WATCHING")
        self.assertGreaterEqual(self.clock[0], NOW + timedelta(seconds=10))

    def test_t2_interior_missing_minute_still_reference(self):
        self.fx.drop_iex_minutes = {datetime(2026, 11, 9, 14, 40, tzinfo=UTC)}
        rec = self.build()
        self.assertFalse(rec["iex_history"]["complete"])
        self.assertEqual(rec["sides"]["CALL"]["state"], "REFERENCE ONLY")

    def test_market_closed_no_send(self):
        ps = [mock.patch.object(mr, "calendar", return_value=[]),
              mock.patch.object(mr, "send_slack_blocks"), mock.patch.object(mr, "send_slack")]
        for p in ps:
            p.start()
        try:
            rc = mr.run("0646", now=NOW)
            mr.send_slack_blocks.assert_not_called()
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(rc, 0)

    def test_0600_is_reference_with_footer(self):
        self._run("0600")
        md = (self.logs / "morning_rundown_2026-11-09_0600.md").read_text()
        self.assertIn("PRE-MARKET · reference", md)
        self.assertIn("watchlist comes at 06:46 PT", md)
        self.assertNotIn("WATCHING", md)
        self.assertFalse((self.logs / "morning_rundown_plan_2026-11-09.json").exists())

    def test_3c_3d_wording_never_renders(self):
        self._run()
        md = (self.logs / "morning_rundown_2026-11-09.md").read_text().lower()
        for bad in ("accept up to the full premium", "no stop", "stop placed", "stop has been placed"):
            self.assertNotIn(bad, md)

    def test_build_failure_isolated_per_symbol(self):
        orig = mr.build_symbol

        def flaky(sym, *a, **k):
            if sym == "QQQ":
                raise RuntimeError("boom")
            return orig(sym, *a, **k)
        with mock.patch.object(mr, "build_symbol", side_effect=flaky):
            rc = self._run()
        self.assertEqual(rc, 1)
        hb = json.loads((self.logs / "hb.json").read_text())
        self.assertFalse(hb["ok"])
        self.assertIn("failed", hb["symbols"]["QQQ"])
        self.assertIn("WATCHING", (self.logs / "morning_rundown_2026-11-09.md").read_text())

    def test_wednesday_logs_weekly_gex_skip(self):
        wed = date(2026, 11, 11)
        self.assertEqual(mr.log_weekly_gex_skip(wed, [wed]), "weekly GEX report skipped: no trading Mon/Tue")
        self.assertIsNone(mr.log_weekly_gex_skip(wed, [date(2026, 11, 10), wed]))


class TestNoExecutionImports(unittest.TestCase):
    def test_no_execution_imports_and_no_order_calls(self):
        for name in ("morning_rundown.py", "morning_watch.py"):
            src = (_ROOT / "scripts" / name).read_text()
            self.assertNotRegex(src, r"(?m)^\s*(from|import)\s+execution")
            self.assertNotIn("submit_order", src)
            self.assertNotIn("/v2/orders", src)
            self.assertNotIn("data/state", src.replace("data/state untouched", ""))


class TestPollKeepsGoingOnNonExecutable(unittest.TestCase):
    def test_fresh_zero_bid_quote_does_not_end_the_window(self):
        """Codex pre-ship 11-09: a current but zero-bid quote must not end the poll; the two-sided quote that arrives
        later in the window is the one used."""
        clock = [datetime(2026, 11, 9, 14, 0, tzinfo=UTC)]
        occ = "SPY261109C00101000"
        calls = []

        def snaps(symbols):
            calls.append(clock[0])
            t = clock[0].isoformat().replace("+00:00", "Z")
            bid = 0.0 if len(calls) == 1 else 0.48
            return {occ: {"latestQuote": {"ap": 0.50, "bp": bid, "t": t}}}

        def sleep(sec):
            clock[0] = clock[0] + timedelta(seconds=sec)

        with mock.patch.object(mr, "fetch_snapshots", side_effect=snaps):
            out = mr.poll_quotes([occ], now_fn=lambda: clock[0], sleep_fn=sleep)
        self.assertGreaterEqual(len(calls), 2)
        q = out["quotes"][occ]
        self.assertEqual((q["bid"], q["ask"], q["current"]), (0.48, 0.50, True))
        self.assertTrue(mr.two_sided(q)[0])


if __name__ == "__main__":
    unittest.main()
