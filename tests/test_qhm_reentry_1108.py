# ruff: noqa: E501
"""QHM-REENTRY-1108 (design qhm_reentry_rule_2026-11-08.md, owner-approved 2026-11-09; Release = Rung A).

After QHM's OWN full exit the pick is locked until its next earnings report has passed, unless a completed SIP daily
close since the exit is at or below exit_price x (1 + Rung A) with catalysts CLEAR; the first order is then a limit at
the trigger with a fresh ask at or below it. Owner sales and unexplained closes keep the re-date-only rule.
Every broker, data and network call is mocked: these tests can never reach Alpaca, FMP, Finnhub or Slack."""
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import execution
import execution.quarterly_hold_manager as qhm_mod
from execution.quarterly_hold_manager import HoldPosition, HoldState, QuarterlyHoldManager, ReconcileResult

try:
    import execution.broker  # noqa: F401
except ModuleNotFoundError:   # the production host has no test-only Alpaca SDK
    _stub = ModuleType("execution.broker")
    for _n in ("cancel_stop_confirmed", "get_order", "get_order_by_client_order_id", "replace_stop_order",
               "cancel_order", "get_open_orders"):
        setattr(_stub, _n, Mock())
    sys.modules["execution.broker"] = _stub
    execution.broker = _stub

ET = qhm_mod.ET
NOW = datetime(2026, 11, 10, 11, 0, tzinfo=ET)     # Tuesday
EXIT = date(2026, 11, 2)                            # Monday
REPORT = date(2026, 12, 15)
ROWS_CROSSED = [{"day": EXIT, "low": 295.0, "high": 305.0, "close": 298.0},
                {"day": date(2026, 11, 9), "low": 285.0, "high": 292.0, "close": 287.0}]
ROWS_ABOVE = [{"day": EXIT, "low": 295.0, "high": 305.0, "close": 298.0},
              {"day": date(2026, 11, 9), "low": 290.0, "high": 296.0, "close": 293.0}]


def _mgr(positions=None, nbf="2026-07-25", symbol="GE"):
    m = object.__new__(QuarterlyHoldManager)
    m.dry_run = False
    m.broker = Mock()
    m.broker.get_position.return_value = None
    m.alerter = None
    m._positions = dict(positions or {})
    m._owner_sale_unproven = {}
    m._owner_sale_retry_day = {}
    m._thesis_config = {symbol: {"not_before_date": nbf}}
    m._save_state = Mock(return_value=True)
    m._alert = Mock()
    m._now_et = Mock(return_value=NOW)
    m._get_open_orders = Mock(return_value=[])
    return m


def _locked(**kw):
    base = dict(symbol="GE", direction="long", target_equity_pct=0.2, state=HoldState.CLOSED,
                closed_reason="qhm_stop_out", updated_at="2026-11-02T10:30:00-05:00",
                reentry_exit_price=300.0, reentry_exit_date=EXIT.isoformat(), reentry_exit_route="qhm_stop_out",
                reentry_trigger=288.0, reentry_zone_date=EXIT.isoformat(),
                reentry_earnings_date=REPORT.isoformat(), reentry_earnings_source="finnhub_symbol",
                reentry_earnings_hour="bmo")
    base.update(kw)
    return HoldPosition(**base)


def _wire_release(m, rows=ROWS_CROSSED, catalyst=("CLEAR", 120, "fresh"), earnings=(REPORT, "finnhub_symbol", "bmo")):
    m._reentry_earnings = Mock(return_value=earnings)
    m._reentry_closes = Mock(return_value=rows)
    m._reentry_catalyst = Mock(return_value=catalyst)
    return m


def _guard():
    """The ownership-ledger writes the close paths make (never touches the real ledger file)."""
    return patch.multiple("execution.ownership_guard",
                          apply_authorized_tier_reduction=Mock(return_value=True),
                          apply_authorized_tier_fill_reduction=Mock(return_value=True))


class _Base(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "logs").mkdir()
        self._p = [patch.object(qhm_mod, "_ROOT", root), patch("trade_logger.log_event")]
        self.root = root
        self.log_event = None
        for p in self._p:
            started = p.start()
            if isinstance(started, Mock):
                self.log_event = started
        qhm_mod._EARNINGS_SRC_LOGGED.clear()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self._tmp.cleanup()

    def events(self):
        return [c.kwargs for c in self.log_event.call_args_list if c.args and c.args[0] == "qhm_reentry"]


