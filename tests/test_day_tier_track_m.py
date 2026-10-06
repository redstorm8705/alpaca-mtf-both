#!/usr/bin/env python3
# ruff: noqa: E501
"""Track M — QQQ Monday weekend-gap-down buy (Rafael-approved 2026-10-05). Eligibility, auto-off, sizing scale,
the explicit 1% stop, the no-target exit path, the shrink-only risk multiplier and the runner's one-shot marker."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from execution import day_trade_manager as dtm
from strategy import day_tier_track_m as tm

ET = tm.ET


def _bars(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz=ET) for t, _ in rows])
    return pd.DataFrame({"open": [v for _, v in rows], "close": [v for _, v in rows]}, index=idx)


MON = datetime(2026, 10, 12, 9, 47, tzinfo=ET)      # a Monday
FRI = "2026-10-09"


def _fetch_live(last="2026-10-12 09:46", px=595.0):
    return lambda symbol, tf, start, end, feed="iex", adjustment="raw", asof=None: \
        _bars([("2026-10-12 09:44", px - 0.5), (last, px)])


def _fetch_window(daily_last="2026-10-09", fri_close=600.0, open_930=594.0, first="2026-10-12 09:30", live=None):
    live = live or _fetch_live()

    def f(symbol, tf, start, end, feed="sip", adjustment="raw", asof=None):
        if feed == "iex":              # the real-time entry-reference read
            return live(symbol, tf, start, end, feed=feed, adjustment=adjustment)
        if tf == "1Day":
            return _bars([("2026-10-08", 601.0), (daily_last, fri_close)])
        return _bars([(first, open_930)]) if open_930 is not None else None
    return f


class Window(unittest.TestCase):
    def test_bounds(self):
        self.assertFalse(tm.in_window(MON.replace(minute=44)))
        self.assertTrue(tm.in_window(MON.replace(minute=45)))
        self.assertTrue(tm.in_window(MON.replace(hour=10, minute=14)))
        self.assertFalse(tm.in_window(MON.replace(hour=10, minute=15)))


class Evaluate(unittest.TestCase):
    def _ev(self, now=MON, prev=FRI, mins=373.0, window=None, live=None):
        win = window or _fetch_window(live=live)
        with mock.patch("data.fetcher.fetch_bars_window", side_effect=win), \
                mock.patch("data.fetcher.fetch_bars", side_effect=AssertionError("delayed default feed must not be used")):
            return tm.evaluate(now, prev, mins)

    def test_eligible_gap_down(self):
        r = self._ev()
        self.assertTrue(r["eligible"], r)
        self.assertEqual(r["entry_ref"], 595.0)
        self.assertEqual(r["stop_ref"], round(595.0 * 0.99, 2))
        self.assertLess(r["gap_pct"], 0)

    def test_not_monday(self):
        r = self._ev(now=datetime(2026, 10, 13, 9, 47, tzinfo=ET))
        self.assertFalse(r["eligible"])
        self.assertFalse(r["retry"])

    def test_friday_holiday_skips(self):
        r = self._ev(prev="2026-10-08")              # previous session Thursday
        self.assertFalse(r["eligible"])
        self.assertFalse(r["retry"])

    def test_calendar_unknown_retries(self):
        r = self._ev(prev=None)
        self.assertFalse(r["eligible"])
        self.assertTrue(r["retry"])

    def test_half_day_skips(self):
        r = self._ev(mins=195.0)
        self.assertFalse(r["eligible"])
        self.assertFalse(r["retry"])
        self.assertFalse(self._ev(mins=None)["eligible"])

    def test_gap_up_or_flat_skips_definitively(self):
        for o in (600.0, 603.0):
            r = self._ev(window=_fetch_window(open_930=o))
            self.assertFalse(r["eligible"])
            self.assertFalse(r["retry"])

    def test_todays_forming_daily_bar_is_ignored(self):
        def f(symbol, tf, start, end, feed="sip", adjustment="raw", asof=None):
            if feed == "iex":
                return _fetch_live()(symbol, tf, start, end)
            if tf == "1Day":   # Alpaca stamps daily bars at 00:00 ET, so today's forming bar can be returned
                return _bars([("2026-10-08", 601.0), ("2026-10-09", 600.0), ("2026-10-12", 590.0)])
            return _bars([("2026-10-12 09:30", 594.0)])
        r = self._ev(window=f)
        self.assertTrue(r["eligible"], r)
        self.assertEqual(r["friday_close"], 600.0)

    def test_stale_friday_bar_retries(self):
        r = self._ev(window=_fetch_window(daily_last="2026-10-08"))
        self.assertFalse(r["eligible"])
        self.assertTrue(r["retry"])

    def test_missing_open_bar_retries(self):
        r = self._ev(window=_fetch_window(open_930=None))
        self.assertTrue(r["retry"])
        self.assertFalse(r["eligible"])

    def test_first_bar_not_930_retries(self):
        r = self._ev(window=_fetch_window(first="2026-10-12 09:31"))
        self.assertTrue(r["retry"])

    def test_live_reference_uses_realtime_iex_feed(self):
        calls = []
        base = _fetch_window()

        def spy(symbol, tf, start, end, feed="sip", adjustment="raw", asof=None):
            calls.append((tf, feed))
            return base(symbol, tf, start, end, feed=feed, adjustment=adjustment)
        self.assertTrue(self._ev(window=spy)["eligible"])
        self.assertIn(("1Min", "iex"), calls)

    def test_stale_live_bar_retries(self):
        r = self._ev(live=_fetch_live(last="2026-10-12 09:30"))
        self.assertFalse(r["eligible"])
        self.assertTrue(r["retry"])

    def test_fetch_exception_never_raises(self):
        def boom(*a, **k):
            raise RuntimeError("api down")
        r = self._ev(window=boom)
        self.assertFalse(r["eligible"])
        self.assertTrue(r["retry"])


def _trade(tid, pnl, track="M", partial=None):
    ev = [{"event": "entry_fill", "trade_id": tid, "track": track}]
    if partial is not None:
        ev.append({"event": "partial_exit_fill", "trade_id": tid, "realized_pnl": partial})
    ev.append({"event": "exit_fill", "trade_id": tid, "realized_pnl": pnl})
    return ev


class AutoOff(unittest.TestCase):
    def test_unreadable_journal_is_off(self):
        self.assertTrue(tm.auto_off([], False)[0])

    def test_no_history_is_on(self):
        self.assertFalse(tm.auto_off([], True)[0])

    def test_trailing_eight_mean_nonpositive_turns_off(self):
        ev = sum([_trade(f"T{i}", 5.0 if i % 2 else -6.0) for i in range(8)], [])
        self.assertTrue(tm.auto_off(ev, True)[0])        # mean -0.5

    def test_trailing_eight_positive_stays_on(self):
        ev = sum([_trade(f"T{i}", 7.0 if i % 2 else -6.0) for i in range(8)], [])
        self.assertFalse(tm.auto_off(ev, True)[0])

    def test_four_consecutive_losses_turn_off(self):
        ev = sum([_trade(f"T{i}", 9.0) for i in range(3)] + [_trade(f"L{i}", -1.0) for i in range(4)], [])
        self.assertTrue(tm.auto_off(ev, True)[0])

    def test_three_losses_do_not(self):
        ev = sum([_trade(f"T{i}", 9.0) for i in range(3)] + [_trade(f"L{i}", -1.0) for i in range(3)], [])
        self.assertFalse(tm.auto_off(ev, True)[0])

    def test_only_track_m_trades_count(self):
        ev = sum([_trade(f"A{i}", -5.0, track="A") for i in range(8)], [])
        self.assertFalse(tm.auto_off(ev, True)[0])

    def test_partial_exits_are_included(self):
        # exit +2 but a partial -10 -> the trade is a loss
        ev = sum([_trade(f"L{i}", 2.0, partial=-10.0) for i in range(4)], [])
        self.assertTrue(tm.auto_off(ev, True)[0])

    def test_trade_closed_by_partials_counts(self):
        ev = [{"event": "entry_fill", "trade_id": "P1", "track": "M", "fill_qty": 2},
              {"event": "partial_exit_fill", "trade_id": "P1", "realized_pnl": -3.0, "fill_qty": 1},
              {"event": "partial_exit_fill", "trade_id": "P1", "realized_pnl": -4.0, "fill_qty": 1}]
        self.assertEqual(tm._closed_track_m_pnls(ev), [-7.0])
        self.assertEqual(tm._closed_track_m_pnls(ev[:2]), [])     # half closed -> still open

    def test_open_trade_not_counted(self):
        ev = [{"event": "entry_fill", "trade_id": "O1", "track": "M"}]
        self.assertEqual(tm._closed_track_m_pnls(ev), [])


class RiskMult(unittest.TestCase):
    def test_half_until_fifteen(self):
        ev = sum([_trade(f"T{i}", 1.0) for i in range(14)], [])
        self.assertEqual(tm.risk_mult(ev), 0.5)
        ev += _trade("T15", 1.0)
        self.assertEqual(tm.risk_mult(ev), 1.0)


class StopAndTarget(unittest.TestCase):
    def test_explicit_stop(self):
        trig = {"mode": "TRACK_M", "stop_ref": 589.05}
        self.assertEqual(dtm._compute_stop_price(trig, "long", 595.0), 589.05)

    def test_missing_or_wrong_side_stop_aborts(self):
        self.assertIsNone(dtm._compute_stop_price({"mode": "TRACK_M"}, "long", 595.0))
        self.assertIsNone(dtm._compute_stop_price({"mode": "TRACK_M", "stop_ref": 596.0}, "long", 595.0))

    def test_other_modes_unchanged(self):
        self.assertEqual(dtm._compute_stop_price({"mode": "RIDE", "wall_ref": 100.0}, "long", 101.0), 99.9)


class RiskMultInCaps(unittest.TestCase):
    def _q(self, rm):
        return dtm._bounded_entry_qty(1000, 100.0, 95.0, 10000.0, {}, {}, 50000.0, 0.0, 0.25, [],
                                      risk_equity=10000.0, symbol="AAPL", track="A", risk_mult=rm)

    def test_half_risk_halves_shares(self):
        full, _ = self._q(1.0)
        half, _ = self._q(0.5)
        # risk binds: 1.5% x $10,000 / $5 stop = 30 shares (below the 65-share single-name cap); half risk = 15
        self.assertEqual(full, 30)
        self.assertEqual(half, 15)

    def test_risk_mult_cannot_raise_or_be_invalid(self):
        for bad in (1.01, 2.0, 0.0, -0.5, float("nan"), "x", None):
            self.assertEqual(self._q(bad)[0], 0, bad)


class FamilyRouteCandidate(unittest.TestCase):
    def test_exact_admitted_identity_and_aware_score_timestamp(self):
        candidate = tm.route_candidate(MON)
        self.assertEqual(candidate["family_id"], "monday_weekend_dip_v1")
        self.assertEqual(candidate["hypothesis_version"], "daytier-track-m-2026-10-05")
        self.assertEqual(candidate["routing_score"], 1.0)
        self.assertEqual(datetime.fromisoformat(candidate["score_asof"]), MON)


class PlaceEntryNoTarget(unittest.TestCase):
    """End-to-end place_entry with the broker mocked: a Track-M entry must place a PLAIN protective stop at the
    trigger's stop level and NO take-profit/OCO, and stamp the entry_fill with track 'M'."""

    def test_plain_stop_no_oco(self):
        st: dict = {}
        order = SimpleNamespace(id="O1", client_order_id="DT-QQQ-b-1", filled_qty=2, filled_avg_price=595.1,
                                status="filled")
        acct = SimpleNamespace(buying_power=9000.0, equity=2500.0, last_equity=2500.0, maintenance_margin=500.0,
                               trading_blocked=False, account_blocked=False)
        broker = mock.MagicMock()
        broker.get_account.return_value = acct
        broker.get_open_positions.return_value = []
        broker.get_open_orders.return_value = []
        broker.get_asset_maintenance_margin_rate.return_value = 0.25
        broker.submit_limit_order.return_value = order
        broker.get_order.return_value = order
        broker.get_open_position.return_value = SimpleNamespace(current_price=595.2, qty=2, side="long")
        broker.submit_day_stop_order.return_value = SimpleNamespace(id="S1")
        broker.PROTECTION_ALREADY_HELD = object()
        broker.PROTECTION_UNKNOWN = object()
        lease = SimpleNamespace(approved=True, lease="L", reason="")
        decision, trigger = tm.build_order_dicts({"entry_ref": 595.0, "stop_ref": 589.05, "reason": "t"}, 0.5)
        size = {"size_ok": True, "shares": 3, "track": "M", "risk_mult": 0.5, "budget": 0.0}
        with mock.patch.dict("sys.modules", {}), \
                mock.patch("execution.broker", broker, create=True), \
                mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_load_state", side_effect=lambda: st), \
                mock.patch.object(dtm, "_save_state", return_value=True), \
                mock.patch.object(dtm, "_min_stop_room_ok", return_value=(True, "ok")), \
                mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None), \
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "none")), \
                mock.patch.object(dtm, "_confirm_fill", return_value=True), \
                mock.patch("execution.tier_capital_allocator.live_admit", return_value=lease), \
                mock.patch("execution.tier_capital_allocator.live_bind", return_value=True), \
                mock.patch("execution.tier_capital_allocator.live_order_id", return_value="DT-QQQ-b-1"), \
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch("strategy.day_tier_logger.log_decision", return_value=True), \
                mock.patch("strategy.day_tier_logger.log_entry_fill", return_value=True) as lef, \
                mock.patch("strategy.day_tier_logger.log_stop_placed", return_value=True), \
                mock.patch("trade_logger.log_event", return_value=None):
            import execution
            with mock.patch.object(execution, "broker", broker, create=True):
                ok = dtm.place_entry("QQQ", decision, trigger, size, bar_id="20261012-TM", equity=2500.0,
                                     decision_id="TM-20261012")
        self.assertTrue(ok)
        broker.submit_oco_exit.assert_not_called()
        broker.submit_day_stop_order.assert_called_once()
        args = broker.submit_day_stop_order.call_args[0]
        self.assertEqual(args[0], "QQQ")
        self.assertEqual(args[2], "sell")
        self.assertEqual(args[3], 589.05)
        self.assertEqual(lef.call_args.kwargs.get("track"), "M")


