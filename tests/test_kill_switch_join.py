#!/usr/bin/env python3
# ruff: noqa: E501
"""KILL-JOIN-1107 (owner-approved rev 4 + rev 4.1; logs/design_records/kill_join_2026-11-07.md).

2026-11-06: QHM bought 1 NVDA at 10:13:58 ET; the ownership ledger still showed NVDA flat, so the exact kill join
failed until the 11:00 ET rebuild (46 minutes of no new entries) and one cycle crashed at 10:16 ET because
execute_entries() did not catch the kill check's RuntimeError. These tests pin:
  * the crash fix: an entry loop whose kill check raises returns cleanly, enters nothing and checks once per attempt;
  * the recorded fault clears on the next check;
  * the rebuild request file: atomic, unique temp name, at most once per minute;
  * every unresolved-join reason is logged once;
  * run_ledger_sync: one lock for every pass, requests never lost, watermark only after a healed pass, back-off after
    a refused shrink, only scheduled passes page/count.
Nothing here reaches the broker or the network: every broker, Alpaca and fetch call is mocked, and all files live in
a temporary directory.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import config
import run_ledger_sync as rls
from execution import entry_logic as el
from execution import ownership_guard
from execution import risk_manager as rm


def _risk() -> rm.RiskManager:
    with mock.patch.object(rm, "_load_kill_state", return_value={}):
        return rm.RiskManager(2500.0, daily_start_value=2500.0)


def _reset_join_state() -> None:
    rm._KILL_JOIN_STATE["logged"].clear()
    rm._KILL_JOIN_STATE.update(first_request_wall=None, last_request_wall=None, last_write_mono=None, generation=None)


class _TmpStateMixin:
    """Points the rebuild request file (both writer and runner), the lock, the streak and the heal-confirmation file
    at a temporary directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.request_path = self.dir / "ledger_rebuild_request.json"
        self.streak_path = self.dir / ".ledger_sync_streak.json"
        self.confirm_path = self.dir / "ledger_heal_confirmations.json"
        patches = [
            mock.patch.object(rm, "_LEDGER_REBUILD_REQUEST_FILE", self.request_path),
            mock.patch.object(rls, "_REQUEST_PATH", self.request_path),
            mock.patch.object(rls, "_LOCK_PATH", self.dir / "ledger_sync.lock"),
            mock.patch.object(rls, "_STREAK_PATH", self.streak_path),
            mock.patch.object(rls, "_LOGS", self.dir),
            mock.patch.object(rls, "_HEAL_CONFIRM_PATH", self.confirm_path),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        _reset_join_state()
        self.addCleanup(_reset_join_state)

    def write_request(self, generation: str, fingerprint: str = "fp-1", requested_utc: str = "2026-11-09T15:14:00+00:00"):
        self.request_path.write_text(json.dumps({"generation": generation, "fingerprint": fingerprint,
                                                 "requested_utc": requested_utc}))

    def streak(self) -> dict:
        return json.loads(self.streak_path.read_text()) if self.streak_path.exists() else {}


# ── (a) crash fix ────────────────────────────────────────────────────────────────────────────────────────────────────
class EntryLoopKillFault(unittest.TestCase):
    """execute_entries() with a kill check that raises on every call."""

    def _signals(self):
        return [{"symbol": s, "direction": "long", "trade_mode": config.TradeMode.INTRADAY, "score": 12,
                 "max_score": 12} for s in ("AAA", "BBB", "CCC")]

    def _run_cycle(self, risk, tracker):
        fake_main = SimpleNamespace(_spy_risk_magnitude=0.0, _spy_event_type="", _orb_computed_date="",
                                    _orb_feed_failed=False, _spy_last_close=0.0, _orb_high=0.0, _orb_low=0.0)
        gate_state = SimpleNamespace(entry_confirm_buffer={}, conviction_streak={})
        with mock.patch.dict(sys.modules, {"main": fake_main}), \
                mock.patch.object(el, "_get_halt_entries", return_value=False), \
                mock.patch.object(el, "is_market_open", return_value=True), \
                mock.patch.object(el, "get_quarterly_hold_symbols", return_value=set()), \
                mock.patch("data.premarket.get_premarket_detail", return_value={}), \
                mock.patch.object(el._catalyst, "CATALYST_GATE_ENABLED", False), \
                mock.patch.object(el._param_engine, "h4_entry_confirm_scans", return_value=0), \
                mock.patch.object(config, "ORB_ENABLED", False), \
                mock.patch.object(config, "TOD_EXPANSION_WINDOW_START", 0), \
                mock.patch.object(el, "send_slack"):
            return el.execute_entries(self._signals(), risk, tracker, mock.Mock(), mock.Mock(),
                                      gate_state=gate_state)

    def test_raising_kill_check_breaks_the_loop_with_one_check_per_attempt(self):
        risk = _risk()
        risk.reconcile_open_positions_from_alpaca = mock.Mock(return_value=True)
        risk.open_positions = 0
        kill = mock.Mock(side_effect=RuntimeError("kill-switch exact tier attribution is unresolved"))
        risk.check_kill_switch = kill
        tracker = mock.Mock()
        tracker.open_trades = {}
        with self.assertLogs(rm.logger, level="ERROR") as logs:
            entered = self._run_cycle(risk, tracker)
        self.assertEqual(entered, [])
        self.assertEqual(kill.call_count, 1)                    # one attempt (the loop breaks), one check
        self.assertIn("kill-join-1107", "\n".join(logs.output))
        self.assertTrue(risk.kill_blocks_entries())
        tracker.is_in_trade.assert_not_called()                 # never fell through to the power-hour expansion
        # A second cycle: still no exception, still exactly one more check.
        self.assertEqual(self._run_cycle(risk, tracker), [])
        self.assertEqual(kill.call_count, 2)

    def test_position_limit_still_takes_the_expansion_path_without_a_second_kill_check(self):
        risk = _risk()
        risk.reconcile_open_positions_from_alpaca = mock.Mock(return_value=True)
        risk.open_positions = config.MAX_OPEN_POSITIONS      # position limit, not a kill
        kill = mock.Mock(return_value=False)
        risk.check_kill_switch = kill
        tracker = mock.Mock()
        tracker.open_trades = {}
        with mock.patch.object(config, "BUCKET_B_MAX_POSITIONS_POWER", 0):   # expansion denied → break
            entered = self._run_cycle(risk, tracker)
        self.assertEqual(entered, [])
        self.assertEqual(kill.call_count, 1)
        self.assertFalse(risk.kill_blocks_entries())


class KillFaultClears(unittest.TestCase):
    def test_fault_recorded_then_cleared_by_the_next_check(self):
        risk = _risk()
        with mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True), \
                mock.patch.object(rm, "_publish_kill_snapshot"), \
                mock.patch.object(rm, "_save_kill_state"):
            with mock.patch.object(risk, "_intraday_trading_pnl", return_value=(0.0, False)):
                self.assertFalse(risk.can_open_position())
            self.assertIn("exact tier attribution is unresolved", risk.kill_check_fault or "")
            self.assertTrue(risk.kill_blocks_entries())

            def _exact():
                risk._kill_attr = {"exact": True, "ledger_unreadable": False, "unproven": {}}
                return (0.0, True)

            with mock.patch.object(risk, "_intraday_trading_pnl", side_effect=_exact):
                self.assertFalse(risk.check_kill_switch())
            self.assertIsNone(risk.kill_check_fault)
            self.assertFalse(risk.kill_blocks_entries())
            with mock.patch.object(risk, "_intraday_trading_pnl", side_effect=_exact):
                self.assertTrue(risk.can_open_position())

    def test_latched_kill_is_unchanged(self):
        risk = _risk()
        risk.killed = True
        risk.kill_check_fault = "stale"
        self.assertTrue(risk.check_kill_switch())
        self.assertIsNone(risk.kill_check_fault)
        self.assertFalse(risk.can_open_position())
        self.assertTrue(risk.kill_blocks_entries())

    def test_direct_check_kill_switch_still_raises_for_its_callers(self):
        risk = _risk()
        with mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True), \
                mock.patch.object(risk, "_intraday_trading_pnl", return_value=(0.0, False)):
            with self.assertRaises(RuntimeError):
                risk.check_kill_switch()


