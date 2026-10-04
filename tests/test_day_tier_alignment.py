#!/usr/bin/env python3
# ruff: noqa: E501
"""strategy/day_tier_alignment: the 2m/5m short-term alignment gate, the counter-trend TREND-FAILURE test
(15m lead + structure break, 30m confirmation — "a failed trend, not a bull flag"), the closed-bar resampling,
and the counter-trend fade reversal criterion (design record logs/design_records/day_tier_short_term_alignment_2026-10-04.md)."""
import math
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import config
from strategy import day_tier_alignment as al

ET = ZoneInfo("America/New_York")
EF, ES = f"ema_{config.EMA_FAST}", f"ema_{config.EMA_SLOW}"
HIST = f"{config.MACD_FAST['label']}_histogram"


DAY = datetime(2026, 10, 5, tzinfo=ET)
NOW = DAY.replace(hour=12)
BAR_AT = DAY.replace(hour=11, minute=50)      # the last closed bar the alignment reads
ALIGN_NOW = DAY.replace(hour=11, minute=54)


def _frame(ema_fast, ema_slow, close, vwap, hist, at=BAR_AT):
    return pd.DataFrame([{EF: ema_fast, ES: ema_slow, "close": close, "vwap": vwap, HIST: hist}],
                        index=pd.DatetimeIndex([at]))


def _align(direction, frames, now=ALIGN_NOW):
    return al.short_term_alignment("NVDA", direction, frames=frames, now=now)


BULL = _frame(101, 100, 102, 101, 0.3)
BEAR = _frame(99, 100, 98, 99, -0.2)


class ShortTermAlignment(unittest.TestCase):
    def test_2m_and_5m_bullish_aligns_long_not_short(self):
        fr = {2: BULL, 5: BULL, 15: BULL}
        self.assertTrue(_align("long", fr)["aligned"])
        self.assertFalse(_align("short", fr)["aligned"])

    def test_all_bearish_aligns_short(self):
        self.assertTrue(_align("short", {2: BEAR, 5: BEAR, 15: BEAR})["aligned"])

    def test_15m_is_information_only(self):
        out = _align("long", {2: BULL, 5: BULL, 15: BEAR})
        self.assertTrue(out["aligned"])
        self.assertEqual({k: v for k, v in out["info"].items() if k.endswith("15")},
                         {"ema15": False, "vwap15": False, "macd_fast15": False})

    def test_2m_structure_disagreeing_blocks(self):
        out = _align("long", {2: _frame(99, 100, 102, 101, 0.3), 5: BULL, 15: BULL})
        self.assertFalse(out["aligned"])
        self.assertIn("ema2", out["reason"])

    def test_macd_is_logged_not_blocking_by_default(self):
        # A with-trend fade buys a dip: 2m/5m MACD-fast is on the wrong side by construction (board 2026-10-04, option B).
        fr = {2: _frame(101, 100, 102, 101, -0.1), 5: _frame(101, 100, 102, 101, -0.2), 15: BULL}
        out = _align("long", fr)
        self.assertTrue(out["aligned"])
        self.assertEqual(set(out["checks"]), {"ema2", "vwap2", "ema5", "vwap5"})
        self.assertFalse(out["info"]["macd_fast2"])
        self.assertFalse(out["info"]["macd_fast5"])

    def test_macd_blocks_when_configured(self):
        fr = {2: _frame(101, 100, 102, 101, -0.1), 5: BULL, 15: BULL}
        with mock.patch.object(config, "DAYTRADE_ALIGN_BLOCKING_CHECKS", ("ema", "vwap", "macd_fast")):
            out = _align("long", fr)
        self.assertFalse(out["aligned"])
        self.assertIn("macd_fast2", out["reason"])

    def test_empty_blocking_set_fails_closed(self):
        with mock.patch.object(config, "DAYTRADE_ALIGN_BLOCKING_CHECKS", ()):
            self.assertFalse(_align("long", {2: BULL, 5: BULL, 15: BULL})["aligned"])

    def test_5m_disagreeing_blocks(self):
        out = _align("long", {2: BULL, 5: _frame(101, 100, 100, 101, 0.3), 15: BULL})
        self.assertFalse(out["aligned"])
        self.assertIn("vwap5", out["reason"])

    def test_prior_session_bars_at_the_open_fail_closed(self):
        # 09:31 Monday: the newest closed 2m/5m bars are Friday's close — never "aligned" on them (cold-2nd).
        fri = DAY - timedelta(days=3)
        fr = {2: _frame(101, 100, 102, 101, 0.3, at=fri.replace(hour=15, minute=58)),
              5: _frame(101, 100, 102, 101, 0.3, at=fri.replace(hour=15, minute=55)), 15: BULL}
        out = _align("long", fr, now=DAY.replace(hour=9, minute=31, second=30))
        self.assertFalse(out["aligned"])
        self.assertIn("today", out["reason"])

    def test_stale_2m_bar_today_fails_closed(self):
        fr = {2: _frame(101, 100, 102, 101, 0.3, at=DAY.replace(hour=11, minute=30)), 5: BULL, 15: BULL}
        self.assertFalse(_align("long", fr)["aligned"])   # 2m bar ended 11:32, 22 min before 11:54

    def test_stale_5m_bar_today_fails_closed(self):
        fr = {2: BULL, 5: _frame(101, 100, 102, 101, 0.3, at=DAY.replace(hour=11, minute=35)), 15: BULL}
        self.assertFalse(_align("long", fr)["aligned"])   # 5m bar ended 11:40, 14 min before 11:54

    def test_missing_indicator_fails_closed(self):
        out = _align("long", {2: _frame(101, 100, 102, None, 0.3), 5: BULL, 15: BULL})
        self.assertFalse(out["aligned"])

    def test_no_frames_fails_closed(self):
        with mock.patch.object(al, "load_frames", return_value=None):
            out = al.short_term_alignment("NVDA", "long")
        self.assertFalse(out["aligned"])
        self.assertIn("insufficient", out["reason"])

    def test_fetch_error_fails_closed(self):
        with mock.patch.object(al, "load_frames", side_effect=RuntimeError("api down")):
            self.assertFalse(al.short_term_alignment("NVDA", "long")["aligned"])

    def test_bad_direction(self):
        self.assertFalse(al.short_term_alignment("NVDA", "up")["aligned"])


