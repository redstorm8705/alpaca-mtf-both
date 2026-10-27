#!/usr/bin/env python3
# ruff: noqa: E501
"""TRACK-A-STOP-1026 (owner-approved 2026-10-26; design record logs/design_records/track_a_noise_stop_2026-10-26.md):
a Track A stop sits at least k x the stock's time-of-day 5-minute noise from the entry limit (widen only), the whole lot
targets f x its 20-session median daily range, and shares come from the existing sizing at the wider stop.
No test reaches the broker or the data API: every call is mocked."""
import contextlib
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

from execution import broker
from execution import day_trade_manager as dtm
from execution import earned_sizing as es

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 26, 10, 30, tzinfo=ET)          # a Monday


def _sessions(n, before=None):
    out, d = [], (before or NOW.date())
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def _daily(n_prior, today=True):
    days = _sessions(n_prior)
    rows = [{"open": 100.0, "high": 101.0 + i, "low": 100.0, "close": 100.5} for i, _ in enumerate(days)]
    idx = [datetime(d.year, d.month, d.day, tzinfo=ET) for d in days]
    if today:                                              # a partial today bar must be ignored (no look-ahead)
        rows.append({"open": 100.0, "high": 500.0, "low": 1.0, "close": 100.0})
        idx.append(datetime(NOW.year, NOW.month, NOW.day, tzinfo=ET))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _fivemin(days, tr_by_day):
    rows, idx = [], []
    for d, tr in zip(days, tr_by_day, strict=True):
        for m in range(-60, 65, 5):                        # bars from 9:30 to 11:30 around 10:30
            ts = datetime(d.year, d.month, d.day, 10, 30, tzinfo=ET) + timedelta(minutes=m)
            rows.append({"open": 100.0, "high": 100.0 + tr, "low": 100.0, "close": 100.0})
            idx.append(ts)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


class Geometry(unittest.TestCase):
    def _run(self, daily, five, **cfg):
        def _fetch(sym, tf, start, end, feed="sip"):
            return daily if tf == "1Day" else five
        with contextlib.ExitStack() as x:
            x.enter_context(mock.patch("data.fetcher.fetch_bars_window", side_effect=_fetch))
            for k, v in cfg.items():
                x.enter_context(mock.patch.object(dtm.config, k, v, create=True))
            return dtm._track_a_noise_geometry("TSLA", NOW)

    def test_values_from_prior_sessions_only(self):
        days5 = _sessions(5)
        g = self._run(_daily(25), _fivemin(days5, [1.0, 2.0, 3.0, 4.0, 5.0]))
        self.assertIsNotNone(g)
        self.assertAlmostEqual(g["atr5"], 3.0)            # median of the five sessions' mean true range
        self.assertAlmostEqual(g["room"], 18.0)           # 6 x 3.0
        # ranges of the last 20 prior sessions are 6..25 (high = 101 + i over 25 days) -> median 15.5; today ignored
        self.assertAlmostEqual(g["range_median"], 15.5)
        self.assertAlmostEqual(g["target_dist"], round(0.35 * 15.5, 4))
        self.assertEqual(g["atr5_sessions"], 5)

    def test_time_of_day_window_only(self):
        days5 = _sessions(5)
        five = _fivemin(days5, [2.0] * 5)
        # a huge bar far from 10:30 is outside the window and must not move the noise
        d = days5[-1]
        five.loc[pd.Timestamp(datetime(d.year, d.month, d.day, 15, 0, tzinfo=ET))] = [100.0, 150.0, 100.0, 100.0]
        g = self._run(_daily(25), five.sort_index())
        self.assertAlmostEqual(g["atr5"], 2.0)

    def test_short_history_keeps_todays_geometry(self):
        self.assertIsNone(self._run(_daily(10), _fivemin(_sessions(5), [1.0] * 5)))
        self.assertIsNone(self._run(_daily(25), _fivemin(_sessions(3), [1.0] * 3)))   # only 3 of 5 sessions
        self.assertIsNone(self._run(pd.DataFrame(), pd.DataFrame()))
        self.assertIsNone(self._run(None, None))

    def test_kill_flag(self):
        self.assertIsNone(self._run(_daily(25), _fivemin(_sessions(5), [1.0] * 5),
                                    DAYTRADE_TRACK_A_NOISE_STOP_ENABLED=False))

    def test_zero_noise_keeps_todays_geometry(self):
        self.assertIsNone(self._run(_daily(25), _fivemin(_sessions(5), [0.0] * 5)))

    def test_fetch_error_never_raises(self):
        with mock.patch("data.fetcher.fetch_bars_window", side_effect=RuntimeError("down")):
            self.assertIsNone(dtm._track_a_noise_geometry("TSLA", NOW))


