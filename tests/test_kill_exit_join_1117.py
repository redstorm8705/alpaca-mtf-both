#!/usr/bin/env python3
# ruff: noqa: E501
"""KILL-EXIT-JOIN-1117 (owner-approved; logs/design_records/kill_exit_join_2026-11-17.md, rev 4).

2026-11-16: QHM's last GOOGL share was sold by its own GTC stop at the open. Once the ledger healed GOOGL to flat,
every kill check hit "protected_fill_on_ledger_flat_symbol" and ran on the backup measure all day. The exact join now
excludes a qhm-tagged sell on a ledger-flat symbol only when its order is the exit order on QHM's CLOSED record, the
order's total fill today equals that record's exit_qty, and the broker holds no position in the symbol.

Nothing here reaches the broker or the network: the fills API, order lookups, positions and the ledger are mocked,
and every state file lives in a temporary directory.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import config
import execution
import execution.quarterly_hold_manager as qhm_mod
from execution import ownership_guard
from execution import risk_manager as rm
from execution.quarterly_hold_manager import HoldPosition, HoldState, QuarterlyHoldManager, ReconcileResult

try:
    import execution.broker  # noqa: F401
except ModuleNotFoundError:   # the production host has no test-only Alpaca SDK
    _stub = ModuleType("execution.broker")
    for _n in ("cancel_stop_confirmed", "get_order", "get_order_by_client_order_id", "replace_stop_order",
               "cancel_order", "get_open_orders"):
        setattr(_stub, _n, mock.Mock())
    sys.modules["execution.broker"] = _stub
    execution.broker = _stub

STOP_ID = "c6873dcc-989f-4d8f-b145-8319aabc3f4b"            # the 11-16 GOOGL stop, as found on the server
STOP_COID = "QH-GOOGL-s-1794582132824-111eb63c"


def _now_iso(minutes: int = 0) -> str:
    """A transaction time today (PT), `minutes` after now minus one hour, so it stays on today's PT date."""
    t = datetime.now(timezone.utc) - timedelta(hours=1) + timedelta(minutes=minutes)
    if t.astimezone(rm._PT).date() != datetime.now(rm._PT).date():
        t = datetime.now(timezone.utc)
    return t.isoformat().replace("+00:00", "Z")


def _order(oid, coid, sym, side, qty, filled, tif="gtc"):
    return {"id": oid, "client_order_id": coid, "symbol": sym, "side": side, "qty": str(qty),
            "filled_qty": str(filled), "time_in_force": tif}


def _fill(aid, oid, sym, side, qty, px, cum, minutes=0):
    return {"id": aid, "order_id": oid, "symbol": sym, "side": side, "qty": str(qty), "price": str(px),
            "cum_qty": str(cum), "transaction_time": _now_iso(minutes)}


def _positions(rows):
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = [{"symbol": s, "qty": str(q), "current_price": str(cur), "lastday_price": str(pc)}
                              for s, q, cur, pc in rows]
    return resp


def _ledger(rows=None):
    positions = {}
    for sym, (net, qhm) in (rows or {}).items():
        positions[sym] = {"alpaca_net_qty": net, "drift": 0.0, "tiers": {
            "daytrade": {"qty": net - qhm}, "intraday": {"qty": 0.0}, "qhm": {"qty": qhm}, "forever6": {"qty": 0.0}}}
    return {"version": 1, "positions": positions}


def _closed_record(exit_order_id=STOP_ID, exit_qty=1, **extra):
    """The 11-16 GOOGL record as found on the server, plus the two rev-4 fields."""
    rec = {"symbol": "GOOGL", "direction": "long", "target_equity_pct": 0.1, "state": "CLOSED", "qty_filled": 1,
           "stop_order_id": STOP_ID, "closed_reason": "qhm_stop_out",
           "exit_order_id": exit_order_id, "exit_qty": exit_qty}
    rec.update(extra)
    return rec


