#!/usr/bin/env python3
# ruff: noqa: E501  — mock-heavy fixtures run long (matches tests/ convention)
"""
tests/test_day_tier_track_b_live.py — Track B Increment-2 PART 2 (live wiring, RISK-PATH).
Design: logs/design_records/day_tier_track_b_inc2_2026-09-22.md (PART 2 + the 2026-09-23 corrections).

Guards the Part-2 behaviors a regression could silently break:
  1. TRACK-B EXPOSURE CAP — _bounded_entry_qty caps a Track-B entry at its budget share count (requested_qty)
     and open Track-B notional at the budget; Track A keeps risk-basis sizing; min()-only; honors
     DAYTRADE_TRACK_B_CASH_ONLY (an exposure cap, not a funding rule).
  2. place_entry threads the track: a Track-B size dict submits the budget-capped qty and stamps track="B" on
     the durable entry_fill + the state record; a Track-A dict is unchanged (track="A").
  3. _track_b_daily_context: prior_close from SETTLED SIP (end = today 00:00 ET), ADV from IEX (same basis as
     the IEX frame's volume), today's bar dropped, once-per-day file cache, fail-safe (None, None).
  4. fetch_bars_window: feed "iex" maps to DataFeed.IEX; an unknown feed is honest-empty (no client call).
Plus the durable-log stamp (log_entry_fill/open_trades_from_log) and the runner's window gate.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import config
import run_day_tier
from execution import broker
from execution import day_trade_manager as dtm

ET = ZoneInfo("America/New_York")


def _bounded(requested, track="A", order_price=70.0, stop_price=69.0, equity=2500.0, bp=4000.0, symbol="UBER",
             track_budget=131.25, open_trades=None, positions=None):
    """A wide-room account (no positions/orders, zero maintenance) so ONLY risk/track caps can bind."""
    return dtm._bounded_entry_qty(requested, order_price, stop_price, equity, open_trades or {},
                                  {p.symbol: p for p in (positions or [])}, bp, 0.0, 0.30, [],
                                  risk_equity=equity, symbol=symbol, track=track, track_budget=track_budget)


class TrackBBudgetCap(unittest.TestCase):
    def test_track_b_is_capped_at_budget_share_count(self):
        # risk-basis: 1.5% x 2500 / $1 stop = 37 sh; thin-name cap $1000/$70 = 14 sh -> Track A would wire 14.
        qty_a, _ = _bounded(1, track="A")
        qty_b, why_b = _bounded(1, track="B")
        self.assertGreater(qty_a, 1)
        self.assertEqual(qty_b, 1)
        self.assertIn("track-B budget cap", why_b)

    def test_share_count_cap_binds_when_budget_affords_more(self):
        # T1: a $500 budget affords 7 shares at $70, but the sizing asked for 1 -> wired 1 (the share-count cap).
        self.assertEqual(_bounded(1, track="B", track_budget=500.0)[0], 1)
        # And the budget-room cap binds when it is the smaller one: 3 requested, $150 budget -> 2 at $70.
        self.assertEqual(_bounded(3, track="B", track_budget=150.0)[0], 2)

    def test_cap_is_min_only_never_upsizes(self):
        # A very wide stop -> risk_qty (0) below requested (3): the cap must not raise it.
        qty_b, _ = _bounded(3, track="B", stop_price=20.0)
        self.assertEqual(qty_b, 0)

    def test_cap_honors_the_config_switch(self):
        with mock.patch.object(config, "DAYTRADE_TRACK_B_CASH_ONLY", False):
            qty_b, _ = _bounded(1, track="B")
        self.assertGreater(qty_b, 1)  # board-gated switch off -> Track-A-style risk sizing

    def test_non_bool_switch_keeps_cap_on(self):
        for junk in ("False", None, 0, ""):  # only an explicit False disables the cap
            with mock.patch.object(config, "DAYTRADE_TRACK_B_CASH_ONLY", junk):
                self.assertEqual(_bounded(1, track="B")[0], 1, repr(junk))

    def test_open_track_b_notional_consumes_the_budget(self):
        # 1 NFLX B lot open at $72 -> $59.25 of the $131.25 budget left -> a $70 UBER entry wires 0.
        open_b = {"t1": {"symbol": "NFLX", "track": "B", "fill_qty": 1, "entry_price": 72.0}}
        self.assertEqual(_bounded(1, track="B", open_trades=open_b)[0], 0)
        # An open Track-A lot does NOT consume Track B's budget (a small one, so the shared day-tier gross
        # room — equity x 0.60 for a thin name — is not what binds).
        open_a = {"t2": {"symbol": "MSFT", "track": "A", "fill_qty": 1, "entry_price": 100.0}}
        self.assertEqual(_bounded(1, track="B", open_trades=open_a)[0], 1)

    def test_state_only_b_lot_counts_against_the_budget(self):
        # Risk seat R2: a B lot missing from the durable log (failed log write) but present in state still
        # consumes the Track-B budget -> the next $70 B entry wires 0; the same lot does not touch Track A.
        extra = {"entry::NFLX::x": {"symbol": "NFLX", "track": "B", "fill_qty": 1, "entry_price": 72.0}}
        self.assertEqual(dtm._bounded_entry_qty(1, 70.0, 69.0, 2500.0, {}, {}, 4000.0, 0.0, 0.30, [],
                                                risk_equity=2500.0, symbol="UBER", track="B",
                                                track_budget=131.25, extra_b_lots=extra)[0], 0)
        qa = dtm._bounded_entry_qty(1, 70.0, 69.0, 2500.0, {}, {}, 4000.0, 0.0, 0.30, [], risk_equity=2500.0,
                                    symbol="UBER", track="A", extra_b_lots=extra)[0]
        self.assertEqual(qa, _bounded(1, track="A")[0])

    def test_budget_measured_at_order_price_closes_slippage_overshoot(self):
        # budget $131.25 affords 1 share at entry_ref $131.00, but the marketable limit is $131.26 -> 0.
        self.assertEqual(_bounded(1, track="B", order_price=131.26, stop_price=129.0)[0], 0)

    def test_missing_budget_fails_closed(self):
        for bad in (None, 0, -5, float("nan"), "x"):
            self.assertEqual(_bounded(1, track="B", track_budget=bad)[0], 0, repr(bad))

    def test_unknown_track_is_track_a(self):
        self.assertEqual(_bounded(1, track="Z")[0], _bounded(1, track="A")[0])
        self.assertEqual(_bounded(1, track=None)[0], _bounded(1, track="A")[0])  # type: ignore[arg-type]

    def test_track_b_lowercase_is_capped(self):
        self.assertEqual(_bounded(1, track="b")[0], 1)


class PlaceEntryTrackThreading(unittest.TestCase):
    """place_entry: Track B submits the budget-capped qty and stamps track B; Track A's code path unchanged."""

    def _run(self, track, decision_track=None, state=None, open_log=None, events=None, events_ok=True):
        state = {} if state is None else state
        submitted: dict = {}
        logged: dict = {}
        acct = SimpleNamespace(buying_power="4000", equity="2500", last_equity="2500", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)

        def _submit_limit(symbol, qty, side, px, tier=None):
            submitted["qty"] = qty
            return SimpleNamespace(id="ENT1", client_order_id="DT-UBER-b-1-x")

        def _log_entry_fill(trade_id, symbol, **kw):
            logged.update(kw)
            return True

        def _final_fill(_oid):
            return float(submitted.get("qty", 0)), 70.1

        pos = SimpleNamespace(symbol="UBER", current_price=70.0, qty=1, side="long", avg_entry_price=70.0)
        oco = SimpleNamespace(id="TP1", legs=[SimpleNamespace(id="ST1", order_type="stop", type="stop")])
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "DRIVE", "entry_ref": 70.0,
                   "target": None, "wall_ref": 69.0}
        size = {"size_ok": True, "shares": 1, "budget": 131.25}
        if track is not None:
            size["track"] = track

        patches = [
            mock.patch.object(dtm, "_enabled", return_value=True),
            mock.patch.object(dtm, "_load_state", side_effect=lambda: state),
            mock.patch.object(dtm, "_save_state", return_value=True),
            mock.patch.object(dtm, "_min_stop_room_ok", return_value=(True, "ok")),
            mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None),
            mock.patch.object(dtm, "_confirm_fill", return_value=True),
            mock.patch.object(dtm, "_final_fill", side_effect=_final_fill),
            mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value=open_log or {}),
            mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(list(events or []), events_ok)),
            mock.patch("strategy.day_tier_logger.log_decision", return_value=True),
            mock.patch("strategy.day_tier_logger.log_entry_fill", side_effect=_log_entry_fill),
            mock.patch("strategy.day_tier_logger.log_stop_placed", return_value=True),
            mock.patch("strategy.day_tier_logger.log_target_placed", return_value=True),
            mock.patch("trade_logger.log_event", return_value=True),
            mock.patch.object(broker, "get_account", return_value=acct),
            mock.patch.object(broker, "get_open_positions", return_value=[]),
            mock.patch.object(broker, "get_open_orders", return_value=[]),
            mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30),
            mock.patch.object(broker, "submit_limit_order", side_effect=_submit_limit),
            mock.patch.object(broker, "cancel_open_orders_for_symbol", return_value=0),
            mock.patch.object(broker, "get_open_position", return_value=pos),
            mock.patch.object(broker, "submit_oco_exit", return_value=oco),
        ]
        with ExitStack() as stack:  # >20 nested `with` items is a SyntaxError on the OCI py3.10 target
            for p in patches:
                stack.enter_context(p)
            ok = dtm.place_entry("UBER", {"would_consider": True, "track": decision_track or track}, trigger, size,
                                 bar_id="20260923-1000", equity=2500.0)
        rec = next((v for k, v in state.items() if k.startswith("entry::") and v.get("symbol") == "UBER"), {})
        return ok, submitted.get("qty"), logged.get("track"), rec.get("track")

    def test_track_b_submits_budget_capped_qty_and_stamps_b(self):
        ok, qty, log_track, state_track = self._run("B")
        self.assertTrue(ok)
        self.assertEqual(qty, 1)            # the budget share count, NOT the ~14-sh risk size
        self.assertEqual(log_track, "B")
        self.assertEqual(state_track, "B")

    def test_state_only_b_lot_consumes_budget_through_place_entry(self):
        # Risk seat R3: a same-day FILLED Track-B NFLX lot whose log write failed (not in open_trades_from_log)
        # must use up the budget -> the $70 UBER Track-B entry wires 0 (no order submitted).
        today = datetime.now(ET).strftime("%Y%m%d")
        st = {f"entry::NFLX::{today}-0945": {"symbol": "NFLX", "track": "B", "state": "filled", "coid": "DT-NFLX-x",
                                             "bar_id": f"{today}-0945", "fill_qty": 1, "fill_px": 72.0, "side": "long"}}
        ok, qty, _, _ = self._run("B", state=st)
        self.assertFalse(ok)
        self.assertIsNone(qty)

    def test_logged_b_lot_is_counted_once_not_twice(self):
        # The same lot present in BOTH the log and state is counted once. Order price = round(70*1.002,2) = 70.14.
        # Once: (131.25 - 40) / 70.14 = 1.30 -> 1 share. Twice: (131.25 - 80) / 70.14 = 0.73 -> 0 shares.
        today = datetime.now(ET).strftime("%Y%m%d")
        st = {f"entry::SMCI::{today}-0945": {"symbol": "SMCI", "track": "B", "state": "protected", "coid": "DT-SMCI-x",
                                             "bar_id": f"{today}-0945", "fill_qty": 1, "fill_px": 40.0, "side": "long"}}
        log = {"DT-SMCI-x": {"trade_id": "DT-SMCI-x", "symbol": "SMCI", "track": "B", "fill_qty": 1,
                             "entry_price": 40.0, "side": "long"}}
        ok, qty, _, _ = self._run("B", state=st, open_log=log)
        self.assertTrue(ok)
        self.assertEqual(qty, 1)

    def test_exited_b_lot_no_longer_consumes_the_budget(self):
        # Cold-2nd R1: a Track-B NFLX lot that already EXITED today (entry_fill + exit_fill logged, so not in
        # the open set) keeps a "protected" state record; it must NOT use the budget -> UBER wires 1.
        today = datetime.now(ET).strftime("%Y%m%d")
        st = {f"entry::NFLX::{today}-0945": {"symbol": "NFLX", "track": "B", "state": "protected", "coid": "DT-NFLX-x",
                                             "bar_id": f"{today}-0945", "fill_qty": 1, "fill_px": 100.0, "side": "long"}}
        ev = [{"event": "entry_fill", "trade_id": "DT-NFLX-x"}, {"event": "exit_fill", "trade_id": "DT-NFLX-x"}]
        ok, qty, _, _ = self._run("B", state=st, events=ev)
        self.assertTrue(ok)
        self.assertEqual(qty, 1)

    def test_unreadable_log_counts_state_lots_fail_closed(self):
        today = datetime.now(ET).strftime("%Y%m%d")
        st = {f"entry::NFLX::{today}-0945": {"symbol": "NFLX", "track": "B", "state": "protected", "coid": "DT-NFLX-x",
                                             "bar_id": f"{today}-0945", "fill_qty": 1, "fill_px": 100.0, "side": "long"}}
        # Even with the lot's entry_fill+exit_fill present, an UNREADABLE log means exits cannot be proven ->
        # the same-day record is counted (over-count, fail-closed) -> the $70 UBER entry wires 0.
        ev = [{"event": "entry_fill", "trade_id": "DT-NFLX-x"}, {"event": "exit_fill", "trade_id": "DT-NFLX-x"}]
        ok, qty, _, _ = self._run("B", state=st, events=ev, events_ok=False)
        self.assertFalse(ok)
        self.assertIsNone(qty)

    def test_size_track_b_alone_still_caps(self):
        # T2: size dict says "B", decision says "A" -> still the Track-B cap + stamp (either dict routes to B).
        ok, qty, log_track, state_track = self._run("B", decision_track="A")
        self.assertTrue(ok)
        self.assertEqual(qty, 1)
        self.assertEqual(log_track, "B")
        self.assertEqual(state_track, "B")

    def test_decision_track_b_alone_still_caps(self):
        # Defense in depth: a size dict missing "track" but a Track-B decision still routes to the B cap.
        ok, qty, log_track, _ = self._run(None, decision_track="B")
        self.assertTrue(ok)
        self.assertEqual(qty, 1)
        self.assertEqual(log_track, "B")

    def test_track_a_unchanged(self):
        ok, qty, log_track, state_track = self._run("A")
        self.assertTrue(ok)
        self.assertGreater(qty, 1)          # Track A keeps risk-basis sizing
        self.assertEqual(log_track, "A")
        self.assertEqual(state_track, "A")