class ConvSizeNoiseRoom(unittest.TestCase):
    def test_tight_stop_never_inside_noise_room(self):
        # limit 100, today's (already widened) stop 94 -> S 6; ATR 0.5 x 1.5 = 0.75 tight; noise room 4
        r = es.conv_size("long", 100.0, 94.0, 100.0, 0.5, 0.02, 1.5, 2.0, 0.0, 0.0, 100.0, min_room=4.0)
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["stop"], 96.0)
        self.assertEqual(r["binding"], "track-a noise room")

    def test_noise_room_never_wider_than_todays_stop(self):
        r = es.conv_size("short", 100.0, 103.0, 100.0, 0.5, 0.02, 1.5, 2.0, 0.0, 0.0, 100.0, min_room=9.0)
        self.assertAlmostEqual(r["stop"], 103.0)
        self.assertEqual(r["binding"], "stop")

    def test_no_room_unchanged(self):
        a = es.conv_size("long", 100.0, 94.0, 100.0, 0.5, 0.02, 1.5, 2.0, 0.0, 0.0, 100.0)
        b = es.conv_size("long", 100.0, 94.0, 100.0, 0.5, 0.02, 1.5, 2.0, 0.0, 0.0, 100.0, min_room=None)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a["stop"], 99.25)


class _Leg:
    def __init__(self, oid, otype):
        self.id, self.order_type, self.type = oid, otype, otype