# ── Lock written on every QHM close path, in the same save as CLOSED ──────────────────────────────────────────────
class LockOnEveryQhmClosePath(_Base):
    def _capture_saves(self, m, pos):
        seen = []
        m._save_state = Mock(side_effect=lambda: seen.append(
            (pos.state, pos.reentry_exit_route, pos.reentry_exit_price, pos.reentry_trigger)) or True)
        return seen

    def _zone_and_earnings(self, m):
        m._reentry_zone = Mock(return_value=(-0.04, EXIT.isoformat()))
        m._reentry_earnings = Mock(return_value=(REPORT, "finnhub_symbol", "bmo"))

    def test_own_stop_fill_detected_flat(self):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2, state=HoldState.ACTIVE,
                           qty_filled=2, qty_total=2, avg_entry_price=310.0, stop_price=290.0, stop_order_id="qh-stop")
        m = _mgr({"GE": pos})
        self._zone_and_earnings(m)
        seen = self._capture_saves(m, pos)
        fill = SimpleNamespace(status="filled", filled_avg_price="300.00", filled_at="2026-11-02T15:30:00.12345Z")
        with _guard(), \
                patch("execution.broker.get_order", return_value=fill):
            self.assertTrue(m._detect_external_close(pos, ReconcileResult()))
        self.assertIn((HoldState.CLOSED, "qhm_stop_out", 300.0, 288.0), seen)
        self.assertEqual(pos.closed_reason, "qhm_stop_out")
        self.assertEqual(pos.reentry_exit_date, EXIT.isoformat())
        self.assertEqual(pos.reentry_earnings_date, REPORT.isoformat())
        self.assertEqual(self.events()[-1]["stage"], "close")

    def test_profit_lock_stop_is_labelled(self):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2, state=HoldState.ACTIVE,
                           qty_filled=2, qty_total=2, avg_entry_price=280.0, stop_price=295.0,
                           stop_order_id="qh-stop", last_lock_date="2026-10-30")
        m = _mgr({"GE": pos})
        self._zone_and_earnings(m)
        fill = SimpleNamespace(status="filled", filled_avg_price="300.00", filled_at="2026-11-02T15:30:00Z")
        with _guard(), \
                patch("execution.broker.get_order", return_value=fill):
            m._detect_external_close(pos, ReconcileResult())
        self.assertEqual(pos.reentry_exit_route, "qhm_profit_lock_stop")
        self.assertEqual(pos.closed_reason, "qhm_stop_out")   # the lock never touches closed_reason

    def _terminal_stop(self):
        return SimpleNamespace(id="qh-stop", symbol="GE", client_order_id="QH-GE-s-1-x", side="sell", type="stop",
                               time_in_force="gtc", stop_price=290.0, status="filled", qty="2", filled_qty="2",
                               filled_avg_price="300.0", filled_at="2026-11-02T15:30:00Z")

    def _pending_stop_pos(self):
        return HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2,
                            state=HoldState.PENDING_STOP_REPLACE, qty_filled=2, qty_total=2,
                            avg_entry_price=310.0, stop_price=290.0, stop_order_id="qh-stop")

    def test_registered_stop_terminal_fill(self):
        pos = self._pending_stop_pos()
        m = _mgr({"GE": pos})
        self._zone_and_earnings(m)
        seen = self._capture_saves(m, pos)
        with _guard(), \
                patch("execution.broker.get_order", return_value=self._terminal_stop()):
            self.assertFalse(m.resubmit_stop_if_needed("GE", []))
        self.assertEqual(pos.state, HoldState.CLOSED)
        self.assertEqual(seen, [(HoldState.CLOSED, "qhm_stop_out", 300.0, 288.0)])
        self.assertIsNone(pos.closed_reason)

    def test_registered_stop_failed_save_restores_lock_fields(self):
        pos = self._pending_stop_pos()
        m = _mgr({"GE": pos})
        self._zone_and_earnings(m)
        attempted = []
        m._save_state = Mock(side_effect=lambda: attempted.append(
            (pos.state, pos.reentry_exit_route, pos.reentry_trigger)) and False)
        with _guard(), \
                patch("execution.broker.get_order", return_value=self._terminal_stop()):
            m.resubmit_stop_if_needed("GE", [])
        self.assertEqual(attempted[0], (HoldState.CLOSED, "qhm_stop_out", 288.0))   # the failed save carried the lock
        self.assertEqual(pos.state, HoldState.PENDING_STOP_REPLACE)
        self.assertIsNone(pos.reentry_exit_route)
        self.assertIsNone(pos.reentry_trigger)

    def test_pending_exit_fill_is_lock_only(self):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2, state=HoldState.PENDING_EXIT,
                           qty_filled=2)
        m = _mgr({"GE": pos})
        self._zone_and_earnings(m)
        m._load_earnings_trim_state = Mock(return_value={})
        m._reconcile_pending_exit(pos, ReconcileResult())
        self.assertEqual(pos.state, HoldState.CLOSED)
        self.assertEqual(pos.reentry_exit_route, "qhm_exit_order")
        self.assertIsNone(pos.reentry_exit_price)
        self.assertIsNone(pos.reentry_trigger)          # no exit price -> lock-only

    def test_earnings_trim_full_exit_route(self):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2, state=HoldState.PENDING_EXIT,
                           qty_filled=2)
        m = _mgr({"GE": pos})
        self._zone_and_earnings(m)
        m._load_earnings_trim_state = Mock(return_value={"GE": {"tier1": "done", "tier2": "done"}})
        m._reconcile_pending_exit(pos, ReconcileResult())
        self.assertEqual(pos.reentry_exit_route, "qhm_earnings_trim_exit")

    def test_owner_add_terminal_stop_fill(self):
        from tests.test_qhm_owner_buy_autoheal import REPLACEMENT_ID, _hold, _manager, _stop
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3, "delta_qty": 1,
            "broker_avg": 302.496667, "old_stop_id": "old-stop", "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID, "order_qty_by_id": {"owner-order": 1},
        })
        m = _manager(pos)
        m.broker.get_position.return_value = None
        m._get_open_orders = Mock(return_value=[])
        m._reentry_zone = Mock(return_value=(-0.04, "2026-10-23"))
        m._reentry_earnings = Mock(return_value=(REPORT, "finnhub_symbol", "amc"))
        seen = self._capture_saves(m, pos)
        terminal = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        terminal.status, terminal.filled_qty = "filled", "3"
        terminal.filled_avg_price, terminal.filled_at = "272.50", "2026-10-23T15:00:00Z"
        old = _stop("old-stop", "2", "272.71", "QH-GE-s-old-proof")
        old.status = "replaced"
        with patch("execution.ownership_guard.apply_authorized_owner_buy_assignment", return_value=True), \
                patch("execution.broker.get_order_by_client_order_id", return_value=terminal), \
                patch("execution.broker.get_order", return_value=old):
            self.assertTrue(m._recover_owner_add_stage(pos))
        self.assertEqual(pos.state, HoldState.CLOSED)
        self.assertIn((HoldState.CLOSED, "qhm_stop_out", 272.5, round(272.5 * 0.96, 2)), seen)


# ── Owner sales and unexplained closes keep the existing re-date-only rule ────────────────────────────────────────
class OwnerAndExternalUnchanged(_Base):
    def test_owner_sale_never_dip_released(self):
        prev = _locked(closed_reason="owner_app_sale")
        m = _wire_release(_mgr({"GE": prev}))
        m.add_candidate("GE", 0.2)
        self.assertIs(m._positions["GE"], prev)
        m._reentry_closes.assert_not_called()
        self.assertEqual(self.events(), [])

    def test_owner_confirmed_redated_uses_old_rule(self):
        prev = _locked(closed_reason="owner_confirmed")
        m = _wire_release(_mgr({"GE": prev}, nbf="2026-11-05"))
        m.add_candidate("GE", 0.2)
        self.assertEqual(m._positions["GE"].state, HoldState.PENDING_ENTRY)
        self.assertIsNone(m._positions["GE"].reentry_release)

    def test_external_close_unchanged(self):
        prev = _locked(closed_reason="external_close_detected")
        m = _wire_release(_mgr({"GE": prev}))
        m.add_candidate("GE", 0.2)
        self.assertIs(m._positions["GE"], prev)
        m._reentry_earnings.assert_not_called()