# ── (c) request file ─────────────────────────────────────────────────────────────────────────────────────────────────
class RebuildRequestFile(_TmpStateMixin, unittest.TestCase):
    def test_written_once_per_minute_atomically_with_unique_temp_names(self):
        temp_names = []
        real_ntf = rm.tempfile.NamedTemporaryFile

        def _spy(*a, **k):
            h = real_ntf(*a, **k)
            temp_names.append(h.name)
            return h

        clock = {"t": 1000.0}
        with mock.patch.object(rm.tempfile, "NamedTemporaryFile", side_effect=_spy), \
                mock.patch.object(rm.time, "monotonic", side_effect=lambda: clock["t"]):
            rm._request_ledger_rebuild("fp-a", "reason")
            first = json.loads(self.request_path.read_text())
            clock["t"] = 1000.0 + rm._REBUILD_REQUEST_WRITE_SECONDS - 1
            rm._request_ledger_rebuild("fp-a", "reason")
            self.assertEqual(json.loads(self.request_path.read_text()), first)   # rate-limited: no second write
            clock["t"] = 1000.0 + rm._REBUILD_REQUEST_WRITE_SECONDS
            rm._request_ledger_rebuild("fp-b", "reason")
            second = json.loads(self.request_path.read_text())
        self.assertEqual(len(temp_names), 2)
        self.assertEqual(len(set(temp_names)), 2)
        for name in temp_names:
            self.assertEqual(Path(name).parent, self.dir)
            self.assertTrue(Path(name).name.startswith(".ledger_rebuild_request.json."))
            self.assertFalse(Path(name).exists())                   # renamed into place, nothing left behind
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(second["fingerprint"], "fp-b")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["ledger_rebuild_request.json"])

    def test_write_failure_is_logged_and_never_raises(self):
        with mock.patch.object(rm.os, "replace", side_effect=OSError("disk full")), \
                self.assertLogs(rm.logger, level="WARNING") as logs:
            rm._request_ledger_rebuild("fp", "reason")
        self.assertIn("request write failed", "\n".join(logs.output))
        self.assertFalse(self.request_path.exists())
        self.assertEqual([p for p in self.dir.iterdir()], [])        # temp file cleaned up