class DailyContext(unittest.TestCase):
    @staticmethod
    def _daily(closes, vols, last_day):
        """Consecutive CALENDAR-day daily bars ending on `last_day` (midnight ET stamps, as Alpaca returns)."""
        idx = pd.date_range(end=pd.Timestamp(last_day, tz="America/New_York"), periods=len(closes),
                            freq="D").tz_convert("UTC")
        return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": vols},
                            index=idx)

    def setUp(self):
        run_day_tier._daily_ctx_cache.clear()
        self._td = TemporaryDirectory()
        self._file = Path(self._td.name) / "ctx.json"
        self._p = mock.patch.object(run_day_tier, "_DAILY_CTX_FILE", self._file)
        self._p.start()
        # Inject a controllable fake data.fetcher (like tests/test_day_tier_track_b.py) so these tests never
        # import the alpaca SDK — order-independent even when another module stubbed alpaca in sys.modules.
        self._orig_fetcher = sys.modules.get("data.fetcher")
        self._fake = types.ModuleType("data.fetcher")
        self._fake.fetch_bars_window = lambda *a, **k: pd.DataFrame()
        sys.modules["data.fetcher"] = self._fake
        self.now = datetime.now(ET).replace(hour=10, minute=15, second=0, microsecond=0)
        self.today = self.now.strftime("%Y-%m-%d")
        self.prev = (self.now - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        self._cal = mock.patch.object(run_day_tier, "_prev_session_date", return_value=self.prev)
        self._cal.start()

    def tearDown(self):
        self._cal.stop()
        if self._orig_fetcher is not None:
            sys.modules["data.fetcher"] = self._orig_fetcher
        else:
            sys.modules.pop("data.fetcher", None)
        self._p.stop()
        self._td.cleanup()
        run_day_tier._daily_ctx_cache.clear()

    def _fetch(self, fn):
        self._fake.fetch_bars_window = fn

    def _serve(self, sip, iex, calls=None):
        def _fbw(symbol, tf, start, end, feed="sip", adjustment="raw"):
            if calls is not None:
                calls.append((feed, adjustment, end))
            return sip if feed == "sip" else iex
        self._fetch(_fbw)

    def test_split_adjusted_sip_close_iex_adv_today_dropped_and_cached(self):
        # 18 prior days + TODAY's forming bar (last row) — today must be dropped from both.
        sip = self._daily([100.0] * 16 + [101.0, 102.0, 999.0], [1e8] * 19, self.today)
        iex = self._daily([100.1] * 18 + [999.0], [2e6] * 6 + [3e6] * 6 + [4e6] * 6 + [9e9], self.today)
        calls: list = []
        self._serve(sip, iex, calls)
        pc, adv = run_day_tier._track_b_daily_context("UBER", self.now)
        self.assertEqual(pc, 102.0)                       # SIP settled prior close, not today's 999
        self.assertAlmostEqual(adv, 3e6)                  # mean of the IEX prior days, not the SIP 1e8
        self.assertEqual({c[0] for c in calls}, {"sip", "iex"})
        self.assertEqual({c[1] for c in calls}, {"split"})  # today's share basis (split-consistent)
        for _feed, _adj, end in calls:
            self.assertEqual((end.hour, end.minute), (0, 0))  # settled history only (never a recent-SIP window)
        self.assertEqual(json.loads(self._file.read_text())["ctx"]["UBER"], [102.0, 3e6])
        self.assertEqual(json.loads(self._file.read_text())["basis"], run_day_tier._DAILY_CTX_BASIS)
        # A FRESH process (in-memory cache cleared) reads the day's file — no refetch.
        run_day_tier._daily_ctx_cache.clear()

        def _no_refetch(*a, **k):
            raise AssertionError("refetch")
        self._fetch(_no_refetch)
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (102.0, 3e6))

    def test_missing_previous_session_bar_is_rejected_and_not_cached(self):
        # Last settled bar is D-2 (D-1 missing) -> a two-day "gap" would be measured -> skip, never cache.
        d2 = (self.now - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        self._serve(self._daily([100.0] * 18, [1e8] * 18, d2), self._daily([100.0] * 18, [2e6] * 18, d2))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))
        self.assertFalse(self._file.exists())

    def test_only_sip_missing_previous_session_is_rejected(self):
        d2 = (self.now - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        self._serve(self._daily([100.0] * 18, [1e8] * 18, d2), self._daily([100.0] * 18, [2e6] * 18, self.prev))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_only_iex_missing_previous_session_is_rejected(self):
        d2 = (self.now - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        self._serve(self._daily([100.0] * 18, [1e8] * 18, self.prev), self._daily([100.0] * 18, [2e6] * 18, d2))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_adv_minimum_days_boundary(self):
        # 14 prior days -> rejected; 15 -> accepted (both feeds ending on the previous session).
        self._serve(self._daily([100.0] * 14, [1e8] * 14, self.prev), self._daily([100.0] * 14, [2e6] * 14, self.prev))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))
        run_day_tier._daily_ctx_cache.clear()
        self._serve(self._daily([100.0] * 15, [1e8] * 15, self.prev), self._daily([100.0] * 15, [2e6] * 15, self.prev))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (100.0, 2e6))

    def test_too_few_adv_days_rejected(self):
        self._serve(self._daily([100.0] * 10, [1e8] * 10, self.prev), self._daily([100.0] * 10, [2e6] * 10, self.prev))
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_unreadable_calendar_fails_closed(self):
        self._serve(self._daily([100.0] * 18, [1e8] * 18, self.prev), self._daily([100.0] * 18, [2e6] * 18, self.prev))
        with mock.patch.object(run_day_tier, "_prev_session_date", return_value=None):
            self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_fetch_failure_is_fail_safe(self):
        self._fetch(lambda *a, **k: pd.DataFrame())
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

        def _down(*a, **k):
            raise RuntimeError("down")
        self._fetch(_down)
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_same_day_cache_on_another_basis_is_ignored(self):
        # T3: today's date but an OLD basis tag (e.g. a raw-close / SIP-volume draft) -> a miss, never served.
        today = self.now.strftime("%Y%m%d")
        self._file.write_text(json.dumps({"date": today, "basis": "old", "ctx": {"UBER": [1.0, 1.0]}}))
        self._fetch(lambda *a, **k: pd.DataFrame())
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_stale_day_cache_is_ignored(self):
        # A PRIOR day's file on the CURRENT basis (the realistic case) -> a miss on the date alone.
        self._file.write_text(json.dumps({"date": "19990101", "basis": run_day_tier._DAILY_CTX_BASIS,
                                          "ctx": {"UBER": [1.0, 1.0]}}))
        self._fetch(lambda *a, **k: pd.DataFrame())
        self.assertEqual(run_day_tier._track_b_daily_context("UBER", self.now), (None, None))

    def test_asof_replay_never_writes_the_live_cache(self):
        past = datetime(2026, 9, 21, 10, 15, tzinfo=ET)
        with mock.patch.object(run_day_tier, "_prev_session_date", return_value="2026-09-20"):
            self._serve(self._daily([100.0] * 17 + [101.0], [1e8] * 18, "2026-09-20"),
                        self._daily([100.0] * 18, [2e6] * 18, "2026-09-20"))
            self.assertEqual(run_day_tier._track_b_daily_context("UBER", past), (101.0, 2e6))
        self.assertFalse(self._file.exists())