# ── Release rules in add_candidate ────────────────────────────────────────────────────────────────────────────────
class ReleaseRules(_Base):
    def _add(self, prev, **wire):
        m = _wire_release(_mgr({"GE": prev}), **wire)
        m.add_candidate("GE", 0.2)
        return m

    def test_earnings_passed_bmo_releases_on_report_day(self):
        m = self._add(_locked(), earnings=(NOW.date(), "finnhub_symbol", "bmo"))
        new = m._positions["GE"]
        self.assertEqual((new.state, new.reentry_release), (HoldState.PENDING_ENTRY, "earnings_passed"))

    def test_earnings_amc_waits_for_next_session(self):
        prev = _locked()
        m = self._add(prev, rows=ROWS_ABOVE, earnings=(NOW.date(), "finnhub_symbol", "amc"))
        self.assertIs(m._positions["GE"], prev)
        m2 = self._add(_locked(), earnings=(NOW.date() - timedelta(days=1), "finnhub_symbol", "amc"))
        self.assertEqual(m2._positions["GE"].reentry_release, "earnings_passed")

    def test_unknown_hour_counts_as_after_close(self):
        prev = _locked()
        m = self._add(prev, rows=ROWS_ABOVE, earnings=(NOW.date(), "config", ""))
        self.assertIs(m._positions["GE"], prev)

    def test_close_at_trigger_with_clear_catalyst_releases_at_tranche_one(self):
        m = self._add(_locked())
        new = m._positions["GE"]
        self.assertEqual(new.state, HoldState.PENDING_ENTRY)
        self.assertEqual(new.reentry_release, "dip_trigger")
        self.assertEqual(new.reentry_trigger, 288.0)
        # no catch-up: the ordinary schedule from tranche 1
        self.assertEqual((new.tranche, new.tranches_filled, new.entry_day, new.qty_filled), (1, 0, None, 0))
        ev = self.events()[-1]
        self.assertEqual((ev["decision"], ev["kind"], ev["close"], ev["crossing_date"]),
                         ("released", "dip_trigger", 287.0, "2026-11-09"))

    def test_negative_catalyst_keeps_lock(self):
        prev = _locked()
        m = self._add(prev, catalyst=("NEGATIVE", 120, "negative catalyst: downgrade"))
        self.assertIs(m._positions["GE"], prev)
        self.assertEqual(self.events()[-1]["catalyst_state"], "NEGATIVE")

    def test_unknown_catalyst_keeps_lock(self):
        prev = _locked()
        m = self._add(prev, catalyst=("UNKNOWN", None, "catalyst cache stale"))
        self.assertIs(m._positions["GE"], prev)

    def test_close_above_trigger_keeps_lock(self):
        prev = _locked()
        m = self._add(prev, rows=ROWS_ABOVE)
        self.assertIs(m._positions["GE"], prev)
        m._reentry_catalyst.assert_not_called()

    def test_no_zone_is_lock_only(self):
        prev = _locked(reentry_trigger=None, reentry_zone_date=None)
        m = self._add(prev)
        self.assertIs(m._positions["GE"], prev)
        self.assertEqual(self.events()[-1]["reason"], "lock_only")

    def test_missing_bars_keep_lock(self):
        prev = _locked()
        m = self._add(prev, rows=None)
        self.assertIs(m._positions["GE"], prev)

    def test_exit_fill_outside_its_session_bar_keeps_lock(self):
        prev = _locked(reentry_exit_price=330.0, reentry_trigger=316.8)   # e.g. a split between exit and now
        m = self._add(prev)
        self.assertIs(m._positions["GE"], prev)
        self.assertEqual(self.events()[-1]["reason"], "exit_bar_mismatch")

    def test_unknown_earnings_keeps_lock_and_pages_once(self):
        prev = _locked()
        m = self._add(prev, earnings=(None, "unknown", ""))
        m.add_candidate("GE", 0.2)
        self.assertIs(m._positions["GE"], prev)
        self.assertEqual(m._alert.call_count, 1)

    def test_redated_pick_releases(self):
        prev = _locked()
        m = _wire_release(_mgr({"GE": prev}, nbf="2026-11-05"), rows=ROWS_ABOVE)
        m.add_candidate("GE", 0.2)
        self.assertEqual(m._positions["GE"].reentry_release, "owner_redated")
        self.assertEqual(self.events()[-1]["kind"], "owner_redated")


class OldStateFile(_Base):
    def test_closed_hold_without_lock_fields_loads_and_is_locked(self):
        state = {"GE": {"symbol": "GE", "direction": "long", "target_equity_pct": 0.2, "state": "CLOSED",
                        "closed_reason": "qhm_stop_out", "updated_at": "2026-11-02T10:30:00-05:00"}}
        path = self.root / "quarterly_holds.json"
        path.write_text(json.dumps(state))
        m = _mgr()
        m.state_path = path
        m._load_state()
        prev = m._positions["GE"]
        self.assertIsNone(prev.reentry_exit_route)
        _wire_release(m)
        m.add_candidate("GE", 0.2)
        self.assertIs(m._positions["GE"], prev)
        self.assertEqual(self.events()[-1]["reason"], "lock_only")

    def test_legacy_close_released_once_earnings_passed(self):
        prev = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2, state=HoldState.CLOSED,
                            updated_at="2026-08-20T10:00:00-04:00")
        m = _wire_release(_mgr({"GE": prev}), earnings=(date(2026, 10, 21), "finnhub_symbol", "bmo"))
        m.add_candidate("GE", 0.2)
        self.assertEqual(m._positions["GE"].reentry_release, "earnings_passed")
        self.assertEqual(m._positions["GE"].reentry_exit_date, "2026-08-20")


