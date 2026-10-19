#!/usr/bin/env python3
# ruff: noqa: E501
"""CONV-1018 conviction-band sizing (execution/earned_sizing.py + day_trade_manager._conv_entry).
Design record: logs/design_records/earned_concentration_sizing_2026-10-18.md, "Gate-compliant revision".
No test reaches the broker: every broker/data call is mocked."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import data.alpaca_data  # noqa: F401 — patched below by dotted path
from execution import earned_sizing as es

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")


def _trade(tid, conv, entry, stop, exit_px, qty=10, side="long", day=datetime(2026, 10, 7, 10, 0, tzinfo=ET),
           full=True, symbol="AAPL"):
    sign = 1 if side == "long" else -1
    pnl = round(sign * (exit_px - entry) * qty, 2)
    ev = [
        {"event": "decision", "trade_id": tid, "symbol": symbol, "size": {"conviction": conv}, "decision": {}},
        {"event": "entry_fill", "trade_id": tid, "symbol": symbol, "side": side, "fill_price": entry,
         "fill_qty": qty, "ts": day.astimezone(PT).isoformat()},
        {"event": "stop_placed", "trade_id": tid, "symbol": symbol, "stop_price": stop},
    ]
    if full:
        ev.append({"event": "exit_fill", "trade_id": tid, "symbol": symbol, "fill_qty": qty, "realized_pnl": pnl,
                   "ts": (day + timedelta(minutes=30)).astimezone(PT).isoformat()})
    return ev


class Bands(unittest.TestCase):
    def test_cuts_match_replay_rule(self):
        convs = [i / 10 for i in range(9)]           # 0.0 .. 0.8, n=9
        self.assertEqual(es.band_cuts(convs), [0.3, 0.6])   # c[n//3], c[2n//3]

    def test_band_index_edges(self):
        cuts = [0.47, 0.60]
        self.assertEqual(es.band_index(0.46, cuts), 0)
        self.assertEqual(es.band_index(0.47, cuts), 1)   # first cut inclusive
        self.assertEqual(es.band_index(0.60, cuts), 1)   # last cut stays in the middle band
        self.assertEqual(es.band_index(0.61, cuts), 2)

    def test_too_few_trades_no_cuts(self):
        self.assertEqual(es.band_cuts([0.5, 0.6]), [])


class Completed(unittest.TestCase):
    def test_r_and_open_trade_excluded(self):
        ev = _trade("T1", 0.5, 100.0, 99.0, 102.0) + _trade("T2", 0.5, 50.0, 49.0, 49.0, full=False)
        out = es.completed_trades(ev)
        self.assertEqual([t["trade_id"] for t in out], ["T1"])
        self.assertAlmostEqual(out[0]["R"], 2.0)
        self.assertEqual(out[0]["conviction"], 0.5)

    def test_short_r_and_partials_covering_entry(self):
        day = datetime(2026, 10, 7, 10, 0, tzinfo=ET)
        ev = _trade("S1", 0.5, 100.0, 101.0, 0.0, side="short", full=False)
        ev += [{"event": "partial_exit_fill", "trade_id": "S1", "fill_qty": 5, "realized_pnl": 5.0,
                "ts": (day + timedelta(minutes=5)).isoformat()},
               {"event": "partial_exit_fill", "trade_id": "S1", "fill_qty": 5, "realized_pnl": 10.0,
                "ts": (day + timedelta(minutes=9)).isoformat()}]
        out = es.completed_trades(ev)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["R"], 1.5)        # $15 / 10 sh / $1 stop

    def test_overshoot_ratio(self):
        ev = _trade("A", 0.5, 100.0, 99.0, 98.5) + _trade("B", 0.5, 100.0, 99.0, 101.0)
        self.assertAlmostEqual(es.overshoot_ratio(es.completed_trades(ev)), 0.5)


class Plan(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 19, 10, 0, tzinfo=ET)   # Monday
        prior = datetime(2026, 10, 14, 10, 0, tzinfo=ET)
        ev = []
        # low band losers, middle band winners, top band losers (3 each)
        for i, c in enumerate((0.40, 0.41, 0.42)):
            ev += _trade(f"L{i}", c, 100.0, 99.0, 99.0, day=prior)
        for i, c in enumerate((0.50, 0.51, 0.52)):
            ev += _trade(f"M{i}", c, 100.0, 99.0, 101.0, day=prior)
        for i, c in enumerate((0.70, 0.71, 0.72)):
            ev += _trade(f"T{i}", c, 100.0, 99.0, 99.5, day=prior)
        self.ev = ev

    def test_proven_middle_band(self):
        p = es.plan(0.51, self.ev, now=self.now)
        self.assertEqual(p["band"], "middle")
        self.assertTrue(p["proven"])
        self.assertEqual(p["band_n"], 4)              # cuts c[3]=0.50, c[6]=0.70: the last cut stays in the middle

    def test_unproven_bands(self):
        self.assertFalse(es.plan(0.40, self.ev, now=self.now)["proven"])
        self.assertFalse(es.plan(0.80, self.ev, now=self.now)["proven"])

    def test_this_weeks_trades_wait_for_monday(self):
        later = _trade("NEW", 0.51, 100.0, 99.0, 90.0, day=datetime(2026, 10, 19, 9, 45, tzinfo=ET))
        p = es.plan(0.51, self.ev + later, now=self.now + timedelta(hours=2))
        self.assertTrue(p["proven"])                  # this week's loser is not in the band stats yet ...
        self.assertAlmostEqual(p["G"], 9.0)           # ... but it raises G at once

    def test_band_with_one_trade_not_judged(self):
        prior = datetime(2026, 10, 14, 10, 0, tzinfo=ET)
        ev = []
        for i, c in enumerate((0.40, 0.50, 0.70)):
            ev += _trade(f"S{i}", c, 100.0, 99.0, 101.0, day=prior)
        self.assertIsNone(es.plan(0.50, ev, now=self.now))   # one trade per band -> today's sizing

    def test_no_record_or_no_conviction(self):
        self.assertIsNone(es.plan(0.5, [], now=self.now))
        self.assertIsNone(es.plan(None, self.ev, now=self.now))


class DecisionJoin(unittest.TestCase):
    def test_conviction_joined_by_decision_id(self):
        ev = _trade("T1", None, 100.0, 99.0, 101.0)
        ev[1]["decision_id"] = "D-7"
        ev.append({"event": "decision", "decision_id": "D-7", "trade_id": "DT-other", "size": {"conviction": 0.55}})
        self.assertEqual(es.completed_trades(ev)[0]["conviction"], 0.55)


class ConvSize(unittest.TestCase):
    def test_realtime_room_floor_wins(self):
        r = es.conv_size("long", 100.0, 98.0, 100.0, 0.4, 0.1, 1.5, 2.0, 0.0, 0.0, 120.0, rt_room=1.1)
        self.assertAlmostEqual(r["stop"], 98.90)
        self.assertEqual(r["binding"], "realtime")

    def test_floor_stop_and_divides_by_stop_plus_gap(self):
        # long, limit 100, today's stop 98 (S=2); floor max(1.5*0.4, 2*0.1)=0.6 -> stop 99.40
        r = es.conv_size("long", 100.0, 98.0, 100.0, 0.4, 0.1, 1.5, 2.0, 0.77, 0.0, 120.0)
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["stop"], 99.40)
        self.assertAlmostEqual(r["d"], 0.6)
        self.assertAlmostEqual(r["A"], 1.54)            # max(0.77*2, 1.5*0.4)
        self.assertAlmostEqual(r["per_share"], 2.14)    # d' + A, never d' alone
        self.assertEqual(r["qty"], 56)                  # floor(120 / 2.14)

    def test_jump_charge_binds(self):
        r = es.conv_size("long", 100.0, 98.0, 100.0, 0.4, 0.1, 1.5, 2.0, 0.0, 0.03, 120.0)
        self.assertAlmostEqual(r["per_share"], 3.0)     # 100 x 3% > d' + A
        self.assertEqual(r["qty"], 40)

    def test_missing_atr_or_spread_keeps_todays_stop(self):
        for atr, spr in ((None, 0.1), (0.4, None), (0.0, 0.1), (0.4, 0.0)):
            r = es.conv_size("long", 100.0, 98.0, 100.0, atr, spr, 1.5, 2.0, 0.5, 0.0, 120.0)
            self.assertAlmostEqual(r["stop"], 98.0)
            self.assertAlmostEqual(r["d"], 2.0)

    def test_never_wider_than_todays_stop(self):
        r = es.conv_size("short", 100.0, 101.0, 100.0, 5.0, 0.1, 1.5, 2.0, 0.0, 0.0, 120.0)
        self.assertAlmostEqual(r["stop"], 101.0)

    def test_short_side_and_anchor(self):
        # short, limit 100, stop 102, live 100.30 is more adverse -> stop from 100.30
        r = es.conv_size("short", 100.0, 102.0, 100.30, 0.2, 0.05, 1.5, 2.0, 0.0, 0.0, 60.0)
        self.assertAlmostEqual(r["stop"], 100.60)
        self.assertGreater(r["stop"], 100.30)

    def test_no_budget_no_shares(self):
        r = es.conv_size("long", 100.0, 98.0, 100.0, 0.4, 0.1, 1.5, 2.0, 0.5, 0.0, -5.0)
        self.assertEqual(r["qty"], 0)

    def test_bad_input_not_ok(self):
        self.assertFalse(es.conv_size("long", 100.0, 100.0, 100.0, 0.4, 0.1, 1.5, 2.0, 0.5, 0.0, 50.0)["ok"])
        self.assertFalse(es.conv_size("up", 100.0, 98.0, 100.0, 0.4, 0.1, 1.5, 2.0, 0.5, 0.0, 50.0)["ok"])


class Jump(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(es, "_STATE", Path(self.tmp.name) / "earned_sizing.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def _bars(self, t0, rows):
        idx = [t0 + timedelta(minutes=i + 1) for i in range(len(rows))]
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(idx))

    def test_ratchets_and_retries_failed_fetch(self):
        trades = es.completed_trades(_trade("A", 0.5, 100.0, 99.0, 101.0) + _trade("B", 0.5, 100.0, 99.0, 101.0))
        t0 = trades[0]["entry_ts"]
        calls = []

        def fetch(t):
            calls.append(t["trade_id"])
            if t["trade_id"] == "B":
                raise RuntimeError("timeout")
            return self._bars(t0, [[100, 100.5, 99.0, 99.5], [99.5, 100, 99.4, 99.8]])

        j = es.jump_ratio(trades, fetch=fetch)
        self.assertAlmostEqual(j, 0.01)                 # (100 - 99) / 100 on the first bar
        self.assertEqual(sorted(calls), ["A", "B"])
        calls.clear()
        j2 = es.jump_ratio(trades, fetch=lambda t: self._bars(t0, [[100, 100, 99.9, 100]]))
        self.assertAlmostEqual(j2, 0.01)                # B scanned now; smaller move does not lower J
        self.assertTrue(os.path.exists(es._STATE))

    def test_corrupt_j_rescans_everything(self):
        import json as _json
        trades = es.completed_trades(_trade("A", 0.5, 100.0, 99.0, 101.0))
        t0 = trades[0]["entry_ts"]
        es._STATE.write_text(_json.dumps({"J": "bad", "scanned": ["A"]}))
        j = es.jump_ratio(trades, fetch=lambda t: self._bars(t0, [[100, 100.5, 98.0, 99.5]]))
        self.assertAlmostEqual(j, 0.02)                 # A rescanned; J rebuilt, not lowered to None/0

    def test_none_before_any_path(self):
        self.assertIsNone(es.jump_ratio([], fetch=lambda t: None))


class ConvEntry(unittest.TestCase):
    """_conv_entry with mocked journal, data and account inputs (no broker)."""

    def test_unreadable_journal_keeps_todays_sizing(self):
        from execution import day_trade_manager as dtm
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], False)):
            self.assertEqual(dtm._conv_entry("AAPL", "long", {"conviction": 0.5}, {}, 100.0, 98.0, 100.0,
                                             2400.0, 2400.0, {}, {}, {}, 5000.0, 500.0, 0.3, []), (None, None))

    def test_proven_band_sizes_and_account_rooms_cut_shares(self):
        from execution import day_trade_manager as dtm
        prior = datetime(2026, 10, 14, 10, 0, tzinfo=ET)
        ev = []
        for i, c in enumerate((0.40, 0.41, 0.42, 0.50, 0.51, 0.52, 0.70, 0.71, 0.72)):
            ev += _trade(f"X{i}", c, 100.0, 99.0, 101.0 if 0.45 < c < 0.6 else 99.0, day=prior)
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(ev, True)), \
                mock.patch.object(es, "jump_ratio", return_value=0.005), \
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "none")), \
                mock.patch.object(dtm, "_robust_atr_5m", return_value=0.4), \
                mock.patch.object(dtm, "_realtime_vol_room", return_value=None), \
                mock.patch("data.alpaca_data.get_quote_spreads", return_value=[0.1, 0.1, 0.2]):
            plan, sized = dtm._conv_entry("AAPL", "long", {"conviction": 0.51}, {}, 100.0, 98.0, 100.0,
                                          2400.0, 2400.0, {}, {}, {}, 2400.0, 0.0, 0.3, [])
        self.assertTrue(plan["proven"])
        # budget 5% x 2400 = 120; per-share max(0.6 + max(0*2, 0.6), 0.5) = 1.2 -> 100 sh by budget;
        # buying power room 2400 - 1200 reserve = 1200 -> 12 sh at $100 -> rooms cut shares, stop stays 99.40
        self.assertEqual(sized["qty"], 12)
        self.assertAlmostEqual(sized["stop"], 99.40)

    def _sized(self, symbol, size, buying_power=20000.0):
        from execution import day_trade_manager as dtm
        prior = datetime(2026, 10, 14, 10, 0, tzinfo=ET)
        ev = []
        for i, c in enumerate((0.40, 0.41, 0.42, 0.50, 0.51, 0.52, 0.70, 0.71, 0.72)):
            ev += _trade(f"X{i}", c, 100.0, 99.0, 101.0 if 0.45 < c < 0.6 else 99.0, day=prior)
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(ev, True)), \
                mock.patch.object(es, "jump_ratio", return_value=0.005), \
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "none")), \
                mock.patch.object(dtm, "_robust_atr_5m", return_value=0.4), \
                mock.patch.object(dtm, "_realtime_vol_room", return_value=None), \
                mock.patch.object(dtm, "_track_b_reserve_active", return_value=False), \
                mock.patch("data.alpaca_data.get_quote_spreads", return_value=[0.1, 0.1, 0.2]):
            return dtm._conv_entry(symbol, "long", size, {}, 100.0, 98.0, 100.0,
                                   2400.0, 2400.0, {}, {}, {}, buying_power, 0.0, 0.3, [])[1]

    def test_deep_name_clamped_to_single_name_room(self):
        # Parity with _bounded_entry_qty (cold review 2026-10-19): with buying power ample, the single-name room
        # DAYTRADE_MAX_SINGLE_NAME_NOTIONAL_PCT x equity binds a deep name, 10/10 or not.
        from config import DAYTRADE_MAX_SINGLE_NAME_NOTIONAL_PCT as sn
        for sz in ({"conviction": 0.51, "track": "A"}, {"conviction": 0.51, "track": "B", "max_size": True}):
            sized = self._sized("AAPL", sz)
            self.assertEqual(sized["qty"], int(sn * 2400.0 // 100.0))
            self.assertIn("single_name", sized["why"])

    def test_thin_name_clamped_to_thin_room(self):
        from config import DAYTRADE_THIN_NAME_MAX_NOTIONAL_USD as thin, DAYTRADE_TRACK_A_BASE_CEILING_PCT as base
        sized = self._sized("XYZQ", {"conviction": 0.51, "track": "B", "max_size": True})
        self.assertEqual(sized["qty"], int(min(thin, base * 2400.0) // 100.0))
        self.assertIn("thin_name", sized["why"])


class DailyRiskConvLots(unittest.TestCase):
    """_daily_risk_used charges a CONV lot its full per-share risk, journaled or state-only (cold-2nd 2026-10-19)."""

    def _used(self, open_trades, state, journal):
        from execution import day_trade_manager as dtm
        with mock.patch.object(dtm, "_realized_loss_today", return_value=(0.0, True)), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(journal, True)), \
                mock.patch.object(dtm, "_now_et", return_value=datetime(2026, 10, 20, 10, 0, tzinfo=ET)):
            return dtm._daily_risk_used(open_trades, state, 48.0)

    def test_journaled_conv_lot_full_risk(self):
        ot = {"C1": {"trade_id": "C1", "fill_qty": 40, "entry_price": 100.0}}
        st = {"entry::AAPL::20261020-0945": {"coid": "C1", "state": "protected", "bar_id": "20261020-0945",
                                              "risk_per_share": 3.0, "qty": 40}}
        jr = [{"event": "stop_placed", "trade_id": "C1", "stop_price": 99.4},
              {"event": "entry_fill", "trade_id": "C1"}]
        self.assertAlmostEqual(self._used(ot, st, jr)[1], 120.0)      # not 40 x 0.60 = 24

    def test_state_only_conv_lot_full_risk(self):
        st = {"entry::AAPL::20261020-0945": {"coid": "C2", "state": "fill_unverified", "bar_id": "20261020-0945",
                                              "risk_per_share": 3.0, "qty": 40}}
        self.assertAlmostEqual(self._used({}, st, [])[1], 120.0)      # not the $48 ceiling

    def test_legacy_lot_unchanged(self):
        st = {"entry::AAPL::20261020-0945": {"coid": "L1", "state": "submitted", "bar_id": "20261020-0945", "qty": 40}}
        self.assertAlmostEqual(self._used({}, st, [])[1], 48.0)

    def test_non_positive_rps_blocks(self):
        st = {"entry::AAPL::20261020-0945": {"coid": "C4", "state": "submitted", "bar_id": "20261020-0945",
                                              "risk_per_share": -3.0, "qty": 40}}
        self.assertEqual(self._used({}, st, [])[1], float("inf"))

    def test_unreadable_conv_lot_blocks(self):
        st = {"entry::AAPL::20261020-0945": {"coid": "C3", "state": "submitted", "bar_id": "20261020-0945",
                                              "risk_per_share": "bad", "qty": 40}}
        self.assertEqual(self._used({}, st, [])[1], float("inf"))


class PlaceEntryConv(unittest.TestCase):
    """place_entry end to end up to submit, with the CONV result injected (all broker calls mocked)."""

    def _run(self, conv, track="A", max_size=None):
        from types import SimpleNamespace
        from execution import broker
        from execution import day_trade_manager as dtm
        from data.live_price import LivePrice
        sub, saved = {}, []
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)

        def _submit(sym, qty, side, px, **k):
            sub.update(qty=qty, px=px)
            return None
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "RIDE", "entry_ref": 100.0, "target": None,
                   "wall_ref": 97.0}
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_load_state", return_value={}), \
                mock.patch.object(dtm, "_save_state", side_effect=lambda st: saved.append(
                    {k: dict(v) for k, v in st.items() if isinstance(v, dict)}) or True), \
                mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _l, st: (st, "kept")), \
                mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None), \
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")), \
                mock.patch.object(dtm, "_conv_entry", return_value=conv) as ce, \
                mock.patch("data.live_price.live_price", return_value=LivePrice(100.0, "iex_trade", 1.0)), \
                mock.patch("data.alpaca_data.get_latest_quote", return_value=None), \
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch("strategy.day_tier_logger.log_decision", return_value=True), \
                mock.patch.object(broker, "get_account", return_value=acct), \
                mock.patch.object(broker, "get_open_positions", return_value=[]), \
                mock.patch.object(broker, "get_open_orders", return_value=[]), \
                mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30), \
                mock.patch("execution.tier_capital_allocator.live_admit",
                           return_value=SimpleNamespace(approved=True, lease=None, reason="")), \
                mock.patch("execution.tier_capital_allocator.live_order_id", return_value="DT-x"), \
                mock.patch("execution.tier_capital_allocator.live_release", return_value=None), \
                mock.patch.object(broker, "submit_limit_order", side_effect=_submit):
            size = {"size_ok": True, "shares": 5, "conviction": 0.5, "track": track, "budget": 300.0}
            if max_size is not None:
                size["max_size"] = max_size
            ok = dtm.place_entry("AAPL", {"would_consider": True, "conviction": 0.5}, trigger, size,
                                 bar_id="20261020-0945", equity=2500.0)
        return ok, sub, saved, ce

    def test_unproven_band_still_trades_at_todays_size(self):
        # owner 2026-10-19: losing ranges keep trading (data collection) at today's sizing, never skipped
        ok, sub, saved, _ce = self._run(({"proven": False, "band": "top", "band_mean_r": -0.27, "band_n": 12}, None))
        self.assertIn("qty", sub)                      # an order was submitted
        entry = next(v for s in saved for k, v in s.items() if k.startswith("entry::"))
        self.assertNotIn("risk_per_share", entry)      # not upsized
        self.assertAlmostEqual(entry["stop_px"], 96.9) # today's stop: wall 97 less the existing buffer

    def test_proven_band_wires_conv_qty_and_tight_stop(self):
        sized = {"ok": True, "stop": 99.40, "qty": 7, "per_share": 1.2, "why": "conv test"}
        ok, sub, saved, _ce = self._run(({"proven": True, "band": "middle", "band_mean_r": 0.3, "band_n": 12}, sized))
        self.assertEqual(sub["qty"], 7)
        rec = [s for s in saved if any(k.startswith("entry::") for k in s)][0]
        entry = next(v for k, v in rec.items() if k.startswith("entry::"))
        self.assertAlmostEqual(entry["stop_px"], 99.40)
        self.assertAlmostEqual(entry["risk_per_share"], 1.2)

    def test_no_record_keeps_todays_sizing(self):
        ok, sub, saved, _ce = self._run((None, None))
        self.assertIn("qty", sub)                      # today's path still submits
        entry = next(v for s in saved for k, v in s.items() if k.startswith("entry::"))
        self.assertNotIn("risk_per_share", entry)

    def test_track_b_keeps_exposure_cap(self):
        sized = {"ok": True, "stop": 99.40, "qty": 50, "per_share": 1.2, "why": "conv test"}
        ok, sub, _saved, _ce = self._run(({"proven": True, "band": "middle", "band_mean_r": 0.3, "band_n": 12}, sized),
                                         track="B")
        self.assertEqual(sub["qty"], 2)                # floor($300 budget / $100.20 limit) = 2

    def test_track_b_ten_of_ten_skips_exposure_cap(self):
        # CEO 2026-10-10 "no caps if the conviction is high and the stops are being respected": a 10/10 (max_size)
        # Track-B entry keeps its conviction size, as _bounded_entry_qty does (10/19 AMZN was wired 1 of a sized 18).
        sized = {"ok": True, "stop": 99.40, "qty": 50, "per_share": 1.2, "why": "conv test"}
        ok, sub, _saved, _ce = self._run(({"proven": True, "band": "middle", "band_mean_r": 0.3, "band_n": 12}, sized),
                                         track="B", max_size=True)
        self.assertEqual(sub["qty"], 50)

    def test_track_b_not_ten_of_ten_keeps_exposure_cap(self):
        sized = {"ok": True, "stop": 99.40, "qty": 50, "per_share": 1.2, "why": "conv test"}
        ok, sub, _saved, _ce = self._run(({"proven": True, "band": "middle", "band_mean_r": 0.3, "band_n": 12}, sized),
                                         track="B", max_size=False)
        self.assertEqual(sub["qty"], 2)

    def test_track_m_not_routed(self):
        _ok, _sub, _saved, ce = self._run((None, None), track="M")
        ce.assert_not_called()


class ScaleOutExit(unittest.TestCase):
    """CONV-1018 split exit: a CONV-sized lot puts 1/(targets + 1) in the OCO at the target and the rest under a plain
    stop; other lots keep one OCO for the whole fill. Every broker call is mocked."""

    def _run(self, conv_sized, filled=10, rest_ok=True, oco_ok=True, undo_booked=0):
        from types import SimpleNamespace
        from execution import broker
        from execution import day_trade_manager as dtm
        from data.live_price import LivePrice
        calls = {"oco": [], "stops": [], "cancel_legs": 0}
        state: dict = {}
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "RIDE", "entry_ref": 100.0, "target": None,
                   "wall_ref": 97.0}

        def _oco(sym, qty, side, tp, stop, tier="daytrade"):
            calls["oco"].append((qty, tp, stop))
            return SimpleNamespace(id="TP1", legs=[SimpleNamespace(id="SL1", order_type="stop")]) if oco_ok else None

        def _stop(sym, qty, side, stop, tier="daytrade", **k):
            calls["stops"].append((qty, stop, k.get("allow_cancel_blocking", True)))
            if not rest_ok and k.get("allow_cancel_blocking") is False:
                return None
            return SimpleNamespace(id=f"ST{len(calls['stops'])}")

        def _cancel_legs(sym):
            calls["cancel_legs"] += 1

        conv = (({"proven": True, "band": "middle", "band_mean_r": 0.3, "band_n": 12}, conv_sized)
                if conv_sized else (None, None))
        pos = SimpleNamespace(qty=str(filled), side="long", current_price="100.1", avg_entry_price="100.0")
        import contextlib
        with contextlib.ExitStack() as _es:
            _es.enter_context(mock.patch.object(dtm, "_enabled", return_value=True))
            _es.enter_context(mock.patch.object(dtm, "_load_state", return_value=state))
            _es.enter_context(mock.patch.object(dtm, "_save_state", return_value=True))
            _es.enter_context(mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _l, st: (st, "kept")))
            _es.enter_context(mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None))
            _es.enter_context(mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")))
            _es.enter_context(mock.patch.object(dtm, "_conv_entry", return_value=conv))
            _es.enter_context(mock.patch.object(dtm, "_confirm_fill", return_value=True))
            _es.enter_context(mock.patch.object(dtm, "_await_entry_terminal", return_value=True))
            _es.enter_context(mock.patch.object(dtm, "_final_fill", return_value=(float(filled), 100.0)))
            _es.enter_context(mock.patch.object(dtm, "_cancel_daytrade_exit_legs", side_effect=_cancel_legs))

            def _undo_book(t):
                t["qty"] = int(t["qty"]) - undo_booked
                return False
            _es.enter_context(mock.patch.object(dtm, "_record_confirmed_stop_exit", side_effect=_undo_book))
            _es.enter_context(mock.patch.object(dtm.time, "sleep"))
            _es.enter_context(mock.patch("data.live_price.live_price", return_value=LivePrice(100.0, "iex_trade", 1.0)))
            _es.enter_context(mock.patch("data.alpaca_data.get_latest_quote", return_value=None))
            _es.enter_context(mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}))
            _es.enter_context(mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)))
            _es.enter_context(mock.patch("strategy.day_tier_logger.log_decision", return_value=True))
            _es.enter_context(mock.patch("strategy.day_tier_logger.log_entry_fill", return_value=True))
            _es.enter_context(mock.patch("strategy.day_tier_logger.log_stop_placed", return_value=True))
            _es.enter_context(mock.patch("strategy.day_tier_logger.log_target_placed", return_value=True))
            _es.enter_context(mock.patch("trade_logger.log_event", return_value=True))
            _es.enter_context(mock.patch.object(broker, "get_account", return_value=acct))
            _es.enter_context(mock.patch.object(broker, "get_open_positions", return_value=[]))
            _es.enter_context(mock.patch.object(broker, "get_open_orders", return_value=[]))
            _es.enter_context(mock.patch.object(broker, "get_open_position", return_value=pos))
            _es.enter_context(mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30))
            _es.enter_context(mock.patch.object(broker, "cancel_open_orders_for_symbol", return_value=None))
            _es.enter_context(mock.patch.object(broker, "submit_oco_exit", side_effect=_oco))
            _es.enter_context(mock.patch.object(broker, "submit_day_stop_order", side_effect=_stop))
            _es.enter_context(mock.patch("execution.tier_capital_allocator.live_admit",
                           return_value=SimpleNamespace(approved=True, lease=None, reason="")))
            _es.enter_context(mock.patch("execution.tier_capital_allocator.live_order_id", return_value="DT-x"))
            _es.enter_context(mock.patch("execution.tier_capital_allocator.live_bind", return_value=True))
            _es.enter_context(mock.patch("execution.tier_capital_allocator.live_release", return_value=None))
            _es.enter_context(mock.patch.object(broker, "submit_limit_order",
                                  return_value=SimpleNamespace(id="E1", client_order_id="DT-x")))
            ok = dtm.place_entry("AAPL", {"would_consider": True, "conviction": 0.5}, trigger,
                                 {"size_ok": True, "shares": filled, "conviction": 0.5, "track": "A"},
                                 bar_id="20261020-0945", equity=2500.0)
        rec = next((v for k, v in state.items() if k.startswith("entry::")), {})
        return ok, calls, rec

    def _sized(self, qty):
        return {"ok": True, "stop": 99.40, "qty": qty, "per_share": 1.2, "why": "conv test"}

    def test_conv_lot_splits_half_at_target(self):
        ok, calls, rec = self._run(self._sized(10), filled=10)
        self.assertTrue(ok)
        self.assertEqual(calls["oco"][0][0], 5)                       # half in the OCO
        self.assertEqual(calls["stops"], [(5, 99.40, False)])         # the rest: plain stop, cancels nothing
        self.assertEqual(rec.get("stop2_order_id"), "ST1")
        self.assertEqual(rec.get("state"), "protected")

    def test_odd_lot_rounds_target_part_down(self):
        _ok, calls, _rec = self._run(self._sized(3), filled=3)
        self.assertEqual(calls["oco"][0][0], 1)
        self.assertEqual(calls["stops"][0][0], 2)

    def test_one_share_not_split(self):
        _ok, calls, rec = self._run(self._sized(1), filled=1)
        self.assertEqual(calls["oco"][0][0], 1)
        self.assertEqual(calls["stops"], [])
        self.assertNotIn("stop2_order_id", rec)

    def test_non_conv_lot_keeps_one_oco(self):
        _ok, calls, rec = self._run(None, filled=10)
        self.assertEqual(calls["oco"][0][0], 10)
        self.assertEqual(calls["stops"], [])
        self.assertNotIn("stop2_order_id", rec)

    def test_rest_stop_failure_undoes_split(self):
        ok, calls, rec = self._run(self._sized(10), filled=10, rest_ok=False)
        self.assertTrue(ok)
        self.assertEqual(calls["cancel_legs"], 1)                      # the split OCO was cancelled
        self.assertEqual(calls["stops"][-1], (10, 99.40, True))       # whole lot under one plain stop
        self.assertNotIn("stop2_order_id", rec)
        self.assertEqual(rec.get("state"), "protected")

    def test_undo_after_tp_fill_protects_only_held_shares(self):
        # the take-profit half filled before the undo cancel: it is booked, and the fallback stop covers the 5 left
        _ok, calls, _rec = self._run(self._sized(10), filled=10, rest_ok=False, undo_booked=5)
        self.assertEqual(calls["stops"][-1], (5, 99.40, True))

    def test_oco_failure_falls_back_to_whole_lot_stop(self):
        _ok, calls, rec = self._run(self._sized(10), filled=10, oco_ok=False)
        self.assertEqual(calls["stops"], [(10, 99.40, True)])
        self.assertNotIn("stop2_order_id", rec)

    def test_kill_flag_disables_split(self):
        from execution import day_trade_manager as dtm
        real = dtm._cfg
        with mock.patch.object(dtm, "_cfg", side_effect=lambda n, d: False if n == "DAYTRADE_CONV_SCALE_OUT_ENABLED"
                               else real(n, d)):
            _ok, calls, _rec = self._run(self._sized(10), filled=10)
        self.assertEqual(calls["oco"][0][0], 10)


class SplitLotBooking(unittest.TestCase):
    """A filled take-profit half is booked as a partial; the riding half's stop books the rest."""

    def test_tp_half_then_stop_half(self):
        from types import SimpleNamespace
        from execution import day_trade_manager as dtm
        orders = {"SL1": SimpleNamespace(status="canceled", filled_qty="0", filled_avg_price=None),
                  "ST2": SimpleNamespace(status="new", filled_qty="0", filled_avg_price=None),
                  "TP1": SimpleNamespace(status="filled", filled_qty="5", filled_avg_price="102.4")}
        journal = [{"event": "entry_fill", "trade_id": "C1"},
                   {"event": "stop_placed", "trade_id": "C1", "stop_order_id": "SL1"},
                   {"event": "target_placed", "trade_id": "C1", "tp_order_id": "TP1"},
                   {"event": "stop_placed", "trade_id": "C1", "stop_order_id": "ST2"}]
        tgt = {"trade_id": "C1", "symbol": "AAPL", "side": "long", "entry_price": 100.0, "qty": 10,
               "stop_order_id": "SL1", "tp_order_id": "TP1", "stop2_order_id": "ST2"}
        booked = []
        with mock.patch("execution.broker.get_order", side_effect=lambda oid: orders[oid]), \
                mock.patch.object(dtm.time, "sleep"), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(journal, True)), \
                mock.patch.object(dtm, "_record_partial_exit",
                                  side_effect=lambda t, oid, q, px, mk, why: booked.append((oid, q, px, why)) or True):
            self.assertFalse(dtm._record_confirmed_stop_exit(tgt))   # partial, the lot stays open
        self.assertEqual(booked, [("TP1", 5, 102.4, "take_profit_partial")])