class PrevSession(unittest.TestCase):
    def test_last_calendar_date_before_today(self):
        run_day_tier._prev_session_cache.clear()
        cal = [{"date": "2026-09-18"}, {"date": "2026-09-21"}, {"date": "2026-09-22"}]
        with mock.patch("reporting.pnl_ledger._get_json", return_value=cal):
            self.assertEqual(run_day_tier._prev_session_date(datetime(2026, 9, 23, 10, 0, tzinfo=ET)), "2026-09-22")
        run_day_tier._prev_session_cache.clear()

    def test_calendar_error_is_none_and_remembered_for_the_process(self):
        run_day_tier._prev_session_cache.clear()
        with mock.patch("reporting.pnl_ledger._get_json", side_effect=RuntimeError("down")) as gj:
            self.assertIsNone(run_day_tier._prev_session_date(datetime(2026, 9, 23, 10, 0, tzinfo=ET)))
            self.assertIsNone(run_day_tier._prev_session_date(datetime(2026, 9, 23, 10, 5, tzinfo=ET)))
        self.assertEqual(gj.call_count, 1)  # a failure is cached for the tick, not retried per symbol
        run_day_tier._prev_session_cache.clear()


class FetchWindowFeed(unittest.TestCase):
    def setUp(self):
        # tests/test_day_tier_shadow.py stubs the alpaca SDK in sys.modules at import time; a real
        # fetcher import is impossible after that in the same process. Skip LOUDLY (never a silent
        # pass) — run this module on its own to exercise it: python3 -m unittest tests.test_day_tier_track_b_live
        try:
            import alpaca.data as _ad
        except ImportError:
            self.skipTest("alpaca SDK not installed on this host (run on the OCI py3.10 venv)")
        if not hasattr(_ad, "__path__"):
            self.skipTest("alpaca SDK stubbed in-process by another test module — run this module alone")
    def test_iex_maps_to_iex_feed_and_default_stays_sip(self):
        from alpaca.data.enums import Adjustment, DataFeed
        from data import fetcher
        captured = {}
        client = mock.MagicMock()

        def _get(req):
            captured["feed"] = req.feed
            captured["adj"] = req.adjustment
            return SimpleNamespace(df=pd.DataFrame())
        client.get_stock_bars.side_effect = _get
        s = datetime(2026, 9, 21, 9, 30, tzinfo=ET)
        with mock.patch.object(fetcher, "get_client", return_value=client), \
             mock.patch.object(fetcher, "_rate_gate"):
            fetcher.fetch_bars_window("UBER", config.TF_5M, s, s.replace(hour=10), feed="iex")
            self.assertEqual(captured["feed"], DataFeed.IEX)
            fetcher.fetch_bars_window("UBER", config.TF_5M, s, s.replace(hour=10))
            self.assertEqual(captured["feed"], DataFeed.SIP)  # default unchanged for research callers
            self.assertEqual(captured["adj"], Adjustment.RAW)  # default adjustment unchanged
            fetcher.fetch_bars_window("UBER", config.TF_DAILY, s, s.replace(hour=10), adjustment="split")
            self.assertEqual(captured["adj"], Adjustment.SPLIT)

    def test_unknown_feed_is_honest_empty_without_a_call(self):
        from data import fetcher
        client = mock.MagicMock()
        s = datetime(2026, 9, 21, 9, 30, tzinfo=ET)
        with mock.patch.object(fetcher, "get_client", return_value=client):
            df = fetcher.fetch_bars_window("UBER", config.TF_5M, s, s.replace(hour=10), feed="otc")
            df2 = fetcher.fetch_bars_window("UBER", config.TF_5M, s, s.replace(hour=10), adjustment="all")
        self.assertTrue(df.empty)
        self.assertTrue(df2.empty)
        client.get_stock_bars.assert_not_called()


