# ruff: noqa: E501 — test fixtures mirror real order/fill payloads on one line
"""Tests for reporting/broker_ground_truth.py — broker stop-coverage classification.

Every fixture is synthetic but mirrors a real production case found while building the module
(logs/design_records/audit_broker_ground_truth_2026-09-24.md): SOFI 2026-09-22 (core intraday,
software stop by design), UBER 2026-09-18 (breakeven-push resubmit failed → 3.5h lapse), AMZN
2026-09-23 (day-tier OCO leg), GE 2026-09-14 (quarterly-hold GTC stop created 07-27), GEV
2026-09-11 (quarterly-hold stop cancelled 12:03, resubmitted 13:07)."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from reporting import broker_ground_truth as bgt

ET = bgt.ET
UTC = bgt.UTC
OPEN = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
CLOSE = datetime(2026, 9, 22, 16, 0, tzinfo=ET)


def iso(h, m, day=22, month=9, frac=".123456"):
    return datetime(2026, month, day, h, m, tzinfo=ET).astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%S") + frac + "Z"


def stop(oid, sym, side, qty, created, end=None, coid="IN-x", status="canceled", end_field="canceled_at"):
    o = {"id": oid, "symbol": sym, "side": side, "type": "stop", "qty": str(qty), "status": status,
         "created_at": created, "client_order_id": coid}
    if end:
        o[end_field] = end
    return o


def fill(sym, side, qty, t, oid):
    return {"symbol": sym, "side": side, "qty": str(qty), "transaction_time": t, "order_id": oid}


def entry(oid, sym, side, coid):
    return {"id": oid, "symbol": sym, "side": side, "type": "market", "qty": "1",
            "status": "filled", "created_at": iso(9, 0), "client_order_id": coid}


def cycles(start=OPEN, end=CLOSE, every=7):
    out, t = [], start
    while t <= end:
        out.append(t.astimezone(UTC))
        t += timedelta(minutes=every)
    return out


def run(orders, fills, positions, cyc="default"):
    return bgt.classify(OPEN, CLOSE, orders, fills, positions,
                        cycles() if cyc == "default" else cyc)


def ok(res):
    return {"status": "OK", **res}


class TestClassify(unittest.TestCase):
    def test_fresh_core_entry_software_only_by_design(self):
        # SOFI 9/22: core short at 12:53, broker DAY stop only from the 15:49 sweep.
        orders = [entry("e1", "SOFI", "sell", "IN-SOFI-s-1"),
                  stop("s1", "SOFI", "buy", 4, iso(15, 49), iso(16, 0), end_field="expired_at")]
        fills = [fill("SOFI", "sell", 4, iso(12, 53), "e1")]
        p = run(orders, fills, {"SOFI": -4.0})["positions"]["SOFI"]
        self.assertEqual(p["class"], bgt.CLASS_DESIGN)
        self.assertEqual(p["uncovered_min"], 176)
        self.assertEqual((p["owners"], p["uncovered_at_close"]), (["core"], False))

    def test_carried_core_opening_window_is_design(self):
        orders = [stop("g", "INTC", "sell", 2, iso(16, 29, day=21), iso(9, 20)),   # overnight GTC
                  stop("d", "INTC", "sell", 2, iso(9, 36), iso(16, 0), end_field="expired_at")]
        p = run(orders, [], {"INTC": 2.0})["positions"]["INTC"]
        self.assertEqual(p["class"], bgt.CLASS_DESIGN)
        self.assertEqual(p["owners"], ["core"])
        self.assertEqual(p["uncovered_windows_et"], ["09:30-09:36"])

    def test_carried_core_midday_lapse_is_lapsed_high(self):
        # UBER 9/18: DAY stop cancelled 10:05 by the breakeven push, resubmit failed until 13:29.
        orders = [stop("g", "UBER", "buy", 2, iso(16, 29, day=21), iso(9, 20)),
                  stop("d1", "UBER", "buy", 2, iso(9, 38), iso(10, 5)),
                  stop("d2", "UBER", "buy", 2, iso(13, 29), iso(16, 0), end_field="expired_at")]
        gt = ok(run(orders, [], {"UBER": -2.0}))
        self.assertEqual(gt["positions"]["UBER"]["class"], bgt.CLASS_LAPSED)
        al = bgt.alarm_findings(gt)
        self.assertEqual([a["severity"] for a in al], ["critical"])      # >= 60 min lapse
        self.assertIn("(lapsed 204m beyond design)", al[0]["detail"])

    def test_fresh_core_holding_that_lost_its_stop_is_lapsed(self):
        orders = [entry("e1", "NFLX", "sell", "IN-NFLX-s-1"),
                  stop("s1", "NFLX", "buy", 2, iso(15, 30), iso(15, 40))]
        fills = [fill("NFLX", "sell", 2, iso(15, 0), "e1")]
        p = run(orders, fills, {"NFLX": -2.0})["positions"]["NFLX"]
        self.assertEqual(p["class"], bgt.CLASS_LAPSED)

    def test_short_resolved_lapse_is_low(self):
        orders = [stop("g", "MARA", "sell", 3, iso(16, 29, day=21), iso(9, 20)),
                  stop("d1", "MARA", "sell", 3, iso(9, 34), iso(10, 52)),
                  stop("d2", "MARA", "sell", 3, iso(10, 58), iso(16, 0), end_field="expired_at")]
        gt = ok(run(orders, [], {"MARA": 3.0}))
        self.assertEqual(gt["positions"]["MARA"]["class"], bgt.CLASS_LAPSED)
        self.assertEqual(bgt.alarm_findings(gt)[0]["severity"], "low")

    def test_core_never_given_a_stop_by_the_sweep_is_lapsed_at_close(self):
        orders = [entry("e1", "SOFI", "sell", "IN-SOFI-s-1")]
        fills = [fill("SOFI", "sell", 4, iso(12, 53), "e1")]
        gt = ok(run(orders, fills, {"SOFI": -4.0}))
        p = gt["positions"]["SOFI"]
        self.assertEqual((p["class"], p["uncovered_at_close"]), (bgt.CLASS_LAPSED, True))
        al = bgt.alarm_findings(gt)
        self.assertEqual(al[0]["severity"], "high")
        self.assertIn("STILL UNCOVERED", al[0]["title"])

    def test_sweep_deadline_counts_uncovered_minutes_after_1555_as_lapsed(self):
        # exited 15:58 with no broker stop ever → the 15:55-15:58 minutes are a lapse
        orders = [entry("e1", "PLTR", "buy", "IN-PLTR-b-1"), entry("x1", "PLTR", "sell", "IN-PLTR-s-1")]
        fills = [fill("PLTR", "buy", 1, iso(14, 0), "e1"),
                 fill("PLTR", "sell", 1, iso(15, 58, frac=".000000"), "x1")]
        p = run(orders, fills, {})["positions"]["PLTR"]
        self.assertEqual((p["class"], p["lapsed_min"], p["uncovered_at_close"]), (bgt.CLASS_LAPSED, 3, False))

    def test_entry_at_or_after_1530_without_gtc_is_lapsed(self):
        # entry_logic places a GTC at entry from 15:30 ET — no software-only window for it
        orders = [entry("e1", "NVDA", "buy", "IN-NVDA-b-1")]
        fills = [fill("NVDA", "buy", 1, iso(15, 35), "e1")]
        p = run(orders, fills, {"NVDA": 1.0})["positions"]["NVDA"]
        self.assertEqual((p["class"], p["lapsed_min"]), (bgt.CLASS_LAPSED, 25))

    def test_gtc_at_entry_rule_is_wall_clock_capped_at_close(self):
        # early-close day (13:00): an entry at 12:40 is NOT under the 15:30 GTC rule
        close = datetime(2026, 9, 22, 13, 0, tzinfo=ET)
        orders = [entry("e1", "NVDA", "buy", "IN-NVDA-b-1"),
                  stop("s", "NVDA", "sell", 1, iso(12, 50), iso(13, 0), end_field="expired_at")]
        fills = [fill("NVDA", "buy", 1, iso(12, 40), "e1")]
        res = bgt.classify(OPEN, close, orders, fills, {"NVDA": 1.0}, cycles(end=close))
        self.assertEqual(res["positions"]["NVDA"]["class"], bgt.CLASS_DESIGN)

    def test_f6_holding_without_stop_is_by_design(self):
        orders = [entry("e1", "SPY", "buy", "F6-SPY-b-1")]
        fills = [fill("SPY", "buy", 1, iso(10, 0), "e1")]
        p = run(orders, fills, {"SPY": 1.0})["positions"]["SPY"]
        self.assertEqual((p["owners"], p["class"]), (["f6"], bgt.CLASS_DESIGN))

    def test_entry_at_1530_with_gtc_at_entry_is_covered(self):
        orders = [entry("e1", "NVDA", "buy", "IN-NVDA-b-1"),
                  stop("g", "NVDA", "sell", 1, iso(15, 35, frac=".500000"), None, status="new")]
        fills = [fill("NVDA", "buy", 1, iso(15, 35), "e1")]
        self.assertEqual(run(orders, fills, {"NVDA": 1.0})["positions"]["NVDA"]["class"], bgt.CLASS_COVERED)

    def test_compliance_detector(self):
        gt = {"status": "OK", "positions": {
            "SOFI": {"class": bgt.CLASS_DESIGN}, "GEV": {"class": bgt.CLASS_NAKED}}}
        rep = ("### CATASTROPHIC ALERT: 2\n* stop_protection | SOFI placed MISSING buy stop — naked | SOFI\n"
               "* GEV has no stop | GEV\n### VERDICT: FAIL\n### LOG ANOMALIES\n* SOFI naked\n")
        self.assertEqual(bgt.naked_claims_on_cleared(rep, gt), ["SOFI"])
        # NEW BUGS self-reports are the permitted route — not counted
        nb = "### NEW BUGS FOUND\n* stop_protection | SOFI resubmit FAILED, 4 shares unprotected\n"
        self.assertEqual(bgt.naked_claims_on_cleared(nb, gt), [])
        self.assertEqual(bgt.naked_claims_on_cleared(rep, dict(gt, status="UNKNOWN")), [])

    def test_sweep_stop_at_1552_is_still_design(self):
        orders = [entry("e1", "PLTR", "buy", "IN-PLTR-b-1"),
                  stop("s1", "PLTR", "sell", 1, iso(15, 52), iso(16, 0), end_field="expired_at")]
        fills = [fill("PLTR", "buy", 1, iso(14, 57), "e1")]
        p = run(orders, fills, {"PLTR": 1.0})["positions"]["PLTR"]
        self.assertEqual((p["class"], p["lapsed_min"], p["uncovered_at_close"]),
                         (bgt.CLASS_DESIGN, 0, False))

    def test_exit_cancelled_stop_at_1558_never_filled_is_lapsed(self):
        orders = [entry("e1", "SOFI", "sell", "IN-SOFI-s-1"),
                  stop("s1", "SOFI", "buy", 4, iso(15, 50), iso(15, 58))]
        fills = [fill("SOFI", "sell", 4, iso(12, 53), "e1")]
        gt = ok(run(orders, fills, {"SOFI": -4.0}))
        p = gt["positions"]["SOFI"]
        self.assertEqual((p["class"], p["uncovered_at_close"]), (bgt.CLASS_LAPSED, True))
        self.assertEqual(bgt.alarm_findings(gt)[0]["severity"], "high")

    def test_day_tier_oco_leg_counts_from_created_at(self):
        # AMZN 9/23: OCO stop leg created at entry; submitted_at is not used.
        parent = {"id": "p1", "symbol": "AMZN", "side": "buy", "type": "limit", "qty": "5",
                  "status": "canceled", "created_at": iso(12, 56), "canceled_at": iso(15, 30),
                  "client_order_id": "DT-AMZN-b-1",
                  "legs": [{"id": "leg1", "symbol": "AMZN", "side": "buy", "type": "stop", "qty": "5",
                            "status": "filled", "created_at": iso(12, 56), "submitted_at": iso(15, 29),
                            "filled_at": iso(15, 30), "client_order_id": "uuid-leg"}]}
        orders = [entry("e1", "AMZN", "sell", "DT-AMZN-s-1"), parent]
        fills = [fill("AMZN", "sell", 5, iso(12, 56), "e1"), fill("AMZN", "buy", 5, iso(15, 30), "leg1")]
        p = run(orders, fills, {})["positions"]["AMZN"]
        self.assertEqual((p["class"], p["owners"]), (bgt.CLASS_COVERED, ["day"]))

    def test_old_qhm_gtc_stop_counts(self):
        orders = [stop("q", "GE", "sell", 1, iso(10, 17, day=27, month=7), None,
                       coid="QH-GE-s-1", status="new")]
        p = run(orders, [], {"GE": 1.0})["positions"]["GE"]
        self.assertEqual((p["class"], p["owners"]), (bgt.CLASS_COVERED, ["qhm"]))

    def test_multiple_stops_sum_to_cover(self):
        orders = [stop("a", "GE", "sell", 6, iso(10, 0, day=1), None, coid="QH-GE-s-1", status="new"),
                  stop("b", "GE", "sell", 4, iso(10, 0, day=1), None, coid="QH-GE-s-2", status="new")]
        self.assertEqual(run(orders, [], {"GE": 10.0})["positions"]["GE"]["class"], bgt.CLASS_COVERED)

    def test_non_core_uncovered_is_naked_critical(self):
        orders = [stop("q1", "GEV", "sell", 1, iso(10, 0, day=1), iso(12, 3), coid="QH-GEV-s-1"),
                  stop("q2", "GEV", "sell", 1, iso(13, 7), None, coid="QH-GEV-s-2", status="new")]
        gt = ok(run(orders, [], {"GEV": 1.0}))
        p = gt["positions"]["GEV"]
        self.assertEqual((p["class"], p["uncovered_min"]), (bgt.CLASS_NAKED, 64))
        self.assertEqual(bgt.alarm_findings(gt)[0]["severity"], "critical")

    def test_carried_with_no_stop_ever_is_naked(self):
        p = run([], [], {"XYZ": 3.0})["positions"]["XYZ"]
        self.assertEqual((p["class"], p["owners"]), (bgt.CLASS_NAKED, ["other"]))

    def test_carried_owner_from_first_in_session_stop_when_none_before_open(self):
        orders = [stop("d", "AMD", "sell", 2, iso(9, 34), iso(16, 0), end_field="expired_at")]
        p = run(orders, [], {"AMD": 2.0})["positions"]["AMD"]
        self.assertEqual((p["owners"], p["class"]), (["core"], bgt.CLASS_DESIGN))

    def test_carried_owner_is_the_stop_going_into_the_open(self):
        orders = [stop("dt", "AMD", "sell", 1, iso(10, 0, day=21), iso(11, 0, day=21), coid="DT-AMD-s-1"),
                  stop("g", "AMD", "sell", 2, iso(16, 29, day=21), iso(9, 20)),
                  stop("d", "AMD", "sell", 2, iso(9, 36), iso(16, 0), end_field="expired_at")]
        p = run(orders, [], {"AMD": 2.0})["positions"]["AMD"]
        self.assertEqual((p["owners"], p["class"]), (["core"], bgt.CLASS_DESIGN))

    def test_carried_with_two_tiers_overnight_keeps_both_owners(self):
        orders = [stop("q", "AMD", "sell", 1, iso(16, 29, day=21), iso(9, 20), coid="QH-AMD-s-1"),
                  stop("g", "AMD", "sell", 2, iso(16, 30, day=21), iso(9, 20)),
                  stop("d", "AMD", "sell", 3, iso(9, 36), iso(16, 0), end_field="expired_at")]
        p = run(orders, [], {"AMD": 3.0})["positions"]["AMD"]
        self.assertEqual((p["owners"], p["class"]), (["core", "qhm"], bgt.CLASS_NAKED))

    def test_stale_core_stop_does_not_relabel_a_qhm_holding(self):
        # cold-2nd r6 threat 1: QHM stop alive overnight + an expired core stop from yesterday
        orders = [stop("q1", "GEV", "sell", 10, iso(10, 0, day=27, month=7), iso(9, 25), coid="QH-GEV-s-1"),
                  stop("q2", "GEV", "sell", 10, iso(9, 40), None, coid="QH-GEV-s-2", status="new"),
                  stop("c", "GEV", "sell", 10, iso(15, 47, day=21), iso(15, 58, day=21))]
        p = run(orders, [], {"GEV": 10.0})["positions"]["GEV"]
        self.assertEqual((p["owners"], p["class"]), (["qhm"], bgt.CLASS_NAKED))

    def test_no_stop_alive_overnight_unions_all_prior_tiers(self):
        orders = [stop("q", "GEV", "sell", 1, iso(10, 0, day=21), iso(12, 0, day=21), coid="QH-GEV-s-1"),
                  stop("c", "GEV", "sell", 1, iso(13, 0, day=21), iso(14, 0, day=21))]
        p = run(orders, [], {"GEV": 1.0})["positions"]["GEV"]
        self.assertEqual((p["owners"], p["class"]), (["core", "qhm"], bgt.CLASS_NAKED))

    def test_mid_session_window_keys_design_rules_off_the_real_close(self):
        # a fresh core entry at 12:53 evaluated at 13:30 is still inside its by-design window
        orders = [entry("e1", "SOFI", "sell", "IN-SOFI-s-1")]
        fills = [fill("SOFI", "sell", 4, iso(12, 53), "e1")]
        mid = datetime(2026, 9, 22, 13, 30, tzinfo=ET)
        res = bgt.classify(OPEN, CLOSE, orders, fills, {"SOFI": -4.0},
                           [c for c in cycles() if c <= mid], window_end=mid)
        p = res["positions"]["SOFI"]
        self.assertEqual((p["class"], p["lapsed_min"], p["uncovered_at_close"]), (bgt.CLASS_DESIGN, 0, True))

    def test_adding_day_tier_shares_to_core_holding_is_not_core_only(self):
        orders = [entry("e1", "AMD", "buy", "IN-AMD-b-1"), entry("e2", "AMD", "buy", "DT-AMD-b-1")]
        fills = [fill("AMD", "buy", 1, iso(10, 0), "e1"), fill("AMD", "buy", 1, iso(11, 0), "e2")]
        p = run(orders, fills, {"AMD": 2.0})["positions"]["AMD"]
        self.assertEqual((p["owners"], p["class"]), (["core", "day"], bgt.CLASS_NAKED))

    def test_flip_long_to_short_starts_new_holding(self):
        orders = [entry("e1", "AMD", "buy", "IN-AMD-b-1"), entry("e2", "AMD", "sell", "DT-AMD-s-1"),
                  stop("s1", "AMD", "sell", 1, iso(10, 0), iso(11, 0), coid="IN-AMD-s-1")]
        fills = [fill("AMD", "buy", 1, iso(10, 0), "e1"), fill("AMD", "sell", 2, iso(11, 0), "e2")]
        p = run(orders, fills, {"AMD": -1.0})["positions"]["AMD"]
        self.assertEqual((p["owners"], p["class"]), (["core", "day"], bgt.CLASS_NAKED))

    def test_unreadable_cycle_log_is_unknown_not_design(self):
        orders = [entry("e1", "SOFI", "sell", "IN-SOFI-s-1")]
        fills = [fill("SOFI", "sell", 4, iso(12, 53), "e1")]
        gt = ok(run(orders, fills, {"SOFI": -4.0}, cyc=None))
        self.assertEqual(gt["cycle_log"], "UNKNOWN")
        self.assertEqual(gt["positions"]["SOFI"]["class"], bgt.CLASS_UNKNOWN)
        titles = [a["title"] for a in bgt.alarm_findings(gt)]
        self.assertTrue(any("cycle log UNREADABLE" in t for t in titles))

    def test_unreadable_cycle_log_alarms_even_when_everything_is_covered(self):
        orders = [stop("q", "GE", "sell", 1, iso(10, 0, day=1), None, coid="QH-GE-s-1", status="new")]
        gt = ok(run(orders, [], {"GE": 1.0}, cyc=None))
        self.assertEqual([a["severity"] for a in bgt.alarm_findings(gt)], ["high"])

    def test_cycle_gap_class_is_critical_and_stall_alarm_emitted(self):
        orders = [entry("e1", "SOFI", "sell", "IN-SOFI-s-1")]
        fills = [fill("SOFI", "sell", 4, iso(12, 0), "e1")]
        cyc = [c for c in cycles() if not (datetime(2026, 9, 22, 12, 30, tzinfo=ET)
                                           < c < datetime(2026, 9, 22, 13, 30, tzinfo=ET))]
        gt = ok(run(orders, fills, {"SOFI": -4.0}, cyc=cyc))
        self.assertEqual(gt["positions"]["SOFI"]["class"], bgt.CLASS_GAP)
        al = bgt.alarm_findings(gt)
        self.assertIn("critical", [a["severity"] for a in al])
        self.assertTrue(any(a["title"].startswith("Bot loop stalled") for a in al))

    def test_stall_alone_is_alarmed(self):
        orders = [stop("q", "GE", "sell", 1, iso(10, 0, day=1), None, coid="QH-GE-s-1", status="new")]
        cyc = [c for c in cycles() if c < datetime(2026, 9, 22, 11, 0, tzinfo=ET)]
        gt = ok(run(orders, [], {"GE": 1.0}, cyc=cyc))
        self.assertEqual([a["severity"] for a in bgt.alarm_findings(gt)], ["high"])

    def test_partial_qty_stop_is_not_coverage(self):
        orders = [stop("q", "LLY", "sell", 1, iso(10, 0, day=1), None, coid="QH-LLY-s-1", status="new")]
        self.assertEqual(run(orders, [], {"LLY": 2.0})["positions"]["LLY"]["class"], bgt.CLASS_NAKED)

    def test_wrong_side_rejected_and_ended_without_time_are_not_coverage(self):
        orders = [stop("a", "LLY", "buy", 2, iso(10, 0, day=1), None, coid="QH-LLY-b-1", status="new"),
                  stop("b", "LLY", "sell", 2, iso(9, 0), None, coid="QH-LLY-s-1", status="rejected"),
                  stop("c", "LLY", "sell", 2, iso(9, 0, day=1), None, coid="QH-LLY-s-2", status="canceled")]
        p = run(orders, [], {"LLY": 2.0})["positions"]["LLY"]
        self.assertEqual((p["class"], p["rejected_stop_orders"]), (bgt.CLASS_NAKED, 1))

    def test_unparseable_fill_time_raises(self):
        with self.assertRaises(ValueError):
            run([], [fill("A", "buy", 1, "garbage", "x")], {"A": 1.0})

    def test_tolerance_boundary(self):
        for frac, want in ((".000000", bgt.CLASS_COVERED), (".060000", bgt.CLASS_NAKED)):
            orders = [stop("q", "GE", "sell", 1, iso(9, 32, frac=frac), None, coid="QH-GE-s-1",
                           status="new")]
            self.assertEqual(run(orders, [], {"GE": 1.0})["positions"]["GE"]["class"], want, frac)


class TestCycleGaps(unittest.TestCase):
    def test_no_cycles_is_whole_session(self):
        self.assertEqual(bgt.cycle_gaps([], OPEN, CLOSE), [(OPEN, CLOSE)])

    def test_threshold_is_strictly_greater(self):
        at15 = [OPEN + timedelta(minutes=15 * i) for i in range(27)]
        self.assertEqual(bgt.cycle_gaps(at15, OPEN, CLOSE), [])
        at16 = [OPEN + timedelta(minutes=16 * i) for i in range(25)]
        self.assertTrue(bgt.cycle_gaps(at16, OPEN, CLOSE))

    def test_read_cycle_times(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mtf_bot.log"
            p.write_text("2026-09-22 14:00:01,5 | INFO | x | [CYCLE] duration=30s\n"
                         "garbage [CYCLE] duration=1s\n"
                         "2026-09-22 14:07:01,5 | INFO | x | other line\n")
            self.assertEqual(bgt.read_cycle_times(p), [datetime(2026, 9, 22, 14, 0, 1, tzinfo=UTC)])
            self.assertIsNone(bgt.read_cycle_times(Path(d) / "missing.log"))


class TestRenderAndCollect(unittest.TestCase):
    def test_render_and_summary(self):
        orders = [stop("q", "GE", "sell", 1, iso(10, 0, day=1), None, coid="QH-GE-s-1", status="new")]
        gt = dict(ok(run(orders, [], {"GE": 1.0})), session="2026-09-22", window_et="09:30-16:00")
        self.assertIn("GE", bgt.render(gt))
        self.assertIn("COVERED", bgt.render(gt))
        self.assertEqual(bgt.checked_summary(gt), "GE covered")
        self.assertIn("no trading session", bgt.render({"status": "NO_SESSION", "session": "x"}))

    def test_not_ok_status_is_one_high_alarm(self):
        gt = {"status": "UNKNOWN", "reason": "boom", "positions": {}}
        self.assertEqual([a["severity"] for a in bgt.alarm_findings(gt)], ["high"])
        self.assertIn("UNKNOWN", bgt.render(gt))
        # C3 (adversarial r8): the real outage path must still permit a quoted self-report
        self.assertIn("broker-unverified", bgt.render(gt))
        self.assertNotIn("do not assert 'naked'", bgt.render(gt))

    def test_collect_failure_is_unknown(self):
        with mock.patch.object(bgt, "_session_bounds", side_effect=RuntimeError("boom")):
            gt = bgt.collect("2026-09-22")
        self.assertEqual(gt["status"], "UNKNOWN")
        self.assertIn("boom", gt["reason"])

    def test_collect_no_session(self):
        with mock.patch.object(bgt, "_session_bounds", return_value=None):
            gt = bgt.collect("2026-09-20")
        self.assertEqual(gt["status"], "NO_SESSION")
        self.assertEqual(bgt.alarm_findings(gt), [])

    def test_collect_before_open_is_no_session(self):
        with mock.patch.object(bgt, "_session_bounds", return_value=(OPEN, CLOSE)):
            gt = bgt.collect("2026-09-22", now=OPEN - timedelta(minutes=1))
        self.assertEqual(gt["status"], "NO_SESSION")

    def test_collect_caps_window_at_now(self):
        seen = {}

        def fake_classify(o, c, *a, window_end=None):
            seen["close"], seen["end"] = c, window_end
            return {"positions": {}, "cycle_log": "OK", "cycle_gaps_et": []}
        now = datetime(2026, 9, 22, 13, 30, tzinfo=ET)
        with mock.patch.object(bgt, "_session_bounds", return_value=(OPEN, CLOSE)), \
             mock.patch("reporting.pnl_ledger._get_json", return_value=[]), \
             mock.patch.object(bgt, "_fetch_orders", return_value=[]), \
             mock.patch.object(bgt, "_fetch_fills_since", return_value=[]), \
             mock.patch.object(bgt, "read_cycle_times", return_value=[]), \
             mock.patch.object(bgt, "classify", side_effect=fake_classify):
            gt = bgt.collect("2026-09-22", now=now)
        self.assertEqual(gt["status"], "OK")
        self.assertEqual((seen["close"], seen["end"]), (CLOSE, now))


class TestCardAlarms(unittest.TestCase):
    def _gt(self, classes):
        return {"status": "OK", "cycle_log": "OK", "cycle_gaps_et": [], "positions": {
            s: {"class": c, "uncovered_min": 5, "exposed_min": 100, "lapsed_min": 5,
                "uncovered_windows_et": ["10:00-10:05"], "owners": ["core"], "uncovered_at_close": False}
            for s, c in classes.items()}}

    def test_lows_go_to_the_footer_not_the_card(self):
        loud, low = bgt.card_alarms(self._gt({"MARA": bgt.CLASS_LAPSED, "GEV": bgt.CLASS_NAKED}))
        self.assertEqual([a["severity"] for a in loud], ["critical"])
        self.assertIn("MARA", low)

    def test_cap_keeps_criticals_first(self):
        classes = {f"S{i}": bgt.CLASS_NAKED for i in range(3)}
        classes.update({f"H{i}": bgt.CLASS_UNKNOWN for i in range(9)})
        loud, _ = bgt.card_alarms(self._gt(classes))
        self.assertEqual(len(loud), bgt._CARD_ALARM_CAP)
        self.assertEqual([a["severity"] for a in loud[:3]], ["critical"] * 3)
        self.assertTrue(loud[-1]["title"].startswith("+5 more"))

    def test_card_keeps_llm_lows_inline(self):
        from scripts.audit_slack import render_card
        _loud, low = bgt.card_alarms(self._gt({"MARA": bgt.CLASS_LAPSED}))
        llm = [{"severity": "low", "title": "a", "detail": "tiny log typo"},
               {"severity": "low", "title": "b", "detail": "stale comment"}]
        pnl = {"today": [], "source_note": "", "lifetime": [], "injected_numbers": []}
        card = render_card("nightly", "2026-09-22", "PASS", pnl, _loud + llm)
        self.assertIn("tiny log typo", str(card["blocks"]))
        self.assertIn("MARA", low)


class TestFetchPaging(unittest.TestCase):
    def test_fill_race_during_snapshot_is_unknown(self):
        with mock.patch.object(bgt, "_session_bounds", return_value=(OPEN, CLOSE)), \
             mock.patch("reporting.pnl_ledger._get_json", return_value=[]), \
             mock.patch.object(bgt, "_fetch_fills_since", side_effect=[[], [{"id": "f"}]]):
            gt = bgt.collect("2026-09-22", now=CLOSE + timedelta(minutes=5))
        self.assertEqual(gt["status"], "UNKNOWN")
        self.assertIn("fills changed", gt["reason"])

    def test_orders_truncation_raises(self):
        page = [{"id": str(i), "created_at": "2026-09-22T14:00:00Z"} for i in range(500)]
        with mock.patch("reporting.pnl_ledger._get_json", side_effect=[page, page]):
            with self.assertRaises(RuntimeError):
                bgt._fetch_orders(OPEN, CLOSE)

    def test_page_with_legs_counting_to_the_limit_keeps_paging(self):
        # D1 (adversarial r8): 494 orders + 6 OCO legs IS a full page — must fetch the next one
        p1 = [{"id": f"a{i}", "created_at": "2026-09-22T14:00:00Z"} for i in range(494)]
        p1[0]["legs"] = [{"id": f"l{i}"} for i in range(6)]
        p2 = [{"id": "old", "created_at": "2026-07-27T14:00:00Z"}]
        with mock.patch("reporting.pnl_ledger._get_json", side_effect=[p1, p2, [], []]) as g:
            out = bgt._fetch_orders(OPEN, CLOSE)
        self.assertIn("old", {o["id"] for o in out})
        self.assertEqual(g.call_count, 4)          # 3 history pages (last = empty) + open orders

    def test_cursor_uses_submitted_at(self):
        # adversarial r9: an overnight GTC is created hours before it is submitted; Alpaca's `until`
        # filters on submitted_at, so the cursor must too
        p1 = [{"id": "ms", "created_at": "2026-07-08T06:00:38.50Z", "submitted_at": "2026-07-08T08:00:45.25Z"}]
        with mock.patch("reporting.pnl_ledger._get_json", side_effect=[p1, [], []]) as g:
            bgt._fetch_orders(OPEN, CLOSE)
        self.assertIn("until=2026-07-08T08:00:45.251", g.call_args_list[1][0][0])

    def test_short_middle_page_does_not_end_the_history(self):
        # cold-2nd r9 nit: a page that counts < 500 is NOT proof of the end — keep paging
        p1 = [{"id": f"a{i}", "created_at": "2026-09-22T14:00:00Z"} for i in range(499)]
        p2 = [{"id": "old", "created_at": "2026-07-27T14:00:00Z"}]
        with mock.patch("reporting.pnl_ledger._get_json", side_effect=[p1, p2, p2, []]):
            out = bgt._fetch_orders(OPEN, CLOSE)
        self.assertIn("old", {o["id"] for o in out})

    def test_open_orders_at_limit_raises(self):
        with mock.patch("reporting.pnl_ledger._get_json", side_effect=[[], [{"id": str(i)} for i in range(500)]]):
            with self.assertRaises(RuntimeError):
                bgt._fetch_orders(OPEN, CLOSE)

    def test_fills_pages_until_short_page(self):
        p1 = [{"id": str(i)} for i in range(100)]
        with mock.patch("reporting.pnl_ledger._get_json", side_effect=[p1, [{"id": "x"}]]) as g:
            self.assertEqual(len(bgt._fetch_fills_since(OPEN)), 101)
        self.assertIn("page_token=99", g.call_args_list[1][0][0])


if __name__ == "__main__":
    unittest.main()