class PlaceEntry(unittest.TestCase):
    """place_entry end to end with every broker/data call mocked: Track A widened stop, existing sizing at the wider
    stop, whole-lot range target; Track B and missing history unchanged."""

    def _run(self, geom, track="A", conv=(None, None), fill_qty=None):
        from data.live_price import LivePrice
        sub, oco, stops, saved = {}, [], [], []
        acct = SimpleNamespace(equity="2650", last_equity="2650", buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)

        def _submit(sym, qty, side, px, **k):
            sub.update(qty=qty, px=px)
            return SimpleNamespace(id="E1", client_order_id="DT-x")

        def _oco(sym, qty, direction, tp, stop, **k):
            oco.append({"qty": qty, "tp": tp, "stop": stop})
            return SimpleNamespace(id="TP1", legs=[_Leg("ST1", "stop")])

        def _stop(sym, qty, side, px, **k):
            stops.append({"qty": qty, "stop": px})
            return SimpleNamespace(id="ST2")
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "RIDE", "entry_ref": 100.0, "target": None,
                   "wall_ref": 97.0}
        geo = mock.Mock(return_value=geom)
        with contextlib.ExitStack() as x:
            for p in (
                mock.patch.object(dtm, "_enabled", return_value=True),
                mock.patch.object(dtm, "_load_state", return_value={}),
                mock.patch.object(dtm, "_save_state", side_effect=lambda st: saved.append(
                    {k: dict(v) for k, v in st.items() if isinstance(v, dict)}) or True),
                mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _l, st: (st, "kept")),
                mock.patch.object(dtm, "_track_a_noise_geometry", geo),
                mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None),
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")),
                mock.patch.object(dtm, "_conv_inputs", return_value={"stage": "ok"}),
                mock.patch.object(dtm, "_conv_compute", return_value=conv),
                mock.patch.object(dtm, "_confirm_fill", return_value=True),
                mock.patch.object(dtm, "_await_entry_terminal", return_value=True),
                mock.patch.object(dtm, "_final_fill", side_effect=lambda _o: (float(fill_qty or sub["qty"]), 100.20)),
                mock.patch.object(dtm.time, "sleep"),
                mock.patch("data.live_price.live_price", return_value=LivePrice(100.0, "iex_trade", 1.0)),
                mock.patch("data.alpaca_data.get_latest_quote", return_value=None),
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}),
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)),
                mock.patch("strategy.day_tier_logger.log_decision", return_value=True),
                mock.patch("strategy.day_tier_logger.log_entry_fill", return_value=True),
                mock.patch("strategy.day_tier_logger.log_stop_placed", return_value=True),
                mock.patch("strategy.day_tier_logger.log_target_placed", return_value=True),
                mock.patch("trade_logger.log_event", return_value=True),
                mock.patch.object(broker, "get_account", return_value=acct),
                mock.patch.object(broker, "get_open_positions", return_value=[]),
                mock.patch.object(broker, "get_open_position", return_value=None),
                mock.patch.object(broker, "get_open_orders", return_value=[]),
                mock.patch.object(broker, "cancel_open_orders_for_symbol", return_value=None),
                mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30),
                mock.patch("execution.tier_capital_allocator.live_admit",
                           return_value=SimpleNamespace(approved=True, lease=None, reason="")),
                mock.patch("execution.tier_capital_allocator.live_order_id", return_value="DT-x"),
                mock.patch("execution.tier_capital_allocator.live_bind", return_value=True),
                mock.patch("execution.tier_capital_allocator.live_release", return_value=None),
                mock.patch.object(broker, "submit_limit_order", side_effect=_submit),
                mock.patch.object(broker, "submit_oco_exit", side_effect=_oco),
                mock.patch.object(broker, "submit_day_stop_order", side_effect=_stop),
            ):
                x.enter_context(p)
            size = {"size_ok": True, "shares": 15, "conviction": 0.5, "track": track, "budget": 1500.0}
            ok = dtm.place_entry("TSLA", {"would_consider": True, "conviction": 0.5}, trigger, size,
                                 bar_id="20261026-1030", equity=2650.0)
        entry = next((v for s in reversed(saved) for k, v in s.items() if k.startswith("entry::")), {})
        return ok, sub, oco, stops, entry, geo

    _GEOM = {"atr5": 1.0, "atr5_sessions": 5, "k": 6.0, "f": 0.35, "range_median": 20.0, "range_sessions": 20,
             "room": 6.0, "target_dist": 7.0}

    def test_today(self):
        ok, sub, oco, _st, entry, _g = self._run(None)
        self.assertTrue(ok)
        self.assertAlmostEqual(entry["stop_px"], 96.9)      # wall 97 less the buffer, unchanged
        self.assertAlmostEqual(oco[0]["tp"], round(100.20 + 2 * (100.20 - 96.9), 2))   # today's 2R target
        self.assertEqual(sub["qty"], 12)                    # 1.5% x $2,650 / $3.30 = 12 shares

    def test_track_a_wider_stop_fewer_shares_whole_lot_range_target(self):
        ok, sub, oco, stops, entry, _g = self._run(dict(self._GEOM))
        self.assertTrue(ok)
        self.assertAlmostEqual(entry["stop_px"], round(100.20 - 6.0, 2))   # limit 100.20 less the 6.0 noise room
        self.assertEqual(sub["qty"], 6)                     # same $39.75 budget / $6.00 stop -> 6 (fewer than 12)
        self.assertLessEqual(sub["qty"] * 6.0, 0.015 * 2650 + 1e-9)        # dollar risk inside the existing budget
        self.assertEqual(len(oco), 1)
        self.assertEqual(oco[0]["qty"], 6)                   # whole lot in the OCO, no split
        self.assertAlmostEqual(oco[0]["tp"], round(100.20 + 7.0, 2))       # fill + 0.35 x range
        self.assertEqual(stops, [])

    def test_conviction_lot_is_not_split(self):
        sized = {"ok": True, "stop": 94.20, "qty": 4, "per_share": 6.5, "why": "conv test"}
        ok, sub, oco, stops, _e, _g = self._run(dict(self._GEOM),
                                                conv=({"proven": True, "band": "top", "band_mean_r": 0.2,
                                                       "band_n": 9}, sized))
        self.assertTrue(ok)
        self.assertEqual(sub["qty"], 4)
        self.assertEqual(oco[0]["qty"], 4)                   # CONV-1018 half/half split replaced by the whole lot
        self.assertEqual(stops, [])

    def test_never_more_shares_than_today(self):
        _ok, today, *_ = self._run(None)
        _ok, wide, *_ = self._run(dict(self._GEOM))
        self.assertLessEqual(wide["qty"], today["qty"])

    def test_narrower_room_keeps_wider_existing_stop(self):
        g = dict(self._GEOM, room=1.0)
        _ok, _sub, _oco, _st, entry, _g = self._run(g)
        self.assertAlmostEqual(entry["stop_px"], 96.9)      # widen only: the existing 3.30 room already exceeds 1.0

    def test_track_b_untouched(self):
        _ok, _sub, _oco, _st, entry, geo = self._run(dict(self._GEOM), track="B")
        geo.assert_not_called()
        self.assertAlmostEqual(entry["stop_px"], 96.9)

    def test_decision_stack_logged(self):
        with self.assertLogs(dtm.logger, level="INFO") as cm:
            self._run(dict(self._GEOM))
        self.assertTrue(any("change=track-a-stop-1026" in m and "room 6.0000" in m for m in cm.output))

    def test_room_above_one_share_budget_is_capped_not_skipped(self):
        # SNDK-style: noise room $60 > the $39.75 one-share budget (1.5% x $2,650) -> stop capped, still 1 share
        ok, sub, oco, _st, entry, _g = self._run(dict(self._GEOM, room=60.0))
        self.assertTrue(ok)
        self.assertAlmostEqual(entry["stop_px"], round(100.20 - 39.75, 2))
        self.assertEqual(sub["qty"], 1)
        self.assertLessEqual(sub["qty"] * (100.20 - entry["stop_px"]), 0.015 * 2650 + 1e-9)
        self.assertEqual(oco[0]["qty"], 1)
        self.assertAlmostEqual(oco[0]["tp"], round(100.20 + 7.0, 2))       # range target kept

    def test_cap_unavailable_keeps_todays_stop_and_target(self):
        with mock.patch.object(dtm, "_track_a_one_share_stop", return_value=None):
            ok, sub, oco, _st, entry, _g = self._run(dict(self._GEOM, room=60.0))
        self.assertTrue(ok)
        self.assertAlmostEqual(entry["stop_px"], 96.9)
        self.assertEqual(sub["qty"], 12)
        self.assertAlmostEqual(oco[0]["tp"], round(100.20 + 2 * (100.20 - 96.9), 2))


