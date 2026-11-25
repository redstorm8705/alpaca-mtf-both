#!/usr/bin/env python3
# ruff: noqa: E501
"""KILL-BACKUP-UNTAGGED-1118 (owner-approved; logs/design_records/kill_backup_untagged_2026-11-18.md, rev 10).

The kill-switch backup never counts an owner's Alpaca-app order in a QHM/F6 name as Day/Swing P&L. An untagged fill
in a symbol that held QHM/F6 today is decided by Alpaca's order `source`. Every broker call is mocked (the order read
is `_fetch_symbol_orders`, or `requests.get` under it); the read history lives in a temporary directory.
Rev 10: every kill check records a ledger file read; the carried evidence X is the earliest clean read of today stamped
before PT midnight and before today's first fill.
"""
import fcntl
import json
import multiprocessing
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

from execution import risk_manager as rm

_PT = rm._PT
BASE = datetime.now(_PT).replace(hour=6, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
TODAY = datetime.now(_PT).strftime("%Y-%m-%d")
YDAY_STAMP = (BASE - timedelta(hours=17)).isoformat()   # 13:00 PT yesterday: the prior session's last fill


def entry(read_min, stamp_raw, held=(), unkeyed=False, credited=(), active=None, protected=None):
    """One stored ledger file read (schema 2)."""
    prot = dict(protected) if protected is not None else {s: 5.0 for s in held}
    return {"read_utc": T(read_min).isoformat(), "stamp_raw": stamp_raw, "unkeyed": unkeyed,
            "credited": sorted(credited), "held": sorted(set(held) | set(prot)), "active": dict(active or {}),
            "protected": prot}


def x_read(held):
    """A clean read of the prior session's end state (stamped yesterday), taken at 00:30 PT today: the evidence X."""
    return entry(-330, YDAY_STAMP, held)


def T(minutes):
    """A time today (PT) `minutes` after 06:00 PT, as an aware UTC datetime."""
    return BASE + timedelta(minutes=minutes)


def S(minutes):
    return T(minutes).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _pos(sym, qty, cur, prior):
    return {"symbol": sym, "qty": str(qty), "current_price": str(cur), "lastday_price": str(prior)}


def owner(oid, sym, side, source_key=True):
    row = {"id": oid, "symbol": sym, "side": side, "client_order_id": "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
           "legs": None}
    if source_key:
        row["source"] = None
    return row


def bracket(parent_id, parent_coid, sym, parent_side, legs):
    return {"id": parent_id, "symbol": sym, "side": parent_side, "source": "access_key",
            "client_order_id": parent_coid,
            "legs": [{"id": lid, "symbol": sym, "side": lside, "source": "access_key",
                      "client_order_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"} for lid, lside in legs]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.reads_file = self.tmp / "kill_backup_reads.json"
        p = mock.patch.object(rm, "_KILL_BACKUP_READS_FILE", self.reads_file)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        rm._UNTAGGED_ORDER_CACHE.update(pt_date="", facts={})
        rm._FILL_SEQ_LOGGED.update(pt_date="", oids=set())
        rm._PRIOR_CLOSE_CACHE.clear()
        rm._PRIOR_CLOSE_MISS.clear()
        self.orders = {}
        self.fetch = mock.patch.object(rm, "_fetch_symbol_orders", side_effect=lambda s: self.orders.get(s))
        self.fetch_mock = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        # the prior-session fills read behind X (default: no fill after X's stamp; a test sets False / None)
        rm._SESSION_END_CACHE.update(pt_date="", results={})
        self.session_end = True
        se = mock.patch.object(rm, "_no_fill_after_stamp", side_effect=lambda stamp, day: self.session_end)
        self.session_end_mock = se.start()
        self.addCleanup(se.stop)

    def history(self, reads, carried=(), prev_last=None, lost=False, extra=()):
        """reads = [(minutes, {active}, {protected}, [credited])] stamped at that minute today; carried = the held set
        of a clean prior-session read X taken first (None: no X); extra = raw entries appended after X."""
        rows = [] if carried is None else [x_read(carried)]
        rows += list(extra)
        rows += [entry(m, T(m).isoformat(), credited=c, active=a, protected=p) for m, a, p, c in reads]
        data = {"schema": rm._KILL_BACKUP_READS_SCHEMA, "pt_date": TODAY, "evidence_lost": lost,
                "prev_last": prev_last, "reads": rows}
        self.reads_file.write_text(json.dumps(data))

    def run_b(self, fills, positions, claims, ckpt_min, prior=None, credited=frozenset()):
        """fills = [(sym, side, qty, px, tier, minutes, oid)]."""
        rm._LAST_GOOD_CHECKPOINT["credited"] = credited
        f = tuple((s, sd, q, px, t) for s, sd, q, px, t, _m, _o in fills)
        ts = tuple(m if isinstance(m, str) else S(m) for *_r, m, _o in fills)
        oids = tuple(o for *_r, o in fills)
        with mock.patch.object(rm, "_ledger_checkpoint", return_value=(claims, T(ckpt_min), True)), \
                mock.patch.object(rm, "_prior_session_close", return_value=prior):
            return rm._backup_trading_pnl(f, ts, positions, oids)

    def assertNoMeasure(self, result, key):
        value, terms = result
        self.assertIsNone(value)
        self.assertTrue(any(t.get(key) is True for t in terms), terms)


class Case1112(Base):
    def test_1112_nvda_app_buy_first_fill_is_no_measure_never_minus_10_42(self):
        """11-12: the owner's app buy of 2 NVDA @ 159.30 (order 1bd07fea, source null) was the day's first fill; the
        rebuild that set the checkpoint had booked the shares to QHM (p=5, a=0). No valid read before it -> no measure."""
        self.orders["NVDA"] = [owner("1bd07fea", "NVDA", "buy")]
        self.history([(30, {}, {"NVDA": 5.0}, [])], carried=["NVDA"])   # the checkpoint read itself, after the buy
        fills = [("NVDA", "buy", 2, 159.30, None, 20, "1bd07fea")]
        r = self.run_b(fills, [_pos("NVDA", 5, 190.0, 173.73)], {"NVDA": (0.0, 5.0)}, 30)
        self.assertNoMeasure(r, "no_read_before_fill")
        self.assertNotEqual(r[0], -10.42)

    def test_same_buy_with_missing_read_history_is_no_measure(self):
        self.orders["NVDA"] = [owner("1bd07fea", "NVDA", "buy")]
        fills = [("NVDA", "buy", 2, 159.30, None, 20, "1bd07fea")]
        with self.assertLogs(rm.logger, level="WARNING") as logs:
            r = self.run_b(fills, [_pos("NVDA", 5, 190.0, 173.73)], {"NVDA": (0.0, 5.0)}, 30)
        self.assertNoMeasure(r, "no_read_before_fill")
        self.assertTrue(any("missing scope evidence" in m for m in logs.output))


class OwnerOrders(Base):
    def test_owner_app_sale_with_no_day_swing_shares_left_out_no_plus_12_54(self):
        """Masked-loss seat: owner sells 2 QHM NVDA at prior close + 6.27 (+12.54 if counted) after the checkpoint;
        a Day AMD lot is down $10. The sale is left out and booked to QHM: B = -10, never +2.54."""
        self.orders["NVDA"] = [owner("o-sale", "NVDA", "sell")]
        self.history([(30, {"AMD": 2.0}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        fills = [("NVDA", "sell", 2, 106.27, None, 40, "o-sale")]
        positions = [_pos("NVDA", 3, 100.0, 100.0), _pos("AMD", 2, 240.0, 245.0)]
        with self.assertLogs(rm.logger, level="INFO") as logs:
            value, terms = self.run_b(fills, positions, {"NVDA": (0.0, 5.0), "AMD": (2.0, 0.0)}, 30)
        self.assertEqual(value, -10.0)
        nvda = [t for t in terms if t.get("symbol") == "NVDA" and "a" in t][0]
        self.assertEqual((nvda["a"], nvda["p"], nvda["u"]), (0.0, 3.0, 0.0))
        self.assertFalse(any(t.get("symbol") == "NVDA" and "fills" in t for t in terms))
        line = [m for m in logs.output if "[change=kill-backup-untagged-1118]" in m and "o-sale" in m][0]
        for part in ("symbol=NVDA", "order_id=o-sale", "side=sell", "source=null", "rule=owner app order"):
            self.assertIn(part, line)

    def test_owner_app_sale_while_day_swing_holds_the_symbol_after_checkpoint(self):
        """GAI round 3: an owner sale in a symbol Day/Swing holds cannot be attributed."""
        self.orders["NVDA"] = [owner("o-sale", "NVDA", "sell")]
        self.history([(30, {"NVDA": 2.0}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        fills = [("NVDA", "sell", 2, 106.27, None, 40, "o-sale")]
        r = self.run_b(fills, [_pos("NVDA", 5, 100.0, 100.0)], {"NVDA": (2.0, 5.0)}, 30)
        self.assertNoMeasure(r, "owner_order_with_day_swing_position")

    def test_shared_nvda_swing_and_qhm_pre_checkpoint_sale_is_no_measure(self):
        """Board rev-4 input: Swing 3 + QHM 5; owner sells 5 in the app before the checkpoint; the read before the
        sale shows Swing 3 -> no measure (a real -$30 Swing loss could hide behind it)."""
        self.orders["NVDA"] = [owner("o-sale", "NVDA", "sell")]
        self.history([(10, {"NVDA": 3.0}, {"NVDA": 5.0}, []), (50, {"NVDA": 3.0}, {}, [])], carried=["NVDA"])
        fills = [("AMD", "buy", 1, 100.0, "intraday", 10, "d1"), ("NVDA", "sell", 5, 110.0, None, 30, "o-sale")]
        r = self.run_b(fills, [_pos("NVDA", 3, 100.0, 110.0), _pos("AMD", 1, 100.0, 100.0)],
                       {"NVDA": (3.0, 0.0), "AMD": (1.0, 0.0)}, 50)
        self.assertNoMeasure(r, "owner_order_with_day_swing_position")

    def test_owner_buy_booked_intraday_before_holds_overlay_is_no_measure(self):
        """Board rev-5 input: the rebuild booked the owner's 2-share app buy to Day/Swing (checkpoint a=2) while the
        read before it shows Day/Swing 0 -> consistency check fails -> no measure."""
        self.orders["NVDA"] = [owner("o-buy", "NVDA", "buy")]
        self.history([(10, {"AMD": 1.0}, {"NVDA": 5.0}, []), (50, {"NVDA": 2.0, "AMD": 1.0}, {"NVDA": 5.0}, [])],
                     carried=["NVDA"])
        fills = [("AMD", "buy", 1, 100.0, "intraday", 10, "d1"), ("NVDA", "buy", 2, 105.0, None, 30, "o-buy")]
        positions = [_pos("NVDA", 7, 110.0, 100.0), _pos("AMD", 1, 100.0, 100.0)]
        r = self.run_b(fills, positions, {"NVDA": (2.0, 5.0), "AMD": (1.0, 0.0)}, 50)
        self.assertNoMeasure(r, "checkpoint_day_swing_mismatch")
        # the same buy once the holds overlay booked it to QHM: left out, NVDA's QHM shares never enter
        value, _terms = self.run_b(fills, positions, {"NVDA": (0.0, 7.0), "AMD": (1.0, 0.0)}, 50)
        self.assertEqual(value, 0.0)

    def test_whole_hold_owner_sale_before_checkpoint_left_out_reading_unchanged(self):
        """Board rev-6 input: owner sells all 5 QHM NVDA before the checkpoint (claim now 0); the read before shows
        QHM 5 -> in scope, left out; the reading equals the Day/Swing result without it (never +25)."""
        self.orders["NVDA"] = [owner("o-sale", "NVDA", "sell")]
        self.history([(10, {"AMD": 2.0}, {"NVDA": 5.0}, []), (50, {"AMD": 2.0}, {}, [])], carried=[])
        fills = [("AMD", "buy", 2, 245.0, "intraday", 10, "d1"), ("NVDA", "sell", 5, 105.0, None, 30, "o-sale")]
        positions = [_pos("AMD", 2, 240.0, 245.0)]
        value, _t = self.run_b(fills, positions, {"AMD": (2.0, 0.0)}, 50, prior=100.0)
        self.assertEqual(value, -10.0)

    def test_whole_hold_sale_as_first_fill_with_yesterdays_read(self):
        """Board rev-7 input: the whole-hold sale is the day's first fill; the prior session's end state (X) shows
        QHM 5 -> in scope; no valid read before it -> no measure (never +25)."""
        self.orders["NVDA"] = [owner("o-sale", "NVDA", "sell")]
        self.history([(50, {}, {}, [])], carried=["NVDA"])
        fills = [("NVDA", "sell", 5, 105.0, None, 30, "o-sale")]
        r = self.run_b(fills, [], {}, 50, prior=100.0)
        self.assertNoMeasure(r, "no_read_before_fill")

    def test_whole_hold_sale_with_missing_reads_file(self):
        """Board rev-8 input: no reads file (ship day / unreadable): every untagged-fill symbol in scope."""
        self.orders["NVDA"] = [owner("o-sale", "NVDA", "sell")]
        fills = [("NVDA", "sell", 5, 105.0, None, 30, "o-sale")]
        self.assertNoMeasure(self.run_b(fills, [], {}, 50, prior=100.0), "no_read_before_fill")
        self.reads_file.write_text("{not json")
        self.assertNoMeasure(self.run_b(fills, [], {}, 50, prior=100.0), "no_read_before_fill")

    def test_untagged_fill_out_of_scope_still_counts(self):
        """Complete evidence with no QHM/F6 in the symbol: the untagged fill counts as Day/Swing, no order read."""
        self.history([(10, {"AMD": 10.0}, {}, [])], carried=[])
        fills = [("AMD", "buy", 1, 95.0, None, 30, "o1")]
        value, _t = self.run_b(fills, [_pos("AMD", 11, 90.0, 100.0)], {"AMD": (10.0, 0.0)}, 20)
        self.assertEqual(value, -110.0 + 5.0)
        self.fetch_mock.assert_not_called()


class QuantityCap(Base):
    def _setup(self, *rows):
        self.orders["NVDA"] = list(rows)
        self.history([(10, {}, {"NVDA": 5.0}, [])], carried=["NVDA"])

    def test_owner_sale_of_10_with_qhm_5_is_no_measure(self):
        """Codex rev-8 input: a sale larger than the protected holding opens a short no protected tier owns."""
        self._setup(owner("o10", "NVDA", "sell"))
        fills = [("NVDA", "sell", 10, 105.0, None, 30, "o10")]
        self.assertNoMeasure(self.run_b(fills, [_pos("NVDA", -5, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10),
                             "owner_sale_exceeds_protected")

    def test_two_owner_sales_of_5_with_qhm_5_second_is_no_measure(self):
        self._setup(owner("oa", "NVDA", "sell"), owner("ob", "NVDA", "sell"))
        fills = [("NVDA", "sell", 5, 105.0, None, 30, "oa"), ("NVDA", "sell", 5, 105.0, None, 40, "ob")]
        r = self.run_b(fills, [_pos("NVDA", -5, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10)
        self.assertNoMeasure(r, "owner_sale_exceeds_protected")
        self.assertEqual([t.get("order_id") for t in r[1] if t.get("owner_sale_exceeds_protected")], ["ob"])

    def test_two_same_timestamp_sales_form_one_batch(self):
        """Codex rev-9 input: two sales of 5 with the same fill time, QHM 5 -> batch of 10 > 5 -> no measure."""
        self._setup(owner("oa", "NVDA", "sell"), owner("ob", "NVDA", "sell"))
        fills = [("NVDA", "sell", 5, 105.0, None, 30, "oa"), ("NVDA", "sell", 5, 105.0, None, 30, "ob")]
        self.assertNoMeasure(self.run_b(fills, [_pos("NVDA", -5, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10),
                             "owner_sale_exceeds_protected")

    def test_partial_fill_owner_sale_within_holding_left_out(self):
        self._setup(owner("ox", "NVDA", "sell"))
        fills = [("NVDA", "sell", 2, 105.0, None, 30, "ox"), ("NVDA", "sell", 3, 106.0, None, 40, "ox")]
        value, _t = self.run_b(fills, [], {"NVDA": (0.0, 5.0)}, 10)
        self.assertEqual(value, 0.0)

    def test_partial_fill_owner_sale_above_holding_is_no_measure(self):
        self._setup(owner("ox", "NVDA", "sell"))
        fills = [("NVDA", "sell", 3, 105.0, None, 30, "ox"), ("NVDA", "sell", 3, 106.0, None, 40, "ox")]
        self.assertNoMeasure(self.run_b(fills, [_pos("NVDA", -1, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10),
                             "owner_sale_exceeds_protected")

    def test_multi_order_owner_sale_within_holding_left_out(self):
        self._setup(owner("oa", "NVDA", "sell"), owner("ob", "NVDA", "sell"))
        fills = [("NVDA", "sell", 2, 105.0, None, 30, "oa"), ("NVDA", "sell", 3, 106.0, None, 40, "ob")]
        value, _t = self.run_b(fills, [], {"NVDA": (0.0, 5.0)}, 10)
        self.assertEqual(value, 0.0)


class BotLegs(Base):
    def test_overnight_short_covered_by_oco_leg_then_restart_counts_minus_18(self):
        """Board rev-2 input: a Day short of 3 NVDA (DT parent from yesterday) next to QHM 5 is covered today by its
        untagged OCO stop leg at prior close + 6; a restart and a rebuild (Day/Swing claim 0) follow. Counted: -18."""
        self.orders["NVDA"] = [bracket("P1", "DT-NVDA-s-1700000000000-abc", "NVDA", "sell", [("L-stop", "buy")])]
        self.history([(10, {"NVDA": -3.0, "AMD": 1.0}, {"NVDA": 5.0}, []),
                      (50, {"AMD": 1.0}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        fills = [("AMD", "buy", 1, 100.0, "intraday", 10, "d1"), ("NVDA", "buy", 3, 106.0, None, 30, "L-stop")]
        positions = [_pos("NVDA", 5, 100.0, 100.0), _pos("AMD", 1, 100.0, 100.0)]
        value, _t = self.run_b(fills, positions, {"NVDA": (0.0, 5.0), "AMD": (1.0, 0.0)}, 50)
        self.assertEqual(value, -18.0)

    def _race(self, ckpt_min, reads):
        self.orders["NVDA"] = [bracket("P1", "DT-NVDA-b-1700000000000-abc", "NVDA", "buy", [("L-tp", "sell")])]
        self.history(reads, carried=["NVDA"])
        fills = [("NVDA", "buy", 2, 100.0, "daytrade", 10, "P1"),
                 ("NVDA", "sell", 2, 100.0, "daytrade", 20, "D-exit"),
                 ("NVDA", "sell", 2, 112.0, None, 30, "L-tp")]
        return self.run_b(fills, [_pos("NVDA", 3, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, ckpt_min)

    def test_co_hold_bracket_tp_leg_after_day_exit_before_checkpoint(self):
        """Board rev-9 input: the TP leg fills after the tagged Day exit and sells QHM shares (+24 if counted)."""
        r = self._race(40, [(10, {"NVDA": 2.0}, {"NVDA": 5.0}, []), (40, {}, {"NVDA": 5.0}, [])])
        self.assertNoMeasure(r, "bot_leg_exceeds_day_swing_position")
        self.assertNotEqual(r[0], 24.0)

    def test_co_hold_bracket_tp_leg_after_day_exit_after_checkpoint(self):
        r = self._race(25, [(10, {"NVDA": 2.0}, {"NVDA": 5.0}, []), (25, {}, {"NVDA": 5.0}, [])])
        self.assertNoMeasure(r, "bot_leg_exceeds_day_swing_position")

    def test_untagged_access_key_order_without_tagged_parent_is_no_measure(self):
        self.orders["NVDA"] = [{"id": "rem", "symbol": "NVDA", "side": "sell", "source": "access_key",
                                "client_order_id": "4c3b2a19-0f8e-4d7c-9b6a-5f4e3d2c1b0a", "legs": None}]
        self.history([(10, {"NVDA": 2.0}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        fills = [("NVDA", "sell", 2, 100.0, None, 30, "rem")]
        self.assertNoMeasure(self.run_b(fills, [_pos("NVDA", 5, 100.0, 100.0)], {"NVDA": (2.0, 5.0)}, 10),
                             "bot_order_without_day_swing_parent")
        # a leg under a QHM-tagged parent is not Day/Swing either
        rm._UNTAGGED_ORDER_CACHE.update(pt_date="", facts={})
        self.orders["NVDA"] = [bracket("Q1", "QH-NVDA-b-1700000000000-abc", "NVDA", "buy", [("rem", "sell")])]
        self.assertNoMeasure(self.run_b(fills, [_pos("NVDA", 5, 100.0, 100.0)], {"NVDA": (2.0, 5.0)}, 10),
                             "bot_order_without_day_swing_parent")


class SourceUnknown(Base):
    fills = [("NVDA", "sell", 2, 100.0, None, 30, "o1")]

    def _go(self):
        self.history([(10, {}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        return self.run_b(self.fills, [_pos("NVDA", 3, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10)

    def test_missing_source_field_is_no_measure(self):
        self.orders["NVDA"] = [owner("o1", "NVDA", "sell", source_key=False)]
        self.assertNoMeasure(self._go(), "untagged_source_unknown")

    def test_unknown_source_value_is_no_measure(self):
        row = owner("o1", "NVDA", "sell")
        row["source"] = "correspondent"
        self.orders["NVDA"] = [row]
        self.assertNoMeasure(self._go(), "untagged_source_unknown")

    def test_order_read_failure_is_no_measure(self):
        self.orders["NVDA"] = None
        self.assertNoMeasure(self._go(), "untagged_source_unknown")

    def test_order_not_in_read_is_no_measure(self):
        self.orders["NVDA"] = [owner("other", "NVDA", "sell")]
        self.assertNoMeasure(self._go(), "untagged_source_unknown")

    def test_unparseable_fill_time_is_no_measure(self):
        self.orders["NVDA"] = [owner("o1", "NVDA", "sell")]
        self.history([(10, {}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        fills = [("NVDA", "sell", 2, 100.0, None, "garbage", "o1")]
        self.assertNoMeasure(self.run_b(fills, [_pos("NVDA", 3, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10),
                             "fill_time_unreadable")

    def test_order_read_timeout_is_no_measure_and_bounded(self):
        self.fetch.stop()
        from execution.manual_sale_proof import _PAGE_SIZE, _REQUEST_TIMEOUT_SECONDS
        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}), \
                mock.patch.object(rm.requests, "get", side_effect=requests.Timeout("slow")) as get:
            r = self._go()
        self.fetch.start()
        self.assertNoMeasure(r, "untagged_source_unknown")
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["timeout"], _REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["params"]["limit"], str(_PAGE_SIZE))
        self.assertEqual(kwargs["params"]["nested"], "true")

    def test_no_api_keys_never_calls_the_broker(self):
        self.fetch.stop()
        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}), \
                mock.patch.object(rm.requests, "get") as get:
            r = self._go()
        self.fetch.start()
        get.assert_not_called()
        self.assertNoMeasure(r, "untagged_source_unknown")


class NormalDay(Base):
    def test_only_tagged_fills_result_unchanged(self):
        """The KILL-BACKUP-1111 11-06 shape: no untagged fill -> the same -20.00, no order read, no history needed."""
        fills = [("NVDA", "buy", 1, 185.0, "qhm", 30, "q1"), ("AMD", "buy", 2, 250.0, "intraday", 30, "d1")]
        positions = [_pos("NVDA", 2, 190.0, 180.0), _pos("AMD", 2, 240.0, 245.0)]
        value, _t = self.run_b(fills, positions, {"NVDA": (0.0, 1.0)}, 10)
        self.assertEqual(value, -20.0)
        self.fetch_mock.assert_not_called()
        self.assertFalse(self.reads_file.exists())


class SameBatchCoverage(Base):
    def test_tp_leg_in_the_same_batch_as_the_tagged_day_exit_is_no_measure(self):
        """Cold review threat 2 (kbu_repro2): Day 2 + QHM 5; at the SAME time the tagged Day exit sells 2 and the
        bracket's untagged take-profit leg sells 2 (QHM's shares). Never +30."""
        self.orders["NVDA"] = [bracket("par1", "DT-NVDA-b-1700000000000-abc", "NVDA", "buy", [("leg1", "sell")])]
        self.history([(5, {"NVDA": 2.0}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        fills = [("NVDA", "sell", 2, 200.0, "daytrade", 20, "dtexit"), ("NVDA", "sell", 2, 200.0, None, 20, "leg1")]
        r = self.run_b(fills, [_pos("NVDA", 3, 195.0, 190.0)], {"NVDA": (2.0, 5.0)}, 5)
        self.assertNoMeasure(r, "bot_leg_exceeds_day_swing_position")
        self.assertNotEqual(r[0], 30.0)

    def test_leg_covered_after_same_batch_day_fill_still_counts(self):
        """Day 4: the tagged exit sells 2 and the leg sells 2 at the same time -> covered, counted."""
        self.orders["NVDA"] = [bracket("par1", "DT-NVDA-b-1700000000000-abc", "NVDA", "buy", [("leg1", "sell")])]
        self.history([(5, {"NVDA": 4.0}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        fills = [("NVDA", "sell", 2, 200.0, "daytrade", 20, "dtexit"), ("NVDA", "sell", 2, 200.0, None, 20, "leg1")]
        value, _t = self.run_b(fills, [_pos("NVDA", 5, 190.0, 190.0)], {"NVDA": (4.0, 5.0)}, 5)
        self.assertEqual(value, 40.0)


class Nits(Base):
    def test_failed_order_read_is_retried_next_check_not_found_is_cached(self):
        self.orders["NVDA"] = None                                    # transient failure: one request per check
        self.history([(10, {}, {"NVDA": 5.0}, [])], carried=["NVDA"])
        fills = [("NVDA", "sell", 2, 100.0, None, 30, "o1")]
        for _ in range(3):
            self.assertNoMeasure(self.run_b(fills, [_pos("NVDA", 3, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10),
                                 "untagged_source_unknown")
        self.assertEqual(self.fetch_mock.call_count, 3)
        self.orders["NVDA"] = [owner("o1", "NVDA", "sell")]           # the next check's read succeeds: decided
        value, _t = self.run_b(fills, [_pos("NVDA", 3, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10)
        self.assertEqual(value, 0.0)
        self.assertEqual(self.fetch_mock.call_count, 4)
        rm._UNTAGGED_ORDER_CACHE.update(pt_date="", facts={})
        self.orders["NVDA"] = [owner("other", "NVDA", "sell")]       # a successful read without the order
        for _ in range(2):
            self.assertNoMeasure(self.run_b(fills, [_pos("NVDA", 3, 100.0, 100.0)], {"NVDA": (0.0, 5.0)}, 10),
                                 "untagged_source_unknown")
        self.assertEqual(self.fetch_mock.call_count, 5)              # definitive "not found": cached for the day

    def test_record_never_rewrites_an_unchanged_file(self):
        e = entry(10, T(10).isoformat(), ["NVDA"], active={"NVDA": 2.0})
        with mock.patch.object(rm, "_write_backup_reads", wraps=rm._write_backup_reads) as w:
            for _ in range(3):
                rm._record_backup_read(dict(e, read_utc=T(11).isoformat()))
        self.assertEqual(w.call_count, 1)
        before = self.reads_file.stat().st_mtime_ns
        rm._record_backup_read(dict(e, read_utc=T(12).isoformat()))
        self.assertEqual(self.reads_file.stat().st_mtime_ns, before)


def _race_writer(path, symbol, rounds):
    """Child process for the two-writer race: records `rounds` reads that each hold `symbol` (alternating claims)."""
    from execution import risk_manager as child_rm
    child_rm._KILL_BACKUP_READS_FILE = Path(path)
    for k in range(rounds):
        child_rm._record_backup_read(entry(k, YDAY_STAMP, [symbol], active={symbol: float(k % 2)}))


class ReadHistory(Base):
    def test_record_stores_the_raw_read_and_dedups(self):
        e = entry(10, YDAY_STAMP, ["NVDA"], unkeyed=True, credited=["c1"], active={"NVDA": 2.0})
        rm._record_backup_read(e)
        rm._record_backup_read(dict(e, read_utc=T(12).isoformat()))      # unchanged apart from the read time
        rm._record_backup_read(dict(e, unkeyed=False))                   # a taint flag changed: stored
        data = json.loads(self.reads_file.read_text())
        self.assertEqual(data["schema"], rm._KILL_BACKUP_READS_SCHEMA)
        self.assertEqual([r["unkeyed"] for r in data["reads"]], [True, False])
        self.assertEqual(data["reads"][0]["stamp_raw"], YDAY_STAMP)
        self.assertEqual(data["reads"][0]["credited"], ["c1"])
        self.assertFalse(data["evidence_lost"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["kill_backup_reads.json", "kill_backup_reads.json.lock"])   # no temp file left over

    def test_new_pt_day_carries_the_newest_read_as_y_and_resets(self):
        old = entry(0, "2000-01-01T20:00:00+00:00", ["LLY"])
        newest = entry(0, "2000-01-01T21:00:00+00:00", ["NVDA"])
        self.reads_file.write_text(json.dumps({"schema": rm._KILL_BACKUP_READS_SCHEMA, "pt_date": "2000-01-01",
                                               "evidence_lost": True, "prev_last": None, "reads": [old, newest]}))
        rm._record_backup_read(entry(1, YDAY_STAMP, []))
        data = json.loads(self.reads_file.read_text())
        self.assertEqual((data["pt_date"], data["evidence_lost"]), (TODAY, False))
        self.assertEqual(data["prev_last"]["held"], ["NVDA"])
        self.assertEqual(len(data["reads"]), 1)

    def test_checkpoint_read_records_the_raw_stamp(self):
        led = {"fills_through_utc": YDAY_STAMP, "unkeyed_edit_since_stamp": False,
               "credited_since_stamp": ["qhm_registered_stop:abc-1"], "positions": {"NVDA": {
                   "tiers": {"day": {"qty": 0.0}, "swing": {"qty": 1.0}, "qhm": {"qty": 5.0}, "forever_6": {"qty": 0.0}}}}}
        with mock.patch("execution.ownership_guard.load_ledger", return_value=led), \
                self.assertLogs(rm.logger, level="WARNING"):
            _claims, ts, _from_file = rm._ledger_checkpoint()
        self.assertIsNone(ts)                                   # yesterday's stamp: no checkpoint, still recorded
        r = json.loads(self.reads_file.read_text())["reads"][-1]
        self.assertEqual((r["stamp_raw"], r["credited"], r["held"], r["active"]),
                         (YDAY_STAMP, ["abc-1"], ["NVDA"], {"NVDA": 1.0}))

    def test_last_good_fallback_and_unreadable_ledger_are_never_recorded(self):
        rm._LAST_GOOD_CHECKPOINT.update(claims={"NVDA": (0.0, 2.0)}, ts=T(10))
        with mock.patch("execution.ownership_guard.load_ledger", side_effect=ValueError("corrupt")), \
                self.assertLogs(rm.logger, level="CRITICAL"):
            rm._ledger_checkpoint()
        with mock.patch("execution.ownership_guard.load_ledger", side_effect=ValueError("corrupt")):
            rm._record_kill_check_read()
        self.assertFalse(self.reads_file.exists())

    def test_every_kill_check_records_a_read_on_the_exact_path_and_logs_its_cost(self):
        import config
        led = {"fills_through_utc": YDAY_STAMP, "positions": {}}
        with mock.patch.object(rm, "_load_kill_state", return_value={}):
            risk = rm.RiskManager(2500.0, daily_start_value=2500.0)

        def exact(self_):
            self_._kill_attr = {"exact": True}
            return -5.0, True

        with mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True), \
                mock.patch("execution.ownership_guard.load_ledger", return_value=led), \
                mock.patch.object(rm.RiskManager, "_intraday_trading_pnl", exact), \
                mock.patch.object(rm, "_publish_kill_snapshot"), \
                self.assertLogs(rm.logger, level="DEBUG") as logs:
            self.assertFalse(risk.check_kill_switch())
        self.assertEqual(json.loads(self.reads_file.read_text())["reads"][-1]["stamp_raw"], YDAY_STAMP)
        self.assertTrue(any("per-check ledger read + record took" in m for m in logs.output))

    def _seed(self, stamp, fills_ok=True):
        rm._intraday_cache.update(fill_ts=(S(30),), fill_oid=("o1",))
        with mock.patch("execution.ownership_guard.load_ledger",
                        return_value={"fills_through_utc": stamp, "positions": {}}), \
                mock.patch.object(rm, "_today_fills", return_value=((("AMD", "buy", 1, 1.0, "intraday"),), fills_ok)):
            return rm.seed_kill_backup_reads()

    def test_seed_true_when_x_is_available(self):
        self.assertTrue(self._seed(YDAY_STAMP))
        self.assertEqual(len(json.loads(self.reads_file.read_text())["reads"]), 1)

    def test_seed_false_and_logs_when_x_is_unavailable(self):
        with self.assertLogs(rm.logger, level="WARNING") as logs:
            self.assertFalse(self._seed(T(20).isoformat()))      # deploy after today's first fill: stamp from today
        self.assertTrue(any("seed: no carried evidence X" in m for m in logs.output))
        rm._FILL_SEQ_LOGGED.update(pt_date="", oids=set())
        with self.assertLogs(rm.logger, level="WARNING"):
            self.assertFalse(self._seed(YDAY_STAMP, fills_ok=False))

    def test_old_format_or_earlier_day_file_is_missing_not_lost(self):
        self.reads_file.write_text(json.dumps({"schema": 1, "pt_date": TODAY, "carried": None, "reads": []}))
        rm._record_backup_read(x_read(["NVDA"]))
        data = json.loads(self.reads_file.read_text())
        self.assertFalse(data["evidence_lost"])
        self.assertTrue(rm._backup_read_history(TODAY, (S(30),), ("o1",))[1])
        # an unparseable file last written on an earlier day: missing (fresh file), not lost
        self.reads_file.write_text("{torn")
        old = (BASE - timedelta(days=3)).timestamp()
        os.utime(self.reads_file, (old, old))
        rm._record_backup_read(x_read(["NVDA"]))
        self.assertFalse(json.loads(self.reads_file.read_text())["evidence_lost"])
        # a malformed file of an earlier PT day: missing
        self.reads_file.write_text(json.dumps({"schema": rm._KILL_BACKUP_READS_SCHEMA, "pt_date": "2000-01-01",
                                               "evidence_lost": False, "prev_last": None, "reads": [{"bad": 1}]}))
        self.assertEqual(rm._load_backup_reads()[1], "missing")
        # today's file with malformed fields: corrupt
        self.reads_file.write_text(json.dumps({"schema": rm._KILL_BACKUP_READS_SCHEMA, "pt_date": TODAY,
                                               "evidence_lost": False, "prev_last": None, "reads": [{"bad": 1}]}))
        self.assertEqual(rm._load_backup_reads()[1], "corrupt")

    def test_lock_busy_past_the_bounded_wait_skips_the_read_with_one_warning(self):
        lock = self.reads_file.with_name(self.reads_file.name + ".lock")
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)            # the other process holds the lock
        saved = dict(rm._READS_LOCK_STATS)
        rm._READS_LOCK_STATS.update(section_max_s=0.02, section_min_s=0.005)
        try:
            t0 = datetime.now(timezone.utc)
            with self.assertLogs(rm.logger, level="WARNING") as logs:
                rm._record_backup_read(x_read(["NVDA"]))
            waited = (datetime.now(timezone.utc) - t0).total_seconds()
        finally:
            rm._READS_LOCK_STATS.update(saved)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertFalse(self.reads_file.exists())
        self.assertEqual(len([m for m in logs.output if "lock busy" in m]), 1)
        self.assertLess(waited, 1.0)

    def test_a_test_process_never_touches_the_repo_path(self):
        """With the file NOT redirected (the repo path), a test process neither reads nor writes it."""
        with mock.patch.object(rm, "_KILL_BACKUP_READS_FILE", rm._KILL_BACKUP_READS_DEFAULT), \
                mock.patch.object(rm, "_write_backup_reads") as w, \
                mock.patch.object(Path, "read_text", side_effect=AssertionError("read the repo file")):
            self.assertTrue(rm._kbu_under_test())
            self.assertFalse(rm._reads_file_allowed())
            rm._record_backup_read(x_read(["NVDA"]))
            self.assertEqual(rm._load_backup_reads(), (None, "missing"))
        w.assert_not_called()


class RepoFileGuard(unittest.TestCase):
    """Cold review blocker: a run of the kill test modules must never create or change the repo's real read history
    (or its lock). Runs them in a child `python -m unittest` with tests/ as the top level and MTF_TEST_MODE removed
    (the `discover -s tests` layout: tests/__init__ and conftest do not run there)."""

    @staticmethod
    def _kill_modules(repo):
        """Every tests/test_*kill*.py that this interpreter can load (modules that import pytest are left out)."""
        import ast
        out = []
        for p in sorted((repo / "tests").glob("test_*kill*.py")):
            names = set()
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.split(".")[0])
            if "pytest" not in names:
                out.append(p.stem)
        return out

    def test_kill_test_modules_never_touch_the_repo_read_history(self):
        if os.getenv("KBU_GUARD_CHILD") == "1":
            self.skipTest("inside the guard's own child run")
        import re
        import subprocess
        import sys as _sys
        repo = Path(rm.__file__).resolve().parent.parent
        modules = self._kill_modules(repo)
        self.assertIn(Path(__file__).stem, modules)
        expected = sum(unittest.defaultTestLoader.loadTestsFromName(f"tests.{m}").countTestCases() for m in modules)
        targets = [rm._KILL_BACKUP_READS_DEFAULT, rm._KILL_BACKUP_READS_DEFAULT.with_name(
            rm._KILL_BACKUP_READS_DEFAULT.name + ".lock")]

        def snap():
            return [(p.exists(), p.stat().st_mtime_ns if p.exists() else None,
                     p.stat().st_size if p.exists() else None) for p in targets]

        before = snap()
        env = {k: v for k, v in os.environ.items() if k != "MTF_TEST_MODE"}
        env["KBU_GUARD_CHILD"] = "1"
        env["PYTHONPATH"] = os.pathsep.join([str(repo / "tests"), str(repo)])
        run = subprocess.run([_sys.executable, "-m", "unittest", *modules], cwd=repo / "tests", env=env,
                             capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        ran = re.search(r"Ran (\d+) tests?", run.stderr)
        self.assertIsNotNone(ran, run.stderr[-2000:])
        self.assertGreaterEqual(int(ran.group(1)), expected)
        self.assertEqual(snap(), before, "a kill test run created or changed the repo-path read history")


class TestProcessDetection(unittest.TestCase):
    """Both branches of _kbu_under_test: the alerts.py guard, and the `__main__` module spec (unittest/pytest) used
    when neither MTF_TEST_MODE nor a recognisable argv[0] is present."""

    def _main_with_spec(self, name):
        import sys as _sys
        import types
        fake = types.ModuleType("__main__")
        fake.__spec__ = None if name is None else types.SimpleNamespace(name=name)
        return mock.patch.dict(_sys.modules, {"__main__": fake})

    def test_spec_branch_detects_unittest_and_pytest_main(self):
        with mock.patch("alerts._under_test", return_value=False):
            for name in ("unittest.__main__", "pytest.__main__"):
                with self._main_with_spec(name):
                    self.assertTrue(rm._kbu_under_test(), name)

    def test_production_main_is_not_a_test_process(self):
        with mock.patch("alerts._under_test", return_value=False):
            for name in (None, "main", "run_day_tier"):
                with self._main_with_spec(name):
                    self.assertFalse(rm._kbu_under_test(), name)

    def test_alerts_guard_branch(self):
        with mock.patch("alerts._under_test", return_value=True), self._main_with_spec(None):
            self.assertTrue(rm._kbu_under_test())


class ProtectedFillScope(Base):
    """Cold re-review: a QHM/F6-tagged fill today puts its symbol in scope whatever its time or credit."""

    def _fills(self, tier):
        return [("AAPL", "buy", 2, 104.0, tier, 5, "q1"), ("AAPL", "sell", 2, 110.0, None, 10, "own1")]

    def test_protected_buy_and_owner_sale_both_before_checkpoint_is_no_measure(self):
        """F6 buys 2 AAPL and the owner sells both in the app, all before the checkpoint; the rebuild nets QHM/F6 to
        0 and no read ever shows AAPL. True Day/Swing: -20 (AMD). Never 0.0."""
        for tier in sorted(rm._PROTECTED_FILL_TIERS):
            with self.subTest(tier=tier):
                rm._UNTAGGED_ORDER_CACHE.update(pt_date="", facts={})
                self.orders["AAPL"] = [owner("own1", "AAPL", "sell")]
                self.history([(30, {"AMD": 2.0}, {}, [])], carried=[])
                fills = self._fills(tier) + [("AMD", "buy", 2, 250.0, "intraday", -100, "d1")]
                r = self.run_b(fills, [_pos("AMD", 2, 240.0, 250.0)], {"AMD": (2.0, 0.0)}, 30, prior=100.0)
                self.assertNoMeasure(r, "no_read_before_fill")
                self.assertNotEqual(r[0], 0.0)

    def test_same_without_the_amd_lot_never_plus_20(self):
        self.orders["AAPL"] = [owner("own1", "AAPL", "sell")]
        self.history([(30, {}, {}, [])], carried=[])
        r = self.run_b(self._fills("forever6"), [], {}, 30, prior=100.0)
        self.assertNoMeasure(r, "no_read_before_fill")
        self.assertNotEqual(r[0], 20.0)

    def test_credited_protected_fill_still_puts_the_symbol_in_scope(self):
        """The QHM buy was credited to the claim by an incremental writer (q1 in credited_since_stamp): still in
        scope. The owner sale after the checkpoint then gives no measure while no valid read precedes it."""
        self.orders["AAPL"] = [owner("own1", "AAPL", "sell")]
        self.history([], carried=[])                                  # X exists, no read shows AAPL
        fills = [("AAPL", "buy", 2, 104.0, "qhm", 40, "q1"), ("AAPL", "sell", 2, 110.0, None, 50, "own1")]
        r = self.run_b(fills, [], {}, 30, prior=100.0, credited=frozenset({"q1"}))
        self.assertNoMeasure(r, "no_read_before_fill")
        self.fetch_mock.assert_called_with("AAPL")


class CarriedEvidenceX(Base):
    """Board rev-10 C1/C3/C5: X (a clean read of the prior session's end state) alone makes the evidence complete."""

    def _x(self, fill_ts=(), fill_oids=()):
        return rm._backup_read_history(TODAY, tuple(fill_ts), tuple(fill_oids))

    def test_clean_read_stamped_before_midnight_and_first_fill_is_x(self):
        self.history([], carried=["NVDA"])
        held, complete, _reads = self._x([S(30)], ["o1"])
        self.assertTrue(complete)
        self.assertEqual(held, {"NVDA"})

    def test_unkeyed_owner_sale_heal_before_the_first_read_is_missing_never_plus_25(self):
        """The owner's app sale of all 5 QHM NVDA was healed into the ledger with no fill id before today's first read
        (unkeyed flag set, QHM claim gone). That read is not X -> every untagged-fill symbol in scope -> no measure."""
        self.orders["NVDA"] = [owner("own1", "NVDA", "sell")]
        self.history([], carried=None, extra=[entry(20, YDAY_STAMP, [], unkeyed=True)])
        self.assertFalse(self._x([S(10)], ["own1"])[1])
        r = self.run_b([("NVDA", "sell", 5, 105.0, None, 10, "own1")], [], {}, 40, prior=100.0)
        self.assertNoMeasure(r, "no_read_before_fill")
        self.assertNotEqual(r[0], 25.0)

    def test_credited_fill_of_today_taints_x(self):
        self.history([], carried=None, extra=[entry(-330, YDAY_STAMP, [], credited=["q1"])])
        self.assertFalse(self._x([S(10)], ["q1"])[1])
        self.assertTrue(self._x([S(10)], ["other"])[1])         # a credit with no fill today does not taint it

    def test_read_stamped_after_the_premarket_fill_is_missing(self):
        self.history([], carried=None, extra=[entry(-50, T(-60).isoformat(), ["NVDA"])])
        self.assertFalse(self._x([S(-60)], ["pm1"])[1])

    def test_read_stamped_before_midnight_but_not_before_the_first_fill_is_not_x(self):
        early_fill = (BASE - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")   # 23:00 PT yesterday
        late_stamp = (BASE - timedelta(minutes=390)).isoformat()                     # 23:30 PT yesterday
        self.history([], carried=None, extra=[entry(-330, late_stamp, [])])
        self.assertFalse(self._x([early_fill], ["f1"])[1])                           # stamp not before the fill
        self.assertTrue(self._x([S(10)], ["f1"])[1])

    def test_five_day_old_y_without_x_is_missing(self):
        """kbu_repro shape: Y is five days old and holds nothing; no X today -> missing -> the whole-hold owner sale
        is in scope -> no measure (never the out-of-scope +$25)."""
        y = entry(0, "2000-01-01T21:00:00+00:00", [])
        y["read_utc"] = (BASE - timedelta(days=5)).isoformat()
        self.orders["NVDA"] = [owner("own1", "NVDA", "sell")]
        self.history([(30, {}, {}, [])], carried=None, prev_last=y)
        self.assertFalse(self._x([S(10)], ["own1"])[1])
        self.assertNoMeasure(self.run_b([("NVDA", "sell", 5, 195.0, None, 10, "own1")], [], {}, 30, prior=190.0),
                             "no_read_before_fill")

    def test_y_only_adds_symbols(self):
        self.history([], carried=[], prev_last=entry(-600, YDAY_STAMP, ["LLY"]))
        held, complete, _r = self._x([S(10)], ["o1"])
        self.assertTrue(complete)
        self.assertIn("LLY", held)

    def test_after_hours_qhm_fill_seen_only_in_x_is_in_scope(self):
        """QHM bought GOOGL after hours yesterday (only X shows it); the owner sells it in the app today before any
        valid read; the checkpoint and Y no longer show it -> in scope via X -> no measure."""
        ah_stamp = (BASE - timedelta(hours=14)).isoformat()      # 16:00 PT yesterday (after hours)
        self.orders["GOOGL"] = [owner("own1", "GOOGL", "sell")]
        self.history([(30, {}, {}, [])], carried=None, prev_last=entry(-900, YDAY_STAMP, []),
                     extra=[entry(-330, ah_stamp, ["GOOGL"])])
        held, complete, _r = self._x([S(10)], ["own1"])
        self.assertTrue(complete)
        self.assertIn("GOOGL", held)
        self.assertNoMeasure(self.run_b([("GOOGL", "sell", 2, 330.0, None, 10, "own1")], [], {}, 30, prior=320.0),
                             "no_read_before_fill")

    def test_corrupt_file_mid_day_is_missing_for_the_rest_of_the_day(self):
        self.history([], carried=["NVDA"])
        self.assertTrue(self._x([S(30)], ["o1"])[1])
        self.reads_file.write_text("{torn")
        with self.assertLogs(rm.logger, level="WARNING"):
            rm._record_backup_read(x_read(["NVDA"]))             # a clean read again, after the corruption
        rm._record_backup_read(dict(x_read(["NVDA", "LLY"]), read_utc=T(40).isoformat()))
        data = json.loads(self.reads_file.read_text())
        self.assertTrue(data["evidence_lost"])
        self.assertFalse(self._x([S(30)], ["o1"])[1])

    def test_x_log_line_once_per_day(self):
        self.history([], carried=["NVDA"])
        with self.assertLogs(rm.logger, level="INFO") as logs:
            self._x([S(30)], ["o1"])
            self._x([S(30)], ["o1"])
        lines = [m for m in logs.output if "carried evidence X" in m]
        self.assertEqual(len(lines), 1)
        self.assertIn('"found": true', lines[0])
        self.assertIn(YDAY_STAMP, lines[0])


class TwoWriters(Base):
    def test_two_processes_racing_lose_no_held_symbol(self):
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_race_writer, args=(str(self.reads_file), sym, 40)) for sym in ("AAA", "BBB")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        held = rm._backup_read_history(TODAY, (S(30),), ("o1",))[0]
        self.assertEqual(held & {"AAA", "BBB"}, {"AAA", "BBB"})

    def test_read_modify_write_holds_the_lock(self):
        lock = self.reads_file.with_name(self.reads_file.name + ".lock")
        seen = []
        real = rm._load_backup_reads

        def probe():
            fd = os.open(str(lock), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                seen.append("held")
            finally:
                os.close(fd)
            return real()

        with mock.patch.object(rm, "_load_backup_reads", side_effect=probe):
            rm._record_backup_read(x_read(["NVDA"]))
        self.assertEqual(seen, ["held"])



class XSessionEnd(Base):
    """Cold review rev 10c: X counts only if no account fill came after its stamp and before PT midnight."""

    def test_stale_x_after_hours_protected_buy_is_no_measure_never_plus_15(self):
        """Yesterday after the last rebuild (stamp 13:00 PT) a QHM/F6 buy of 5 NVDA, never credited: X lacks NVDA.
        Today the owner sells all 5 in the app before the checkpoint; the rebuild nets QHM to 0. Real -10 (AMD)."""
        self.session_end = False                                       # the after-hours fill is after X's stamp
        self.orders["NVDA"] = [owner("o-sale", "NVDA", "sell")]
        self.history([(10, {"AMD": 2.0}, {}, []), (50, {"AMD": 2.0}, {}, [])], carried=[])
        fills = [("AMD", "buy", 2, 245.0, "intraday", 5, "d1"), ("NVDA", "sell", 5, 105.0, None, 30, "o-sale")]
        r = self.run_b(fills, [_pos("AMD", 2, 240.0, 245.0)], {"AMD": (2.0, 0.0)}, 50, prior=100.0)
        self.assertIsNone(r[0])                       # in scope (X missing); the 06:10 read shows no QHM NVDA, so
        self.assertNoMeasure(r, "owner_sale_exceeds_protected")   # the sale of 5 exceeds the protected position
        self.assertFalse(rm._backup_read_history(TODAY, (S(5), S(30)), ("d1", "o-sale"))[1])
        self.session_end = True                       # contrast: with X valid, NVDA is out of scope (the old +15)
        rm._UNTAGGED_ORDER_CACHE.update(pt_date="", facts={})
        self.assertEqual(self.run_b(fills, [_pos("AMD", 2, 240.0, 245.0)], {"AMD": (2.0, 0.0)}, 50,
                                    prior=100.0)[0], 15.0)

    def test_read_failure_makes_x_missing(self):
        self.session_end = None
        self.history([], carried=["NVDA"])
        self.assertFalse(rm._backup_read_history(TODAY, (S(30),), ("o1",))[1])

    def test_not_read_unless_x_is_otherwise_valid(self):
        self.history([], carried=None, extra=[entry(-330, YDAY_STAMP, [], unkeyed=True)])
        rm._backup_read_history(TODAY, (S(30),), ("o1",))
        self.session_end_mock.assert_not_called()

    def test_exact_path_never_reads_prior_session_fills(self):
        import config
        with mock.patch.object(rm, "_load_kill_state", return_value={}):
            risk = rm.RiskManager(2500.0, daily_start_value=2500.0)

        def exact(self_):
            self_._kill_attr = {"exact": True}
            return -5.0, True

        with mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True), \
                mock.patch("execution.ownership_guard.load_ledger",
                           return_value={"fills_through_utc": YDAY_STAMP, "positions": {}}), \
                mock.patch.object(rm.RiskManager, "_intraday_trading_pnl", exact), \
                mock.patch.object(rm, "_publish_kill_snapshot"):
            self.assertFalse(risk.check_kill_switch())
        self.session_end_mock.assert_not_called()


class NoFillAfterStampRead(unittest.TestCase):
    """The real bounded read behind X (requests.get mocked; no network)."""

    STAMP = datetime.fromisoformat(YDAY_STAMP)

    def setUp(self):
        rm._SESSION_END_CACHE.update(pt_date="", results={})
        keys = mock.patch.dict(os.environ, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
        keys.start()
        self.addCleanup(keys.stop)

    def _resp(self, rows, status=200):
        return mock.Mock(status_code=status, json=mock.Mock(return_value=rows))

    def _row(self, dt):
        return {"transaction_time": dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}

    def test_after_hours_fill_after_the_stamp_means_x_missing(self):
        after_hours = self.STAMP + timedelta(hours=3)                  # 16:00 PT yesterday
        with mock.patch.object(rm.requests, "get", return_value=self._resp([self._row(after_hours)])):
            self.assertFalse(rm._no_fill_after_stamp(self.STAMP, TODAY))

    def test_no_fill_after_the_stamp_keeps_x(self):
        with mock.patch.object(rm.requests, "get", return_value=self._resp([])) as get:
            self.assertTrue(rm._no_fill_after_stamp(self.STAMP, TODAY))
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["after"], self.STAMP.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(params["until"], rm._pt_midnight().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        from execution.manual_sale_proof import _REQUEST_TIMEOUT_SECONDS
        self.assertEqual(get.call_args.kwargs["timeout"], _REQUEST_TIMEOUT_SECONDS)
        rm._SESSION_END_CACHE.update(pt_date="", results={})
        # the stamp's own fill (same second, not after it) is not a later fill
        with mock.patch.object(rm.requests, "get", return_value=self._resp([self._row(self.STAMP)])):
            self.assertTrue(rm._no_fill_after_stamp(self.STAMP, TODAY))

    def test_read_failure_unparseable_time_or_full_page_means_x_missing(self):
        with mock.patch.object(rm.requests, "get", side_effect=requests.Timeout("slow")):
            self.assertIsNone(rm._no_fill_after_stamp(self.STAMP, TODAY))
        with mock.patch.object(rm.requests, "get", return_value=self._resp({"code": 1}, status=422)):
            self.assertIsNone(rm._no_fill_after_stamp(self.STAMP, TODAY))
        with mock.patch.object(rm.requests, "get", return_value=self._resp([{"transaction_time": "garbage"}])):
            self.assertFalse(rm._no_fill_after_stamp(self.STAMP, TODAY))
        rm._SESSION_END_CACHE.update(pt_date="", results={})
        full = [self._row(self.STAMP)] * int("100")
        with mock.patch.object(rm.requests, "get", return_value=self._resp(full)):
            self.assertFalse(rm._no_fill_after_stamp(self.STAMP, TODAY))

    def test_read_happens_once_per_pt_day_and_failures_are_retried(self):
        with mock.patch.object(rm.requests, "get", side_effect=requests.ConnectionError("down")) as get:
            rm._no_fill_after_stamp(self.STAMP, TODAY)
            rm._no_fill_after_stamp(self.STAMP, TODAY)
        self.assertEqual(get.call_count, 2)                            # a failure is not cached
        with mock.patch.object(rm.requests, "get", return_value=self._resp([])) as get:
            for _ in range(3):
                self.assertTrue(rm._no_fill_after_stamp(self.STAMP, TODAY))
        self.assertEqual(get.call_count, 1)                            # a result is cached for the PT day

    def test_no_keys_never_calls_the_broker(self):
        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}), \
                mock.patch.object(rm.requests, "get") as get:
            self.assertIsNone(rm._no_fill_after_stamp(self.STAMP, TODAY))
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