class RunnerOneShot(unittest.TestCase):
    @staticmethod
    def _admitted_route():
        return SimpleNamespace(admitted=True, allocation=1.0, reason="ok")

    def test_marker_blocks_second_attempt(self):
        import run_day_tier as r
        st = {"_track_m_day": {"date": "20261012"}}
        dtm_mod = mock.MagicMock()
        dtm_mod._load_state.return_value = st
        with mock.patch.object(r, "datetime") as dt:
            dt.now.return_value = MON
            n, note = r._run_track_m(dtm_mod, 2500.0, 373.0, "20261012")
        self.assertEqual((n, note), (0, "used_today"))
        dtm_mod.place_entry.assert_not_called()

    def test_no_marker_no_order(self):
        import run_day_tier as r
        dtm_mod = mock.MagicMock()
        dtm_mod._load_state.return_value = {}
        ev = {"eligible": True, "retry": False, "entry_ref": 595.0, "stop_ref": 589.05, "reason": "gap"}
        with mock.patch.object(r, "datetime") as dt, \
                mock.patch("strategy.day_tier_track_m.evaluate", return_value=ev), \
                mock.patch("strategy.day_tier_track_m.mark_used", return_value=False), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch.object(r, "_prev_session_date", return_value=FRI):
            dt.now.return_value = MON
            n, note = r._run_track_m(dtm_mod, 2500.0, 373.0, "20261012")
        self.assertEqual((n, note), (0, "marker_write_failed"))
        dtm_mod.place_entry.assert_not_called()

    def test_eligible_places_one_entry_with_half_risk(self):
        import run_day_tier as r
        dtm_mod = mock.MagicMock()
        dtm_mod._load_state.return_value = {}
        dtm_mod.place_entry.return_value = True
        ev = {"eligible": True, "retry": False, "entry_ref": 595.0, "stop_ref": 589.05, "reason": "gap"}
        with mock.patch.object(r, "datetime") as dt, \
                mock.patch("strategy.day_tier_track_m.evaluate", return_value=ev), \
                mock.patch("strategy.day_tier_track_m.mark_used", return_value=True), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch("strategy.day_tier_family_router.route_families", return_value=[self._admitted_route()]), \
                mock.patch.object(r, "_prev_session_date", return_value=FRI):
            dt.now.return_value = MON
            n, note = r._run_track_m(dtm_mod, 2500.0, 373.0, "20261012")
        self.assertEqual((n, note), (1, "entered"))
        self._late(r, ev)
        size = dtm_mod.place_entry.call_args[0][3]
        self.assertEqual(size["track"], "M")
        self.assertEqual(size["risk_mult"], 0.5)
        trig = dtm_mod.place_entry.call_args[0][2]
        self.assertTrue(trig["no_target"])

    def _late(self, r, ev):
        """the window is re-checked just before ordering: a tick that started 10:14 but read data past 10:15 stops"""
        dtm_mod = mock.MagicMock()
        dtm_mod._load_state.return_value = {}
        with mock.patch.object(r, "datetime") as dt, \
                mock.patch("strategy.day_tier_track_m.evaluate", return_value=ev), \
                mock.patch("strategy.day_tier_track_m.mark_used", return_value=True), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch.object(r, "_prev_session_date", return_value=FRI):
            dt.now.side_effect = [MON.replace(hour=10, minute=14), MON.replace(hour=10, minute=15)]
            self.assertEqual(r._run_track_m(dtm_mod, 2500.0, 373.0, "20261012"), (0, "window_closed"))
        dtm_mod.place_entry.assert_not_called()

    def test_router_denial_retries_without_using_daily_shot(self):
        import run_day_tier as r
        dtm_mod = mock.MagicMock()
        dtm_mod._load_state.return_value = {}
        ev = {"eligible": True, "retry": False, "entry_ref": 595.0, "stop_ref": 589.05, "reason": "gap"}
        denied = SimpleNamespace(admitted=False, allocation=0.0, reason="version mismatch")
        with mock.patch.object(r, "datetime") as dt, \
                mock.patch("strategy.day_tier_track_m.evaluate", return_value=ev), \
                mock.patch("strategy.day_tier_track_m.mark_used") as mark_used, \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch("strategy.day_tier_family_router.route_families", return_value=[denied]), \
                mock.patch.object(r, "_prev_session_date", return_value=FRI):
            dt.now.return_value = MON
            self.assertEqual(r._run_track_m(dtm_mod, 2500.0, 373.0, "20261012"), (0, "router_denied"))
        mark_used.assert_not_called()
        dtm_mod.place_entry.assert_not_called()

    def test_invalid_router_share_never_uses_shot_or_places_order(self):
        import run_day_tier as r
        ev = {"eligible": True, "retry": False, "entry_ref": 595.0, "stop_ref": 589.05, "reason": "gap"}
        for bad in (1.01, 2.0, float("nan")):
            with self.subTest(allocation=bad):
                dtm_mod = mock.MagicMock()
                dtm_mod._load_state.return_value = {}
                routed = SimpleNamespace(admitted=True, allocation=bad, reason="malformed")
                with mock.patch.object(r, "datetime") as dt, \
                        mock.patch("strategy.day_tier_track_m.evaluate", return_value=ev), \
                        mock.patch("strategy.day_tier_track_m.mark_used") as mark_used, \
                        mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                        mock.patch("strategy.day_tier_family_router.route_families", return_value=[routed]), \
                        mock.patch.object(r, "_prev_session_date", return_value=FRI):
                    dt.now.return_value = MON
                    self.assertEqual(r._run_track_m(dtm_mod, 2500.0, 373.0, "20261012"),
                                     (0, "router_denied"))
                mark_used.assert_not_called()
                dtm_mod.place_entry.assert_not_called()

    def test_router_share_can_only_shrink_track_m_risk(self):
        import run_day_tier as r
        dtm_mod = mock.MagicMock()
        dtm_mod._load_state.return_value = {}
        dtm_mod.place_entry.return_value = True
        ev = {"eligible": True, "retry": False, "entry_ref": 595.0, "stop_ref": 589.05, "reason": "gap"}
        routed = SimpleNamespace(admitted=True, allocation=0.5, reason="capped")
        with mock.patch.object(r, "datetime") as dt, \
                mock.patch("strategy.day_tier_track_m.evaluate", return_value=ev), \
                mock.patch("strategy.day_tier_track_m.mark_used", return_value=True), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch("strategy.day_tier_family_router.route_families", return_value=[routed]), \
                mock.patch.object(r, "_prev_session_date", return_value=FRI):
            dt.now.side_effect = [MON, MON]
            self.assertEqual(r._run_track_m(dtm_mod, 2500.0, 373.0, "20261012"), (1, "entered"))
        size = dtm_mod.place_entry.call_args.args[3]
        self.assertEqual(size["risk_mult"], 0.25)

    def test_retry_does_not_use_the_shot(self):
        import run_day_tier as r
        dtm_mod = mock.MagicMock()
        dtm_mod._load_state.return_value = {}
        ev = {"eligible": False, "retry": True, "reason": "09:30 SIP opening bar unavailable — skip"}
        with mock.patch.object(r, "datetime") as dt, \
                mock.patch("strategy.day_tier_track_m.evaluate", return_value=ev), \
                mock.patch("strategy.day_tier_track_m.mark_used") as mu, \
                mock.patch.object(r, "_prev_session_date", return_value=FRI):
            dt.now.return_value = MON
            n, note = r._run_track_m(dtm_mod, 2500.0, 373.0, "20261012")
        self.assertEqual((n, note), (0, "retry"))
        mu.assert_not_called()

    def test_not_monday_is_noop(self):
        import run_day_tier as r
        dtm_mod = mock.MagicMock()
        with mock.patch.object(r, "datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 13, 9, 47, tzinfo=ET)
            self.assertEqual(r._run_track_m(dtm_mod, 2500.0, 373.0, "20261013"), (0, "outside_window"))


if __name__ == "__main__":
    unittest.main()