class DurableTrackStamp(unittest.TestCase):
    def test_entry_fill_stamp_and_open_set_default(self):
        from strategy import day_tier_logger
        with TemporaryDirectory() as td:
            path = Path(td) / "ev.jsonl"
            with mock.patch.object(day_tier_logger, "_JSONL", path):
                kw = dict(order_id="o", decision_id="d", side="long", requested_limit=70.2, fill_price=70.1,
                          fill_qty=1.0, market_price_at_fill=70.1, equity_at_entry=2500.0, budget=131.25,
                          notional=70.1)
                self.assertTrue(day_tier_logger.log_entry_fill("DT-B", "UBER", track="B", **kw))
                self.assertTrue(day_tier_logger.log_entry_fill("DT-A", "MSFT", **kw))              # default -> A
                self.assertTrue(day_tier_logger.log_entry_fill("DT-X", "SMCI", track="zz", **kw))  # junk -> A
                with path.open("a") as f:  # a pre-stamp (legacy) row with no track field
                    f.write(json.dumps({"event": "entry_fill", "trade_id": "DT-OLD", "symbol": "AAPL",
                                        "fill_qty": 1, "fill_price": 1}) + "\n")
                opened = day_tier_logger.open_trades_from_log()
        self.assertEqual(opened["DT-B"]["track"], "B")
        self.assertEqual(opened["DT-A"]["track"], "A")
        self.assertEqual(opened["DT-X"]["track"], "A")
        self.assertEqual(opened["DT-OLD"]["track"], "A")