# ── trend failure ──────────────────────────────────────────────────────────────────────────────────────


def _f15(rows):
    """rows: (high, low, close, ema13) for today's 15m bars from 09:30."""
    idx = [DAY.replace(hour=9, minute=30) + timedelta(minutes=15 * i) for i in range(len(rows))]
    return pd.DataFrame([{"high": h, "low": lo, "close": c, EF: e, ES: e, "vwap": c, HIST: 0.0, "atr": 1.0}
                         for h, lo, c, e in rows], index=pd.DatetimeIndex(idx))


def _bear_b2(df):
    """Make the last 15m bar bearish on EMA13<EMA30, close<VWAP and MACD-fast (the 15m lead, B2)."""
    df = df.copy()
    last = df.index[-1]
    df.loc[last, EF], df.loc[last, ES] = 101.5, 102.0
    df.loc[last, "vwap"], df.loc[last, HIST] = 102.0, -0.2
    return df


# An up-trend that FAILED: rally to 105.5 (bar 5), then a lower high and a close through the last higher low.
FAILED_UPTREND = [(100.5, 99.5, 100.2, 99.0), (101.5, 100.0, 101.3, 100.0), (102.5, 101.0, 102.3, 101.0),
                  (103.5, 102.0, 103.2, 102.0), (104.5, 102.8, 104.3, 103.0), (105.5, 104.0, 104.5, 103.5),
                  (104.6, 102.5, 102.8, 103.8), (103.0, 100.5, 100.8, 103.0), (101.5, 99.5, 99.7, 102.5)]
# The same rally followed by a shallow BULL FLAG that holds well above its higher low.
BULL_FLAG = FAILED_UPTREND[:6] + [(105.2, 104.2, 104.4, 103.8), (104.8, 103.9, 104.1, 103.9), (104.6, 103.8, 104.0, 104.0)]


