# ruff: noqa: E501
"""MORNING-1029 — scripts/morning_watch.py machine tests (spec §9a-8 (a)–(n), v9 items 4–6, v9.2, v9.3, v8 weekly).

A fake clock drives the watcher; IEX bars, option snapshots, the calendar and Slack are all mocked; a guard fails the
test on any unmocked network connection. Fixture: SPY CALL trigger 101 / target 102 / invalidation 99, t_max 10 min.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import scripts.morning_rundown as mr  # noqa: E402
import scripts.morning_watch as mw  # noqa: E402

UTC = timezone.utc
DAY = date(2026, 11, 9)
BASE = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)     # 10:00 ET / 07:00 PT
CLOSE = datetime(2026, 11, 9, 21, 0, tzinfo=UTC)    # 16:00 ET
M = timedelta(minutes=1)
C101, C102 = "SPY261109C00101000", "SPY261109C00102000"


def _no_network(*_a, **_k):
    raise AssertionError("unmocked network call in a test")


def _plan(cutoff=BASE - M, t_max=10.0, b_high=0.0, b_low=0.0, put_watch=False):
    return {"ts_utc": "2026-11-09T14:46:00Z", "date": DAY.isoformat(), "session_close": CLOSE.isoformat(),
            "symbols": {"SPY": {"stats": {"t_max_min": t_max, "b_high": b_high, "b_low": b_low, "merge_distance": 0.1},
                                "iex_cutoff_bar_ts": mr.iso_z(cutoff),
                                "sides": {"CALL": {"state_0646": "WATCHING", "watch": True, "trigger": 101.0, "target": 102.0,
                                                   "invalidation": 99.0, "rec_id": "2026-11-09-SPY-CALL",
                                                   "candidates": [{"occ": C101, "strike": 101.0, "is_call": True},
                                                                  {"occ": C102, "strike": 102.0, "is_call": True}]},
                                          "PUT": {"state_0646": "WATCHING", "watch": put_watch, "trigger": 99.0, "target": 98.0,
                                                  "invalidation": 101.0, "rec_id": "2026-11-09-SPY-PUT",
                                                  "candidates": [{"occ": "SPY261109P00099000", "strike": 99.0, "is_call": False}]}}}}}


class Market:
    def __init__(self, clock):
        self.clock = clock
        self.bars: dict[datetime, tuple] = {}
        self.missing: set[datetime] = set()
        self.quotes = {C101: (0.90, 0.95), C102: (0.35, 0.40), "SPY261109P00099000": (0.30, 0.35)}
        self.quote_fail_until: datetime | None = None
        self.snapshot_calls: list[datetime] = []
        self.bid_after_calls: tuple[int, str, float] | None = None
        self.recheck = None          # callable(now) -> quote dict | raises; used for single-contract (re-check) calls
        self.lag_seconds = 0.0       # publication lag of a completed bar
        self.empty_until: datetime | None = None

    def get_bars(self, symbol, start, end, feed):
        rows = []
        t = start.replace(second=0, microsecond=0)
        while t < end:
            if self.empty_until is not None and self.clock[0] < self.empty_until:
                break
            if t + M + timedelta(seconds=self.lag_seconds) <= self.clock[0] and t not in self.missing:
                o, h, lo, c = self.bars.get(t, (100.0, 100.1, 99.9, 100.0))
                rows.append((t, o, h, lo, c))
            t += M
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows],
                             "close": [r[4] for r in rows], "volume": [100.0] * len(rows)},
                            index=pd.DatetimeIndex([r[0] for r in rows]))

    def snapshots(self, symbols):
        now = self.clock[0]
        self.snapshot_calls.append(now)
        if self.quote_fail_until is not None and now < self.quote_fail_until:
            return {}
        if self.recheck is not None and len(symbols) == 1:
            q = self.recheck(now)
            return {symbols[0]: {"latestQuote": q}} if q is not None else {}
        out = {}
        for s in symbols:
            if s not in self.quotes:
                continue
            bid, ask = self.quotes[s]
            if self.bid_after_calls and len(self.snapshot_calls) > self.bid_after_calls[0] and s == self.bid_after_calls[1]:
                bid = self.bid_after_calls[2]
            out[s] = {"latestQuote": {"bp": bid, "ap": ask, "bs": 7, "as": 9, "bx": "C", "ax": "C", "c": "A",
                                      "t": now.isoformat().replace("+00:00", "Z")}}
        return out


class Base(unittest.TestCase):
    deliver_ok = True

    def _deliver(self, text):
        """send_slack_delivery mock: records the text only when delivery 'succeeds' (deliver_ok)."""
        if self.deliver_ok:
            self.sent.append(text)
        return self.deliver_ok

    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection"):
            p = mock.patch(target, side_effect=_no_network)
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        logs = Path(self.tmp.name)
        self.logs = logs
        state = {"recs": logs / "recs.jsonl", "recommendations": logs / "0dte.jsonl", "heartbeat": logs / "hb.json",
                 "chain_dir": logs / "chain"}
        self.clock = [BASE]
        self.mkt = Market(self.clock)
        self.sent: list[str] = []

        def blocks(bl, fb):
            self.sent.append(bl[0]["text"]["text"])
            return True
        for p in (mock.patch.object(mr, "_LOGS", logs), mock.patch.object(mw, "_LOGS", logs),
                  mock.patch.dict(mr.STATE, state), mock.patch.object(mw, "EVENTS", logs / "events.jsonl"),
                  mock.patch.object(mr, "get_bars", side_effect=self.mkt.get_bars),
                  mock.patch.object(mr, "fetch_snapshots", side_effect=self.mkt.snapshots),
                  mock.patch.object(mr, "model_premium", side_effect=lambda S, K, T, iv, c: self.model),
                  mock.patch.object(mw, "send_slack_blocks", side_effect=blocks),
                  mock.patch.object(mw, "send_slack", side_effect=lambda t: self.sent.append(t)),
                  mock.patch.object(mw, "send_slack_delivery", side_effect=self._deliver)):
            p.start()
            self.addCleanup(p.stop)
        self.model = 0.40

    def now(self):
        return self.clock[0]

    def sleep(self, sec):
        self.clock[0] = self.clock[0] + timedelta(seconds=sec)

    def watch(self, minutes=20, plan=None, state=None):
        plan = plan or _plan()
        st = state or mw.init_state(plan, DAY)
        w = mw.Watcher(plan, st, DAY, BASE + timedelta(minutes=minutes), now_fn=self.now, sleep_fn=self.sleep)
        w.run_loop()
        return w

    def triggered(self):
        return [t for t in self.sent if "TRIGGERED" in t and "summary" not in t]


class TestWatcherBreaks(Base):
    def test_a_wick_through_trigger_closing_inside_is_no_alert(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.5, 100.4, 100.9)
        w = self.watch()
        self.assertEqual(self.triggered(), [])
        self.assertEqual(w.state["symbols"]["SPY"]["sides"]["CALL"]["status"], "WATCHING")
        self.assertIn("WATCHING-expired", self.sent[-1])
        self.assertIn("Morning watch summary", self.sent[-1])

    def test_b_close_beyond_rebuilds_from_fresh_quotes(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.mkt.quotes[C101] = (0.90, 0.97)
        w = self.watch()
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertIn("TRIGGERED — ACTIONABLE", trig[0])
        self.assertIn("Limit $0.97", trig[0])
        self.assertIn("sell-stop 101C at $0.40", trig[0])
        self.assertIn("Confirm the stop is still below the bid", trig[0])
        self.assertTrue(self.mkt.snapshot_calls)
        self.assertGreaterEqual(min(self.mkt.snapshot_calls), BASE + 3 * M)   # every quote fetch AFTER the break bar closed
        s = w.state["symbols"]["SPY"]["sides"]["CALL"]
        self.assertEqual((s["status"], s["alert_state"], s["contract"]), ("TRIGGERED", "ACTIONABLE", C101))
        rows = [json.loads(x) for x in (self.logs / "0dte.jsonl").read_text().splitlines()]
        t = [r for r in rows if r["event"] == "triggered"][0]
        self.assertEqual((t["limit"], t["stop"], t["stop_check_bid"]), (0.97, 0.40, 0.90))
        self.assertIn("bid_depth_at_stop", t)
        self.assertIn("r", t)
        self.assertTrue((self.logs / "morning_watch_quotes_2026-11-09.jsonl").exists())   # per-minute archive

    def test_c_fresh_ask_over_cap(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.mkt.quotes = {C101: (1.35, 1.40), C102: (1.30, 1.36)}
        self.watch()
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertIn("TRIGGERED — NO ACTIONABLE CONTRACT UNDER $1.25", trig[0])
        self.assertNotRegex(trig[0], r"Limit \$|sell-stop")

    def test_d_target_before_break_stale_invalidation_before_break_void(self):
        self.mkt.bars[BASE + M] = (100.5, 102.05, 100.4, 100.5)
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        w = self.watch()
        self.assertEqual(self.triggered(), [])
        self.assertEqual(w.state["symbols"]["SPY"]["sides"]["CALL"]["status"], "STALE")
        self.sent.clear()
        self.clock[0] = BASE
        self.mkt.bars = {BASE + M: (99.5, 99.8, 98.95, 99.5), BASE + 2 * M: (100.5, 101.3, 100.4, 101.2)}
        w = self.watch()
        self.assertEqual(self.triggered(), [])
        self.assertEqual(w.state["symbols"]["SPY"]["sides"]["CALL"]["status"], "VOID")

    def test_e_restart_after_alert_no_second_alert(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.watch(minutes=6)
        self.assertEqual(len(self.triggered()), 1)
        st = mr.read_json(mw.state_path(DAY))
        st["summary_sent"] = False
        self.watch(minutes=12, state=st)
        self.assertEqual(len(self.triggered()), 1)

    def test_h_gap_backfill_invalidation_then_trigger_close_void(self):
        self.clock[0] = BASE + 4 * M
        self.mkt.bars = {BASE: (100.0, 100.1, 98.80, 99.5), BASE + 2 * M: (100.5, 101.3, 100.4, 101.20)}
        w = self.watch()
        self.assertEqual(self.triggered(), [])
        self.assertEqual(w.state["symbols"]["SPY"]["sides"]["CALL"]["status"], "VOID")

    def test_i_missing_minute_voids_and_no_later_alert(self):
        self.clock[0] = BASE + 3 * M
        self.mkt.missing = {BASE + M}
        self.mkt.bars[BASE + 5 * M] = (100.5, 101.3, 100.4, 101.2)
        w = self.watch()
        self.assertEqual(self.triggered(), [])
        s = w.state["symbols"]["SPY"]["sides"]["CALL"]
        self.assertEqual((s["status"], s["void_reason"]), ("VOID", "watch history incomplete"))
        self.assertEqual(sum(t.startswith("⚠️") and "watch history incomplete" in t for t in self.sent), 1)

    def test_j_trigger_inside_restart_gap_older_than_t_max_stale(self):
        self.clock[0] = BASE + 15 * M
        self.mkt.bars[BASE + M] = (100.5, 101.3, 100.4, 101.2)
        self.watch(minutes=25)
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertIn("TRIGGERED — STALE, no entry", trig[0])
        self.assertNotRegex(trig[0], r"Limit \$|sell-stop")

    def test_k_quote_retry_succeeds_before_t_max(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.mkt.quote_fail_until = BASE + 3 * M + timedelta(seconds=150)
        self.watch()
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertIn("TRIGGERED — ACTIONABLE", trig[0])

    def test_k_quote_fails_until_t_max(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.mkt.quote_fail_until = BASE + timedelta(hours=2)
        self.watch(minutes=30)
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertIn("TRIGGERED — NO CURRENT EXECUTABLE QUOTE", trig[0])

    def _l(self, bar_1001):
        plan = _plan(cutoff=BASE - 2 * M)
        self.mkt.bars[BASE - M] = (100.5, 101.3, 100.4, 101.2)            # 09:59 bar closes 101.2 at 10:00
        self.mkt.bars[BASE + M] = bar_1001                                 # 10:01 bar (completes 10:02)
        self.mkt.quote_fail_until = BASE + 2 * M + timedelta(seconds=30)   # quotes back at 10:02:30
        self.watch(plan=plan)
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertIn("TRIGGERED — STALE, no entry", trig[0])
        self.assertNotIn("ACTIONABLE", trig[0])

    def test_l_invalidation_during_quote_retry(self):
        self._l((100.0, 100.2, 98.9, 99.5))

    def test_l_target_during_quote_retry(self):
        self._l((101.0, 102.1, 100.9, 101.5))

    def test_g_after_cutoff_exits_without_alerting(self):
        cal = [{"date": DAY, "open": BASE, "close": CLOSE}]
        late = datetime(2026, 11, 9, 20, 31, tzinfo=UTC)   # 15:31 ET
        with mock.patch.object(mr, "calendar", return_value=cal):
            rc = mw.run_watch(now_fn=lambda: late, sleep_fn=self.sleep, lock_path=self.logs / "w.lock")
        self.assertEqual(rc, 0)
        self.assertEqual(self.sent, [])

    def test_early_close_day_ends_at_close_minus_cutoff(self):
        early = [{"date": DAY, "open": BASE, "close": datetime(2026, 11, 9, 18, 0, tzinfo=UTC)}]   # 13:00 ET close
        at = datetime(2026, 11, 9, 17, 31, tzinfo=UTC)                                             # 12:31 ET
        with mock.patch.object(mr, "calendar", return_value=early):
            self.assertEqual(mw.run_watch(now_fn=lambda: at, sleep_fn=self.sleep, lock_path=self.logs / "w.lock"), 0)
        self.assertEqual(self.sent, [])

    def test_singleton_lock(self):
        lk = mw.acquire_lock(self.logs / "w.lock")
        self.assertIsNotNone(lk)
        self.assertIsNone(mw.acquire_lock(self.logs / "w.lock"))
        lk.close()


class TestOutcomeAndStops(Base):
    def test_v92_one_terminal_outcome(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.mkt.bars[BASE + 5 * M] = (100.0, 100.2, 98.9, 99.2)      # invalidation -> STOP
        self.mkt.bars[BASE + 9 * M] = (101.0, 102.2, 101.0, 102.0)    # later target touch -> nothing
        w = self.watch(minutes=15)
        exits = [t for t in self.sent if "invalidation printed — exit" in t]
        self.assertEqual(len(exits), 1)
        self.assertIn("ts_utc 2026-11-09T15:05:00Z", exits[0])
        self.assertFalse(any("target printed" in t for t in self.sent))
        self.assertEqual(w.state["symbols"]["SPY"]["sides"]["CALL"]["outcome"], "STOP")

    def test_v92_same_bar_both_is_stop(self):
        s = {"side": "CALL", "status": "TRIGGERED", "outcome": None, "trigger": 101, "target": 102, "invalidation": 99}
        mw.step(s, {"ts": "x", "open": 100, "high": 102.5, "low": 98.5, "close": 100}, {"b_high": 0, "b_low": 0})
        self.assertEqual(s["outcome"], "STOP")

    def test_v93_stop_above_bid_reference_only(self):
        self.model = 1.00
        self.mkt.quotes = {C101: (0.80, 1.20), C102: (1.25, 1.30)}
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.watch()
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertIn("TRIGGERED — REFERENCE ONLY — no placeable stop", trig[0])
        self.assertNotRegex(trig[0], r"Limit \$|sell-stop")

    def test_v93_stop_below_bid_eligible(self):
        self.model = 0.79
        self.mkt.quotes = {C101: (0.80, 1.20), C102: (1.25, 1.30)}
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.watch()
        trig = self.triggered()
        self.assertIn("TRIGGERED — ACTIONABLE", trig[0])
        self.assertIn("sell-stop 101C at $0.79", trig[0])
        self.assertIn("If the 101C bid is at or below $0.79 when you are filled", trig[0])

    def test_v93_bid_falls_below_stop_before_send(self):
        self.model = 0.79
        self.mkt.quotes = {C101: (0.80, 1.20), C102: (1.25, 1.30)}
        self.mkt.bid_after_calls = (1, C101, 0.70)   # the pre-send re-check sees bid 0.70
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.watch()
        trig = self.triggered()
        self.assertIn("TRIGGERED — REFERENCE ONLY — no placeable stop", trig[0])
        self.assertNotRegex(trig[0], r"Limit \$|sell-stop")


class TestPreSendRecheck(Base):
    """T1: the pre-send re-check never lets a stale, one-sided or failed quote through as ACTIONABLE."""

    def setUp(self):
        super().setUp()
        self.model = 0.50
        self.mkt.quotes = {C101: (0.80, 1.20), C102: (1.25, 1.30)}
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)

    def _q(self, bid, ask):
        return lambda now: {"bp": bid, "ap": ask, "bs": 1, "as": 1, "bx": "C", "ax": "C", "c": "A",
                            "t": now.isoformat().replace("+00:00", "Z")}

    def test_zero_bid_recheck_not_actionable(self):
        self.mkt.recheck = self._q(0.0, 1.20)
        self.watch()
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertNotIn("ACTIONABLE", trig[0])
        self.assertIn("REFERENCE ONLY — no placeable stop", trig[0])
        self.assertNotRegex(trig[0], r"Limit \$|sell-stop")

    def test_locked_recheck_not_actionable(self):
        self.mkt.recheck = self._q(1.00, 1.00)
        self.watch()
        trig = self.triggered()
        self.assertIn("TRIGGERED — NO CURRENT EXECUTABLE QUOTE", trig[0])
        self.assertNotRegex(trig[0], r"Limit \$|sell-stop")

    def test_raising_recheck_not_actionable(self):
        def boom(now):
            raise RuntimeError("snapshot endpoint down")
        self.mkt.recheck = boom
        self.watch(minutes=30)
        trig = self.triggered()
        self.assertEqual(len(trig), 1)
        self.assertIn("TRIGGERED — NO CURRENT EXECUTABLE QUOTE", trig[0])
        self.assertNotIn("ACTIONABLE", trig[0])

    def test_raising_recheck_recovers_within_t_max(self):
        calls = []

        def flaky(now):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return self._q(0.80, 1.20)(now)
        self.mkt.recheck = flaky
        self.watch()
        self.assertIn("TRIGGERED — ACTIONABLE", self.triggered()[0])

    def test_recheck_ask_over_cap(self):
        self.mkt.recheck = self._q(1.20, 1.30)
        self.watch()
        trig = self.triggered()
        self.assertIn("TRIGGERED — NO ACTIONABLE CONTRACT UNDER $1.25", trig[0])
        self.assertNotRegex(trig[0], r"Limit \$|sell-stop")

    def test_recheck_retry_past_cutoff_not_actionable(self):
        """Cold review 11-08: a failed re-check fetch sleeps; if that sleep crosses the cut-off, no ACTIONABLE alert."""
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        plan = _plan()
        st = mw.init_state(plan, DAY)
        w = mw.Watcher(plan, st, DAY, BASE + 20 * M, now_fn=self.now, sleep_fn=self.sleep)
        calls = {"n": 0}

        def recheck(now):
            calls["n"] += 1
            if calls["n"] == 1:
                w.watch_end = now + timedelta(seconds=5)   # cut-off lands inside the 10 s retry sleep
                return None                                # missing snapshot -> 'error' -> retry
            return self._q(0.90, 0.97)(now)
        self.mkt.recheck = recheck
        w.run_loop()
        self.assertGreaterEqual(calls["n"], 2)
        self.assertFalse(any("ACTIONABLE" in t and "NO ACTIONABLE" not in t for t in self.triggered()))
        self.assertEqual(st["symbols"]["SPY"]["sides"]["CALL"].get("stale_reason"),
                         "past the broker's same-day order cut-off")

    def test_older_recheck_not_actionable(self):
        self.mkt.recheck = lambda now: self._q(0.80, 1.20)(now - timedelta(minutes=5))
        self.watch()
        self.assertIn("NO CURRENT EXECUTABLE QUOTE", self.triggered()[0])


class TestCoverage(Base):
    """T2: unpublished trailing minutes and empty fetches are retries; only a bounded or aged hole VOIDs."""

    def test_newest_minute_unpublished_is_waited_for(self):
        self.mkt.lag_seconds = 20.0
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        w = self.watch()
        s = w.state["symbols"]["SPY"]["sides"]["CALL"]
        self.assertNotEqual(s["status"], "VOID")
        self.assertEqual(len(self.triggered()), 1)
        self.assertIn("TRIGGERED — ACTIONABLE", self.triggered()[0])
        self.assertFalse(any("watch history incomplete" in t and t.startswith("⚠️") for t in self.sent))

    def test_trailing_minute_not_processed_past(self):
        plan = _plan()
        st = mw.init_state(plan, DAY)
        w = mw.Watcher(plan, st, DAY, BASE + 20 * M, now_fn=self.now, sleep_fn=self.sleep)
        self.clock[0] = BASE + 3 * M                      # minutes 10:00..10:02 completed
        self.mkt.lag_seconds = 90.0                       # only 10:00 is published yet
        self.assertFalse(w.backfill("SPY"))
        self.assertEqual(st["symbols"]["SPY"]["last_processed_bar_ts"], mr.iso_z(BASE))
        self.assertEqual(st["symbols"]["SPY"]["sides"]["CALL"]["status"], "WATCHING")
        self.mkt.lag_seconds = 0.0
        self.assertTrue(w.backfill("SPY"))                # next poll processes them
        self.assertEqual(st["symbols"]["SPY"]["last_processed_bar_ts"], mr.iso_z(BASE + 2 * M))

    def test_empty_fetch_is_retry_not_void(self):
        self.mkt.empty_until = BASE + 6 * M
        self.mkt.bars[BASE + 8 * M] = (100.5, 101.3, 100.4, 101.2)
        w = self.watch()
        self.assertEqual(w.state["symbols"]["SPY"]["sides"]["CALL"]["status"], "TRIGGERED")
        self.assertFalse(any(t.startswith("⚠️") for t in self.sent))

    def test_bounded_interior_hole_voids(self):
        self.mkt.missing = {BASE + 2 * M}
        w = self.watch()
        s = w.state["symbols"]["SPY"]["sides"]["CALL"]
        self.assertEqual((s["status"], s["void_reason"]), ("VOID", "watch history incomplete"))
        self.assertIn("bounded", s["void_detail"])

    def test_unpublished_older_than_t_max_voids(self):
        self.mkt.missing = {t for t in (BASE + i * M for i in range(2, 60))}
        w = self.watch(minutes=20)
        s = w.state["symbols"]["SPY"]["sides"]["CALL"]
        self.assertEqual(s["status"], "VOID")
        self.assertIn("t_max", s["void_detail"])


class TestDeliveryAndRestart(Base):
    def test_outcome_alert_resent_after_failed_delivery_once(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.mkt.bars[BASE + 5 * M] = (100.0, 100.2, 98.9, 99.2)
        fails = {"n": 0}
        orig = mw.send_slack_blocks.side_effect

        def flaky(bl, fb):
            if "invalidation printed" in bl[0]["text"]["text"] and fails["n"] == 0:
                fails["n"] += 1
                return False
            return orig(bl, fb)
        with mock.patch.object(mw, "send_slack_blocks", side_effect=flaky):
            w = self.watch(minutes=15)
        self.assertEqual(sum("invalidation printed — exit" in t for t in self.sent), 1)
        self.assertTrue(w.state["symbols"]["SPY"]["sides"]["CALL"]["outcome_alerted"])

    def test_outcome_survives_crash_before_send_outcome(self):
        """Codex pre-ship 11-09: the outcome is saved in backfill() before send_outcome() runs; a crash between them
        must still leave the bar on disk so the restarted watcher sends the exit alert (once)."""
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.mkt.bars[BASE + 5 * M] = (100.0, 100.2, 98.9, 99.2)
        plan = _plan()
        with mock.patch.object(mw.Watcher, "send_outcome", side_effect=RuntimeError("process killed")):
            self.watch(minutes=15, plan=plan)   # the loop isolates the error per symbol; send_outcome never completes
        st = mw.load_state(plan, DAY)
        s = st["symbols"]["SPY"]["sides"]["CALL"]
        self.assertEqual(s["outcome"], "STOP")
        self.assertIsNotNone(s.get("outcome_bar"))
        self.assertFalse(s.get("outcome_alerted"))
        self.watch(minutes=15, plan=plan, state=st)
        self.assertEqual(sum("invalidation printed — exit" in t for t in self.sent), 1)

    def test_trigger_alert_resent_after_failed_delivery_never_actionable(self):
        """Codex pre-ship 11-08: a failed TRIGGERED send is re-sent on a later poll, without ACTIONABLE terms."""
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        fails = {"n": 0}
        orig = mw.send_slack_blocks.side_effect

        def flaky(bl, fb):
            if "TRIGGERED — ACTIONABLE" in bl[0]["text"]["text"] and fails["n"] == 0:
                fails["n"] += 1
                return False
            return orig(bl, fb)
        with mock.patch.object(mw, "send_slack_blocks", side_effect=flaky):
            w = self.watch(minutes=10)
        late = [t for t in self.sent if "the alert was delayed" in t]
        self.assertEqual(len(late), 1)
        self.assertNotRegex(late[0], r"ACTIONABLE|Limit \$|sell-stop")
        self.assertNotIn("pending_alert_text", w.state["symbols"]["SPY"]["sides"]["CALL"])

    def test_corrupt_state_does_not_re_alert(self):
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.watch(minutes=6)
        self.assertEqual(len(self.triggered()), 1)
        mw.state_path(DAY).write_text("{not json")
        st = mw.load_state(_plan(), DAY)
        self.assertEqual(st["symbols"]["SPY"]["sides"]["CALL"]["status"], "TRIGGERED")
        self.assertTrue(list(self.logs.glob("morning_watch_state_2026-11-09.json.corrupt-*")))
        self.watch(minutes=12, state=st)
        self.assertEqual(len(self.triggered()), 1)

    def test_wrong_shape_state_is_rebuilt_not_crashed(self):
        """Codex pre-ship 11-08: valid JSON of the wrong top-level type ([] / a string / no symbols) is corrupt state."""
        self.mkt.bars[BASE + 2 * M] = (100.5, 101.3, 100.4, 101.2)
        self.watch(minutes=6)
        with open(mr.STATE["recommendations"], "a", encoding="utf-8") as fh:
            fh.write("[1, 2]\n")                                     # a valid non-object line in the log
            fh.write(json.dumps({"date": DAY.isoformat(), "rec_id": [1], "event": "triggered"}) + "\n")
        for bad in ("[]", '"x"', "{}", '{"symbols": []}', '{"symbols": {"SPY": []}}',
                    '{"symbols": {"SPY": {"sides": {"CALL": 5}}}}'):
            mw.state_path(DAY).write_text(bad)
            st = mw.load_state(_plan(), DAY)
            self.assertEqual(st["symbols"]["SPY"]["sides"]["CALL"]["status"], "TRIGGERED", bad)
            self.assertIn("restored_from_corrupt", st)

    def test_failed_alarm_delivery_is_retried_next_check(self):
        """Codex pre-ship 11-09: a Slack failure must not record the day's alarm as sent."""
        mr.update_heartbeat({"sends": {"watchlist": {"date": DAY.isoformat(), "slack_ok": True, "plan_ok": False}}})
        cal = [{"date": DAY, "open": BASE, "close": CLOSE}]
        self.deliver_ok = False
        with mock.patch.object(mr, "calendar", return_value=cal):
            self.assertEqual(mw.check_heartbeat(now=BASE), 1)
            self.assertNotEqual((mr.read_json(mr.STATE["heartbeat"]) or {}).get("plan_alarm_date"), DAY.isoformat())
            self.deliver_ok = True
            self.assertEqual(mw.check_heartbeat(now=BASE + M), 1)     # retried and delivered
            self.assertEqual(mw.check_heartbeat(now=BASE + 2 * M), 0)  # now recorded: once per day
        self.assertEqual(sum("no plan file exists" in t for t in self.sent), 1)

    def test_plan_missing_after_sent_watchlist_alarms_once(self):
        mr.update_heartbeat({"sends": {"watchlist": {"date": DAY.isoformat(), "slack_ok": True, "plan_ok": False}}})
        cal = [{"date": DAY, "open": BASE, "close": CLOSE}]
        with mock.patch.object(mr, "calendar", return_value=cal):
            self.assertEqual(mw.check_heartbeat(now=BASE), 1)
            self.assertEqual(mw.check_heartbeat(now=BASE + 10 * M), 0)
        self.assertEqual(sum("no plan file exists" in t for t in self.sent), 1)

    def test_dry_run_heartbeat_does_not_suppress_the_real_alarm(self):
        """Codex pre-ship 11-09: a --check-heartbeat --dry-run must not record the day's alarm."""
        mr.update_heartbeat({"sends": {"watchlist": {"date": DAY.isoformat(), "slack_ok": True, "plan_ok": False}}})
        cal = [{"date": DAY, "open": BASE, "close": CLOSE}]
        with mock.patch.object(mr, "calendar", return_value=cal):
            self.assertEqual(mw.check_heartbeat(now=BASE, dry_run=True), 1)
            self.assertNotEqual((mr.read_json(mr.STATE["heartbeat"]) or {}).get("plan_alarm_date"), DAY.isoformat())
            self.assertEqual(sum("no plan file exists" in t for t in self.sent), 0)
            self.assertEqual(mw.check_heartbeat(now=BASE + M), 1)     # the real run still alarms
        self.assertEqual(sum("no plan file exists" in t for t in self.sent), 1)