# ── Pre-order check and the limit at the trigger ──────────────────────────────────────────────────────────────────
class PreOrder(_Base):
    def _pending(self):
        p = _locked(state=HoldState.PENDING_ENTRY, closed_reason=None, reentry_release="dip_trigger")
        return p

    def _quote(self, bid=287.5, ask=287.9, age_s=5):
        ts = (NOW - timedelta(seconds=age_s)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return {"bid": bid, "ask": ask, "ts": ts}

    def test_fresh_ask_at_or_below_trigger_gives_limit_at_trigger(self):
        pos = self._pending()
        m = _wire_release(_mgr({"GE": pos}))
        with patch("data.alpaca_data.get_latest_quote", return_value=self._quote()):
            self.assertEqual(m._reentry_pre_order(pos), (True, 288.0))

    def test_ask_above_trigger_places_nothing(self):
        pos = self._pending()
        m = _wire_release(_mgr({"GE": pos}))
        with patch("data.alpaca_data.get_latest_quote", return_value=self._quote(bid=289.0, ask=289.2)):
            self.assertEqual(m._reentry_pre_order(pos), (False, None))
        self.assertEqual(self.events()[-1]["reason"], "ask_above_trigger")

    def test_missing_stale_or_one_sided_quote_places_nothing(self):
        for q in (None, self._quote(age_s=3600), {"bid": 0, "ask": 287.0, "ts": self._quote()["ts"]},
                  {"bid": 288.0, "ask": 287.0, "ts": self._quote()["ts"]}):
            pos = self._pending()
            m = _wire_release(_mgr({"GE": pos}))
            with patch("data.alpaca_data.get_latest_quote", return_value=q):
                self.assertEqual(m._reentry_pre_order(pos), (False, None), q)

    def test_catalyst_turned_negative_places_nothing(self):
        pos = self._pending()
        m = _wire_release(_mgr({"GE": pos}), catalyst=("NEGATIVE", 0, "negative headline(s) since exit: legal_probe"))
        with patch("data.alpaca_data.get_latest_quote", Mock(return_value=self._quote())):
            self.assertEqual(m._reentry_pre_order(pos), (False, None))
        m._reentry_catalyst.assert_called_once_with("GE", date(2026, 11, 9), EXIT, lock=pos)
        self.assertEqual(self.events()[-1]["catalyst_state"], "NEGATIVE")

    def test_ask_is_checked_before_the_news_read(self):
        pos = self._pending()
        m = _wire_release(_mgr({"GE": pos}))
        with patch("data.alpaca_data.get_latest_quote", Mock(return_value=self._quote(bid=289.0, ask=289.2))):
            self.assertEqual(m._reentry_pre_order(pos), (False, None))
        m._reentry_catalyst.assert_not_called()          # no network news call on an ask-above cycle

    def test_expired_first_order_is_rechecked_and_relimited(self):
        """A dip-released first order that expires unfilled leaves entry_day set; every retry must re-run the check
        and use the limit at the trigger (cold review, must-fix)."""
        pos = self._pending()
        m = _wire_release(_mgr({"GE": pos}))
        m._passes_entry_gate = Mock(return_value=True)
        limits = []

        def _fake_submit(p, today, limit_price=None):
            limits.append(limit_price)
            p.entry_order_id, p.state = "ord-1", HoldState.AWAITING_FILL
            p.entry_day = p.entry_day or today
            return True

        m._submit_tranche = Mock(side_effect=_fake_submit)
        with patch("data.alpaca_data.get_latest_quote", Mock(return_value=self._quote())):
            m.maybe_enter_positions()
        self.assertEqual(limits, [288.0])
        # the DAY order expires unfilled: not on the book, no position -> back to PENDING_ENTRY, entry_day cleared
        m._get_open_orders = Mock(return_value=[])
        m.broker.get_position.return_value = None
        m._reconcile_awaiting_fill(pos, ReconcileResult())
        self.assertEqual(pos.state, HoldState.PENDING_ENTRY)
        self.assertIsNone(pos.entry_day)
        nxt = NOW + timedelta(days=1)
        m._now_et.return_value = nxt
        q = {"bid": 287.5, "ask": 287.9, "ts": nxt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        with patch("data.alpaca_data.get_latest_quote", Mock(return_value=q)):
            m.maybe_enter_positions()
        self.assertEqual(limits, [288.0, 288.0])
        self.assertEqual(m._reentry_catalyst.call_count, 2)          # the news check ran again
        # expires again; the ask is now above the trigger -> nothing placed
        m._reconcile_awaiting_fill(pos, ReconcileResult())
        q_above = {"bid": 289.0, "ask": 289.2, "ts": nxt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        with patch("data.alpaca_data.get_latest_quote", Mock(return_value=q_above)):
            m.maybe_enter_positions()
        self.assertEqual(limits, [288.0, 288.0])
        self.assertEqual(pos.state, HoldState.PENDING_ENTRY)

    def test_failed_bar_read_is_cached_for_the_session(self):
        import pandas as pd
        m = _mgr()
        fetch = Mock(return_value=pd.DataFrame())
        with patch("data.fetcher.fetch_bars_window", fetch):
            self.assertIsNone(m._reentry_closes("GE", EXIT))
            self.assertIsNone(m._reentry_closes("GE", EXIT))
        self.assertEqual(fetch.call_count, 1)
        m._now_et.return_value = NOW + timedelta(days=1)          # next session: read again
        with patch("data.fetcher.fetch_bars_window", fetch):
            m._reentry_closes("GE", EXIT)
        self.assertEqual(fetch.call_count, 2)

    def test_earnings_passed_switches_to_ordinary_path(self):
        pos = self._pending()
        m = _wire_release(_mgr({"GE": pos}), earnings=(NOW.date(), "finnhub_symbol", "bmo"))
        self.assertEqual(m._reentry_pre_order(pos), (True, None))
        self.assertEqual(pos.reentry_release, "earnings_passed")

    def test_maybe_enter_positions_routes_first_order_through_the_check(self):
        pos = self._pending()
        m = _mgr({"GE": pos})
        m._passes_entry_gate = Mock(return_value=True)
        m._submit_tranche = Mock(return_value=True)
        m._reentry_pre_order = Mock(return_value=(True, 288.0))
        m.maybe_enter_positions()
        m._submit_tranche.assert_called_once_with(pos, NOW.strftime("%Y-%m-%d"), limit_price=288.0)
        m._reentry_pre_order.return_value = (False, None)
        m._submit_tranche.reset_mock()
        m.maybe_enter_positions()
        m._submit_tranche.assert_not_called()

    def test_ordinary_pick_has_no_ceiling(self):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2)
        m = _mgr({"GE": pos})
        m._passes_entry_gate = Mock(return_value=True)
        m._submit_tranche = Mock(return_value=True)
        m._reentry_pre_order = Mock()
        m.maybe_enter_positions()
        m._reentry_pre_order.assert_not_called()
        m._submit_tranche.assert_called_once_with(pos, NOW.strftime("%Y-%m-%d"), limit_price=None)

    def test_submit_tranche_places_limit_at_trigger(self):
        pos = self._pending()
        m = _mgr({"GE": pos})
        m._get_account_equity = Mock(return_value=10000.0)
        m._get_quarterly_notional_excl = Mock(return_value=0.0)
        m._get_live_price = Mock(return_value=287.9)
        m._qhm_cap_room_shares = Mock(return_value=100)
        m._grandfathered_notional_for_allocator = Mock(return_value=0.0)
        m._dispatcher = Mock()
        m._dispatcher.submit_limit.return_value = SimpleNamespace(id="ord-1")
        with patch("execution.tier_capital_allocator.live_admit",
                   return_value=SimpleNamespace(approved=True, lease="L", reason="")), \
                patch("execution.tier_capital_allocator.live_order_id", return_value="QH-GE-b-1-x"), \
                patch("execution.tier_capital_allocator.live_bind"):
            self.assertTrue(m._submit_tranche(pos, "2026-11-10", limit_price=288.0))
        args = m._dispatcher.submit_limit.call_args.args
        self.assertEqual((args[1], args[3], args[4]), ("GE", "buy", 288.0))
        self.assertEqual(pos.state, HoldState.AWAITING_FILL)


# ── Real helpers on temp files: catalyst cache, dip zones, the event line ─────────────────────────────────────────
_CREDS = {"ALPACA_API_KEY": "test-key", "ALPACA_SECRET_KEY": "test-secret"}   # never reach a network: the read is mocked


RECENT_EXIT = date(2026, 11, 9)     # Monday, the day before NOW


def _news(headline, created="2026-11-09T18:00:00Z", summary=""):
    return {"id": hash((headline, created)), "symbols": ["GE"], "headline": headline, "summary": summary,
            "created_at": created, "url": "", "source": "alpaca_news"}


def _complete(*items, pages=1):
    from events.catalyst_engine import NewsFetch
    return NewsFetch(list(items), True, "", pages)


def _incomplete(error, *items):
    from events.catalyst_engine import NewsFetch
    return NewsFetch(list(items), False, error, 2)


def _news_patches(fetch):
    """Mock the complete news-since reader (autospec keeps its real signature); the old one-page fetch, the
    newest-only summary and the cron cache must not be used."""
    import events.catalyst_engine as _cat
    from unittest.mock import create_autospec
    spec = create_autospec(_cat.fetch_news_since, side_effect=fetch)
    return (patch.dict("os.environ", _CREDS), patch("events.catalyst_engine.fetch_news_since", spec),
            patch("events.catalyst_engine._fetch_alpaca_news",
                  Mock(side_effect=AssertionError("the one-page 72 h fetch must not decide"))),
            patch("events.catalyst_engine.get_active_catalysts",
                  Mock(side_effect=AssertionError("newest-only summary must not decide"))),
            patch("events.catalyst_engine.read_cached_state",
                  Mock(side_effect=AssertionError("the cron cache must not decide coverage"))))


def _start(exit_day):
    return datetime(exit_day.year, exit_day.month, exit_day.day, tzinfo=ET)


class CatalystLiveRead(_Base):
    """The dip release classifies EVERY headline for the one symbol from the exit date to now, from a complete read."""

    def _read(self, fetch, crossing=RECENT_EXIT, exit_day=RECENT_EXIT):
        a, b, c, d, e = _news_patches(fetch)
        with a, b, c, d, e:
            return _mgr()._reentry_catalyst("GE", crossing, exit_day)

    def test_empty_complete_read_is_clear_and_asks_from_the_exit_date(self):
        fetch = Mock(return_value=_complete())
        self.assertEqual(self._read(fetch)[0], "CLEAR")
        fetch.assert_called_once_with("GE", _start(RECENT_EXIT))

    def test_any_negative_headline_is_negative_even_if_a_newer_one_is_positive(self):
        fetch = Mock(return_value=_complete(_news("GE upgraded to buy", "2026-11-10T14:00:00Z"),
                                            _news("GE downgraded at bank", "2026-11-09T15:00:00Z")))
        state, _, why = self._read(fetch)
        self.assertEqual(state, "NEGATIVE")
        self.assertIn("downgrade", why)

    def test_negative_in_the_summary_counts(self):
        fetch = Mock(return_value=_complete(_news("GE update", summary="auditor flags going concern doubt")))
        self.assertEqual(self._read(fetch)[0], "NEGATIVE")

    def test_exit_older_than_three_days_is_read_in_full(self):
        """The LLY case: the dip comes days after the sale; a complete clean read releases."""
        fetch = Mock(return_value=_complete(*[_news("GE news %d" % i) for i in range(120)], pages=3))
        state, _, why = self._read(fetch, crossing=date(2026, 11, 9), exit_day=EXIT)
        self.assertEqual(state, "CLEAR")
        self.assertIn("120 headline(s) in 3 page(s)", why)
        fetch.assert_called_once_with("GE", _start(EXIT))

    def test_incomplete_read_is_unknown_even_with_no_negative_seen(self):
        for err in ("HTTP 429", "repeated page token", "page cap 4 reached", ""):
            state, _, why = self._read(Mock(return_value=_incomplete(err, _news("GE raises guidance"))))
            self.assertEqual(state, "UNKNOWN", err)
            self.assertIn("news read incomplete", why)

    def test_failures_are_unknown(self):
        for fetch in (Mock(side_effect=RuntimeError("news API down")), Mock(return_value=None),
                      Mock(return_value={}), Mock(return_value=_complete("not a row"))):
            self.assertEqual(self._read(fetch)[0], "UNKNOWN", fetch)

    def test_read_not_after_the_crossing_close_is_unknown_without_a_read(self):
        fetch = Mock(return_value=_complete())
        self.assertEqual(self._read(fetch, crossing=NOW.date())[0], "UNKNOWN")
        fetch.assert_not_called()

    def test_missing_credentials_is_unknown_without_a_read(self):
        fetch = Mock(return_value=_complete())
        with patch.dict("os.environ", {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}), \
                patch("events.catalyst_engine.fetch_news_since", fetch):
            self.assertEqual(_mgr()._reentry_catalyst("GE", RECENT_EXIT, RECENT_EXIT)[0], "UNKNOWN")
        fetch.assert_not_called()


class CatalystLiveReadReleases(_Base):
    """End to end through add_candidate with the real catalyst helper and a mocked complete read."""
    def _add(self, fetch, exit_day=RECENT_EXIT):
        prev = _locked(reentry_exit_date=exit_day.isoformat())
        m = _mgr({"GE": prev})
        m._reentry_earnings = Mock(return_value=(REPORT, "finnhub_symbol", "bmo"))
        m._reentry_closes = Mock(return_value=[{"day": exit_day, "low": 285.0, "high": 305.0, "close": 287.0}])
        a, b, c, d, e = _news_patches(fetch)
        with a, b, c, d, e:
            m.add_candidate("GE", 0.2)
        return prev, m

    def test_live_negative_keeps_lock(self):
        fetch = Mock(return_value=_complete(_news("GE upgraded"), _news("GE faces SEC investigation")))
        prev, m = self._add(fetch)
        fetch.assert_called_once_with("GE", _start(RECENT_EXIT))
        self.assertIs(m._positions["GE"], prev)
        self.assertEqual(self.events()[-1]["catalyst_state"], "NEGATIVE")

    def test_live_read_raises_keeps_lock(self):
        fetch = Mock(side_effect=TimeoutError("news API timeout"))
        prev, m = self._add(fetch)
        self.assertIs(m._positions["GE"], prev)
        self.assertEqual(self.events()[-1]["catalyst_state"], "UNKNOWN")

    def test_live_incomplete_keeps_lock(self):
        prev, m = self._add(Mock(return_value=_incomplete("HTTP 429")))
        self.assertIs(m._positions["GE"], prev)
        self.assertEqual(self.events()[-1]["catalyst_state"], "UNKNOWN")

    def test_live_empty_releases(self):
        fetch = Mock(return_value=_complete())
        _, m = self._add(fetch)
        fetch.assert_called_once_with("GE", _start(RECENT_EXIT))
        self.assertEqual(m._positions["GE"].reentry_release, "dip_trigger")
        self.assertEqual(self.events()[-1]["catalyst_state"], "CLEAR")

    def test_exit_days_before_the_dip_releases_on_a_complete_clean_read(self):
        fetch = Mock(return_value=_complete(_news("GE news"), pages=2))
        _, m = self._add(fetch, exit_day=EXIT)
        fetch.assert_called_once_with("GE", _start(EXIT))
        self.assertEqual(m._positions["GE"].reentry_release, "dip_trigger")
        self.assertEqual(self.events()[-1]["catalyst_state"], "CLEAR")


class PendingExitArmedAfterSave(_Base):
    def _mgr(self, saved):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2, state=HoldState.PENDING_EXIT,
                           qty_filled=2)
        m = _mgr({"GE": pos})
        m._save_state = Mock(return_value=saved)
        m._reentry_zone = Mock(return_value=(None, None))
        m._reentry_earnings = Mock(return_value=(REPORT, "finnhub_symbol", "bmo"))
        m._load_earnings_trim_state = Mock(return_value={})
        return m, pos

    def test_armed_record_after_successful_save(self):
        m, pos = self._mgr(True)
        m.reconcile_on_startup()
        self.assertEqual(pos.state, HoldState.CLOSED)
        self.assertEqual([e["stage"] for e in self.events()], ["close"])

    def test_no_armed_record_when_save_fails(self):
        m, pos = self._mgr(False)
        m.reconcile_on_startup()
        self.assertEqual(self.events(), [])


class ZoneAtExit(_Base):
    def _zones(self, session):
        p = self.root / "dip_zones.json"
        p.write_text(json.dumps({"session": session, "zones": {"GE": {"rung_a": -0.04, "rung_b": -0.08}}}))
        return patch("execution.dip_zones._STATE", p)

    def test_fresh_zone_from_prior_session_is_used(self):
        with self._zones("2026-10-30"):           # Friday before a Monday exit
            self.assertEqual(_mgr()._reentry_zone("GE", EXIT), (-0.04, "2026-10-30"))

    def test_stale_zone_is_lock_only(self):
        with self._zones("2026-10-28"):
            self.assertEqual(_mgr()._reentry_zone("GE", EXIT), (None, "2026-10-28"))

    def test_trigger_pinned_at_exit(self):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2)
        m = _mgr()
        m._reentry_earnings = Mock(return_value=(REPORT, "finnhub_symbol", "bmo"))
        with self._zones("2026-11-02"):
            m._arm_reentry_lock(pos, "qhm_stop_out", 300.0, EXIT)
        self.assertEqual((pos.reentry_trigger, pos.reentry_zone_date), (288.0, "2026-11-02"))