class SplitLotStopOutSameTick(unittest.TestCase):
    """Both stops of a split lot fill before the next tick (cold-2nd + masked-loss seat 2026-10-19): one call books
    both halves (partial + exit_fill), returns True, and no halt follows. Each order is booked once."""

    def _book(self, orders, journal, tgt):
        from execution import day_trade_manager as dtm
        partials, exits = [], []

        def _partial(t, oid, q, px, mk, why):
            partials.append((oid, q))
            t["qty"] = max(0, int(t.get("qty") or 0) - q)
            return True
        with mock.patch("execution.broker.get_order", side_effect=lambda oid: orders[oid]), \
                mock.patch.object(dtm.time, "sleep"), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(journal, True)), \
                mock.patch("strategy.day_tier_logger.log_exit_fill",
                           side_effect=lambda *a, **k: exits.append((k["order_id"], k["fill_qty"], k["realized_pnl"]))
                           or True), \
                mock.patch("trade_logger.log_event", return_value=True), \
                mock.patch.object(dtm, "_record_partial_exit", side_effect=_partial):
            res = dtm._record_confirmed_stop_exit(tgt)
        return res, partials, exits

    def test_both_stops_filled_books_whole_lot_once(self):
        from types import SimpleNamespace
        orders = {"SL1": SimpleNamespace(status="filled", filled_qty="5", filled_avg_price="99.38"),
                  "ST2": SimpleNamespace(status="filled", filled_qty="5", filled_avg_price="99.36"),
                  "TP1": SimpleNamespace(status="canceled", filled_qty="0", filled_avg_price=None)}
        journal = [{"event": "entry_fill", "trade_id": "C1"},
                   {"event": "stop_placed", "trade_id": "C1", "stop_order_id": "SL1"},
                   {"event": "target_placed", "trade_id": "C1", "tp_order_id": "TP1"},
                   {"event": "stop_placed", "trade_id": "C1", "stop_order_id": "ST2"}]
        tgt = {"trade_id": "C1", "symbol": "AAPL", "side": "long", "entry_price": 100.0, "qty": 10,
               "stop_order_id": "SL1", "tp_order_id": "TP1", "stop2_order_id": "ST2"}
        res, partials, exits = self._book(orders, journal, tgt)
        self.assertTrue(res)
        self.assertEqual(partials, [("ST2", 5)])                       # booked once despite two sources
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0][0], "SL1")
        self.assertEqual(exits[0][1], 5.0)
        self.assertAlmostEqual(exits[0][2], -3.1)                      # (99.38 - 100) x 5: the loss is booked

    def test_tp_half_then_riding_stop_same_tick(self):
        from types import SimpleNamespace
        orders = {"SL1": SimpleNamespace(status="canceled", filled_qty="0", filled_avg_price=None),
                  "ST2": SimpleNamespace(status="filled", filled_qty="5", filled_avg_price="99.40"),
                  "TP1": SimpleNamespace(status="filled", filled_qty="5", filled_avg_price="101.20")}
        journal = [{"event": "entry_fill", "trade_id": "C1"},
                   {"event": "stop_placed", "trade_id": "C1", "stop_order_id": "SL1"},
                   {"event": "target_placed", "trade_id": "C1", "tp_order_id": "TP1"},
                   {"event": "stop_placed", "trade_id": "C1", "stop_order_id": "ST2"}]
        tgt = {"trade_id": "C1", "symbol": "AAPL", "side": "long", "entry_price": 100.0, "qty": 10,
               "stop_order_id": "SL1", "tp_order_id": "TP1", "stop2_order_id": "ST2"}
        res, partials, exits = self._book(orders, journal, tgt)
        self.assertTrue(res)
        self.assertEqual(partials, [("ST2", 5)])                       # the loss half first (stops before targets)
        self.assertEqual([e[0] for e in exits], ["TP1"])

    def test_unreadable_leg_after_partial_is_unknown(self):
        from types import SimpleNamespace
        orders = {"SL1": None,                                          # unreadable on every poll
                  "ST2": SimpleNamespace(status="filled", filled_qty="5", filled_avg_price="99.36"),
                  "TP1": SimpleNamespace(status="canceled", filled_qty="0", filled_avg_price=None)}
        journal = [{"event": "entry_fill", "trade_id": "C1"},
                   {"event": "stop_placed", "trade_id": "C1", "stop_order_id": "SL1"},
                   {"event": "target_placed", "trade_id": "C1", "tp_order_id": "TP1"},
                   {"event": "stop_placed", "trade_id": "C1", "stop_order_id": "ST2"}]
        tgt = {"trade_id": "C1", "symbol": "AAPL", "side": "long", "entry_price": 100.0, "qty": 10,
               "stop_order_id": "SL1", "tp_order_id": "TP1", "stop2_order_id": "ST2"}
        res, partials, exits = self._book(orders, journal, tgt)
        self.assertIsNone(res)                                          # unknown, never "nothing else filled"
        self.assertEqual(partials, [("ST2", 5)])
        self.assertEqual(exits, [])

    def test_single_full_stop_unchanged(self):
        from types import SimpleNamespace
        orders = {"SL1": SimpleNamespace(status="filled", filled_qty="10", filled_avg_price="99.40")}
        journal = [{"event": "entry_fill", "trade_id": "C2"},
                   {"event": "stop_placed", "trade_id": "C2", "stop_order_id": "SL1"}]
        tgt = {"trade_id": "C2", "symbol": "AAPL", "side": "long", "entry_price": 100.0, "qty": 10,
               "stop_order_id": "SL1"}
        res, partials, exits = self._book(orders, journal, tgt)
        self.assertTrue(res)
        self.assertEqual(partials, [])
        self.assertEqual(exits[0][1], 10.0)