class RunnerWindowGate(unittest.TestCase):
    """Outside the window the runner does NO Track-B fetch; inside it routes an ENTER to place_entry(track B)."""

    def _tick(self, in_window, state=None, extra=None, pe_side_effect=None):
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="4000")
        frame = mock.MagicMock(name="frame")
        mom = {"symbol": "UBER", "trigger": "ENTER", "direction": "long", "mode": "DRIVE",
               "entry_ref": 70.0, "structural_level": 69.0, "vol_confirmed": True}
        patches = [
            mock.patch.object(run_day_tier, "_clock_state", return_value=("open", 300.0)),
            mock.patch.object(run_day_tier, "_touch_heartbeat"),
            mock.patch.object(run_day_tier, "_maybe_sample_prices"),
            mock.patch.object(run_day_tier, "_track_b_daily_context", return_value=(100.0, 1e6)),
            mock.patch("execution.broker.get_account", return_value=acct),
            mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 0}),
            mock.patch("execution.day_trade_manager.tier_kill_check", return_value=False),
            mock.patch("execution.day_trade_manager._account_entry_halt_reason", return_value=None),
            mock.patch("strategy.day_tier_track_b.track_b_in_window", return_value=in_window),
            mock.patch("strategy.day_tier_track_b.track_b_universe", return_value=["UBER"]),
            mock.patch("strategy.day_tier_track_b.screen_mover", return_value={"is_mover": True, "gap_direction": "up"}),
            mock.patch("strategy.day_tier_momentum_trigger.compute_momentum_trigger", return_value=mom),
            mock.patch.object(config, "DAYTRADE_ENABLED", True),
            mock.patch.object(config, "DAYTRADE_TRACK_B_ENABLED", True),
            mock.patch.object(config, "DAYTRADE_UNIVERSE", []),
            mock.patch("execution.day_trade_manager._load_state", return_value=state if state is not None else {}),
            mock.patch("execution.day_trade_manager._save_state", return_value=True),
            mock.patch.object(run_day_tier, "_prev_session_date", return_value="2026-09-22"),
        ] + list(extra or [])
        with ExitStack() as stack:  # >20 nested `with` items is a SyntaxError on the OCI py3.10 target
            for p in patches:
                stack.enter_context(p)
            rm = stack.enter_context(mock.patch("execution.risk_manager.RiskManager"))
            bsf = stack.enter_context(mock.patch("strategy.day_tier_track_b.build_session_frame", return_value=frame))
            pe = stack.enter_context(mock.patch("execution.day_trade_manager.place_entry", return_value=True,
                                                side_effect=pe_side_effect))
            rm.return_value.check_kill_switch.return_value = False
            result = run_day_tier.run_tick()
        return result, bsf, pe

    def test_outside_window_fetches_nothing(self):
        result, bsf, pe = self._tick(False)
        self.assertEqual(result["phase"], "scan")
        self.assertFalse(result["track_b_window"])
        bsf.assert_not_called()
        pe.assert_not_called()

    def test_inside_window_routes_enter_to_place_entry_as_track_b(self):
        result, bsf, pe = self._tick(True)
        self.assertTrue(result["track_b_window"])
        self.assertEqual(result["entered_b"], 1)
        bsf.assert_called_once()
        size = pe.call_args.args[3]
        self.assertEqual(size["track"], "B")
        self.assertTrue(size["size_ok"])

    def test_enter_signal_persists_the_daily_shot_before_place_entry(self):
        import copy
        state: dict = {}
        today = datetime.now(ET).strftime("%Y%m%d")
        saves: list = []   # DEEP copies of what was actually handed to _save_state (not the aliased dict)
        events: list = []

        def _save(st):
            saves.append(copy.deepcopy(st))
            events.append("save")
            return True

        def _pe(*a, **k):
            events.append("place_entry")
            return True
        extra = [mock.patch("execution.day_trade_manager._save_state", side_effect=_save)]
        result, bsf, pe = self._tick(True, state=state, extra=extra, pe_side_effect=_pe)
        persisted = [s for s in saves if s.get(run_day_tier._SIGNAL_DAY_KEY)]
        self.assertTrue(persisted, "the marker was never SAVED")
        self.assertEqual(persisted[-1][run_day_tier._SIGNAL_DAY_KEY], {"date": today, "symbols": ["UBER"]})
        self.assertLess(events.index("save"), events.index("place_entry"))  # saved BEFORE place_entry
        # Next tick (a fresh process) starts from the SAVED copy only: the symbol is not re-evaluated.
        result2, bsf2, pe2 = self._tick(True, state=copy.deepcopy(persisted[-1]))
        bsf2.assert_not_called()
        pe2.assert_not_called()

    def test_failed_marker_write_does_not_abort_the_tick(self):
        extra = [mock.patch("execution.day_trade_manager._save_state", return_value=False)]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertEqual(result["phase"], "scan")
        self.assertEqual(result["entered_b"], 1)  # the tick continues; the in-process set still blocks re-eval

    def test_api_budget_cap_defers_without_using_the_daily_shot(self):
        state: dict = {}
        with mock.patch.object(config, "DAYTRADE_MAX_API_CALLS_PER_RUN", 0):
            result, bsf, pe = self._tick(True, state=state)
        self.assertEqual(result["track_b_note"], "api_budget")
        bsf.assert_not_called()
        self.assertNotIn(run_day_tier._SIGNAL_DAY_KEY, state)  # the shot is NOT burned -> next tick re-evaluates

    def test_calendar_unavailable_skips_track_b(self):
        extra = [mock.patch.object(run_day_tier, "_prev_session_date", return_value=None)]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertEqual(result["track_b_note"], "calendar_unavailable")
        bsf.assert_not_called()

    @staticmethod
    def _seq_clock(values):
        """Monotonic reads return `values` in order, then repeat the last one."""
        it = {"i": 0}

        def _mono():
            v = values[min(it["i"], len(values) - 1)]
            it["i"] += 1
            return v
        return SimpleNamespace(monotonic=_mono)

    def test_place_entry_reserve_threshold_both_sides(self):
        # Reads: t0, pre-loop, loop-top, pre-context, RESERVE. With the real 120 s cadence and 35 s reserve:
        # 90 s elapsed (30 s left) -> deferred; 80 s elapsed (40 s left) -> place_entry IS called.
        extra = [mock.patch.object(run_day_tier, "time", self._seq_clock([0.0, 0.0, 0.0, 0.0, 90.0]))]
        result, bsf, pe = self._tick(True, extra=extra)
        pe.assert_not_called()
        self.assertEqual(result["track_b_note"], "tick_budget")
        extra = [mock.patch.object(run_day_tier, "time", self._seq_clock([0.0, 0.0, 0.0, 0.0, 80.0]))]
        result, bsf, pe = self._tick(True, extra=extra)
        pe.assert_called_once()
    def test_one_track_b_entry_per_symbol_per_day(self):
        today = datetime.now(ET).strftime("%Y%m%d")
        state = {f"entry::UBER::{today}-1000": {"symbol": "UBER", "track": "B", "bar_id": f"{today}-1000",
                                                "state": "flattened_no_stop"}}
        result, bsf, pe = self._tick(True, state=state)
        bsf.assert_not_called()   # already had its Track-B entry today -> not even evaluated
        pe.assert_not_called()
        self.assertEqual(result["entered_b"], 0)

    def test_a_prior_day_track_b_record_does_not_block_today(self):
        prior = (datetime.now(ET) - pd.Timedelta(days=1)).strftime("%Y%m%d")
        state = {f"entry::UBER::{prior}-1000": {"symbol": "UBER", "track": "B", "bar_id": f"{prior}-1000",
                                                "state": "flattened_no_stop"},
                 run_day_tier._SIGNAL_DAY_KEY: {"date": prior, "symbols": ["UBER"]}}
        result, bsf, pe = self._tick(True, state=state)
        bsf.assert_called_once()

    def test_a_track_a_record_does_not_block_track_b(self):
        today = datetime.now(ET).strftime("%Y%m%d")
        state = {f"entry::UBER::{today}-1000": {"symbol": "UBER", "track": "A", "bar_id": f"{today}-1000",
                                                "state": "flattened_no_stop"}}
        result, bsf, pe = self._tick(True, state=state)
        bsf.assert_called_once()

    @staticmethod
    def _clock(over_after_calls):
        """A controlled monotonic clock: 0.0 for the first `over_after_calls` reads, then 1000 s (over budget).
        Read order in run_tick: _tick_t0, the pre-loop check, the loop-top check, the pre-daily-context check."""
        calls = {"n": 0}

        def _mono():
            calls["n"] += 1
            return 0.0 if calls["n"] <= over_after_calls else 1000.0
        return SimpleNamespace(monotonic=_mono)

    def test_budget_crossed_at_loop_top_stops_before_any_fetch(self):
        # Pre-loop check passes (reads 1-2 in budget); the IN-LOOP top check (read 3) is over -> break.
        ctx = mock.MagicMock(return_value=(100.0, 1e6))
        extra = [mock.patch.object(run_day_tier, "time", self._clock(2)),
                 mock.patch.object(run_day_tier, "_track_b_daily_context", ctx)]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertTrue(result["track_b_window"])   # proves the PRE-loop check did NOT fire
        bsf.assert_not_called()
        ctx.assert_not_called()
        self.assertEqual(result["track_b_note"], "tick_budget")

    def test_budget_crossed_before_daily_context_skips_the_fetch(self):
        # Reads 1-3 in budget (the frame is built); the re-check before the daily context (read 4) is over.
        ctx = mock.MagicMock(return_value=(100.0, 1e6))
        extra = [mock.patch.object(run_day_tier, "time", self._clock(3)),
                 mock.patch.object(run_day_tier, "_track_b_daily_context", ctx)]
        result, bsf, pe = self._tick(True, extra=extra)
        bsf.assert_called_once()
        ctx.assert_not_called()
        pe.assert_not_called()
        self.assertEqual(result["track_b_note"], "tick_budget")

    def test_track_b_window_or_import_error_does_not_escape_run_tick(self):
        extra = [mock.patch("strategy.day_tier_track_b.track_b_in_window", side_effect=RuntimeError("boom"))]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertEqual(result["phase"], "scan")
        self.assertEqual(result["track_b_note"], "import_error")
        bsf.assert_not_called()

    def test_track_b_flag_off_is_a_no_op(self):
        # T4: with the window, universe and calendar all forced ON, only the FLAG can keep Track B off.
        extra = [mock.patch.object(config, "DAYTRADE_TRACK_B_ENABLED", False)]
        result, bsf, pe = self._tick(True, extra=extra)
        self.assertFalse(result["track_b"])
        self.assertFalse(result["track_b_window"])
        self.assertEqual(result["track_b_note"], "")
        bsf.assert_not_called()
        pe.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