class _JoinBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.state_path = self.dir / "quarterly_holds.json"
        self.request_path = self.dir / "ledger_rebuild_request.json"
        for p in (mock.patch.object(qhm_mod, "_QHM_STATE_FILE", self.state_path),
                  mock.patch.object(rm, "_LEDGER_REBUILD_REQUEST_FILE", self.request_path),
                  mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True),
                  mock.patch.dict(os.environ, {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self._reset()
        self.addCleanup(self._reset)

    @staticmethod
    def _reset():
        rm._intraday_cache.update(ts=0.0, pt_date="", fills=(), fill_ts=(), fill_oid=(), ok=False)
        rm._intraday_order_tier_cache.clear()
        rm._intraday_order_tier_cache.update(pt_date="", tiers={})
        rm._FILL_SEQ_LOGGED.update(pt_date="", oids=set())
        rm._FILL_MISMATCH_LAST_LOGGED["signature"] = None
        rm._KILL_JOIN_STATE["logged"].clear()
        rm._KILL_JOIN_STATE.update(first_request_wall=None, last_request_wall=None, last_write_mono=None,
                                   generation=None)

    def write_state(self, records: dict):
        self.state_path.write_text(json.dumps(records))

    def join(self, fills, orders, broker_rows=(), ledger_rows=None):
        """One kill check's exact join through the real _today_fills + _intraday_trading_pnl (all I/O mocked)."""
        by_id = {o["id"]: o for o in orders}
        with mock.patch.object(rm, "_load_kill_state", return_value={}):
            risk = rm.RiskManager(2500.0, daily_start_value=2500.0)
        with mock.patch("reporting.pnl_ledger.fetch_all_fills", return_value=list(fills)), \
                mock.patch("execution.broker.get_order", side_effect=lambda oid, nested=True: by_id.get(oid)), \
                mock.patch.object(ownership_guard, "load_ledger", return_value=_ledger(ledger_rows)), \
                mock.patch.object(rm.requests, "get", return_value=_positions(broker_rows)):
            return risk._intraday_trading_pnl(), risk

    def googl_day(self, *, qhm_sold=1, stop_qty=1, stop_coid=STOP_COID, extra_fills=(), extra_orders=()):
        """The 11-16 GOOGL day: QHM's stop sells at the open, then a Day round trip (buy 2 @ 300, sell 2 @ 295)."""
        fills = [_fill("a1", STOP_ID, "GOOGL", "sell", qhm_sold, 310.0, qhm_sold, 0),
                 _fill("a2", "day-buy", "GOOGL", "buy", 2, 300.0, 2, 30),
                 _fill("a3", "day-sell", "GOOGL", "sell", 2, 295.0, 2, 45), *extra_fills]
        orders = [_order(STOP_ID, stop_coid, "GOOGL", "sell", stop_qty, qhm_sold),
                  _order("day-buy", "DT-GOOGL-b-1", "GOOGL", "buy", 2, 2, "day"),
                  _order("day-sell", "DT-GOOGL-s-1", "GOOGL", "sell", 2, 2, "day"), *extra_orders]
        return fills, orders


class GooglReplay(_JoinBase):
    def test_qhm_own_exit_is_excluded_and_the_day_loss_counts(self):
        self.write_state({"GOOGL": _closed_record()})
        fills, orders = self.googl_day()
        with self.assertLogs(rm.logger, level="INFO") as logs:
            (pnl, ok), risk = self.join(fills, orders)
        self.assertTrue(ok)
        self.assertEqual(pnl, -10.0)                                 # Day: 2 x (295 - 300); QHM's sale excluded
        self.assertIs(risk._kill_attr["exact"], True)
        out = "\n".join(logs.output)
        self.assertNotIn("protected_fill_on_ledger_flat_symbol", out)
        self.assertIn(f"[change=kill-exit-join-1117] QHM own exit excluded: GOOGL order {STOP_ID} qty 1", out)
        self.assertFalse(self.request_path.exists())                 # no rebuild requested

    def test_before_the_change_fields_the_record_stays_unresolved(self):
        """The record as written before rev 4 (no exit fields) proves nothing: unresolved exactly as on 11-16."""
        rec = _closed_record()
        del rec["exit_order_id"], rec["exit_qty"]
        self.write_state({"GOOGL": rec})
        fills, orders = self.googl_day()
        with self.assertLogs(rm.logger, level="WARNING") as logs:
            self.assertEqual(self.join(fills, orders)[0], (0.0, False))
        self.assertIn("protected_fill_on_ledger_flat_symbol", "\n".join(logs.output))

    def test_one_change_tag_line_per_symbol_per_day(self):
        self.write_state({"GOOGL": _closed_record()})
        fills, orders = self.googl_day()
        with self.assertLogs(rm.logger, level="INFO") as logs:
            for _ in range(3):
                self.assertTrue(self.join(fills, orders)[0][1])
        lines = [x for x in logs.output if "QHM own exit excluded" in x]
        self.assertEqual(len(lines), 1, lines)

    def test_day_and_swing_fills_in_the_same_symbol_are_counted(self):
        self.write_state({"GOOGL": _closed_record()})
        fills, orders = self.googl_day(
            extra_fills=[_fill("a4", "sw-buy", "GOOGL", "buy", 1, 305.0, 1, 50),
                         _fill("a5", "sw-sell", "GOOGL", "sell", 1, 290.0, 1, 55)],
            extra_orders=[_order("sw-buy", "IN-GOOGL-b-1", "GOOGL", "buy", 1, 1, "day"),
                          _order("sw-sell", "IN-GOOGL-s-1", "GOOGL", "sell", 1, 1, "day")])
        (pnl, ok), _risk = self.join(fills, orders)
        self.assertTrue(ok)
        self.assertEqual(pnl, -10.0 + (290.0 - 305.0))             # Day -10 and Swing -15 both counted


class RefusedCases(_JoinBase):
    def _unresolved(self, fills, orders, broker_rows=(), ledger_rows=None):
        with self.assertLogs(rm.logger, level="INFO") as logs:
            out = self.join(fills, orders, broker_rows, ledger_rows)[0]
        self.assertEqual(out, (0.0, False))
        text = "\n".join(logs.output)
        self.assertIn("protected_fill_on_ledger_flat_symbol", text)
        self.assertNotIn("QHM own exit excluded", text)
        return text

    def test_stale_stop_selling_more_than_qhm_held_is_unresolved(self):
        """Taleb seat: QHM held 1, a stale stop sold 2 (one was a Swing share) -> 2 != 1 -> unresolved."""
        self.write_state({"GOOGL": _closed_record(exit_qty=1)})
        fills, orders = self.googl_day(qhm_sold=2, stop_qty=2)
        text = self._unresolved(fills, orders)
        self.assertIn("QHM exit quantity mismatch: GOOGL order", text)
        self.assertIn("filled 2 today, closed record exit_qty 1", text)

    def test_order_id_not_on_the_record_is_unresolved(self):
        self.write_state({"GOOGL": _closed_record(exit_order_id="some-other-order")})
        self._unresolved(*self.googl_day())

    def test_record_not_closed_is_unresolved(self):
        self.write_state({"GOOGL": _closed_record(state="ACTIVE")})
        self._unresolved(*self.googl_day())

    def test_broker_still_holding_the_symbol_is_unresolved(self):
        self.write_state({"GOOGL": _closed_record()})
        fills, orders = self.googl_day()
        self._unresolved(fills, orders, broker_rows=[("GOOGL", 1, 296.0, 300.0)])

    def test_qhm_buy_on_a_flat_symbol_is_unresolved(self):
        self.write_state({"GOOGL": _closed_record(exit_order_id="qh-buy")})
        fills = [_fill("b1", "qh-buy", "GOOGL", "buy", 1, 300.0, 1)]
        orders = [_order("qh-buy", "QH-GOOGL-b-1794582132824-abc", "GOOGL", "buy", 1, 1, "day")]
        self._unresolved(fills, orders, broker_rows=[("GOOGL", 1, 301.0, 299.0)])

    def test_forever6_fill_is_unresolved(self):
        """F6 has no CLOSED record: a forever6-tagged sell stays unresolved even if a QHM record names its id."""
        self.write_state({"GOOGL": _closed_record()})
        fills, orders = self.googl_day(stop_coid="F6-GOOGL-s-1794582132824-111eb63c")
        self._unresolved(fills, orders)

    def test_stop_filling_across_two_days(self):
        """The stop sold 1 yesterday and 1 today; QHM closed 2. Today's fill of that order is 1 != 2 -> unresolved.
        On the first day (the record not yet CLOSED) the partial is not excluded either."""
        fills = [_fill("p2", STOP_ID, "GOOGL", "sell", 1, 310.0, 2)]           # cum 2: began filling yesterday
        orders = [_order(STOP_ID, STOP_COID, "GOOGL", "sell", 2, 2)]
        self.write_state({"GOOGL": _closed_record(exit_qty=2, qty_filled=2)})
        self.assertIn("filled 1 today, closed record exit_qty 2", self._unresolved(fills, orders))
        self._reset()
        day_one = [_fill("p1", STOP_ID, "GOOGL", "sell", 1, 310.0, 1)]
        orders_one = [_order(STOP_ID, STOP_COID, "GOOGL", "sell", 2, 1)]
        self.write_state({"GOOGL": _closed_record(state="ACTIVE", exit_order_id=None, exit_qty=None, qty_filled=1)})
        self._unresolved(day_one, orders_one)

    def test_fills_without_order_ids_stay_unresolved(self):
        """A fills read not from this check's cached journal (no aligned order ids) cannot prove anything."""
        self.write_state({"GOOGL": _closed_record()})
        with mock.patch.object(rm, "_load_kill_state", return_value={}):
            risk = rm.RiskManager(2500.0, daily_start_value=2500.0)
        with mock.patch.object(rm, "_today_fills", return_value=((("GOOGL", "sell", 1, 310.0, "qhm"),), True)), \
                mock.patch.object(ownership_guard, "load_ledger", return_value=_ledger()), \
                mock.patch.object(rm.requests, "get", return_value=_positions(())):
            self.assertEqual(risk._intraday_trading_pnl(), (0.0, False))


class StateFileUnreadable(_JoinBase):
    def _check(self, setup):
        setup()
        fills, orders = self.googl_day()
        with self.assertLogs(rm.logger, level="WARNING") as logs:
            self.assertEqual(self.join(fills, orders)[0], (0.0, False))
            self.assertEqual(self.join(fills, orders)[0], (0.0, False))
        warn = [x for x in logs.output if "[change=kill-exit-join-1117] closed-record unreadable" in x]
        self.assertEqual(len(warn), 1, warn)                       # one line, not one per check
        self.assertIn("protected_fill_on_ledger_flat_symbol", "\n".join(logs.output))

    def test_missing_file(self):
        self._check(lambda: None)

    def test_unparseable_file(self):
        self._check(lambda: self.state_path.write_text("{not json"))

    def test_read_while_qhm_is_writing(self):
        """A read that lands on a partly written payload (the bytes of a real record cut short)."""
        full = json.dumps({"GOOGL": _closed_record()})
        self._check(lambda: self.state_path.write_text(full[: len(full) // 2]))

    def test_io_error(self):
        self.write_state({"GOOGL": _closed_record()})
        self._check(lambda: self.state_path.chmod(0))

    def test_root_not_an_object(self):
        self._check(lambda: self.state_path.write_text("[]"))


# ── Each QHM close path, driven through its real code, writes the two fields the join reads ─────────────────────────
NOW = datetime(2026, 11, 16, 7, 0, tzinfo=qhm_mod.ET)


def _mgr(positions):
    m = object.__new__(QuarterlyHoldManager)
    m.dry_run = False
    m.broker = mock.Mock()
    m.broker.get_position.return_value = None
    m.alerter = None
    m._positions = dict(positions)
    m._owner_sale_unproven = {}
    m._owner_sale_retry_day = {}
    m._thesis_config = {}
    m._alert = mock.Mock()
    m._now_et = mock.Mock(return_value=NOW)
    m._get_open_orders = mock.Mock(return_value=[])
    m._reentry_zone = mock.Mock(return_value=(-0.04, "2026-11-16"))
    m._reentry_earnings = mock.Mock(return_value=(date(2027, 2, 1), "finnhub_symbol", "amc"))
    return m


def _terminal_stop():
    return SimpleNamespace(id=STOP_ID, symbol="GOOGL", client_order_id=STOP_COID, side="sell", type="stop",
                           time_in_force="gtc", stop_price=290.0, status="filled", qty="1", filled_qty="1",
                           filled_avg_price="310.0", filled_at="2026-11-16T14:31:58Z")


class QhmClosePathsFeedTheJoin(_JoinBase):
    def setUp(self):
        super().setUp()
        (self.dir / "logs").mkdir()
        for p in (mock.patch.object(qhm_mod, "_ROOT", self.dir), mock.patch("trade_logger.log_event"),
                  mock.patch.multiple("execution.ownership_guard",
                                      apply_authorized_tier_reduction=mock.Mock(return_value=True),
                                      apply_authorized_tier_fill_reduction=mock.Mock(return_value=True))):
            p.start()
            self.addCleanup(p.stop)

    def _hold(self, state):
        return HoldPosition(symbol="GOOGL", direction="long", target_equity_pct=0.1, state=state, qty_filled=1,
                            qty_total=1, avg_entry_price=300.0, stop_price=290.0, stop_order_id=STOP_ID)

    def _real_save(self, m):
        m.state_path = self.state_path
        m._save_state = QuarterlyHoldManager._save_state.__get__(m)

    def _join_excludes_exactly_that_fill(self):
        fills, orders = self.googl_day()
        (pnl, ok), _risk = self.join(fills, orders)
        self.assertTrue(ok)
        self.assertEqual(pnl, -10.0)

    def test_external_close_path(self):
        pos = self._hold(HoldState.ACTIVE)
        m = _mgr({"GOOGL": pos})
        self._real_save(m)
        with mock.patch("execution.broker.get_order", return_value=_terminal_stop()):
            self.assertTrue(m._detect_external_close(pos, ReconcileResult()))
        rec = json.loads(self.state_path.read_text())["GOOGL"]
        self.assertEqual((rec["state"], rec["exit_order_id"], rec["exit_qty"]), ("CLOSED", STOP_ID, 1))
        self._join_excludes_exactly_that_fill()

    def test_registered_stop_path(self):
        pos = self._hold(HoldState.PENDING_STOP_REPLACE)
        m = _mgr({"GOOGL": pos})
        self._real_save(m)
        with mock.patch("execution.broker.get_order", return_value=_terminal_stop()):
            self.assertFalse(m.resubmit_stop_if_needed("GOOGL", []))
        rec = json.loads(self.state_path.read_text())["GOOGL"]
        self.assertEqual((rec["state"], rec["exit_order_id"], rec["exit_qty"]), ("CLOSED", STOP_ID, 1))
        self.assertIsNone(rec["stop_order_id"])                      # existing behaviour kept
        self.assertEqual(rec["qty_filled"], 0)
        self._join_excludes_exactly_that_fill()

    def test_registered_stop_failed_save_restores_the_fields(self):
        pos = self._hold(HoldState.PENDING_STOP_REPLACE)
        m = _mgr({"GOOGL": pos})
        seen = []
        m._save_state = mock.Mock(side_effect=lambda: seen.append((pos.state, pos.exit_order_id, pos.exit_qty))
                                  and False)
        with mock.patch("execution.broker.get_order", return_value=_terminal_stop()):
            m.resubmit_stop_if_needed("GOOGL", [])
        self.assertEqual(seen[0], (HoldState.CLOSED, STOP_ID, 1))   # the failed save carried the fields
        self.assertEqual(pos.state, HoldState.PENDING_STOP_REPLACE)
        self.assertIsNone(pos.exit_order_id)
        self.assertIsNone(pos.exit_qty)

    def test_external_close_not_by_our_stop_records_no_exit_order(self):
        pos = self._hold(HoldState.ACTIVE)
        m = _mgr({"GOOGL": pos})
        self._real_save(m)
        other = SimpleNamespace(status="canceled")
        proof = SimpleNamespace(proven=False, reason="no matching order", order_ids=[], retry=False)
        with mock.patch("execution.broker.get_order", return_value=other), \
                mock.patch("execution.manual_sale_proof.prove_owner_sale", return_value=proof), \
                mock.patch.object(ownership_guard, "load_ledger", return_value=_ledger()), \
                mock.patch.object(m, "_cancel_own_sells_on_flat"):
            self.assertTrue(m._detect_external_close(pos, ReconcileResult()))
        rec = json.loads(self.state_path.read_text())["GOOGL"]
        self.assertEqual(rec["state"], "CLOSED")
        self.assertIsNone(rec["exit_order_id"])
        self.assertIsNone(rec["exit_qty"])

    def test_old_state_file_without_the_fields_loads(self):
        rec = _closed_record()
        del rec["exit_order_id"], rec["exit_qty"]
        pos = HoldPosition.from_dict(rec)
        self.assertIsNone(pos.exit_order_id)
        self.assertIsNone(pos.exit_qty)


if __name__ == "__main__":
    unittest.main()