class SplitHealGate(unittest.TestCase):
    """The protected-lot heal runs only when one non-polling read shows an unbooked leg fill."""

    def _gate(self, filled, journal):
        from execution import day_trade_manager as dtm
        tgt = {"trade_id": "C1", "stop_order_id": "SL1", "tp_order_id": "TP1", "stop2_order_id": "ST2"}
        with mock.patch.object(dtm, "_order_filled_qty", side_effect=lambda oid: filled[oid]), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(journal, True)):
            return dtm._split_leg_fill_unbooked(tgt)

    def test_working_lot_no_heal(self):
        self.assertFalse(self._gate({"SL1": (True, 0.0), "TP1": (True, 0.0), "ST2": (True, 0.0)}, []))

    def test_unbooked_tp_fill_heals(self):
        self.assertTrue(self._gate({"SL1": (True, 0.0), "TP1": (True, 5.0), "ST2": (True, 0.0)}, []))

    def test_already_booked_no_heal(self):
        journal = [{"event": "partial_exit_fill", "order_id": "TP1", "fill_qty": 5}]
        self.assertFalse(self._gate({"SL1": (True, 0.0), "TP1": (True, 5.0), "ST2": (True, 0.0)}, journal))

    def test_unreadable_leg_heals(self):
        self.assertTrue(self._gate({"SL1": (False, 0.0), "TP1": (True, 0.0), "ST2": (True, 0.0)}, []))


if __name__ == "__main__":
    unittest.main()