class TestPerBarRule(unittest.TestCase):
    ST = {"b_high": 0.0, "b_low": 0.0}

    def side(self, side="CALL"):
        if side == "CALL":
            return {"side": "CALL", "status": "WATCHING", "trigger": 101.0, "target": 102.0, "invalidation": 99.0}
        return {"side": "PUT", "status": "WATCHING", "trigger": 99.0, "target": 98.0, "invalidation": 101.0}

    def test_m_target_touch_on_trigger_bar_still_triggers(self):
        s = self.side()
        self.assertEqual(mw.step(s, {"ts": "t", "open": 100.5, "high": 102.3, "low": 100.4, "close": 101.4}, self.ST), "break")
        s = self.side()
        self.assertEqual(mw.step(s, {"ts": "t", "open": 100.5, "high": 101.5, "low": 98.9, "close": 101.4}, self.ST), "void")

    def test_n_target_before_trigger_close_is_stale(self):
        s = self.side()
        self.assertEqual(mw.step(s, {"ts": "t", "open": 100.5, "high": 102.0, "low": 100.4, "close": 100.8}, self.ST), "stale")

    def test_f_call_uses_b_high_put_uses_b_low(self):
        bar = {"ts": "t", "open": 100.5, "high": 101.75, "low": 100.4, "close": 100.6}
        s = self.side()
        self.assertEqual(mw.step(s, dict(bar), {"b_high": 0.3, "b_low": 0.0}), "stale")
        s = self.side()
        self.assertIsNone(mw.step(s, dict(bar), {"b_high": 0.0, "b_low": 0.3}))
        pbar = {"ts": "t", "open": 99.5, "high": 99.6, "low": 98.25, "close": 99.4}
        s = self.side("PUT")
        self.assertEqual(mw.step(s, dict(pbar), {"b_high": 0.0, "b_low": 0.3}), "stale")
        s = self.side("PUT")
        self.assertIsNone(mw.step(s, dict(pbar), {"b_high": 0.3, "b_low": 0.0}))