class OneShareStop(unittest.TestCase):
    def test_long_and_short_at_the_budget(self):
        size = {"track": "A"}
        self.assertAlmostEqual(dtm._track_a_one_share_stop("long", 400.0, 2650.0, 2650.0, size), 360.25)
        self.assertAlmostEqual(dtm._track_a_one_share_stop("short", 400.0, 2650.0, 2650.0, size), 439.75)

    def test_one_share_always_affordable(self):
        for lim in (12.34, 99.99, 249.36, 512.07, 1033.33):
            for eq in (2650.0, 2647.63, 2711.11):
                for d in ("long", "short"):
                    s = dtm._track_a_one_share_stop(d, lim, eq, eq, {})
                    self.assertIsNotNone(s)
                    if s == 0.0:                                    # budget wider than a long's whole price: no cap
                        self.assertEqual(d, "long")
                        self.assertLessEqual(lim, eq * 0.015 + 0.01)
                        continue
                    self.assertGreaterEqual(int((eq * 0.015) // abs(lim - s)), 1, (d, lim, eq, s))

    def test_lower_of_live_and_start_equity_and_max_size(self):
        self.assertAlmostEqual(dtm._track_a_one_share_stop("long", 400.0, 2000.0, 2650.0, {}), 370.0)
        self.assertAlmostEqual(dtm._track_a_one_share_stop("long", 400.0, 2650.0, 2650.0, {"max_size": True}), 347.0)
        s = dtm._track_a_one_share_stop("long", 400.0, 2650.0, 2650.0, {"risk_mult": 0.5})
        self.assertTrue(380.12 <= s <= 380.13 and (2650.0 * 0.015 * 0.5) // (400.0 - s) >= 1, s)

    def test_cheap_long_cap_does_not_bind(self):
        # $30 long, $39.75 budget: every positive stop affords one share -> 0.0, so the combine keeps the noise stop
        self.assertEqual(dtm._track_a_one_share_stop("long", 30.0, 2650.0, 2650.0, {}), 0.0)
        self.assertEqual(max(24.0, 0.0), 24.0)                       # place_entry's long combine: max(noise, cap)
        self.assertAlmostEqual(dtm._track_a_one_share_stop("short", 30.0, 2650.0, 2650.0, {}), 69.75)

    def test_bad_input_none(self):
        self.assertIsNone(dtm._track_a_one_share_stop("long", 400.0, float("nan"), 2650.0, {}))
        self.assertIsNone(dtm._track_a_one_share_stop("long", 400.0, 2650.0, 2650.0, {"risk_mult": 2.0}))
        self.assertIsNone(dtm._track_a_one_share_stop("long", 0.0, 2650.0, 2650.0, {}))


if __name__ == "__main__":
    unittest.main()