# ── (b) reasons logged once + (e) resolved metric, through the real exact-join path ─────────────────────────────────
def _ledger(rows: dict) -> dict:
    positions = {}
    for sym, (net, qhm) in rows.items():
        positions[sym] = {"alpaca_net_qty": net, "drift": 0.0, "tiers": {
            "daytrade": {"qty": net - qhm}, "intraday": {"qty": 0.0}, "qhm": {"qty": qhm}, "forever6": {"qty": 0.0}}}
    return {"version": 1, "positions": positions}


def _positions(rows: dict):
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = [{"symbol": s, "qty": str(q), "current_price": "101", "lastday_price": "100"}
                              for s, q in rows.items()]
    return resp


class UnresolvedJoinReasons(_TmpStateMixin, unittest.TestCase):
    def _pnl(self, ledger_rows, broker_rows, fills):
        risk = _risk()
        with mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True), \
                mock.patch.dict(os.environ, {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}), \
                mock.patch.object(ownership_guard, "load_ledger", return_value=_ledger(ledger_rows)), \
                mock.patch.object(rm, "_today_fills", return_value=(tuple(fills), True)), \
                mock.patch.object(rm.requests, "get", return_value=_positions(broker_rows)) as get:
            out = risk._intraday_trading_pnl()
        self.assertTrue(all("paper-api.alpaca.markets" in c.args[0] for c in get.call_args_list))
        return out

    def _join_lines(self, logs):
        return [line for line in logs.output if "exact join unresolved" in line]

    def test_each_of_the_three_checks_logs_its_reason_once(self):
        cases = [
            # (1) ledger net differs from broker
            ({"NVDA": (10.0, 9.0)}, {"NVDA": 11}, [], "ledger_net_differs_from_broker"),
            # (2) non-flat ledger row with no broker position
            ({"GE": (2.0, 2.0)}, {}, [], "ledger_row_without_broker_position"),
            # (3) a QHM-tagged fill today on a symbol the ledger shows flat (the 11-06 NVDA case)
            ({}, {"NVDA": 1}, [("NVDA", "buy", 1, 100.0, "qhm")], "protected_fill_on_ledger_flat_symbol"),
        ]
        for ledger_rows, broker_rows, fills, check in cases:
            with self.subTest(check=check):
                _reset_join_state()
                with self.assertLogs(rm.logger, level="INFO") as logs:
                    self.assertEqual(self._pnl(ledger_rows, broker_rows, fills), (0.0, False))
                    self.assertEqual(self._pnl(ledger_rows, broker_rows, fills), (0.0, False))
                lines = self._join_lines(logs)
                self.assertEqual(len(lines), 1, lines)                      # once, not on every check
                self.assertIn(check, lines[0])
                self.assertIn("kill-join-1107", lines[0])
                detail = json.loads(lines[0].split("unresolved (", 1)[1].rsplit(") — rebuild", 1)[0])
                self.assertIn("ledger_qty", detail)
                self.assertIn("broker_qty", detail)
                self.assertTrue(detail["symbol"])
                self.assertTrue(self.request_path.exists())

    def test_distinct_reasons_each_log_and_an_exact_check_reports_the_wait(self):
        with self.assertLogs(rm.logger, level="INFO") as logs:
            self._pnl({"NVDA": (10.0, 9.0)}, {"NVDA": 11}, [])
            self._pnl({"NVDA": (10.0, 9.0)}, {"NVDA": 12}, [])               # a different broker qty → new reason
            self._pnl({"NVDA": (10.0, 9.0)}, {"NVDA": 12}, [])
            # The rebuild lands: ledger matches the broker → exact again.
            pnl, ok = self._pnl({"NVDA": (12.0, 9.0)}, {"NVDA": 12}, [])
        self.assertTrue(ok)
        self.assertEqual(len(self._join_lines(logs)), 2)
        resolved = [line for line in logs.output if "exact join resolved" in line]
        self.assertEqual(len(resolved), 1)
        self.assertIn("min after the first rebuild request", resolved[0])
        self.assertIsNone(rm._KILL_JOIN_STATE["first_request_wall"])           # episode reset
        self.assertEqual(rm._KILL_JOIN_STATE["logged"], set())


