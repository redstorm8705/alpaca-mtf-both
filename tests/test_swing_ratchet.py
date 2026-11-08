# ruff: noqa: E501
"""SWING-RATCHET-1105 (owner design logs/design_records/swing_ratchet_2026-11-05.md rev 9/9.1): a Swing lot whose
completed close reached entry + 2R gets its own IN- stop raised in place to max(stop, min(entry + 0.25R, bid - $0.01)).
Every broker and data call is a fake; nothing here can reach Alpaca."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

from execution import swing_breakout_manager as sb
from tests.test_swing_breakout_manager import _mods

COID = "IN-BRK.B-b-1761572000000-abcd1234"
PARAMS = {"version": 1, "variant": "R+ k=2", "trigger_r": 2.0, "lock_r": 0.25, "tick_usd": 0.01, "trial_count": 46}
NOW = datetime(2026, 11, 5, 11, 0, tzinfo=sb.ET)
LIVE = ("new", "held", "accepted", "pending_new")


def _order(oid, px, coid="IN-BRK.B-s-1-stop", status="new", qty=2, **kw):
    base = dict(id=oid, status=status, side="sell", order_type="stop", qty=str(qty), stop_price=str(px),
                client_order_id=coid, filled_qty="0", filled_avg_price=None, replaced_by=None, replaces=None)
    base.update(kw)
    return SimpleNamespace(**base)


class RatchetBroker:
    """A fake Alpaca: orders by id; replace moves a stop in place (old -> replaced, new id); modes model failures."""

    def __init__(self, stop_px=527.81, held=2):
        self.orders = {"stop-1": _order("stop-1", stop_px)}
        self.held = held
        self.replaces, self.submits, self.cancels, self.closes = [], [], [], []
        self.replace_mode = "ok"          # ok | none | lost_reply | rejected_new
        self.coid_lookup = "normal"       # normal | missing
        self.submit_status = "new"
        self.submit_mode = "ok"           # ok | lost (POST never reached Alpaca) | held (PROTECTION_ALREADY_HELD)
        self.PROTECTION_ALREADY_HELD = object()
        self.PROTECTION_UNKNOWN = object()
        self.open_orders_ok = True
        self.n = 0

    def get_clock(self):
        return {"is_open": True, "next_close": None}

    def get_open_orders(self, symbol=None):
        if not self.open_orders_ok:
            return None
        return [o for o in self.orders.values() if o.status in LIVE + ("pending_replace", "pending_cancel")]

    def get_order(self, oid):
        return self.orders.get(oid)

    def resolve_live_order(self, oid, max_hops=5):
        # mirrors broker.resolve_live_order: an unreadable successor ends the walk at the last readable order
        o, hops = self.orders.get(oid), 0
        while o is not None and o.status == "replaced" and o.replaced_by and hops < max_hops:
            nxt = self.orders.get(o.replaced_by)
            if nxt is None:
                break
            oid, o, hops = o.replaced_by, nxt, hops + 1
        return o, oid

    def replace_stop_order(self, sym, oid, px, qty=None, client_order_id=None):
        self.replaces.append((sym, oid, px))
        if self.replace_mode == "none":
            return None                                    # rejected (e.g. 40310000): old untouched
        self.n += 1
        nid = f"stop-r{self.n}"
        old = self.orders[oid]
        new = _order(nid, px, coid=f"IN-BRK.B-s-r{self.n}", qty=int(qty) if qty is not None else int(float(old.qty)),
                     replaces=oid,
                     status="rejected" if self.replace_mode == "rejected_new" else "new")
        old.status, old.replaced_by = "replaced", nid
        self.orders[nid] = new
        return None if self.replace_mode == "lost_reply" else new

    def submit_gtc_stop_order(self, sym, qty, side, px, tier="intraday", allow_cancel_blocking=True,
                              client_order_id=None):
        self.submits.append((sym, qty, side, px, client_order_id))
        if self.submit_mode == "lost":
            return None
        if self.submit_mode == "held":
            return self.PROTECTION_ALREADY_HELD
        if any(getattr(o, "client_order_id", None) == client_order_id for o in self.orders.values()):
            return None                                    # Alpaca refuses a duplicate client id
        o = _order(f"rp-{len(self.submits)}", px, coid=client_order_id or f"IN-{sym}-s-{len(self.submits)}-gtc",
                   qty=qty, status=self.submit_status)
        self.orders[o.id] = o
        return o

    def get_order_by_client_order_id(self, cid):
        if self.coid_lookup == "missing":
            return None
        return next((o for o in self.orders.values() if o.client_order_id == cid), None)

    def get_open_position(self, sym):
        return SimpleNamespace(qty=str(self.held)) if self.held else None

    def cancel_stop_confirmed(self, sym, oid):
        self.cancels.append(oid)
        o = self.orders.get(oid)
        if o is not None and o.status in LIVE:
            o.status = "canceled"                            # a confirmed cancel ends the order
        return True

    def cancel_order(self, oid):
        self.cancels.append(oid)
        return True

    def partial_close_position(self, sym, qty, tier="intraday", _return_order=False):
        self.closes.append((sym, qty))
        o = _order(f"close-{len(self.closes)}", 0, status="filled", side="sell", order_type="market", qty=qty,
                   filled_qty=str(qty), filled_avg_price="540.00")
        self.orders[o.id] = o
        self.held = 0
        return o


def _daily(closes, start="2026-10-27"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6}, index=idx)


def _this_session(state):
    """A pending ratchet state seeded by a test is from THIS session unless the test gives it an age."""
    for rec in (state.get("positions") or {}).values():
        rt = rec.get("ratchet") if isinstance(rec, dict) else None
        if isinstance(rt, dict) and rt.get("status") in ("pending", "reprotect_pending"):
            rt.setdefault("attempt_ts", NOW.isoformat())
    return state


def _lot(**kw):
    rec = {"status": "open", "qty": 2, "entry_px": 544.60, "stop_px": 527.81, "init_stop_px": 527.81,
           "init_risk_ps": 16.79, "entry_date": "2026-10-27", "coid": COID, "stop_order_id": "stop-1"}
    rec.update(kw)
    return rec


class Pure(unittest.TestCase):
    def test_brkb_target_is_548_80(self):
        t = sb.ratchet_target(544.60, 544.60 - 527.81, 527.81, 581.00, 0.25, 0.01)
        self.assertEqual(t["lock_px"], 548.80)              # 544.60 + 0.25 x 16.79 = 548.7975, rounded UP
        self.assertEqual((t["target"], t["raise_"]), (548.80, True))

    def test_never_down(self):
        t = sb.ratchet_target(544.60, 16.79, 560.00, 581.00, 0.25, 0.01)
        self.assertEqual((t["target"], t["raise_"]), (560.00, False))
        t = sb.ratchet_target(544.60, 16.79, 540.00, 539.00, 0.25, 0.01)   # bid under the stop: never lowered
        self.assertEqual((t["target"], t["raise_"]), (540.00, False))

    def test_market_below_lock_raises_to_just_under_the_bid(self):
        t = sb.ratchet_target(544.60, 16.79, 527.81, 546.505, 0.25, 0.01)
        self.assertEqual((t["under_bid_px"], t["target"]), (546.49, 546.49))   # rounded DOWN under the bid

    def test_tick_rounding(self):
        self.assertEqual(sb.tick_ceil(548.7975, 0.01), 548.80)
        self.assertEqual(sb.tick_ceil(548.80, 0.01), 548.80)                  # exact cents stay
        self.assertEqual(sb.tick_floor(580.999, 0.01), 580.99)

    def test_reprotect_coid_format_and_limits(self):
        c48 = "IN-" + "A" * 45
        self.assertEqual(len(c48), 48)
        cid = sb.reprotect_coid(c48, 9999)
        self.assertTrue(cid.startswith("IN-rp-") and len(cid) <= 48)
        self.assertRegex(cid, r"^[A-Za-z0-9-]+$")
        self.assertEqual(sb.reprotect_coid(c48, 9999), cid)                  # deterministic: a retry reuses it
        self.assertNotEqual(sb.reprotect_coid(c48, 1), sb.reprotect_coid(c48, 2))
        a, b = "IN-" + "X" * 40 + "AAAAA", "IN-" + "X" * 40 + "BBBBB"   # long ids sharing a prefix
        self.assertNotEqual(sb.reprotect_coid(a, 0), sb.reprotect_coid(b, 0))
        self.assertIsNone(sb.reprotect_coid(c48, 10 ** 30))                  # > 48 chars → refused
        for bad in (-1, "3", True, 1.5, None):
            self.assertIsNone(sb.reprotect_coid(c48, bad))
        self.assertIsNone(sb.reprotect_coid("", 0))
        self.assertEqual(sb._ord_status(SimpleNamespace(status="OrderStatus.NEW")), "new")

    def test_armed_on_completed_closes_only(self):
        df = _daily([550, 560, 579.09])
        a = sb.ratchet_armed(df, "2026-10-27", 544.60, 16.79, 2.0, 0.01)
        self.assertTrue(a["armed"])
        self.assertEqual(a["armed_session"], "2026-10-29")
        self.assertFalse(sb.ratchet_armed(_daily([550, 570, 578.0]), "2026-10-27", 544.60, 16.79, 2.0, 0.01)["armed"])
        self.assertFalse(sb.ratchet_armed(_daily([600, 550], start="2026-10-23"), "2026-10-27", 544.60, 16.79, 2.0, 0.01)["armed"])
        self.assertIsNone(sb.ratchet_armed(None, "2026-10-27", 544.60, 16.79, 2.0, 0.01))

    def test_params_schema(self):
        self.assertIsNotNone(sb._validate_ratchet_params(PARAMS))
        for bad in ({}, dict(PARAMS, version=2), dict(PARAMS, trigger_r=0), dict(PARAMS, lock_r=2.0),
                    dict(PARAMS, lock_r=-0.1), dict(PARAMS, tick_usd=0), dict(PARAMS, trigger_r="2"),
                    dict(PARAMS, trigger_r=float("nan")), dict(PARAMS, lock_r=True)):
            self.assertIsNone(sb._validate_ratchet_params(bad), bad)

    def test_repo_params_file_is_valid(self):
        d = json.loads(sb._RATCHET_PARAMS.read_text())
        p = sb._validate_ratchet_params(d)
        self.assertEqual((p["trigger_r"], p["lock_r"], p["tick_usd"]), (2.0, 0.25, 0.01))


class KellyMaskedLoss(unittest.TestCase):
    def _lots_for(self, extra):
        # 30 trend-break lots so the live sample (not the prior) drives the loss multiple
        base = [{"coid": f"t{i}", "exit_reason": "trend_break", "realized_pnl": 5.0, "risk_usd": 10.0} for i in range(30)]
        return base + extra

    def test_m1_ratchet_fill_never_shrinks_the_loss_multiple(self):
        prior = {"n": 1053, "mean_r": 0.18, "sd_r": 1.5, "loss_mult_stopped": 1.06}
        # entry 100, entry stop 90 (R $10): three protective stops filled at 87 = -1.30R
        prot = [{"coid": f"p{i}", "exit_reason": "protective_stop", "entry_px": 100.0, "stop_px": 90.0,
                 "init_risk_ps": 10.0, "exit_px": 87.0, "qty": 1, "realized_pnl": -13.0, "risk_usd": 10.0} for i in range(3)]
        # ratchet stop at entry + 0.25R = 102.50, gapped to 99.50: R = -0.05, gap excess 0.30R
        rat = {"coid": "r", "exit_reason": "ratchet_stop", "entry_px": 100.0, "stop_px": 102.5, "init_risk_ps": 10.0,
               "exit_px": 99.5, "qty": 1, "realized_pnl": -0.5, "risk_usd": 10.0}
        self.assertAlmostEqual(sb.lot_r(rat), -0.05, places=6)
        k = sb.kelly_risk(self._lots_for(prot + [rat]), prior)
        self.assertEqual(k["source"], "fills")
        self.assertGreaterEqual(k["loss_mult"], 1.30 - 1e-9)
        self.assertEqual(k["n_live_stopped"], 4)
        # a relabelled profit-lock win (filled at its stop) counts exactly 1.0, never below
        win = dict(rat, coid="w", exit_px=102.5, realized_pnl=2.5)
        self.assertEqual(sb.stop_gap_multiple(win), 1.0)
        self.assertEqual(sb.stop_gap_multiple(dict(win, exit_reason="protective_stop")), 1.0)

    def test_m1_brkb_520_gap_counts_its_full_excess(self):
        gap = {"coid": "g", "exit_reason": "ratchet_stop", "entry_px": 544.60, "stop_px": 548.80, "exit_px": 520.0,
               "init_risk_ps": 16.79, "qty": 2, "realized_pnl": round((520.0 - 544.60) * 2, 2), "risk_usd": 33.58}
        self.assertAlmostEqual(sb.stop_gap_multiple(gap), 1 + 28.80 / 16.79, places=9)
        self.assertAlmostEqual(sb.lot_r(gap), -1.465, places=3)

    def test_m1_legacy_records_keep_their_r_based_multiple_floored_at_one(self):
        self.assertEqual(sb.stop_gap_multiple({"exit_reason": "protective_stop", "realized_pnl": -13.0, "risk_usd": 10.0}),
                         1.3)
        # a ratchet stop without its prices is never guessed as 1.0: it is excluded (R4-2)
        self.assertIsNone(sb.stop_gap_multiple({"exit_reason": "ratchet_stop", "realized_pnl": 2.0, "risk_usd": 10.0}))

    def test_m2_entry_risk_never_from_a_raised_stop(self):
        raised = {"entry_px": 544.60, "stop_px": 548.80, "qty": 2, "ratchet": {"order_ids": ["stop-r1"]}}
        self.assertEqual(sb._entry_risk_ps(raised), 0.0)
        self.assertAlmostEqual(sb._entry_risk_ps(dict(raised, risk_usd=33.58)), 16.79, places=6)
        self.assertIsNone(sb._risk_usd_ps(dict(raised, risk_usd=33.58, partial_pnl=4.0)))   # qty no longer entry qty

    def test_lot_r_uses_entry_risk_not_raised_stop(self):
        rec = {"realized_pnl": 8.40, "entry_px": 544.60, "stop_px": 548.80, "init_risk_ps": 16.79, "qty": 2}
        self.assertAlmostEqual(sb.lot_r(rec), 8.40 / 33.58, places=6)
        self.assertAlmostEqual(sb._lot_risk({"entry_px": 544.60, "stop_px": 548.80, "init_stop_px": 527.81, "qty": 2}),
                               33.58, places=6)

    def test_label_by_provenance(self):
        rec = {"ratchet": {"order_ids": ["stop-r1"]}}
        self.assertEqual(sb._stop_exit_label(rec, "stop-r1"), "ratchet_stop")
        self.assertEqual(sb._stop_exit_label(rec, "stop-r1", partial=True), "ratchet_stop_partial")
        self.assertEqual(sb._stop_exit_label(rec, "stop-other"), "protective_stop")   # raised by another path


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.params = d / "ratchet_params.json"
        self.params.write_text(json.dumps(PARAMS))
        self.events = d / "trade_events.jsonl"
        self.ps = [mock.patch.object(sb, "_STATE", d / "sb.json"), mock.patch.object(sb, "_RATCHET_PARAMS", self.params),
                   mock.patch.object(sb, "_TRADE_EVENTS", self.events)]
        for p in self.ps:
            p.start()
        self.logs, self.pages = [], []
        self.ps += [mock.patch.object(sb, "_log", lambda ev, sym, **kw: self.logs.append((ev, sym, kw))),
                    mock.patch.object(sb, "_page", lambda m: self.pages.append(m))]
        self.opens = []                                                  # no session open since the attempt
        self.ps.append(mock.patch.object(sb.SwingBreakoutManager, "_session_opens",
                                         staticmethod(lambda a, b: self.opens)))
        for p in self.ps[-3:]:
            p.start()
        import execution.ownership_guard as og
        self.ps.append(mock.patch.object(og, "load_ledger", lambda: {"positions": {}}))   # never the real ledger
        self.ps[-1].start()
        self.bid = 581.00
        self.quote_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.closes = [550.0, 565.0, 579.09]

    def tearDown(self):
        for p in reversed(self.ps):
            p.stop()
        self.tmp.cleanup()

    def _cycle(self, broker, rec=None, state=None, now=NOW):
        if state is None:
            state = _this_session({"positions": {"BRK.B": rec if rec is not None else _lot()}})
            sb._save_state(state)
        quote = (lambda s: None) if self.bid is None else \
            (lambda s: {"bid": self.bid, "ask": self.bid + 0.10, "ts": self.quote_ts})
        data = SimpleNamespace(get_latest_quote=quote,
                               get_latest_trade_with_time=lambda s: (self.bid, datetime.now(timezone.utc)))
        m = sb.SwingBreakoutManager()
        with _mods({"execution.broker": broker, "data.alpaca_data": data}), \
                mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: _daily(self.closes))), \
                mock.patch("time.sleep"):
            m._ratchet(state, now)
        return state

    def _actions(self):
        return [kw.get("action") for ev, _s, kw in self.logs if ev == "stop_ratchet"]

    def test_brkb_raises_once_to_548_80_and_state_follows(self):
        b = RatchetBroker()
        st = self._cycle(b)
        self.assertEqual(b.replaces, [("BRK.B", "stop-1", 548.80)])
        rec = st["positions"]["BRK.B"]
        self.assertEqual((rec["stop_px"], rec["stop_order_id"], rec["ratchet"]["status"]), (548.80, "stop-r1", "done"))
        self.assertEqual(rec["ratchet"]["order_ids"], ["stop-r1"])
        self.assertEqual(sb._load_state()["positions"]["BRK.B"]["stop_px"], 548.80)     # persisted
        self.assertIn("replaced", self._actions())
        rs = next(kw for ev, _s, kw in self.logs if ev == "stop_ratchet" and kw.get("action") == "replace_sent")
        for k in ("entry_px", "init_risk_ps", "cur_stop", "bid", "ask", "quote_age_s", "trigger_r", "lock_r",
                  "old_order_id", "old_qty", "position_qty", "target_px", "lock_px", "armed_session", "change"):
            self.assertIn(k, rs)
        self.assertEqual(rs["change"], "swing-ratchet-1105")
        self._cycle(b, state=st)                                         # second cycle: no second replace
        self.assertEqual(len(b.replaces), 1)

    def test_brkb_higher_owner_stop_is_left_alone(self):
        b = RatchetBroker()
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "own-1"   # owner moved it in the UI
        b.orders["own-1"] = _order("own-1", 561.85, coid="b5d3c2e1-untagged")
        st = self._cycle(b)
        self.assertEqual((b.replaces, b.submits, b.cancels), ([], [], []))
        self.assertEqual(b.orders["own-1"].stop_price, "561.85")
        self.assertEqual(st["positions"]["BRK.B"]["stop_px"], 527.81)
        self.assertIn("no_op", self._actions())
        self.assertEqual(self.pages, [])

    def test_lower_owner_stop_left_alone_and_paged_once(self):
        b = RatchetBroker()
        b.orders["stop-1"].status = "canceled"
        b.orders["own-1"] = _order("own-1", 530.00, coid="manual")
        st = self._cycle(b)
        self._cycle(b, state=st)
        self.assertEqual((b.replaces, b.submits), ([], []))
        self.assertEqual(len(self.pages), 1)

    def test_stop_already_above_lock_is_never_lowered(self):
        b = RatchetBroker(stop_px=560.00)
        st = self._cycle(b, rec=_lot(stop_px=560.00))
        self.assertEqual(b.replaces, [])
        self.assertEqual((st["positions"]["BRK.B"]["stop_px"], st["positions"]["BRK.B"]["ratchet"]["status"]),
                         (560.00, "done"))

    def test_not_armed_below_trigger(self):
        self.closes = [550.0, 570.0, 578.0]                             # 578.18 needed
        b = RatchetBroker()
        st = self._cycle(b)
        self.assertEqual(b.replaces, [])
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["checked"], "2026-11-05")
        self.assertIn("not_armed", self._actions())

    def test_after_hours_no_op(self):
        b = RatchetBroker()
        b.get_clock = lambda: {"is_open": False, "next_close": None}
        self._cycle(b)
        self.assertEqual((b.replaces, self.logs), ([], []))

    def test_stale_or_crossed_bid_no_replace(self):
        b = RatchetBroker()
        self.quote_ts = "2026-11-05T14:00:00.123456789Z"                 # hours old
        self._cycle(b)
        self.bid = None
        self._cycle(b)
        self.assertEqual(b.replaces, [])

    def test_nanosecond_quote_timestamp_parses(self):
        b = RatchetBroker()
        now = datetime.now(timezone.utc)
        self.quote_ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + "123456789Z"
        self._cycle(b)
        self.assertEqual(len(b.replaces), 1)

    def test_replace_rejected_keeps_old_stop_and_retries_next_cycle(self):
        b = RatchetBroker()
        b.replace_mode = "none"                                         # e.g. 40310000: broker returned None
        st = self._cycle(b)
        rec = st["positions"]["BRK.B"]
        self.assertEqual((rec["stop_px"], rec["stop_order_id"], rec["ratchet"]["status"]), (527.81, "stop-1", "idle"))
        self.assertEqual(b.orders["stop-1"].status, "new")              # old stop never cancelled
        self.assertEqual(b.submits, [])
        b.replace_mode = "ok"
        self._cycle(b, state=st)
        self.assertEqual(len(b.replaces), 2)
        self.assertEqual(st["positions"]["BRK.B"]["stop_px"], 548.80)

    def test_lost_reply_is_found_by_the_link_not_replaced_twice(self):
        b = RatchetBroker()
        b.replace_mode = "lost_reply"                                    # PATCH applied, reply lost
        st = self._cycle(b)
        rec = st["positions"]["BRK.B"]
        self.assertEqual((rec["stop_order_id"], rec["stop_px"]), ("stop-r1", 548.80))
        self.assertEqual(len(b.replaces), 1)

    def test_crash_between_patch_and_save_reconciles_without_a_second_patch(self):
        b = RatchetBroker()
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "stop-r9"
        b.orders["stop-r9"] = _order("stop-r9", 548.80, replaces="stop-1")
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "target_px": 548.80,
                          "lock_px": 548.80, "new_order_id": "", "armed_session": "2026-10-29"}
        st = self._cycle(b, rec=rec)
        self.assertEqual(b.replaces, [])
        self.assertEqual((st["positions"]["BRK.B"]["stop_order_id"], st["positions"]["BRK.B"]["ratchet"]["status"]),
                         ("stop-r9", "done"))

    def test_old_unreadable_stays_pending_no_order(self):
        b = RatchetBroker()
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "gone", "old_stop_px": 527.81, "lock_px": 548.80}
        st = self._cycle(b, rec=rec)
        self.assertEqual((b.replaces, b.submits), ([], []))
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["status"], "pending")
        self.assertEqual(len(self.pages), 1)

    def test_reprotect_is_deferred_while_pending(self):
        b = RatchetBroker()
        rec = _lot(unprotected=True, stop_order_id="")
        rec["ratchet"] = {"status": "pending", "attempt_ts": NOW.isoformat()}
        st = {"positions": {"BRK.B": rec}}
        sb._save_state(st)
        with _mods({"execution.broker": b}):
            self.assertFalse(sb.SwingBreakoutManager()._reprotect("BRK.B", rec, st, 2))
        self.assertEqual(b.submits, [])

    def test_lost_replace_reprotects_and_retry_reuses_the_same_id(self):
        b = RatchetBroker()
        b.replace_mode = "rejected_new"                                  # old replaced, new rejected: no live stop
        b.submit_mode = "lost"                                           # the POST timed out before reaching Alpaca
        st = self._cycle(b)
        rec = st["positions"]["BRK.B"]
        self.assertEqual(rec["ratchet"]["status"], "reprotect_pending")
        self.assertEqual(len(b.submits), 1)
        sym, qty, side, px, cid = b.submits[0]
        self.assertEqual((sym, qty, side, px), ("BRK.B", 2, "sell", 527.81))   # last confirmed level (below the bid)
        self.assertEqual(cid, sb.reprotect_coid(COID, 0))
        b.submit_mode = "ok"
        b.coid_lookup = "missing"                                        # placed, but not yet visible by client id
        self._cycle(b, state=st)                                         # next cycle: retried with the SAME id
        self.assertEqual([s[4] for s in b.submits], [cid, cid])
        self.assertEqual(len([o for o in b.orders.values() if o.client_order_id == cid]), 1)   # never a duplicate
        rec = st["positions"]["BRK.B"]                                   # the one live stop is adopted at once
        self.assertEqual((rec["ratchet"]["status"], rec["unprotected"], rec["stop_order_id"]), ("idle", False, "rp-2"))
        rs = next(kw for ev, _s, kw in self.logs if kw.get("action") == "reprotect_sent")
        for k in ("client_order_id", "submitted_qty", "position_qty", "reprotect_seq"):
            self.assertIn(k, rs)

    def test_reprotect_level_above_bid_goes_under_the_bid(self):
        b = RatchetBroker()
        b.replace_mode = "rejected_new"
        self.bid = 525.00                                                # market fell under the last stop
        self.closes = [550.0, 565.0, 579.09]
        self._cycle(b, rec=_lot())
        self.assertEqual(b.replaces, [])                               # target max(527.81, 524.99) = 527.81: no raise
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80,
                          "armed_session": "2026-10-29"}
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "stop-x"
        b.orders["stop-x"] = _order("stop-x", 548.80, status="rejected", replaces="stop-1")
        self._cycle(b, rec=rec)
        self.assertEqual(b.submits[-1][3], 524.99)                      # owner exception (c): bid − one tick
        self.assertGreater(b.submits[-1][3], 0)

    def test_reprotect_rejected_moves_to_the_next_sequence(self):
        b = RatchetBroker()
        b.replace_mode = "rejected_new"
        b.submit_status = "rejected"
        st = self._cycle(b)
        cid0 = sb.reprotect_coid(COID, 0)
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["reprotect_seq"], 1)
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["reprotect_coid"], sb.reprotect_coid(COID, 1))
        b.submit_status = "new"
        self._cycle(b, state=st)
        self.assertEqual([s[4] for s in b.submits], [cid0, sb.reprotect_coid(COID, 1)])
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["status"], "idle")

    def test_invalid_sequence_places_no_order_and_pages(self):
        b = RatchetBroker()
        b.orders["stop-1"].status = "canceled"                           # no live stop: a re-protection is due
        rec = _lot(stop_order_id="", unprotected=True)
        rec["ratchet"] = {"status": "reprotect_pending", "reprotect_seq": -3, "reprotect_coid": "", "old_stop_px": 527.81}
        self._cycle(b, rec=rec)
        self.assertEqual(b.submits, [])
        self.assertTrue(any("re-protection id invalid" in p for p in self.pages))

    def test_ratchet_stop_fill_is_booked_by_provenance(self):
        b = RatchetBroker()
        st = self._cycle(b)
        new = b.orders["stop-r1"]
        new.status, new.filled_qty, new.filled_avg_price = "filled", "2", "548.80"
        m = sb.SwingBreakoutManager()
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            m._reconcile(st)
        rec = st["positions"]["BRK.B"]
        self.assertEqual((rec["status"], rec["exit_reason"]), ("closed", "ratchet_stop"))
        self.assertAlmostEqual(rec["realized_pnl"], 8.40, places=2)

    def test_reconstructs_entry_risk_from_the_entry_event(self):
        self.events.write_text(json.dumps({"event": "entry", "symbol": "BRK.B", "trade_id": COID, "price": 544.60,
                                           "stop": 527.81}) + "\n")
        b = RatchetBroker()
        rec = _lot()
        rec.pop("init_risk_ps")
        rec.pop("init_stop_px")
        st = self._cycle(b, rec=rec)
        self.assertAlmostEqual(st["positions"]["BRK.B"]["init_risk_ps"], 16.79, places=6)
        self.assertEqual(b.replaces[0][2], 548.80)

    def test_missing_or_disagreeing_entry_record_means_no_ratchet(self):
        b = RatchetBroker()
        rec = _lot()
        rec.pop("init_risk_ps")
        rec.pop("init_stop_px")
        self._cycle(b, rec=dict(rec))
        self.events.write_text(json.dumps({"event": "entry", "symbol": "BRK.B", "trade_id": COID, "price": 544.60,
                                           "stop": 520.00}) + "\n")
        self._cycle(b, rec=dict(rec))
        self.assertEqual(b.replaces, [])
        self.assertEqual(len(self.pages), 2)

    # ── cold review round 10 ──────────────────────────────────────────────────
    def test_r10_1b_unknown_replace_adds_nothing_then_adopts_the_successor(self):
        b = RatchetBroker(held=20)
        rec = self._partial_cover_lot(b)
        b.orders["own-15"].status = "canceled"
        b.replace_mode = "lost_reply"                                       # applied at Alpaca, reply lost
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        r = st["positions"]["BRK.B"]
        self.assertEqual(r["topup_pending"]["old_id"], "own5")
        self.assertEqual(b.submits, [])                                     # nothing added
        self.assertTrue(any(p.startswith("CRITICAL [BRK.B]") and "uncovered" in p for p in self.pages))
        _, st = self._run(b, st)                                            # next cycle: the chain is re-read
        r = st["positions"]["BRK.B"]
        self.assertNotIn("topup_pending", r)
        live = self._own_live(b, st)
        self.assertEqual([(o.id, int(float(o.qty))) for o in live], [(r["stop_order_id"], 10)])
        self.assertEqual(len(b.replaces), 1)

    def test_r10_1b_replace_not_applied_is_retried_never_a_second_stop(self):
        b = RatchetBroker(held=20)
        rec = self._partial_cover_lot(b)
        b.orders["own-15"].status = "canceled"
        b.replace_mode = "none"
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        _, st = self._run(b, st)
        self.assertEqual(len(b.replaces), 2)                                # retried: old stop live at the old qty
        self.assertEqual(b.submits, [])
        self.assertEqual(sum(int(float(o.qty)) for o in self._own_live(b, st)), 5)
        b.replace_mode = "ok"
        _, st = self._run(b, st)
        self.assertEqual([int(float(o.qty)) for o in self._own_live(b, st)], [10])

    def test_r11_1_unreplaceable_stop_is_never_cancelled_and_replaced_once_replaceable(self):
        b = RatchetBroker(held=20)
        rec = self._partial_cover_lot(b)
        b.orders["own-15"].status = "canceled"                              # the owner cancels his stop
        b.orders["own5"].status = "accepted"                                # after the close: not replaceable yet
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        r = st["positions"]["BRK.B"]
        self.assertNotIn("own5", b.cancels)                                 # never cancelled
        self.assertEqual((b.replaces, b.submits), ([], []))
        self.assertEqual([(o.id, int(float(o.qty))) for o in self._own_live(b, st)], [("own5", 5)])   # still covers 5
        self.assertEqual(r["topup_pending"]["reason"], "not_replaceable")
        waits = [p for p in self.pages if "5 sh uncovered" in p and "cannot be replaced yet" in p]
        self.assertEqual(len(waits), 1)
        _, st = self._run(b, st)                                            # same status: no repeat page
        self.assertEqual(len([p for p in self.pages if "cannot be replaced yet" in p]), 1)
        b.orders["own5"].status = "new"                                     # replaceable on the next cycle
        _, st = self._run(b, st)
        r = st["positions"]["BRK.B"]
        self.assertEqual(b.replaces[-1][1:], ("own5", 527.81))
        self.assertEqual([int(float(o.qty)) for o in self._own_live(b, st)], [10])
        self.assertNotIn("topup_pending", r)
        self.assertNotIn("own5", b.cancels)

    def test_r11_1_failing_submit_can_never_leave_zero_own_stops_on_this_path(self):
        b = RatchetBroker(held=20)
        rec = self._partial_cover_lot(b)
        b.orders["own-15"].status = "canceled"
        b.orders["own5"].status = "accepted"
        b.submit_mode = "lost"                                              # any new stop would fail
        st: dict = {"positions": {"BRK.B": rec}}
        for _cycle in range(3):
            _, st = self._run(b, st)
            self.assertGreaterEqual(len(self._own_live(b, st)), 1)          # the partial stop always remains
        self.assertEqual((b.cancels, b.submits), ([], []))

    def test_r10_2_dead_main_stop_reprotects_once_and_never_above_the_lot(self):
        b = RatchetBroker(held=20)
        rec = self._partial_cover_lot(b)                                    # owner stop 15 still live
        b.orders["own5"].status = "canceled"                                # the main own stop dies
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        self.assertEqual([x[1] for x in b.submits], [5])                    # one stop for the uncovered 5
        self.assertLessEqual(sum(int(float(o.qty)) for o in self._own_live(b, st)), 10)
        _, st = self._run(b, st)
        self.assertEqual(len(b.submits), 1)
        # a stray own stop of this lot already live is adopted, never a second one next to it
        b2 = RatchetBroker(held=20)
        rec2 = self._partial_cover_lot(b2)
        b2.orders["own5"].status = "canceled"
        b2.orders["rp-live"] = _order("rp-live", 527.81, coid="IN-rp-abc-0", qty=5)
        _, st2 = self._run(b2, {"positions": {"BRK.B": rec2}})
        self.assertEqual((b2.submits, st2["positions"]["BRK.B"]["stop_order_id"]), ([], "rp-live"))

    def test_r10_2_no_swing_stop_ever_lands_on_f6_shares(self):
        import execution.ownership_guard as og
        b = RatchetBroker(held=20)                                          # 10 Swing + 10 F6
        b.orders["stop-1"].qty = "10"
        b.orders["stop-1"].status = "canceled"                              # the lot's stop dies
        led = {"positions": {"BRK.B": {"tiers": {"intraday": {"qty": 10}, "forever6": {"qty": 10}}}}}
        with mock.patch.object(og, "load_ledger", lambda: led):
            _, st = self._run(b, {"positions": {"BRK.B": _lot(qty=10)}})
            _, st = self._run(b, st)
        self.assertEqual([x[1] for x in b.submits], [10])                   # the lot's 10 only
        self.assertLessEqual(sum(int(float(o.qty)) for o in self._own_live(b, st)), 10)

    def test_r10_3_exit_leg_capped_at_the_lot(self):
        for fill_qty in (2, 3):
            b = RatchetBroker(held=0)
            b.orders["stop-1"].qty = "1"                                    # own stop S for 1
            b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "S2"
            b.orders["S2"] = _order("S2", 527.81, coid="IN-BRK.B-s-S2", qty=fill_qty, status="filled",
                                    filled_qty=str(fill_qty), filled_avg_price="520.00", replaces="stop-1")
            self.logs.clear()
            _, st = self._run(b, {"positions": {"BRK.B": _lot()}})
            r = st["positions"]["BRK.B"]
            self.assertEqual(r["status"], "closed")
            self.assertAlmostEqual(r["realized_pnl"], -49.20, places=2)     # never −73.80
        self.assertTrue(any("exceeds the lot's 2" in p for p in self.pages))

    def test_r10_4_owner_snapshot_resets_when_the_owner_stop_set_changes(self):
        b = RatchetBroker(held=5)                                           # lot 2 + owner 3 under O (5)
        b.orders["stop-1"].status = "canceled"
        b.orders["O"] = _order("O", 500.00, coid="owner-O", qty=5)
        self.closes = [550.0, 560.0, 570.0]
        _, st = self._run(b, {"positions": {"BRK.B": _lot(stop_order_id="", unprotected=True)}})
        self.assertEqual(st["positions"]["BRK.B"]["owner_qty_at_cover"], 3)
        # the owner sells his 3, cancels O, re-buys 3 under O3
        b.orders["O"].status = "canceled"
        b.orders["O3"] = _order("O3", 500.00, coid="owner-O3", qty=3)
        b.held = 5
        _, st = self._run(b, st)
        r = st["positions"]["BRK.B"]
        self.assertNotIn("owner_qty_at_cover", r)                           # the old snapshot is gone
        self.assertEqual([x[1] for x in b.submits], [2])                    # the lot's own stop for its 2
        o3 = b.orders["O3"]
        o3.status, o3.filled_qty, o3.filled_avg_price = "filled", "3", "500.00"   # O3 fills the owner's 3
        b.held = 2
        _, st = self._run(b, st)
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["status"], r["qty"]), ("open", 2))              # the lot stays open with its exits
        self.assertEqual([ev for ev, _s, _kw in self.logs if ev in ("exit", "partial_exit")], [])

    # ── cold review round 9 ───────────────────────────────────────────────────
    def _partial_cover_lot(self, b):
        """10-share Swing lot + 10 owner shares, an owner stop for 15, the lot's own stop for the uncovered 5."""
        b.orders["stop-1"].status = "canceled"
        b.orders["own-15"] = _order("own-15", 480.00, coid="owner-gtc-15", qty=15)
        b.orders["own5"] = _order("own5", 527.81, coid="IN-BRK.B-s-own5", qty=5)
        self.closes = [550.0, 560.0, 570.0]                                 # not armed: coverage only
        return _lot(qty=10, stop_order_id="own5", owner_partial_ids=["own-15"])

    def _own_live(self, b, st):
        rec = st["positions"]["BRK.B"]
        own = sb.SwingBreakoutManager._own_stop_ids(rec)
        return [o for o in b.orders.values() if o.status in LIVE and o.client_order_id
                and (o.id in own or str(o.client_order_id).startswith(("IN-rp-", "IN-BRK.B-s-")))]

    def test_r9_1_owner_cancel_tops_the_lots_own_stop_up_to_its_qty(self):
        b = RatchetBroker(held=20)
        _, st = self._run(b, {"positions": {"BRK.B": self._partial_cover_lot(b)}})
        self.assertEqual(b.replaces, [])                                    # covered: 5 own + 15 owner ≥ 20
        b.orders["own-15"].status = "canceled"                              # the owner cancels his stop
        _, st = self._run(b, st)
        live = self._own_live(b, st)
        self.assertEqual(sum(int(float(o.qty)) for o in live), 10)          # all 10 lot shares, never more
        self.assertTrue(all(float(o.stop_price) == 527.81 for o in live))   # at the lot's stop price
        self.assertEqual(b.replaces[-1][1:], ("own5", 527.81))              # through the replace path
        self.assertFalse(st["positions"]["BRK.B"].get("owner_partial_ids"))  # R9-2: the cancelled owner id dropped
        _, st = self._run(b, st)                                            # nothing more on the next cycle
        self.assertEqual(sum(int(float(o.qty)) for o in self._own_live(b, st)), 10)
        self.assertEqual(len(b.replaces), 1)

    def test_r9_3_owner_shares_bought_after_the_cover_never_count_as_the_lots(self):
        b = RatchetBroker(held=5)                                           # lot 2 + owner 3, owner stop for 5
        b.orders["stop-1"].status = "canceled"
        b.orders["own-5"] = _order("own-5", 500.00, coid="owner-gtc-5", qty=5)
        rec = _lot(stop_order_id="", unprotected=True)
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["owner_stop_ids"], r["owner_cover_held"], r["owner_qty_at_cover"]), (["own-5"], 5, 3))
        b.held = 8                                                          # the owner buys 3 more, no stop
        _, st = self._run(b, st)
        self.assertEqual(b.submits, [])                                     # no stop on the owner's new shares
        self.assertTrue(any("above the 5 at the owner-stop cover" in p for p in self.pages))
        o = b.orders["own-5"]
        o.status, o.filled_qty, o.filled_avg_price = "filled", "5", "500.00"   # the owner stop fills 5 at $500
        b.held = 3                                                          # only the owner's new 3 remain
        _, st = self._run(b, st)
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["status"], r["exit_reason"]), ("closed", "protective_stop"))
        self.assertAlmostEqual(r["realized_pnl"], -89.20, places=2)
        self.assertEqual(b.submits, [])

    # ── cold review round 8 ───────────────────────────────────────────────────
    def test_r8_1_owner_fill_on_the_owners_shares_never_closes_the_lot(self):
        b = RatchetBroker(held=5)                                           # 2 lot shares + 3 of the owner's
        b.orders["stop-1"].status = "canceled"
        b.orders["own-3"] = _order("own-3", 500.00, coid="owner-gtc-3", qty=3)
        self.closes = [550.0, 560.0, 570.0]                                 # not armed: this test is the R8-1 path only
        rec = _lot(stop_order_id="", unprotected=True)
        st = {"positions": {"BRK.B": rec}}
        with _mods({"execution.broker": b}):
            self.assertTrue(sb.SwingBreakoutManager()._reprotect("BRK.B", rec, st, 2))
        self.assertEqual([x[1] for x in b.submits], [2])                    # the lot's own stop for its 2 shares
        self.assertNotIn("owner_stop_ids", rec)                             # R8-1a: a partial cover is not protection
        self.assertEqual(rec["owner_partial_ids"], ["own-3"])
        own_stop = rec["stop_order_id"]
        # the owner's stop fills its 3 shares at $500; the broker now holds the lot's 2; the lot's stop ends and the
        # first re-placement fails
        o = b.orders["own-3"]
        o.status, o.filled_qty, o.filled_avg_price = "filled", "3", "500.00"
        b.held = 2
        b.orders[own_stop].status = "canceled"
        b.submit_mode = "lost"
        _, st = self._run(b, st)
        b.submit_mode = "ok"
        _, st = self._run(b, st)
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["status"], r["qty"], r["unprotected"]), ("open", 2, False))
        self.assertEqual([ev for ev, _s, _kw in self.logs if ev in ("exit", "partial_exit")], [])   # no −89.20
        self.assertEqual(b.submits[-1][1], 2)                               # its own stop re-placed
        self.assertEqual(r["stop_order_id"], f"rp-{len(b.submits)}")
        self.assertEqual(r["ratchet"]["booked_fills"]["own-3"]["qty"], 3)   # seen, never booked to the lot

    def test_r8_4_owner_fill_reduces_the_lot_only_by_shares_that_left(self):
        b = RatchetBroker(held=4)                                           # 2 lot + 2 owner; owner stop for 3
        b.orders["stop-1"].status = "canceled"
        b.orders["own-3"] = _order("own-3", 500.00, coid="owner-gtc-3", qty=3)
        rec = _lot(stop_order_id="", unprotected=True)
        st = {"positions": {"BRK.B": rec}}
        with _mods({"execution.broker": b}):
            sb.SwingBreakoutManager()._reprotect("BRK.B", rec, st, 2)
        self.assertEqual([x[1] for x in b.submits], [1])                    # 1 lot share left uncovered
        o = b.orders["own-3"]
        o.status, o.filled_qty, o.filled_avg_price = "filled", "3", "500.00"
        b.held = 1                                                          # 2 owner + 1 lot share sold
        _, st = self._run(b, st)
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["status"], r["qty"]), ("open", 1))
        self.assertEqual([kw["size"] for ev, _s, kw in self.logs if ev == "partial_exit"], [1])
        self.assertAlmostEqual(r["partial_pnl"], 500.00 - 544.60, places=2)

    def test_r8_2_partial_cover_reprotection_confirms(self):
        b = RatchetBroker(held=10)
        b.orders["stop-1"].status = "canceled"
        cid = sb.reprotect_coid(COID, 0)
        b.orders["own-5"] = _order("own-5", 500.00, coid="owner-gtc-5", qty=5)
        b.orders["rp-x"] = _order("rp-x", 527.81, coid=cid, qty=5)
        rec = _lot(qty=10, stop_order_id="", unprotected=True)
        rec["ratchet"] = {"status": "reprotect_pending", "reprotect_seq": 0, "reprotect_coid": cid,
                          "reprotect_qty_cap": 5, "reprotect_stop_qty": 5, "reprotect_qty": 10, "reprotect_px": 527.81,
                          "attempt_ts": NOW.isoformat()}
        st = self._cycle(b, rec=rec)
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["ratchet"]["status"], r["stop_order_id"], r["unprotected"], r["qty"]),
                         ("idle", "rp-x", False, 10))
        self.assertFalse(sb.SwingBreakoutManager()._ratchet_owns(r, st))   # reconcile / exits not blocked
        self.assertEqual(b.submits, [])

    def test_r8_3_unresolved_chain_adopts_the_partial_cover_stop_qty(self):
        b = RatchetBroker(held=6)                                           # 4 lot + 2 owner; owner stop for 3
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "Q"   # Q unreadable
        b.orders["own-3"] = _order("own-3", 500.00, coid="owner-gtc-3", qty=3)
        b.orders["rp-3"] = _order("rp-3", 527.81, coid="IN-rp-abc-0", qty=3)   # this lot's stop for the uncovered 3
        _, st = self._run(b, {"positions": {"BRK.B": _lot(qty=4)}})
        self.assertEqual(st["positions"]["BRK.B"]["stop_order_id"], "rp-3")
        self.assertEqual(b.submits, [])

    # ── cold review round 7 ───────────────────────────────────────────────────
    def test_r7_1_owner_stop_fill_is_booked_and_counted(self):
        b = RatchetBroker(held=0)
        b.orders["stop-1"].status = "canceled"
        b.orders["own-500"] = _order("own-500", 500.00, coid="owner-gtc-1", qty=2, status="filled",
                                     filled_qty="2", filled_avg_price="500.00")
        rec = _lot(stop_order_id="", owner_stop_ids=["own-500"])
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["status"], r["exit_reason"]), ("closed", "protective_stop"))
        self.assertAlmostEqual(r["realized_pnl"], -89.20, places=2)
        self.assertNotIn("own-500", b.cancels)                              # read only, never cancelled
        arch = st["closed_lots"][0]
        self.assertAlmostEqual(sb.stop_gap_multiple(arch), 1 + 27.81 / 16.79, places=6)   # 2.656 against 527.81
        k = sb.kelly_risk([arch], None)
        self.assertEqual([d["coid"] for d in k["stop_multiples"]], [COID])
        self.assertEqual(k["stop_excluded"], [])

    def test_r7_1_unreadable_owner_fill_stays_unverified_and_pages(self):
        b = RatchetBroker(held=0)
        b.orders["stop-1"].status = "canceled"
        rec = _lot(stop_order_id="", owner_stop_ids=["own-gone"])          # the owner order cannot be read
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        r = st["positions"]["BRK.B"]
        self.assertEqual(r["status"], "open")                               # never booked as an unknown close
        self.assertNotEqual(r.get("exit_reason"), "external_close_unverified")
        self.assertTrue(any(p.startswith("CRITICAL [BRK.B]") and "unreadable" in p for p in self.pages))

    def test_r7_2_owner_stop_covering_only_the_owners_shares_leaves_the_lot_its_own_stop(self):
        b = RatchetBroker(held=20)                                          # 10 Swing + 10 of the owner's own shares
        b.orders["stop-1"].status = "canceled"
        b.orders["own-10"] = _order("own-10", 480.00, coid="owner-gtc-10", qty=10)
        rec = _lot(qty=10, stop_order_id="", unprotected=True)
        st = {"positions": {"BRK.B": rec}}
        with _mods({"execution.broker": b}):
            self.assertTrue(sb.SwingBreakoutManager()._reprotect("BRK.B", rec, st, 10))
        self.assertEqual([x[1] for x in b.submits], [10])                   # a Swing stop for the 10 shares
        self.assertEqual((rec["qty"], rec["unprotected"]), (10, False))
        self.assertNotIn("own-10", b.cancels)

    def test_r7_3_ledger_never_shrinks_protection_below_the_lot(self):
        import execution.ownership_guard as og
        b = RatchetBroker(held=10)
        b.orders["stop-1"].status = "canceled"
        rec = _lot(qty=10, stop_order_id="", unprotected=True)
        st = {"positions": {"BRK.B": rec}}
        led = {"positions": {"BRK.B": {"tiers": {"intraday": {"qty": 2}}}}}
        with _mods({"execution.broker": b}), mock.patch.object(og, "load_ledger", lambda: led):
            m = sb.SwingBreakoutManager()
            self.assertEqual(m._tier_held("BRK.B", rec, 10), 10)
            self.assertTrue(m._reprotect("BRK.B", rec, st, 10))
        self.assertEqual([x[1] for x in b.submits], [10])
        self.assertTrue(any("fewer than the lot" in p for p in self.pages))

    def test_r7_4_unprotected_partial_leg_is_a_stop_leg_and_owner_state_written_only_on_change(self):
        rec = _lot(stop_px=548.80)
        st = {"positions": {"BRK.B": rec}}
        m = sb.SwingBreakoutManager()
        m._book_partial("BRK.B", rec, st, 518.0, 1, "stop_breached_unprotected_partial")
        self.assertAlmostEqual(rec["stop_legs"]["mult_qty"], 1 + 30.80 / 16.79, places=4)
        owner = [_order("own-1", 500.0, coid="owner", qty=1)]
        saves = []
        with mock.patch.object(sb, "_save_state", lambda s_: saves.append(1) or True):
            m._owner_protects("BRK.B", rec, st, owner)
            m._owner_protects("BRK.B", rec, st, owner)
        self.assertEqual(len(saves), 1)

    # ── cold review round 6 ───────────────────────────────────────────────────
    def _unresolved(self, b):
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "Q"   # Q unreadable
        return _lot()

    def test_r6_1_owner_stop_is_never_adopted_or_cancelled(self):
        b = RatchetBroker(held=2)
        rec = self._unresolved(b)
        b.orders["own-500"] = _order("own-500", 500.00, coid="owner-gtc-1", qty=2)   # untagged owner GTC stop
        self.closes = [560.0] * 25                                          # trend break: last trade under $560
        self.bid = 540.00
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        r = st["positions"]["BRK.B"]
        self.assertNotEqual(r["stop_order_id"], "own-500")
        self.assertEqual((r["owner_stop_ids"], r["unprotected"], b.submits), (["own-500"], False, []))
        _, st = self._run(b, st)                                            # no second page, no stop over it
        self.assertEqual(len([p for p in self.pages if "owner (untagged) stop" in p]), 1)
        got, st = self._run(b, st, near=True)                               # the trend exit runs
        self.assertEqual(got, ["BRK.B"])
        self.assertNotIn("own-500", b.cancels)                              # the owner's stop survives
        self.assertEqual(b.orders["own-500"].status, "new")
        self.assertNotEqual(st["positions"]["BRK.B"].get("stop_order_id"), "own-500")
        self.assertEqual(b.submits, [])

    def test_r6_2_through_stop_close_counts_in_the_loss_multiple(self):
        prot = [{"coid": f"p{i}", "exit_reason": "protective_stop", "entry_px": 100.0, "stop_px": 90.0,
                 "init_risk_ps": 10.0, "exit_px": 87.0, "qty": 1, "realized_pnl": -13.0, "risk_usd": 10.0} for i in range(3)]
        brk = {"coid": "b", "exit_reason": "stop_breached_unprotected", "entry_px": 544.60, "stop_px": 548.80,
               "init_risk_ps": 16.79, "exit_px": 518.0, "qty": 1, "realized_pnl": 518.0 - 544.60, "risk_usd": 16.79}
        self.assertAlmostEqual(sb.stop_gap_multiple(brk), 1 + 30.80 / 16.79, places=9)   # 2.83
        base = [{"coid": f"t{i}", "exit_reason": "trend_break", "realized_pnl": 5.0, "risk_usd": 10.0} for i in range(30)]
        k = sb.kelly_risk(base + prot + [brk], None)
        self.assertAlmostEqual(k["loss_mult"], (3 * 1.30 + 1 + 30.80 / 16.79) / 4, places=9)
        self.assertAlmostEqual(k["loss_mult"], 1.68, delta=0.005)
        self.assertIn("stop_breached_unprotected_partial", sb._STOP_PARTIALS)

    def test_r6_3_unresolved_chain_never_closes_and_sizes_from_the_lot(self):
        b = RatchetBroker(held=5)                                           # broker holds more than the 2-sh lot
        rec = self._unresolved(b)
        b.submit_mode = "lost"                                              # the stop cannot be placed
        self.bid = 500.00                                                   # and the price is through the stop
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        self.assertEqual(b.closes, [])                                      # no market close from an unresolved chain
        self.assertEqual([s[1] for s in b.submits], [2])                    # only the lot's 2 shares, never 5
        self.assertTrue(any("more than the lot" in p for p in self.pages))
        self.assertEqual(st["positions"]["BRK.B"]["status"], "open")

    def test_r6_4_same_qty_in_stop_with_an_unresolved_chain_blocks_a_second_stop(self):
        b = RatchetBroker(held=2)
        rec = self._unresolved(b)
        b.orders["in-2"] = _order("in-2", 530.00, coid="IN-BRK.B-s-1762000000000-xx", qty=2)
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        self.assertEqual(b.submits, [])
        self.assertTrue(any("may be this lot's" in p for p in self.pages))
        self.assertNotEqual(st["positions"]["BRK.B"]["stop_order_id"], "in-2")

    # ── cold review round 5 ───────────────────────────────────────────────────
    def test_r5_1_moved_stop_fill_after_stale_release_is_booked_and_reprotected(self):
        # session 1: X (stop-1) replaced by Y, but Y is unreadable all session → stop_order_id stays X
        b = RatchetBroker(held=2)
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "Y"
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80,
                          "attempt_ts": "2026-11-04T11:00:00-05:00", "armed_session": "2026-10-29"}
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        self.assertEqual((st["positions"]["BRK.B"]["stop_order_id"], st["positions"]["BRK.B"]["ratchet"]["status"]),
                         ("stop-1", "pending"))
        # session 2: the lot is released; Y filled 1 of 2 at $520 and ended cancelled
        self.opens = [datetime(2026, 11, 5, 9, 30, tzinfo=sb.ET)]
        b.orders["Y"] = _order("Y", 548.80, coid="IN-BRK.B-s-ry", status="canceled", filled_qty="1",
                               filled_avg_price="520.00", replaces="stop-1")
        b.held = 1
        self.bid = 520.00
        _, st = self._run(b, st)
        _, st = self._run(b, st)                                          # a second cycle books nothing more
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["status"], r["qty"]), ("open", 1))
        self.assertAlmostEqual(r["partial_pnl"], -24.60, places=2)
        self.assertEqual([kw["size"] for ev, _s, kw in self.logs if ev == "partial_exit"], [1])   # Y booked once
        self.assertEqual(r["ratchet"]["booked_fills"]["Y"]["qty"], 1)
        self.assertEqual([s[1] for s in b.submits], [1])                  # a new stop for the share still held
        self.assertEqual(r["unprotected"], False)
        # the leg is measured against Y's own level ($548.80), not the old $527.81
        self.assertAlmostEqual(r["stop_legs"]["mult_qty"], 1 + (548.80 - 520.00) / 16.79, places=4)

    def test_r5_1_released_lot_with_shares_and_no_own_stop_is_reprotected_in_one_cycle(self):
        b = RatchetBroker(held=2)
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "Z"   # Z unreadable
        rec = _lot()
        rec["ratchet"] = {"status": "stale_released", "old_order_id": "stop-1", "released_from": "pending",
                          "attempt_ts": "2026-11-04T11:00:00-05:00"}
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        r = st["positions"]["BRK.B"]
        self.assertEqual(len(b.submits), 1)
        self.assertEqual((b.submits[0][1], r["unprotected"], r["status"]), (2, False, "open"))
        self.assertTrue(any("NO live stop" in p for p in self.pages))

    def test_r5_2_unreadable_ratchet_fill_price_marks_the_leg_unknown_and_pages(self):
        rec = _lot(stop_order_id="Y")
        st = {"positions": {"BRK.B": rec}}
        o = _order("Y", 548.80, status="canceled", filled_qty="1", filled_avg_price=None)
        with _mods({"execution.broker": RatchetBroker()}):
            sb.SwingBreakoutManager()._book_order_fills("BRK.B", rec, st, o, "2026-11-05")
        self.assertEqual((rec["qty"], rec["partial_pnl_unknown"], rec["stop_legs"]["unknown"]), (1, True, True))
        self.assertTrue(any(p.startswith("CRITICAL [BRK.B]") and COID in p for p in self.pages))

    # ── cold review round 4 ───────────────────────────────────────────────────
    def _stale(self, rec):
        self.opens = [datetime(2026, 11, 5, 9, 30, tzinfo=sb.ET)]        # today's open came after the attempt
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80,
                          "attempt_ts": "2026-11-04T11:00:00-05:00", "armed_session": "2026-10-29"}
        return rec

    def test_r4_1_stale_pending_partial_is_booked_once(self):
        # 2-share lot, entry 544.60, R 16.79, old stop X (stop-1) at 527.81, a replace pending since yesterday;
        # this morning X fills 1 share at $520 and ends cancelled
        b = RatchetBroker(held=1)
        b.orders["stop-1"].status, b.orders["stop-1"].filled_qty = "canceled", "1"
        b.orders["stop-1"].filled_avg_price = "520.00"
        self.bid = 520.00
        _, st = self._run(b, {"positions": {"BRK.B": self._stale(_lot())}})
        _, st = self._run(b, st)                                          # a second cycle books nothing more
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["status"], r["qty"]), ("open", 1))
        self.assertAlmostEqual(r["partial_pnl"], -24.60, places=2)
        self.assertEqual([kw["size"] for ev, _s, kw in self.logs if ev == "partial_exit"], [1])   # a single booking
        self.assertEqual(r["ratchet"]["status"], "stale_released")
        self.assertEqual(r["ratchet"]["booked_fills"]["stop-1"]["qty"], 1)
        self.assertEqual([s[1] for s in b.submits], [1])                  # the 1 share still held is re-protected
        self.assertEqual(len([p for p in self.pages if "RELEASED" in p]), 1)

    def test_r4_1_ten_share_stale_variant_books_four_once(self):
        b = RatchetBroker(held=6)
        b.orders["stop-1"].qty = "10"
        b.orders["stop-1"].status, b.orders["stop-1"].filled_qty = "canceled", "4"
        b.orders["stop-1"].filled_avg_price = "527.81"
        self.bid = 520.00
        _, st = self._run(b, {"positions": {"BRK.B": self._stale(_lot(qty=10))}})
        _, st = self._run(b, st)
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["status"], r["qty"]), ("open", 6))
        self.assertEqual([kw["size"] for ev, _s, kw in self.logs if ev == "partial_exit"], [4])
        self.assertAlmostEqual(r["partial_pnl"], round((527.81 - 544.60) * 4, 2), places=2)

    def test_r4_1a_every_owner_dedupes_on_booked_fills(self):
        b = RatchetBroker(held=1)
        b.orders["stop-1"].status, b.orders["stop-1"].filled_qty = "canceled", "1"
        b.orders["stop-1"].filled_avg_price = "520.00"
        rec = _lot()
        st = {"positions": {"BRK.B": rec}}
        m = sb.SwingBreakoutManager()
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            m._reconcile(st)                                              # _reconcile books X's 1 share
            self.assertFalse(m._book_order_fills("BRK.B", rec, st, b.orders["stop-1"], "2026-11-05"))
        self.assertEqual([kw["size"] for ev, _s, kw in self.logs if ev == "partial_exit"], [1])
        self.assertEqual(rec["qty"], 1)

    def test_r4_2_unknown_leg_never_replaced_by_a_guess(self):
        rec = {"coid": "u", "exit_reason": "trend_break", "entry_px": 100.0, "init_risk_ps": 10.0, "qty": 1,
               "exit_px": 110.0, "realized_pnl": 10.0, "risk_usd": 20.0,
               "stop_legs": {"qty": 1, "mult_qty": 2.0, "unknown": True}}
        self.assertEqual(sb.stop_gap_multiple(rec), 2.0)                  # the known 2.0 leg decides
        none_known = dict(rec, coid="n", stop_legs={"qty": 0, "mult_qty": 0.0, "unknown": True})
        self.assertIsNone(sb.stop_gap_multiple(none_known))
        k = sb.kelly_risk([rec, none_known], None)
        self.assertEqual(k["stop_excluded"], ["n"])
        self.assertEqual([d["coid"] for d in k["stop_multiples"]], ["u"])

    def test_stale_page_once_per_episode_then_once_a_session(self):
        b = RatchetBroker()
        b.orders["stop-1"].status = "pending_replace"
        self.bid = 520.00
        st = self._cycle(b, rec=self._stale(_lot()))
        st = self._cycle(b, state=st)
        self.assertEqual(len([p for p in self.pages if "RELEASED" in p]), 1)
        self.assertEqual([p for p in self.pages if "still released" in p], [])     # no reminder the same session
        st = self._cycle(b, state=st, now=datetime(2026, 11, 6, 11, 0, tzinfo=sb.ET))
        st = self._cycle(b, state=st, now=datetime(2026, 11, 6, 12, 0, tzinfo=sb.ET))
        self.assertEqual(len([p for p in self.pages if "still released" in p]), 1)  # one reminder the next session
        self.assertTrue(all("BRK.B" in p and COID in p for p in self.pages if "released" in p.lower()))

    # ── cold review round 3 ───────────────────────────────────────────────────
    def _run(self, broker, st, near=False):
        sb._save_state(_this_session(st))
        data = SimpleNamespace(get_latest_quote=lambda s: {"bid": self.bid, "ask": self.bid + 0.10, "ts": self.quote_ts},
                               get_latest_trade_with_time=lambda s: (self.bid, datetime.now(timezone.utc)))
        m = sb.SwingBreakoutManager()
        with _mods({"execution.broker": broker, "data.alpaca_data": data}), \
                mock.patch.object(sb.SwingBreakoutManager, "_bars", staticmethod(lambda s: _daily(self.closes))), \
                mock.patch.object(sb.SwingBreakoutManager, "_near_close", staticmethod(lambda now: near)), \
                mock.patch("time.sleep"):
            got = m.run_exit_check(NOW)
        return got, sb._load_state()

    def test_r3_1_partial_gap_leg_counts_in_the_loss_multiple(self):
        rec = _lot(stop_px=548.80)
        st = {"positions": {"BRK.B": rec}}
        m = sb.SwingBreakoutManager()
        m._book_partial("BRK.B", rec, st, 520.0, 1, "ratchet_stop_partial")      # 1 sh gapped through the stop
        m._book_exit("BRK.B", rec, st, "ratchet_stop", 548.80, 1)                 # the re-protected share at its stop
        arch = st["closed_lots"][0]
        self.assertEqual(arch["stop_legs"]["qty"], 1)
        want = ((1 + 28.80 / 16.79) + 1.0) / 2
        self.assertAlmostEqual(sb.stop_gap_multiple(arch), want, places=6)
        self.assertAlmostEqual(want, 1.857, delta=0.001)                         # (2.715 + 1.0) / 2
        k = sb.kelly_risk([arch], None)
        self.assertEqual(k["stop_multiples"][0]["mult"], round(want, 4))
        # a protective partial followed by a trend exit still counts its stop leg
        rec2 = _lot(coid="c2")
        st2 = {"positions": {"X": rec2}}
        m._book_partial("X", rec2, st2, 520.0, 1, "protective_stop_partial")
        m._book_exit("X", rec2, st2, "trend_break", 560.0, 1)
        self.assertAlmostEqual(sb.stop_gap_multiple(st2["closed_lots"][0]), 1 + (527.81 - 520.0) / 16.79, places=6)

    def test_r3_2_partial_from_the_lots_own_stop_is_booked_once(self):
        b = RatchetBroker(held=0)
        b.orders["stop-1"].qty = "10"
        b.orders["stop-1"].status, b.orders["stop-1"].filled_qty = "canceled", "4"
        b.orders["stop-1"].filled_avg_price = "527.81"
        rec = _lot(qty=10)
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80,
                          "attempt_ts": NOW.isoformat()}
        _, st = self._run(b, {"positions": {"BRK.B": rec}})
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["qty"], r["stop_order_id"]), (6, ""))
        _, st = self._run(b, st)                                          # next cycle: reconcile finds it gone
        _, st = self._run(b, st)
        r = st["positions"]["BRK.B"]
        legs = [kw["size"] for ev, _s, kw in self.logs if ev == "partial_exit"]
        self.assertEqual(legs, [4])                                       # booked once — never 8
        self.assertAlmostEqual(r["partial_pnl"], round((527.81 - 544.60) * 4, 2), places=2)
        self.assertEqual((r["status"], r["exit_reason"]), ("closed", "external_close_unverified"))

    def test_r3_2_reconcile_books_only_the_unbooked_part_at_its_incremental_price(self):
        b = RatchetBroker()
        b.orders["X"] = _order("X", 548.80, coid="IN-BRK.B-s-r1", qty=10, status="filled", filled_qty="10",
                               filled_avg_price="545.00")
        rec = _lot(qty=6, stop_px=548.80, stop_order_id="X", partial_pnl=16.80)
        rec["ratchet"] = {"status": "done", "order_ids": ["X"], "booked_fills": {"X": {"qty": 4, "avg": 548.80}}}
        st = {"positions": {"BRK.B": rec}}
        with _mods({"execution.broker": b}), mock.patch("time.sleep"):
            sb.SwingBreakoutManager()._reconcile(st)
        r = st["positions"]["BRK.B"]
        self.assertEqual(r["status"], "closed")
        self.assertAlmostEqual(r["exit_px"], (10 * 545.00 - 4 * 548.80) / 6, places=6)
        self.assertAlmostEqual(r["realized_pnl"], 10 * 545.00 - 10 * 544.60, places=2)   # +$4.00 in total
        ex = next(kw for ev, _s, kw in self.logs if ev == "exit")
        self.assertEqual(ex["size"], 6)

    def test_r3_3_stale_pending_does_not_block_the_trend_exit(self):
        b = RatchetBroker()
        b.orders["stop-1"].status = "pending_replace"                     # stuck since yesterday
        self.opens = [datetime(2026, 11, 5, 9, 30, tzinfo=sb.ET)]        # today's session opened after the attempt
        self.closes = [560.0] * 25                                        # prior 20-session low $560
        self.bid = 540.00                                                 # last trade below it: trend break
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80,
                          "attempt_ts": "2026-11-04T11:00:00-05:00"}
        got, st = self._run(b, {"positions": {"BRK.B": rec}}, near=True)
        self.assertEqual(got, ["BRK.B"])
        self.assertEqual((st["positions"]["BRK.B"]["status"], st["positions"]["BRK.B"]["exit_reason"]),
                         ("closed", "trend_break"))
        self.assertTrue(any("before the current session" in p for p in self.pages))

    def test_r3_3_same_session_pending_still_defers_and_stale_pages_once_per_episode(self):
        b = RatchetBroker()
        b.orders["stop-1"].status = "pending_replace"
        self.closes = [560.0] * 25
        self.bid = 540.00
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80,
                          "attempt_ts": NOW.isoformat()}
        got, st = self._run(b, {"positions": {"BRK.B": rec}}, near=True)
        self.assertEqual((got, st["positions"]["BRK.B"]["status"]), ([], "open"))           # this session: deferred
        st["positions"]["BRK.B"]["ratchet"]["attempt_ts"] = "2026-11-04T11:00:00-05:00"
        self.opens = [datetime(2026, 11, 5, 9, 30, tzinfo=sb.ET)]
        n0 = len([p for p in self.pages if "before the current session" in p])
        self._run(b, st)
        _, st = self._run(b, sb._load_state())
        self.assertEqual(len([p for p in self.pages if "before the current session" in p]) - n0, 1)   # once
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["status"], "stale_released")

    def test_r3_3_session_age_from_the_calendar_not_minutes(self):
        m = sb.SwingBreakoutManager()
        rt = {"attempt_ts": "2026-11-05T09:31:00-05:00"}
        self.opens = [datetime(2026, 11, 5, 9, 30, tzinfo=sb.ET)]
        self.assertFalse(m._pending_stale(rt, datetime(2026, 11, 5, 15, 55, tzinfo=sb.ET)))     # same session, 6h
        rt = {"attempt_ts": "2026-11-04T15:55:00-05:00"}
        self.assertTrue(m._pending_stale(rt, datetime(2026, 11, 5, 9, 35, tzinfo=sb.ET)))       # next session, 16h
        self.opens = []                                                   # e.g. a weekend: no open in between
        self.assertFalse(m._pending_stale(rt, datetime(2026, 11, 5, 9, 35, tzinfo=sb.ET)))
        self.assertTrue(m._pending_stale({}, NOW))                        # unknown age: never left unmanaged

    def test_r3_3_co_held_core_stop_neither_blocks_nor_confirms(self):
        b = RatchetBroker()
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "stop-r1"
        b.orders["stop-r1"] = _order("stop-r1", 548.80, coid="IN-BRK.B-s-r1", replaces="stop-1")
        b.orders["core-1"] = _order("core-1", 530.00, coid="IN-BRK.B-s-1762000000000-core", qty=3)   # core MTF
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80,
                          "attempt_ts": NOW.isoformat()}
        st = self._cycle(b, rec=rec)
        r = st["positions"]["BRK.B"]
        self.assertEqual((r["ratchet"]["status"], r["stop_order_id"]), ("done", "stop-r1"))
        with _mods({"execution.broker": b}):
            self.assertIsNone(sb.SwingBreakoutManager()._successor("BRK.B", "core-1", {}))
            self.assertFalse(sb.SwingBreakoutManager()._only_live_stop("BRK.B", r, "core-1"))

    def test_chain_walk_capped_at_the_resolve_hop_limit(self):
        b = RatchetBroker()
        for i in range(8):
            b.orders[f"c{i}"] = _order(f"c{i}", 500.0, status="replaced", replaced_by=f"c{i + 1}")
        b.orders["c8"] = _order("c8", 500.0)
        with _mods({"execution.broker": b}):
            self.assertEqual(sb.SwingBreakoutManager()._chain_ids("c0"), ["c1", "c2", "c3", "c4", "c5"])

    def test_incremental_fill_price(self):
        rec = {"ratchet": {"booked_fills": {"X": {"qty": 4, "avg": 548.80}}}}
        q, px = sb._net_fill(rec, "X", 10, 545.00)
        self.assertEqual(q, 6)
        self.assertAlmostEqual(px, (5450.0 - 2195.2) / 6, places=9)
        self.assertEqual(sb._net_fill(rec, "X", 4, 548.80), (0, 0.0))
        self.assertEqual(sb._net_fill(rec, "Y", 3, 500.0), (3, 500.0))

    # ── cold review round 1 ───────────────────────────────────────────────────
    def test_l1_trigger_rounded_to_the_cent_arms_on_the_boundary(self):
        df = _daily([150.0, 170.0, 182.28])
        a = sb.ratchet_armed(df, "2026-10-27", 142.58, 142.58 - 122.73, 2.0, 0.01)
        self.assertEqual(a["trigger_px"], 182.28)
        self.assertTrue(a["armed"])

    def test_l1_boundary_lot_arms_in_a_full_cycle(self):
        self.closes = [150.0, 170.0, 182.28]
        self.bid = 183.00
        b = RatchetBroker(stop_px=122.73)
        self._cycle(b, rec=_lot(entry_px=142.58, stop_px=122.73, init_stop_px=122.73,
                                init_risk_ps=round(142.58 - 122.73, 6)))
        self.assertEqual(len(b.replaces), 1)
        self.assertEqual(b.replaces[0][2], sb.tick_ceil(142.58 + 0.25 * (142.58 - 122.73), 0.01))   # 147.55

    def test_empty_entry_date_never_arms(self):
        b = RatchetBroker()
        st = self._cycle(b, rec=_lot(entry_date=""))
        self.assertEqual(b.replaces, [])
        self.assertFalse(st["positions"]["BRK.B"]["ratchet"].get("armed_session"))
        self.assertFalse(sb.ratchet_armed(_daily([600.0]), "", 544.60, 16.79, 2.0, 0.01)["armed"])

    def test_m2_lot_without_entry_risk_never_arms(self):
        b = RatchetBroker(stop_px=548.80)
        rec = _lot(stop_px=548.80)
        for k in ("init_risk_ps", "init_stop_px"):
            rec.pop(k)
        st = self._cycle(b, rec=rec)                                    # no risk_usd, no entry event
        self.assertEqual((b.replaces, b.submits), ([], []))
        self.assertNotIn("init_risk_ps", st["positions"]["BRK.B"])

    def test_m2_risk_usd_rebuilds_the_entry_risk(self):
        b = RatchetBroker()
        rec = _lot(risk_usd=33.58)
        for k in ("init_risk_ps", "init_stop_px"):
            rec.pop(k)
        st = self._cycle(b, rec=rec)
        self.assertAlmostEqual(st["positions"]["BRK.B"]["init_risk_ps"], 16.79, places=6)
        self.assertEqual(b.replaces[0][2], 548.80)

    def test_l2_partial_fill_on_an_ended_order_is_booked_before_reprotecting(self):
        b = RatchetBroker(held=6)
        b.orders["stop-1"].qty = "10"
        rec = _lot(qty=10)
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "stop-r1"
        b.orders["stop-r1"] = _order("stop-r1", 548.80, coid="IN-BRK.B-s-r1", qty=10, status="canceled",
                                     filled_qty="4", filled_avg_price="548.80", replaces="stop-1")
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80,
                          "armed_session": "2026-10-29"}
        st = self._cycle(b, rec=rec)
        rec = st["positions"]["BRK.B"]
        self.assertEqual(rec["qty"], 6)
        self.assertAlmostEqual(rec["partial_pnl"], round((548.80 - 544.60) * 4, 2), places=2)
        self.assertTrue(any(ev == "partial_exit" and kw.get("exit_reason") == "ratchet_stop_partial"
                            for ev, _s, kw in self.logs))
        self.assertEqual(b.submits[-1][1], 6)                           # only the remaining 6 re-protected
        self._cycle(b, state=st)                                        # a later cycle never books the 4 again
        self.assertEqual(st["positions"]["BRK.B"]["qty"], 6)

    def test_l2_unreadable_partial_price_marks_pnl_unknown_and_pages(self):
        b = RatchetBroker(held=6)
        b.orders["stop-1"].qty = "10"
        b.orders["stop-1"].status, b.orders["stop-1"].filled_qty = "canceled", "4"
        rec = _lot(qty=10)
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80}
        st = self._cycle(b, rec=rec)
        rec = st["positions"]["BRK.B"]
        self.assertEqual((rec["qty"], rec.get("partial_pnl_unknown")), (6, True))
        self.assertTrue(any("unreadable price" in p for p in self.pages))

    def test_l3_label_follows_the_chain_from_the_old_order(self):
        b = RatchetBroker()
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "A"
        b.orders["A"] = _order("A", 548.80, status="replaced", replaced_by="B")
        b.orders["B"] = _order("B", 549.00)
        rec = {"ratchet": {"old_order_id": "stop-1", "new_order_id": "A", "order_ids": ["A"]}}
        with _mods({"execution.broker": b}):
            m = sb.SwingBreakoutManager()
            self.assertEqual(m._exit_label(rec, "B"), "ratchet_stop")
            self.assertEqual(m._exit_label(rec, "A", partial=True), "ratchet_stop_partial")
            self.assertEqual(m._exit_label(rec, "stop-1"), "protective_stop")      # the old stop itself
            self.assertEqual(m._exit_label(rec, "unrelated"), "protective_stop")

    def test_l3_successor_found_by_saved_id_in_any_status(self):
        b = RatchetBroker()
        b.orders["stop-r5"] = _order("stop-r5", 548.80, status="filled", filled_qty="2", filled_avg_price="548.80",
                                     replaces="stop-1", coid="IN-BRK.B-s-r5")
        with _mods({"execution.broker": b}):
            o = sb.SwingBreakoutManager()._successor("BRK.B", "stop-1", {"new_order_id": "stop-r5"})
        self.assertEqual(o.id, "stop-r5")

    def test_l4_owner_stop_during_reprotect_pending_is_adopted_without_a_resubmit_loop(self):
        b = RatchetBroker()
        b.orders["stop-1"].status = "canceled"
        b.orders["own-9"] = _order("own-9", 535.00, coid="manual-owner")
        rec = _lot(stop_order_id="", unprotected=True)
        rec["ratchet"] = {"status": "reprotect_pending", "reprotect_seq": 0, "reprotect_coid": sb.reprotect_coid(COID, 0),
                          "old_stop_px": 527.81, "armed_session": "2026-10-29"}
        st = self._cycle(b, rec=rec)
        rec = st["positions"]["BRK.B"]
        self.assertEqual((rec["ratchet"]["status"], rec["stop_order_id"], rec["unprotected"]), ("idle", "", False))
        self.assertEqual((rec["ratchet"]["foreign_stop_id"], rec["owner_stop_ids"]), ("own-9", ["own-9"]))   # R6-1
        self.assertNotIn("own-9", rec["ratchet"].get("order_ids") or [])        # never the ratchet's
        for _ in range(3):
            self._cycle(b, state=st)
        self.assertEqual((b.submits, b.replaces), ([], []))
        self.assertEqual(b.orders["own-9"].stop_price, "535.0")

    def test_l4_protection_already_held_rechecks_and_adopts(self):
        b = RatchetBroker()
        b.replace_mode = "rejected_new"
        b.submit_mode = "held"
        st = self._cycle(b)
        self.assertEqual(len(b.submits), 1)
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["status"], "reprotect_pending")
        b.orders["IN-held"] = _order("IN-held", 527.81, coid="owner-manual")       # the (owner) stop that held the shares
        self._cycle(b, state=st)
        rec = st["positions"]["BRK.B"]
        self.assertEqual((rec["ratchet"]["status"], rec["stop_order_id"], rec["owner_stop_ids"]),
                         ("idle", "", ["IN-held"]))                       # the owner's stop: protection, never adopted
        self.assertEqual(len(b.submits), 1)                              # adopted, not resubmitted

    def test_l4_pending_cancel_stays_pending_without_an_order(self):
        b = RatchetBroker()
        b.orders["stop-1"].status = "canceled"
        b.orders["pc"] = _order("pc", 527.81, status="pending_cancel", coid=sb.reprotect_coid(COID, 0))
        rec = _lot(stop_order_id="", unprotected=True)
        rec["ratchet"] = {"status": "reprotect_pending", "reprotect_seq": 0, "reprotect_coid": sb.reprotect_coid(COID, 0),
                          "old_stop_px": 527.81}
        st = self._cycle(b, rec=rec)
        self.assertEqual(b.submits, [])
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["status"], "reprotect_pending")

    def test_untagged_order_at_the_end_of_the_chain_is_not_adopted(self):
        b = RatchetBroker()
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "own-2"
        b.orders["own-2"] = _order("own-2", 540.00, coid="owner-ui")
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80}
        st = self._cycle(b, rec=rec)
        rec = st["positions"]["BRK.B"]
        self.assertEqual(rec["stop_px"], 527.81)                         # not adopted as the ratchet's level
        self.assertNotIn("own-2", rec["ratchet"].get("order_ids") or [])
        self.assertEqual(rec["ratchet"]["foreign_stop_id"], "own-2")
        self.assertEqual((b.submits, b.replaces), ([], []))

    def test_done_requires_exactly_one_live_stop(self):
        b = RatchetBroker()
        b.replace_mode = "lost_reply"
        b.orders["stray"] = _order("stray", 500.00, coid="owner-stray")   # a second (owner) live sell stop
        rec = _lot()
        rec["ratchet"] = {"status": "pending", "old_order_id": "stop-1", "old_stop_px": 527.81, "lock_px": 548.80}
        b.orders["stop-1"].status, b.orders["stop-1"].replaced_by = "replaced", "stop-r1"
        b.orders["stop-r1"] = _order("stop-r1", 548.80, coid="IN-BRK.B-s-r1", replaces="stop-1")
        st = self._cycle(b, rec=rec)
        self.assertEqual(st["positions"]["BRK.B"]["ratchet"]["status"], "pending")
        self.assertEqual(st["positions"]["BRK.B"]["stop_order_id"], "stop-1")

    def test_failed_rebuild_is_tried_once_per_session(self):
        b = RatchetBroker()
        rec = _lot()
        rec.pop("init_risk_ps")
        rec.pop("init_stop_px")
        st = self._cycle(b, rec=rec)
        with mock.patch.object(sb.SwingBreakoutManager, "_entry_event", side_effect=AssertionError("re-read")):
            self._cycle(b, state=st)                                     # same session: the event log is not re-read
        self.assertEqual(b.replaces, [])

    def test_reprotect_at_the_ratchet_level_keeps_provenance(self):
        b = RatchetBroker()
        rec = _lot(stop_px=548.80, stop_order_id="", unprotected=True)
        rec["ratchet"] = {"status": "done", "order_ids": ["stop-r1"], "ratchet_stop_px": 548.80}
        st = {"positions": {"BRK.B": rec}}
        sb._save_state(st)
        with _mods({"execution.broker": b}):
            self.assertTrue(sb.SwingBreakoutManager()._reprotect("BRK.B", rec, st, 2))
        self.assertEqual(b.submits[0][3], 548.80)
        self.assertIn(rec["stop_order_id"], rec["ratchet"]["order_ids"])
        self.assertEqual(sb._stop_exit_label(rec, rec["stop_order_id"]), "ratchet_stop")

    def test_estimated_entry_no_ratchet(self):
        b = RatchetBroker()
        self._cycle(b, rec=_lot(entry_px_estimated=True))
        self.assertEqual(b.replaces, [])

    def test_missing_params_file_no_ratchet_then_last_valid_is_used(self):
        b = RatchetBroker()
        self.params.unlink()
        self._cycle(b)
        self.assertEqual(b.replaces, [])
        self.assertEqual(len(self.pages), 1)
        self.params.write_text(json.dumps(PARAMS))
        self.assertEqual(sb.load_ratchet_params()[1], "file")             # writes the last-valid copy
        self.params.write_text("{not json")
        p, src = sb.load_ratchet_params()
        self.assertEqual((p["trigger_r"], src.startswith("last_valid")), (2.0, True))

    def test_open_orders_unreadable_no_change(self):
        b = RatchetBroker()
        b.open_orders_ok = False
        self._cycle(b)
        self.assertEqual((b.replaces, b.submits), ([], []))

    def test_two_tagged_stops_no_replace(self):
        b = RatchetBroker()
        b.orders["stop-2"] = _order("stop-2", 527.81)
        rec = _lot(stop_order_id="")
        self._cycle(b, rec=rec)
        self.assertEqual(b.replaces, [])
        self.assertEqual(len(self.pages), 1)

    def test_empty_stop_id_resolved_by_tag_then_replaced(self):
        b = RatchetBroker()
        st = self._cycle(b, rec=_lot(stop_order_id=""))
        self.assertEqual(b.replaces, [("BRK.B", "stop-1", 548.80)])
        self.assertEqual(st["positions"]["BRK.B"]["stop_order_id"], "stop-r1")