class EarningsSelection(_Base):
    def test_bmo_report_on_exit_day_preceded_the_exit(self):
        m = _mgr()
        info = {"state": "DATED", "dates": [EXIT, REPORT, date(2026, 8, 3)], "source": "finnhub_symbol"}
        with patch("data.fmp_client.get_earnings_info", return_value=info):
            m._earnings_hours = Mock(return_value={EXIT.isoformat(): "bmo", REPORT.isoformat(): "amc"})
            self.assertEqual(m._reentry_earnings("GE", EXIT, None), (REPORT, "finnhub_symbol", "amc"))
            m._earnings_hours = Mock(return_value={EXIT.isoformat(): "amc"})
            self.assertEqual(m._reentry_earnings("GE", EXIT, None), (EXIT, "finnhub_symbol", "amc"))

    def test_close_path_reads_the_cache_only(self):
        m = _mgr()
        m._earnings_hours = Mock(return_value={})
        live = Mock(side_effect=AssertionError("provider call on the close path"))
        with patch("data.fmp_client.get_earnings_info", live), \
                patch("data.fmp_client.get_cached_earnings_dates", return_value=[REPORT]):
            self.assertEqual(m._reentry_earnings("GE", EXIT, None, live=False), (REPORT, "earnings_cache", ""))
        live.assert_not_called()

    def test_unknown_provider_falls_back_to_lock_date(self):
        m = _mgr()
        with patch("data.fmp_client.get_earnings_info", return_value={"state": "UNKNOWN", "dates": []}):
            m._earnings_hours = Mock(return_value={})
            ed, src, hour = m._reentry_earnings("GE", EXIT, _locked())
        self.assertEqual((ed, src, hour), (REPORT, "lock:finnhub_symbol", "bmo"))