def _f30(rows):
    """rows: (close, low, high, ema13) for today's 30m bars from 09:30."""
    idx = [DAY.replace(hour=9, minute=30) + timedelta(minutes=30 * i) for i in range(len(rows))]
    return pd.DataFrame([{"close": c, "low": lo, "high": hi, EF: e} for c, lo, hi, e in rows],
                        index=pd.DatetimeIndex(idx))


BEAR_30 = _f30([(101.5, 99.6, 102.0, 100.5), (103.4, 101.0, 103.6, 101.5), (104.5, 102.5, 105.5, 102.5),
                (100.8, 100.4, 104.6, 103.0)])   # last 30m: close 100.8 < EMA13 103.0 and < prior low 102.5


def _mirror(df):
    """Reflect prices around 100 so an up-trend failure becomes a down-trend failure (for the long-fade mirror)."""
    out = df.copy()
    for col in ("close", EF, ES, "vwap"):
        if col in out:
            out[col] = 200 - df[col]
    if "high" in df and "low" in df:
        out["high"], out["low"] = 200 - df["low"], 200 - df["high"]
    if HIST in out:
        out[HIST] = -df[HIST]
    return out


class TrendFailure(unittest.TestCase):
    def _tf(self, f15, f30=BEAR_30, direction="short"):
        return al.trend_failure("NVDA", direction, frames={15: f15, 30: f30}, now=NOW)

    def test_failed_uptrend_allows_short_fade(self):
        out = self._tf(_bear_b2(_f15(FAILED_UPTREND)))
        self.assertTrue(out["failed"], out["reason"])
        self.assertAlmostEqual(out["levels"]["higher_low"], 100.0)
        self.assertGreaterEqual(out["levels"]["retrace"], 0.5)

    def test_bull_flag_is_not_a_failed_trend(self):
        out = self._tf(_bear_b2(_f15(BULL_FLAG)))
        self.assertFalse(out["failed"])
        self.assertFalse(out["checks"]["b3_body_break"])
        self.assertFalse(out["checks"]["b3_retrace"])

    def test_15m_must_lead(self):
        out = self._tf(_f15(FAILED_UPTREND))   # structure broke but 15m EMA/VWAP/MACD not bearish
        self.assertFalse(out["failed"])
        self.assertIn("b2_", out["reason"])

    def test_30m_must_confirm(self):
        no_ladder = _f30([(101.5, 99.6, 102.0, 100.5), (103.4, 101.0, 103.6, 101.5), (104.5, 102.5, 105.5, 102.5),
                          (103.8, 103.0, 104.6, 103.0)])   # last 30m close above its EMA13 and the prior low
        out = self._tf(_bear_b2(_f15(FAILED_UPTREND)), f30=no_ladder)
        self.assertFalse(out["failed"])
        self.assertFalse(out["checks"]["b4_close_vs_ema13_30"])
        self.assertFalse(out["checks"]["b4_beyond_prior_30"])

    def test_30m_close_below_ema_but_above_prior_low_is_not_enough(self):
        half = _f30([(101.5, 99.6, 102.0, 100.5), (103.4, 101.0, 103.6, 101.5), (104.5, 102.5, 105.5, 102.5),
                     (102.7, 102.0, 104.6, 103.0)])
        out = self._tf(_bear_b2(_f15(FAILED_UPTREND)), f30=half)
        self.assertFalse(out["failed"])
        self.assertTrue(out["checks"]["b4_close_vs_ema13_30"])
        self.assertFalse(out["checks"]["b4_beyond_prior_30"])

    def test_too_early_before_six_session_bars(self):
        out = self._tf(_bear_b2(_f15(FAILED_UPTREND[:5])))
        self.assertFalse(out["failed"])
        self.assertIn("too early", out["reason"])

    def test_needs_two_closed_bars_after_the_high(self):
        rows = FAILED_UPTREND[:7] + [(105.6, 101.0, 99.7, 102.5)]   # the newest bar printed a new high
        out = self._tf(_bear_b2(_f15(rows)))
        self.assertFalse(out["failed"])
        self.assertFalse(out["checks"]["b3_bars_after_extreme"])

    def test_reclaiming_ema13_after_the_first_reaction_bar_is_a_flag(self):
        rows = list(FAILED_UPTREND)
        rows[7] = (103.0, 100.5, 100.8, 100.0)   # bar h+2 closed ABOVE its EMA13
        out = self._tf(_bear_b2(_f15(rows)))
        self.assertFalse(out["failed"])
        self.assertFalse(out["checks"]["b3_failed_bounce"])

    def test_bounce_back_near_the_high_is_a_flag(self):
        rows = list(FAILED_UPTREND)
        rows[6] = (105.4, 102.5, 102.8, 103.8)   # re-tested within 0.25 x ATR of the 105.5 high
        out = self._tf(_bear_b2(_f15(rows)))
        self.assertFalse(out["failed"])
        self.assertFalse(out["checks"]["b3_failed_bounce"])

    # ── boundaries (cold-2nd mutation survivors) ──
    def test_exactly_one_bar_after_the_high_is_not_enough(self):
        rows = list(FAILED_UPTREND)
        rows[7] = (105.7, 100.5, 100.8, 103.0)   # new high on bar 7 -> only bar 8 after it
        out = self._tf(_bear_b2(_f15(rows)))
        self.assertFalse(out["checks"]["b3_bars_after_extreme"])
        self.assertFalse(out["failed"])

    def test_first_reaction_bar_may_close_above_ema13(self):
        rows = list(FAILED_UPTREND)
        rows[6] = (104.6, 102.5, 104.3, 103.8)   # bar h+1 closes above its EMA13 — allowed by the rule
        out = self._tf(_bear_b2(_f15(rows)))
        self.assertTrue(out["checks"]["b3_failed_bounce"])
        self.assertTrue(out["failed"], out["reason"])

    def test_close_just_under_the_higher_low_is_not_a_body_break(self):
        rows = FAILED_UPTREND[:8] + [(101.5, 99.5, 99.95, 102.5)]   # HL 100.0; needs < 99.90
        out = self._tf(_bear_b2(_f15(rows)))
        self.assertFalse(out["checks"]["b3_body_break"])
        self.assertFalse(out["failed"])

    def _retrace_rows(self, last_close):
        # S = 99.5 (bar 0), HL = 103.4 (bars 1-4), H = 105.5 (bar 5): leg 6.0
        return [(100.5, 99.5, 100.2, 99.0), (104.0, 103.4, 103.8, 100.0), (104.5, 103.6, 104.2, 101.0),
                (104.8, 103.8, 104.5, 102.0), (105.0, 104.0, 104.8, 103.0), (105.5, 104.5, 105.0, 103.5),
                (104.6, 103.0, 103.2, 103.8), (103.8, 102.6, 102.9, 103.6), (103.0, 102.4, last_close, 103.4)]

    def test_retrace_of_exactly_half_qualifies(self):
        out = self._tf(_f15(self._retrace_rows(102.5)))     # (105.5-102.5)/6 = 0.50
        self.assertTrue(out["checks"]["b3_body_break"])
        self.assertTrue(out["checks"]["b3_retrace"])

    def test_retrace_under_half_is_a_flag(self):
        out = self._tf(_f15(self._retrace_rows(102.8)))     # (105.5-102.8)/6 = 0.45
        self.assertTrue(out["checks"]["b3_body_break"])
        self.assertFalse(out["checks"]["b3_retrace"])

    def test_extreme_must_be_todays_session_extreme(self):
        prefix = [(107.0, 100.0, 100.3, 99.0)] + [(100.4, 99.6, 100.1, 99.0)] * 7   # 17 bars; window = last 16
        out = self._tf(_bear_b2(_f15(prefix + FAILED_UPTREND)))
        self.assertFalse(out["checks"]["b3_extreme_is_today"])
        self.assertFalse(out["failed"])
        near = [(105.55, 100.0, 100.3, 99.0)] + [(100.4, 99.6, 100.1, 99.0)] * 7   # within 0.10 x ATR
        self.assertTrue(self._tf(_bear_b2(_f15(near + FAILED_UPTREND)))["checks"]["b3_extreme_is_today"])

    def test_long_fade_is_the_exact_mirror(self):
        out = self._tf(_mirror(_bear_b2(_f15(FAILED_UPTREND))), f30=_mirror(BEAR_30), direction="long")
        self.assertTrue(out["failed"], out["reason"])
        flag = self._tf(_mirror(_bear_b2(_f15(BULL_FLAG))), f30=_mirror(BEAR_30), direction="long")
        self.assertFalse(flag["failed"])

    def test_wrong_side_fade_fails(self):
        self.assertFalse(self._tf(_bear_b2(_f15(FAILED_UPTREND)), direction="long")["failed"])

    def test_stale_frames_from_another_day_fail_closed(self):
        out = al.trend_failure("NVDA", "short", frames={15: _bear_b2(_f15(FAILED_UPTREND)), 30: BEAR_30},
                               now=NOW + timedelta(days=1))
        self.assertFalse(out["failed"])

    def test_no_frames_or_error_fail_closed(self):
        with mock.patch.object(al, "load_frames", return_value=None):
            self.assertFalse(al.trend_failure("NVDA", "short", now=NOW)["failed"])
        with mock.patch.object(al, "load_frames", side_effect=RuntimeError("boom")):
            self.assertFalse(al.trend_failure("NVDA", "short", now=NOW)["failed"])
        self.assertFalse(al.trend_failure("NVDA", "sideways", now=NOW)["failed"])