class CalendarCache(unittest.TestCase):
    def test_failed_calendar_read_is_cached_not_retried_per_lot(self):
        calls = []

        class TN:
            def calendar(self, start, end):
                calls.append((start, end))
                raise RuntimeError("calendar down")

        sb._SESSION_CACHE.clear()
        try:
            with _mods({"execution.swing_overnight": SimpleNamespace(TNBroker=TN)}):
                m = sb.SwingBreakoutManager()
                rt = {"attempt_ts": "2026-11-04T11:00:00-05:00"}
                for _ in range(3):
                    self.assertTrue(m._pending_stale(rt, NOW))            # ET-date fallback still decides
                self.assertIsNone(sb.SwingBreakoutManager._session_opens(datetime(2026, 11, 4).date(), NOW.date()))
            self.assertEqual(len(calls), 1)
        finally:
            sb._SESSION_CACHE.clear()


class Dashboard(unittest.TestCase):
    def test_labels_and_basis(self):
        try:
            import generate_dashboard as gd
        except Exception as e:                                          # pragma: no cover
            self.skipTest(f"dashboard module not importable here: {e}")
        self.assertEqual(gd._EXIT_REASON_LABELS["ratchet_stop"], "profit-lock stop")
        txt = gd._ratchet_text({"entry_px": 544.60, "stop_px": 548.80,
                                "ratchet": {"order_ids": ["x"], "armed_session": "2026-11-04"}})
        self.assertIn("$548.80", txt)
        self.assertIn("above entry", txt)


if __name__ == "__main__":
    unittest.main()