class TestHeartbeatAndWeekly(Base):
    def test_heartbeat_alarm_once(self):
        mr.atomic_write_text(mr.plan_path(DAY), json.dumps(_plan()))
        mr.update_heartbeat({"watcher": {"date": DAY.isoformat(), "last_poll_utc": "2026-11-09T15:00:00Z"}})
        cal = [{"date": DAY, "open": BASE, "close": CLOSE}]
        with mock.patch.object(mr, "calendar", return_value=cal):
            self.assertEqual(mw.check_heartbeat(now=BASE + timedelta(seconds=60)), 0)
            self.assertEqual(mw.check_heartbeat(now=BASE + timedelta(minutes=10)), 1)
            self.assertEqual(mw.check_heartbeat(now=BASE + timedelta(minutes=15)), 0)
        self.assertEqual(sum("watcher silent" in t for t in self.sent), 1)

    def test_weekly_performance(self):
        rows = [
            {"event": "printed", "rec_id": "2026-11-09-SPY-CALL", "date": "2026-11-09", "symbol": "SPY", "side": "CALL",
             "state": "WATCHING", "trigger": 101.0, "ts_utc": "2026-11-09T14:46:00Z"},
            {"event": "triggered", "rec_id": "2026-11-09-SPY-CALL", "date": "2026-11-09", "alert_state": "ACTIONABLE",
             "contract": C101, "limit": 0.95, "stop": 0.40},
            {"event": "outcome", "rec_id": "2026-11-09-SPY-CALL", "date": "2026-11-09", "outcome": "TARGET",
             "outcome_bar_ts_utc": "2026-11-09T15:20:00Z"},
            {"event": "printed", "rec_id": "2026-11-09-SPY-PUT", "date": "2026-11-09", "symbol": "SPY", "side": "PUT",
             "state": "WATCHING", "trigger": 99.0, "ts_utc": "2026-11-09T14:46:00Z"},
        ]
        mr.locked_append_jsonl(mr.STATE["recommendations"], rows)
        mr.locked_append_jsonl(mw.quotes_archive_path(DAY), [
            {"ts_utc": "2026-11-09T15:10:00Z", "rec_id": "2026-11-09-SPY-CALL", "quote": {"bid": 1.10}},
            {"ts_utc": "2026-11-09T15:21:00Z", "rec_id": "2026-11-09-SPY-CALL", "quote": {"bid": 1.50}}])
        self.mkt.bars[BASE + 30 * M] = (99.0, 99.2, 98.5, 99.1)
        self.clock[0] = CLOSE
        cal = [{"date": DAY, "open": BASE, "close": CLOSE}]
        sat = datetime(2026, 11, 14, 14, 20, tzinfo=UTC)
        with mock.patch.object(mr, "calendar", return_value=cal):
            self.assertEqual(mw.weekly_performance(now=sat), 0)
            self.assertEqual(mw.weekly_performance(now=sat), 0)   # marker -> no second send
        md = (self.logs / "0dte_performance_2026-W46.md").read_text()
        self.assertIn("| 2026-11-09-SPY-CALL | yes | ACTIONABLE | TARGET | 0.95 | 1.50 | +0.55 |", md)
        self.assertIn("| 2026-11-09-SPY-PUT | no |", md)
        self.assertIn("0.50 |", md)
        self.assertEqual(sum("0DTE recommendation performance" in t for t in self.sent), 1)