# ── closed-bar resampling + load_frames ────────────────────────────────────────────────────────────────
def _m1_days(days, end_minute_today=None, ext_hours=True):
    """UTC-indexed 1m bars over `days` calendar days ending DAY (weekdays only); today's bars stop at
    end_minute_today (ET) if given. Includes pre/post-market bars when ext_hours (they must be dropped)."""
    rows, idx = [], []
    px = 100.0
    for k in range(days - 1, -1, -1):
        d = DAY - timedelta(days=k)
        if d.weekday() >= 5:
            continue
        start = d.replace(hour=8, minute=0) if ext_hours else d.replace(hour=9, minute=30)
        end = d.replace(hour=17, minute=0) if ext_hours else d.replace(hour=16, minute=0)
        if k == 0 and end_minute_today is not None:
            end = end_minute_today + timedelta(minutes=1)
        t = start
        while t < end:
            px += 0.01 * math.sin(t.minute)
            rows.append({"open": px, "high": px + 0.05, "low": px - 0.05, "close": px, "volume": 100.0})
            idx.append(t.astimezone(ZoneInfo("UTC")))
            t += timedelta(minutes=1)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


class Resample(unittest.TestCase):
    def test_rth_only_and_buckets_anchor_at_0930(self):
        m1 = al._rth_1m(_m1_days(1, end_minute_today=DAY.replace(hour=10, minute=0)))
        self.assertEqual(m1.index[0], DAY.replace(hour=9, minute=30))
        out = al._resample_closed(m1, 2, DAY.replace(hour=10, minute=1, second=30))
        self.assertEqual(out.index[0], DAY.replace(hour=9, minute=30))
        self.assertEqual(out.index[1], DAY.replace(hour=9, minute=32))

    def test_forming_bucket_is_dropped(self):
        m1 = al._rth_1m(_m1_days(1, end_minute_today=DAY.replace(hour=10, minute=0)))   # last 1m bar 10:00
        out = al._resample_closed(m1, 2, DAY.replace(hour=10, minute=1, second=30))
        self.assertEqual(out.index[-1], DAY.replace(hour=9, minute=58))   # the 10:00 bucket has not ended
        out15 = al._resample_closed(m1, 15, DAY.replace(hour=10, minute=1, second=30))
        self.assertEqual(out15.index[-1], DAY.replace(hour=9, minute=45))  # 09:45 bucket ended at 10:00

    def test_bucket_ends_only_count_when_data_reaches_them(self):
        m1 = al._rth_1m(_m1_days(1, end_minute_today=DAY.replace(hour=9, minute=58)))   # data stops 09:59
        out = al._resample_closed(m1, 2, DAY.replace(hour=10, minute=5))
        self.assertEqual(out.index[-1], DAY.replace(hour=9, minute=56))   # 09:58 bucket lacks 09:59's data end
        out5 = al._resample_closed(m1, 5, DAY.replace(hour=10, minute=5))
        self.assertEqual(out5.index[-1], DAY.replace(hour=9, minute=50))   # 09:55 bucket lacks its last minute

    def test_load_frames_full_history(self):
        now = DAY.replace(hour=12, minute=1)
        raw = _m1_days(8, end_minute_today=DAY.replace(hour=12, minute=0))
        al._frames_cache.clear()
        with mock.patch.object(al, "_fetch_1m", return_value=raw):
            fr = al.load_frames("NVDA", now)
        self.assertIsNotNone(fr)
        self.assertEqual(sorted(fr), [2, 5, 15, 30])
        self.assertEqual(fr[30].index[-1], DAY.replace(hour=11, minute=30))
        f2 = fr[2]
        first_today = f2[f2.index.date == DAY.date()].iloc[0]
        tp = (first_today["high"] + first_today["low"] + first_today["close"]) / 3
        self.assertAlmostEqual(first_today["vwap"], tp)   # VWAP resets at today's 09:30 ET

    def test_load_frames_stale_data_fails_closed(self):
        raw = _m1_days(8, end_minute_today=DAY.replace(hour=12, minute=0))
        al._frames_cache.clear()
        with mock.patch.object(al, "_fetch_1m", return_value=raw):
            self.assertIsNone(al.load_frames("NVDA", DAY.replace(hour=12, minute=30)))

    def test_load_frames_empty_or_short_history_fails_closed(self):
        al._frames_cache.clear()
        with mock.patch.object(al, "_fetch_1m", return_value=pd.DataFrame()):
            self.assertIsNone(al.load_frames("NVDA", DAY.replace(hour=12)))
        al._frames_cache.clear()
        with mock.patch.object(al, "_fetch_1m", return_value=_m1_days(1, end_minute_today=DAY.replace(hour=11))):
            self.assertIsNone(al.load_frames("NVDA", DAY.replace(hour=11, minute=1)))   # < 40 30m bars

    def test_one_fetch_per_symbol_per_minute(self):
        raw = _m1_days(8, end_minute_today=DAY.replace(hour=12, minute=0))
        al._frames_cache.clear()
        with mock.patch.object(al, "_fetch_1m", return_value=raw) as f:
            al.load_frames("NVDA", DAY.replace(hour=12, minute=1, second=5))
            al.load_frames("NVDA", DAY.replace(hour=12, minute=1, second=40))
        self.assertEqual(f.call_count, 1)