class EventLine(TestCase):
    def test_qhm_reentry_event_written(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "trade_events.jsonl"
            with patch("trade_logger._JSONL", path):
                m = _wire_release(_mgr({"GE": _locked()}))
                m.add_candidate("GE", 0.2)
            rows = [json.loads(x) for x in path.read_text().splitlines()]
        ev = [r for r in rows if r["event"] == "qhm_reentry"][-1]
        for k in ("exit_price", "trigger", "close", "ask", "zone_date", "catalyst_state", "catalyst_cache_age_s",
                  "earnings_date", "earnings_source", "decision"):
            self.assertIn(k, ev)
        self.assertEqual((ev["exit_price"], ev["trigger"], ev["decision"], ev["stage"]),
                         (300.0, 288.0, "released", "add_candidate"))


# ── Follow-up 3 (2026-11-09): exit-day report hour, incremental cached news read, no catch-up tranches ────────────
class ExitDayReportHour(_Base):
    """An earnings date ON the exit day counts as a later report only when it is explicitly after the close."""
    def _decide(self, hour, dates=(EXIT,)):
        lock = _locked(reentry_earnings_date=None, reentry_earnings_hour=None, reentry_trigger=None)
        m = _mgr({"GE": lock})
        m._now_et.return_value = datetime(2026, 11, 3, 11, 0, tzinfo=ET)       # the morning after the exit
        m._earnings_hours = Mock(return_value={EXIT.isoformat(): hour} if hour else {})
        info = {"state": "DATED", "dates": list(dates), "source": "fmp"}
        with patch("data.fmp_client.get_earnings_info", return_value=info), \
                patch.object(qhm_mod, "_CONFIG_PATH", self.root / "no_config.json"):
            return m._reentry_decide(lock)

    def test_unknown_hour_on_exit_day_does_not_release_next_day(self):
        d = self._decide(None)
        self.assertFalse(d["released"])
        self.assertNotEqual(d["kind"], "earnings_passed")
        self.assertEqual(d["reason"], "earnings_date_unknown")      # skipped: no later report known -> locked

    def test_unknown_hour_on_exit_day_runs_to_the_next_report(self):
        d = self._decide(None, dates=(EXIT, date(2027, 2, 3)))
        self.assertFalse(d["released"])
        self.assertEqual(d["earnings_date"], "2027-02-03")

    def test_amc_on_exit_day_releases_after_the_report(self):
        d = self._decide("amc")
        self.assertTrue(d["released"])
        self.assertEqual((d["kind"], d["earnings_date"]), ("earnings_passed", EXIT.isoformat()))

    def test_bmo_on_exit_day_does_not_release(self):
        d = self._decide("bmo")
        self.assertFalse(d["released"])

    def test_stored_exit_day_date_with_unknown_hour_is_not_used(self):
        m = _mgr()
        m._earnings_hours = Mock(return_value={})
        lock = _locked(reentry_earnings_date=EXIT.isoformat(), reentry_earnings_hour="")
        with patch("data.fmp_client.get_earnings_info", return_value={"state": "UNKNOWN", "dates": []}), \
                patch.object(qhm_mod, "_CONFIG_PATH", self.root / "no_config.json"):
            self.assertEqual(m._reentry_earnings("GE", EXIT, lock), (None, "unknown", ""))


def _complete_through(end, *items):
    from events.catalyst_engine import NewsFetch
    return NewsFetch(list(items), True, "", 1, end)


class IncrementalNewsCache(_Base):
    """The pre-order news read is incremental and cached on the hold (it runs every cycle the ask is at/below)."""
    def _read(self, m, lock, fetch, creds=True):
        a, b, c, d, e = _news_patches(fetch)
        if not creds:
            a = patch.dict("os.environ", {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""})
        with a, b, c, d, e:
            return m._reentry_catalyst("GE", RECENT_EXIT, EXIT, lock=lock)

    def test_reads_from_stored_start_and_advances_only_on_complete(self):
        lock = _locked()
        m = _mgr({"GE": lock})
        end1 = datetime(2026, 11, 10, 15, 55, tzinfo=timezone.utc)
        fetch = Mock(return_value=_complete_through(end1, _news("GE update")))
        self.assertEqual(self._read(m, lock, fetch)[0], "CLEAR")
        fetch.assert_called_once_with("GE", _start(EXIT))                     # first read: the whole window
        self.assertEqual(lock.reentry_news_through, end1.isoformat())
        m._save_state.assert_called()
        self.assertEqual(lock.reentry_news_reread_from, _start(EXIT).astimezone(timezone.utc).isoformat())
        # an incomplete read re-covers the previous read's span (from the end of the read before it) and does NOT
        # advance anything
        fetch2 = Mock(return_value=_incomplete("time budget 30s spent after 3 page(s)"))
        self.assertEqual(self._read(m, lock, fetch2)[0], "UNKNOWN")
        fetch2.assert_called_once_with("GE", _start(EXIT))
        self.assertEqual(lock.reentry_news_through, end1.isoformat())
        # the next complete read starts at the same point; then the start moves to the previous end
        end2 = end1 + timedelta(minutes=5)
        fetch3 = Mock(return_value=_complete_through(end2))
        self.assertEqual(self._read(m, lock, fetch3)[0], "CLEAR")
        fetch3.assert_called_once_with("GE", _start(EXIT))
        self.assertEqual((lock.reentry_news_reread_from, lock.reentry_news_through), (end1.isoformat(), end2.isoformat()))
        # each read overlaps the previous one: a late-indexed negative stamped inside the last read's span is seen
        late = _news("GE faces SEC investigation", (end2 - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        fetch4 = Mock(return_value=_complete_through(end2 + timedelta(minutes=5), late))
        self.assertEqual(self._read(m, lock, fetch4)[0], "NEGATIVE")
        fetch4.assert_called_once_with("GE", end1)

    def test_negative_latches_and_later_checks_make_no_read(self):
        lock = _locked()
        m = _mgr({"GE": lock})
        fetch = Mock(return_value=_complete_through(datetime(2026, 11, 10, 15, 55, tzinfo=timezone.utc),
                                                    _news("GE faces SEC investigation")))
        self.assertEqual(self._read(m, lock, fetch)[0], "NEGATIVE")
        self.assertTrue(lock.reentry_news_negative)
        self.assertTrue(lock.reentry_news_negative_type)
        fetch2 = Mock(return_value=_complete())
        state, _, why = self._read(m, lock, fetch2)
        self.assertEqual(state, "NEGATIVE")
        self.assertIn(lock.reentry_news_negative_type, why)
        fetch2.assert_not_called()

    def test_stored_negative_returns_negative_with_no_fetch(self):
        lock = _locked(reentry_news_negative=True, reentry_news_negative_type="legal_probe")
        fetch = Mock(return_value=_complete())
        state, _, why = self._read(_mgr({"GE": lock}), lock, fetch, creds=False)
        self.assertEqual(state, "NEGATIVE")
        self.assertIn("legal_probe", why)
        fetch.assert_not_called()

    def test_negative_on_an_incomplete_read_still_latches(self):
        lock = _locked()
        fetch = Mock(return_value=_incomplete("HTTP 429", _news("GE downgraded at bank")))
        self.assertEqual(self._read(_mgr({"GE": lock}), lock, fetch)[0], "NEGATIVE")
        self.assertTrue(lock.reentry_news_negative)
        self.assertIsNone(lock.reentry_news_through)

    def test_unreadable_or_pre_window_stored_end_rereads_the_whole_window(self):
        for raw in ("not a time", "2026-11-03T10:00:00", "2026-10-01T10:00:00+00:00"):
            lock = _locked(reentry_news_reread_from=raw)
            fetch = Mock(return_value=_complete())
            self._read(_mgr({"GE": lock}), lock, fetch)
            fetch.assert_called_once_with("GE", _start(EXIT))

    def test_pre_order_uses_the_cache_on_the_pending_hold(self):
        pos = _locked(state=HoldState.PENDING_ENTRY, closed_reason=None, reentry_release="dip_trigger",
                      reentry_news_reread_from="2026-11-10T15:00:00+00:00",
                      reentry_news_through="2026-11-10T15:30:00+00:00")
        m = _mgr({"GE": pos})
        m._reentry_earnings = Mock(return_value=(REPORT, "finnhub_symbol", "bmo"))
        m._reentry_closes = Mock(return_value=ROWS_CROSSED)
        fetch = Mock(return_value=_complete_through(datetime(2026, 11, 10, 15, 59, tzinfo=timezone.utc)))
        q = {"bid": 287.5, "ask": 287.9,
             "ts": (NOW - timedelta(seconds=5)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        a, b, c, d, e = _news_patches(fetch)
        with a, b, c, d, e, patch("data.alpaca_data.get_latest_quote", return_value=q):
            self.assertEqual(m._reentry_pre_order(pos), (True, 288.0))
        fetch.assert_called_once_with("GE", datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc))
        self.assertEqual((pos.reentry_news_reread_from, pos.reentry_news_through),
                         ("2026-11-10T15:30:00+00:00", "2026-11-10T15:59:00+00:00"))

    def test_news_fields_survive_save_and_load(self):
        pos = _locked(reentry_news_through="2026-11-10T15:59:00+00:00", reentry_news_negative=True,
                      reentry_news_negative_type="downgrade")
        back = HoldPosition.from_dict(json.loads(json.dumps(pos.to_dict())))
        self.assertEqual((back.reentry_news_through, back.reentry_news_negative, back.reentry_news_negative_type),
                         ("2026-11-10T15:59:00+00:00", True, "downgrade"))
        old = HoldPosition.from_dict({"symbol": "GE", "direction": "long", "target_equity_pct": 0.2,
                                      "state": "CLOSED"})          # an older state file without the fields
        self.assertEqual((old.reentry_news_through, old.reentry_news_negative), (None, False))

    def test_news_fields_round_trip_through_the_state_file(self):
        path = self.root / "quarterly_holds.json"
        pos = _locked(reentry_news_through="2026-11-10T15:59:00+00:00", reentry_news_negative=True,
                      reentry_news_negative_type="downgrade")
        path.write_text(json.dumps({"GE": pos.to_dict()}))
        m = _mgr()
        m.state_path = path
        m._load_state()
        got = m._positions["GE"]
        self.assertEqual((got.reentry_news_through, got.reentry_news_negative, got.reentry_news_negative_type),
                         ("2026-11-10T15:59:00+00:00", True, "downgrade"))

    def test_new_exit_resets_the_news_cache_and_release_carries_it(self):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2, reentry_news_negative=True,
                           reentry_news_negative_type="x", reentry_news_through="2026-10-01T00:00:00+00:00")
        m = _mgr()
        m._reentry_zone = Mock(return_value=(-0.04, EXIT.isoformat()))
        m._reentry_earnings = Mock(return_value=(REPORT, "finnhub_symbol", "bmo"))
        m._arm_reentry_lock(pos, "qhm_stop_out", 300.0, EXIT)
        self.assertEqual((pos.reentry_news_through, pos.reentry_news_negative, pos.reentry_news_negative_type),
                         (None, False, None))
        prev = _locked(reentry_news_through="2026-11-10T15:59:00+00:00")
        m2 = _wire_release(_mgr({"GE": prev}))
        m2.add_candidate("GE", 0.2)
        self.assertEqual(m2._positions["GE"].reentry_news_through, "2026-11-10T15:59:00+00:00")


class NoCatchUpTranches(_Base):
    """Spec item 16: an expired dip-released first order restarts the tranche clock from the order that fills."""
    def test_expired_monday_retry_filled_friday_has_no_immediate_tranche_two(self):
        monday, friday = datetime(2026, 11, 9, 11, 0, tzinfo=ET), datetime(2026, 11, 13, 11, 0, tzinfo=ET)
        pos = _locked(state=HoldState.PENDING_ENTRY, closed_reason=None, reentry_release="dip_trigger")
        m = _mgr({"GE": pos})
        m._passes_entry_gate = Mock(return_value=True)
        m._reentry_pre_order = Mock(return_value=(True, 288.0))

        def _fake_submit(p, today, limit_price=None):       # what _submit_tranche does to these fields
            p.entry_order_id, p.state = "ord-1", HoldState.AWAITING_FILL
            if p.entry_day is None:
                p.entry_day = today
            return True

        m._submit_tranche = Mock(side_effect=_fake_submit)
        m._now_et.return_value = monday
        m.maybe_enter_positions()
        self.assertEqual(pos.entry_day, "2026-11-09")
        m._reconcile_awaiting_fill(pos, ReconcileResult())        # expired: not on the book, no position
        self.assertEqual((pos.state, pos.entry_day), (HoldState.PENDING_ENTRY, None))
        m._now_et.return_value = friday
        m.maybe_enter_positions()
        self.assertEqual(pos.entry_day, "2026-11-13")
        # the Friday retry fills
        m.broker.get_position.return_value = SimpleNamespace(qty="3", avg_entry_price="287.90")
        m._compute_and_submit_stop = Mock(side_effect=lambda p: setattr(p, "state", HoldState.ACTIVE))
        self.assertTrue(m._check_fill_and_advance(pos))
        self.assertEqual((pos.tranches_filled, pos.tranche, pos.state), (1, 2, HoldState.ACTIVE))
        self.assertFalse(m._is_tranche_due(pos, "2026-11-13"))
        self.assertFalse(m._is_tranche_due(pos, "2026-11-14"))
        self.assertTrue(m._is_tranche_due(pos, "2026-11-15"))      # normal Day-3 spacing from the actual fill
        m._add_tranche_active = Mock(return_value=True)
        m.maybe_enter_positions()                                   # still Friday: no catch-up order
        m._add_tranche_active.assert_not_called()

    def test_ordinary_hold_keeps_entry_day_on_expiry(self):
        pos = HoldPosition(symbol="GE", direction="long", target_equity_pct=0.2, state=HoldState.AWAITING_FILL,
                           entry_order_id="ord-1", entry_day="2026-11-09")
        _mgr({"GE": pos})._reconcile_awaiting_fill(pos, ReconcileResult())
        self.assertEqual((pos.state, pos.entry_day), (HoldState.PENDING_ENTRY, "2026-11-09"))

    def test_partially_filled_dip_hold_keeps_entry_day(self):
        pos = _locked(state=HoldState.AWAITING_FILL, closed_reason=None, reentry_release="dip_trigger",
                      entry_order_id="ord-2", entry_day="2026-11-09", tranches_filled=1, qty_filled=3, tranche=2)
        _mgr({"GE": pos})._reconcile_awaiting_fill(pos, ReconcileResult())
        self.assertEqual(pos.entry_day, "2026-11-09")