class TestQuoteArchiveRetry(Base):
    def test_failed_or_empty_fetch_leaves_minute_for_retry(self):
        self.clock[0] = BASE + 5 * M
        st = mw.init_state(_plan(), DAY)
        s = st["symbols"]["SPY"]["sides"]["CALL"]
        s.update(status="TRIGGERED", contract=C101)
        w = mw.Watcher(_plan(), st, DAY, BASE + 20 * M, now_fn=self.now, sleep_fn=self.sleep)
        arch = self.logs / "morning_watch_quotes_2026-11-09.jsonl"
        with mock.patch.object(mr, "fetch_snapshots", side_effect=RuntimeError("timeout")):
            w.archive_quotes()
        self.assertIsNone(w.state.get("last_archive_minute"))
        with mock.patch.object(mr, "fetch_snapshots", return_value={}):
            w.archive_quotes()
        self.assertIsNone(w.state.get("last_archive_minute"))
        self.assertFalse(arch.exists())
        self.mkt.quotes[C101] = (0.90, 0.97)
        w.archive_quotes()                                    # same minute, fetch works now
        self.assertEqual(w.state.get("last_archive_minute"), mr.iso_z(mr.floor_minute(self.now())))
        self.assertEqual(len(arch.read_text().splitlines()), 1)
        w.archive_quotes()                                    # already archived -> no duplicate row
        self.assertEqual(len(arch.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