# ── counter-trend fade reversal criterion ──────────────────────────────────────────────────────────────
def _ct_trade(tid, pnl, ts):
    return [{"event": "decision", "trade_id": tid, "trigger": {"counter_trend": True}},
            {"event": "exit_fill", "trade_id": tid, "realized_pnl": pnl, "ts": ts}]


class CounterTrendReversal(unittest.TestCase):
    def test_no_history_is_allowed(self):
        self.assertTrue(al.counter_trend_fades_ok([])[0])

    def test_cumulative_loss_disables(self):
        ev = _ct_trade("a", -20.0, "2026-10-05T10:00") + _ct_trade("b", -6.0, "2026-10-05T11:00")
        ok, why = al.counter_trend_fades_ok(ev)
        self.assertFalse(ok)
        self.assertIn("cumulative", why)

    def test_three_consecutive_losses_disable(self):
        ev = (_ct_trade("a", 5.0, "2026-10-05T09:00") + _ct_trade("b", -1.0, "2026-10-05T10:00")
              + _ct_trade("c", -1.0, "2026-10-05T11:00") + _ct_trade("d", -1.0, "2026-10-05T12:00"))
        ok, why = al.counter_trend_fades_ok(ev)
        self.assertFalse(ok)
        self.assertIn("3 consecutive", why)

    def test_a_win_resets_the_streak(self):
        ev = (_ct_trade("a", -1.0, "2026-10-05T09:00") + _ct_trade("b", -1.0, "2026-10-05T10:00")
              + _ct_trade("c", 2.0, "2026-10-05T11:00"))
        self.assertTrue(al.counter_trend_fades_ok(ev)[0])

    def test_partial_exit_loss_counts(self):
        # 8 sh closed at -$20 as a partial, the last 2 sh flat: the trade is a -$20 loss, not $0.
        ev = [{"event": "decision", "trade_id": "p", "trigger": {"counter_trend": True}},
              {"event": "partial_exit_fill", "trade_id": "p", "realized_pnl": -20.0, "ts": "2026-10-05T10:00"},
              {"event": "exit_fill", "trade_id": "p", "realized_pnl": 0.0, "ts": "2026-10-05T10:05"}]
        ok, why = al.counter_trend_fades_ok(ev + _ct_trade("q", -6.0, "2026-10-05T11:00"))
        self.assertFalse(ok)
        self.assertIn("cumulative", why)

    def test_partial_loss_then_small_residual_win_is_a_loss_in_streak(self):
        def big_partial(tid, ts):
            return [{"event": "decision", "trade_id": tid, "trigger": {"counter_trend": True}},
                    {"event": "partial_exit_fill", "trade_id": tid, "realized_pnl": -3.0, "ts": ts},
                    {"event": "exit_fill", "trade_id": tid, "realized_pnl": 0.5, "ts": ts + ":30"}]
        ev = big_partial("a", "2026-10-05T09:00") + big_partial("b", "2026-10-05T10:00") + big_partial("c", "2026-10-05T11:00")
        ok, why = al.counter_trend_fades_ok(ev)
        self.assertFalse(ok)
        self.assertIn("3 consecutive", why)

    def test_open_trade_partial_counts_toward_cumulative_not_streak(self):
        ev = [{"event": "decision", "trade_id": "o", "trigger": {"counter_trend": True}},
              {"event": "partial_exit_fill", "trade_id": "o", "realized_pnl": -30.0, "ts": "2026-10-05T10:00"}]
        ok, why = al.counter_trend_fades_ok(ev)
        self.assertFalse(ok)
        self.assertIn("cumulative", why)

    def test_join_through_entry_fill_when_trade_ids_differ(self):
        # Live logs: the decision record's trade_id differs from the broker-tagged entry/exit trade_id; the
        # entry_fill carries the decision_id (verified on all 19 live trades 2026-10-04).
        ev = [{"event": "decision", "decision_id": "CT-NVDA-1", "trade_id": "", "trigger": {"counter_trend": True}},
              {"event": "decision", "decision_id": "CT-NVDA-1", "trade_id": "DT-NVDA-s-1", "trigger": {"counter_trend": True}},
              {"event": "entry_fill", "trade_id": "DT-NVDA-s-9-daytrade", "decision_id": "CT-NVDA-1"},
              {"event": "exit_fill", "trade_id": "DT-NVDA-s-9-daytrade", "realized_pnl": -30.0, "ts": "2026-10-05T12:00"}]
        ok, why = al.counter_trend_fades_ok(ev)
        self.assertFalse(ok)
        self.assertIn("cumulative", why)

    def test_with_trend_trades_do_not_count(self):
        ev = [{"event": "decision", "trade_id": "w", "trigger": {"counter_trend": False}},
              {"event": "exit_fill", "trade_id": "w", "realized_pnl": -100.0, "ts": "2026-10-05T10:00"}]
        self.assertTrue(al.counter_trend_fades_ok(ev)[0])

    def test_kill_flag_off_disables(self):
        with mock.patch.object(config, "DAYTRADE_COUNTER_TREND_FADES_ENABLED", False, create=True):
            self.assertFalse(al.counter_trend_fades_ok([])[0])

    def test_unreadable_log_fails_closed(self):
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], False)):
            self.assertFalse(al.counter_trend_fades_ok()[0])


if __name__ == "__main__":
    unittest.main()