# ── (d) run_ledger_sync ──────────────────────────────────────────────────────────────────────────────────────────────
class LedgerSyncOnRequest(_TmpStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.slack = mock.Mock()
        self.sync_ledger = mock.Mock(return_value={"healed": True, "positions": {}})
        self.fetch_fills = mock.Mock(return_value=[])
        patches = [
            mock.patch("reporting.pnl_ledger.fetch_all_fills", self.fetch_fills),
            mock.patch("reporting.pnl_ledger.fetch_all_orders", return_value=[]),
            mock.patch("reporting.pnl_ledger.fetch_positions", return_value=[]),
            mock.patch("reporting.pnl_ledger.build_coid_map", return_value={}),
            mock.patch("execution.ownership_guard.sync_ledger", self.sync_ledger),
            mock.patch("execution.quarterly_hold_manager.get_quarterly_hold_quantities", return_value={}),
            mock.patch("execution.tier_transfers.load_transfers", return_value=[]),
            mock.patch.object(config, "FOREVER6_ENABLED", False),
            mock.patch.object(rls, "_slack", self.slack),
            mock.patch.object(rls.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_no_request_exits_without_rebuilding(self):
        self.assertEqual(rls.sync_once(scheduled=False, if_requested=True).get("skipped"), "no_request")
        self.sync_ledger.assert_not_called()

    def test_on_request_heal_marks_the_generation_and_watermark(self):
        self.write_request("G1")
        seen = {}

        def _fills():
            from datetime import datetime, timezone
            seen["fetch_utc"] = datetime.now(timezone.utc).isoformat()
            return []

        self.fetch_fills.side_effect = _fills
        res = rls.sync_once(scheduled=False, if_requested=True)
        self.assertTrue(res["healed"])
        st = self.streak()
        self.assertEqual(st["request_done_generation"], "G1")
        self.assertLessEqual(st["last_rebuild_utc"], seen["fetch_utc"])   # captured just before the fill fetch
        # The same generation is now done → the next on-request pass exits at once.
        self.assertEqual(rls.sync_once(scheduled=False, if_requested=True).get("skipped"), "no_request")
        self.assertEqual(self.sync_ledger.call_count, 1)
        self.slack.assert_not_called()

    def test_overlapping_passes_second_exits_busy_and_keeps_the_request(self):
        self.write_request("G1")
        inner = {}

        def _fills():
            before = self.request_path.read_text()
            inner["res"] = rls.sync_once(scheduled=False, if_requested=True)
            inner["request_unchanged"] = self.request_path.read_text() == before
            inner["streak_during"] = self.streak()
            return []

        self.fetch_fills.side_effect = _fills
        with self.assertLogs(rls.logger, level="INFO") as logs:
            outer = rls.sync_once()                       # the scheduled 20-minute pass holds the lock
        self.assertTrue(inner["res"].get("busy"))
        self.assertFalse(inner["res"].get("ok"))
        self.assertTrue(inner["request_unchanged"])
        self.assertNotIn("request_done_generation", inner["streak_during"])
        self.assertTrue(any("busy; request remains pending" in line for line in logs.output))
        self.assertEqual(self.sync_ledger.call_count, 1)  # the busy pass did not rebuild
        self.assertTrue(outer["healed"])
        self.assertEqual(self.streak()["request_done_generation"], "G1")

    def test_busy_main_exits_zero(self):
        with rls._rebuild_lock() as held, \
                mock.patch.object(rls.logging, "basicConfig"), mock.patch.object(rls.logging, "FileHandler"), \
                mock.patch.object(rls, "_load_env"), mock.patch.object(rls, "_SCHEDULED_LOCK_TIMEOUT_S", 0.0):
            self.assertTrue(held)
            self.write_request("G1")
            self.assertEqual(rls.main(["--if-requested"]), 0)
            self.assertEqual(rls.main([]), 0)              # the scheduled pass is busy too, not a failure
        self.sync_ledger.assert_not_called()

    def test_on_request_pass_with_nothing_pending_never_takes_the_lock(self):
        # Cold-2nd: at :00/:20/:40 both crons fire; an idle on-request pass must not make the scheduled pass exit busy.
        with mock.patch.object(rls, "_rebuild_lock") as lock:
            self.assertEqual(rls.sync_once(scheduled=False, if_requested=True).get("skipped"), "no_request")
            self.write_request("G1")
            st = self.streak()
            st["request_done_generation"] = "G1"
            rls._write_streak(st)
            self.assertEqual(rls.sync_once(scheduled=False, if_requested=True).get("skipped"), "no_request")
        lock.assert_not_called()

    def test_scheduled_pass_waits_for_a_held_lock_then_rebuilds(self):
        import fcntl
        import threading
        rls._LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        holder = open(rls._LOCK_PATH, "a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        released = threading.Event()

        def _release(_s):                               # the mocked sleep: the other pass finishes on the first wait
            if not released.is_set():
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                released.set()

        rls.time.sleep.side_effect = _release
        try:
            res = rls.sync_once(lock_wait_s=rls._SCHEDULED_LOCK_TIMEOUT_S)
        finally:
            holder.close()
        self.assertTrue(released.is_set())
        self.assertTrue(res.get("healed"))
        self.assertNotIn("busy", res)
        self.assertEqual(self.sync_ledger.call_count, 1)

    def test_lock_file_unavailable_returns_lock_stage_without_raising(self):
        # Preship counter-check: open() failing yields None and returns before the finally that closes the handle.
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            res = rls.sync_once()
        self.assertEqual(res.get("stage"), "lock")
        self.assertEqual(res.get("error"), "lock file unavailable")
        self.sync_ledger.assert_not_called()

    def test_scheduled_pass_gives_up_busy_after_the_timeout(self):
        import fcntl
        rls._LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        holder = open(rls._LOCK_PATH, "a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        clock = iter(range(0, 10_000, 5))
        try:
            with mock.patch.object(rls.time, "monotonic", side_effect=lambda: float(next(clock))):
                res = rls.sync_once(lock_wait_s=rls._SCHEDULED_LOCK_TIMEOUT_S)
        finally:
            holder.close()
        self.assertTrue(res.get("busy"))
        self.sync_ledger.assert_not_called()

    def test_request_written_mid_run_stays_pending(self):
        self.write_request("G1")

        def _fills():
            self.write_request("G2", fingerprint="fp-2")    # a new fill after the fetch started
            return []

        self.fetch_fills.side_effect = _fills
        self.assertTrue(rls.sync_once(scheduled=False, if_requested=True)["healed"])
        self.assertEqual(self.streak()["request_done_generation"], "G1")   # only the generation it started from
        self.fetch_fills.side_effect = None
        self.assertTrue(rls.sync_once(scheduled=False, if_requested=True)["healed"])   # G2 is still pending → runs
        self.assertEqual(self.streak()["request_done_generation"], "G2")
        self.assertEqual(self.sync_ledger.call_count, 2)

    def test_refused_shrink_keeps_watermark_and_backs_off(self):
        self.streak_path.write_text(json.dumps({"count": 0, "reasons": [], "last_utc": None,
                                                "last_healed_utc": "2026-11-09T14:00:00+00:00",
                                                "last_rebuild_utc": "2026-11-09T13:59:58+00:00",
                                                "request_done_generation": "G0"}))
        self.sync_ledger.return_value = {"healed": False, "reason": "protected_floor_shrink", "pending_heal": {}}
        self.write_request("G1", fingerprint="fp-1")
        res = rls.sync_once(scheduled=False, if_requested=True)
        self.assertFalse(res["healed"])
        st = self.streak()
        self.assertEqual(st["last_rebuild_utc"], "2026-11-09T13:59:58+00:00")   # never advanced by a refusal
        self.assertEqual(st["request_done_generation"], "G0")
        self.assertEqual(st["count"], 0)                                        # on-request: not counted
        self.assertTrue(st["last_pass_refused"])
        self.slack.assert_not_called()                                          # on-request: never pages
        # A newer request generation with the SAME fills/positions fingerprint → back-off, no rebuild.
        self.write_request("G1b", fingerprint="fp-1")
        self.assertEqual(rls.sync_once(scheduled=False, if_requested=True).get("skipped"), "backoff")
        self.assertEqual(self.sync_ledger.call_count, 1)
        # The scheduled pass still runs, counts and pages on its unchanged timing.
        rls.sync_once()
        self.assertEqual(self.sync_ledger.call_count, 2)
        self.assertEqual(self.streak()["count"], 1)
        # A new fill (new fingerprint) ends the back-off.
        self.write_request("G2", fingerprint="fp-2")
        self.assertNotIn("skipped", rls.sync_once(scheduled=False, if_requested=True))
        self.assertEqual(self.sync_ledger.call_count, 3)
        # So does an operator confirmation written after the refusal.
        self.write_request("G3", fingerprint="fp-2")
        self.assertEqual(rls.sync_once(scheduled=False, if_requested=True).get("skipped"), "backoff")
        self.confirm_path.write_text("{}")
        self.sync_ledger.return_value = {"healed": True, "positions": {}}
        res = rls.sync_once(scheduled=False, if_requested=True)
        self.assertTrue(res["healed"])
        st = self.streak()
        self.assertEqual(st["request_done_generation"], "G3")
        self.assertNotIn("last_pass_refused", st)
        self.assertNotEqual(st["last_rebuild_utc"], "2026-11-09T13:59:58+00:00")

    def test_watermark_only_advances_after_a_healed_pass(self):
        self.streak_path.write_text(json.dumps({"count": 0, "last_rebuild_utc": "W0",
                                                "request_done_generation": "G0"}))
        self.write_request("G1")
        self.sync_ledger.side_effect = RuntimeError("save failed")              # an exception mid-pass
        res = rls.sync_once(scheduled=False, if_requested=True)
        self.assertFalse(res["ok"])
        self.assertEqual(self.streak()["last_rebuild_utc"], "W0")
        self.assertEqual(self.streak()["request_done_generation"], "G0")
        self.slack.assert_not_called()                                          # on-request errors never page
        self.sync_ledger.side_effect = None
        self.sync_ledger.return_value = {"healed": True, "positions": {}}
        self.assertTrue(rls.sync_once(scheduled=False, if_requested=True)["healed"])
        self.assertNotEqual(self.streak()["last_rebuild_utc"], "W0")
        self.assertEqual(self.streak()["request_done_generation"], "G1")

    def test_scheduled_errors_still_page(self):
        self.sync_ledger.side_effect = RuntimeError("boom")
        self.assertFalse(rls.sync_once()["ok"])
        self.slack.assert_called_once()


if __name__ == "__main__":
    unittest.main()
