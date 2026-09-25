# ruff: noqa: E501 — fixtures mirror real broker payloads / log lines on one line
"""midday_audit × broker ground truth (increment 2).

Real cases: 2026-09-23 midday "NAKED AMZN" and 2026-09-24 "NAKED GOOGL" were day-tier positions protected by OCO stop
LEGS the old un-nested open-orders read never saw; 2026-09-22 SOFI was a core entry (software stop by design)."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import midday_audit as ma


_SLACK_GUARDS: list = []


def setUpModule():
    # No test in this file may reach the live Slack channel (cold-2nd r7: a main() test with a
    # readable positions list took the Block Kit card path and POSTED a real card via the worktree
    # .env webhook). Both post paths are blocked module-wide; tests that assert on the legacy text
    # still patch ma._slack themselves.
    from scripts import audit_slack
    _SLACK_GUARDS[:] = [mock.patch.object(ma, "SLACK_WEBHOOK", ""),
                        mock.patch.object(audit_slack, "post_to_slack",
                                          side_effect=AssertionError("test attempted a real Slack post"))]
    for g in _SLACK_GUARDS:
        g.start()


def tearDownModule():
    for g in _SLACK_GUARDS:
        g.stop()


def gt_ok(positions):
    return {"status": "OK", "session": "2026-09-24", "window_pt": "06:30-10:30", "cycle_log": "OK",
            "cycle_gaps_et": [], "cycle_gaps_pt": [], "positions": positions}


def pos(sym, side="long", qty=2, price=100.0):
    return {"symbol": sym, "side": side, "qty": str(qty), "current_price": str(price)}


class TestSnapshotUncovered(unittest.TestCase):
    def test_oco_stop_leg_counts_as_coverage(self):
        # GOOGL 09-24 / AMZN 09-23: the stop is a LEG of the OCO limit parent
        orders = [{"symbol": "GOOGL", "side": "buy", "type": "limit", "qty": "3",
                   "legs": [{"symbol": "GOOGL", "side": "buy", "type": "stop", "qty": "3"}]}]
        self.assertEqual(ma.snapshot_uncovered([pos("GOOGL", "short", 3)], orders), [])

    def test_missing_or_partial_stop_is_listed(self):
        orders = [{"symbol": "GE", "side": "sell", "type": "stop", "qty": "1"}]
        out = ma.snapshot_uncovered([pos("GE", qty=2), pos("LLY")], orders)
        self.assertEqual([(u["symbol"], u["stop_qty"]) for u in out], [("GE", 1.0), ("LLY", 0.0)])

    def test_wrong_side_is_not_coverage(self):
        orders = [{"symbol": "GE", "side": "buy", "type": "stop", "qty": "2"}]
        self.assertEqual(len(ma.snapshot_uncovered([pos("GE")], orders)), 1)

    def test_failed_reads_return_none(self):
        self.assertIsNone(ma.snapshot_uncovered(None, {}))
        self.assertIsNone(ma.snapshot_uncovered([], None))


class TestSoftwareStopFindings(unittest.TestCase):
    DESIGN = {"class": "SOFTWARE-ONLY-BY-DESIGN"}

    def test_self_report_on_held_by_design_symbol_is_critical(self):
        rep = {"SOFI": ["2026-09-24 17:05:10 | ERROR | x | [SOFI] exit order FAILED: rejected"]}
        uncovered = dict(self.DESIGN, uncovered_at_close=True)
        f, unk = ma.software_stop_findings(gt_ok({"SOFI": uncovered}), [pos("SOFI", "short", 4, 17.0)],
                                           {"SOFI": {"direction": "short", "stop": 18.0}}, rep)
        self.assertEqual([x["severity"] for x in f], ["critical"])
        self.assertIn("FAILED", f[0]["detail"])

    def test_repaired_failure_with_broker_stop_resting_is_high_not_critical(self):
        # adversarial B1 (SNOW 2026-08-10): the failure was repaired — a broker stop rests at the check
        rep = {"SNOW": ["2026-08-10 13:31:00 | WARNING | x | [SNOW] DAY stop attempt failed — retrying"]}
        covered_now = dict(self.DESIGN, uncovered_at_close=False)
        f, _ = ma.software_stop_findings(gt_ok({"SNOW": covered_now}), [pos("SNOW", price=315.0)], {}, rep)
        self.assertEqual([x["severity"] for x in f], ["high"])
        self.assertIn("broker stop resting", f[0]["title"])

    def test_self_report_never_lowers_a_breach_critical(self):
        # cold-2nd r6: breach recorded by the tracker is CRITICAL with or without a self-report
        covered_now = dict(self.DESIGN, uncovered_at_close=False)
        trades = {"SNOW": {"direction": "long", "stop": 320.0, "stop_breached": True}}
        rep = {"SNOW": ["[SNOW] exit order FAILED: rejected"]}
        for r in ({}, rep):
            f, _ = ma.software_stop_findings(gt_ok({"SNOW": covered_now}), [pos("SNOW", price=315.0)], trades, r)
            self.assertIn("critical", [x["severity"] for x in f])

    def test_mark_beyond_stop_high_or_critical_when_tracker_saw_breach(self):
        trades = {"NFLX": {"direction": "short", "stop": 75.0, "trail_stop": None}}
        f, _ = ma.software_stop_findings(gt_ok({"NFLX": self.DESIGN}), [pos("NFLX", "short", 2, 76.0)], trades, {})
        self.assertEqual([x["severity"] for x in f], ["high"])
        trades["NFLX"]["stop_breached"] = True
        f, _ = ma.software_stop_findings(gt_ok({"NFLX": self.DESIGN}), [pos("NFLX", "short", 2, 76.0)], trades, {})
        self.assertEqual([x["severity"] for x in f], ["critical"])

    def test_long_uses_the_tighter_trail_stop(self):
        trades = {"INTC": {"direction": "long", "stop": 100.0, "trail_stop": 110.0}}
        f, _ = ma.software_stop_findings(gt_ok({"INTC": self.DESIGN}), [pos("INTC", price=105.0)], trades, {})
        self.assertEqual(len(f), 1)
        f, _ = ma.software_stop_findings(gt_ok({"INTC": self.DESIGN}), [pos("INTC", price=111.0)], trades, {})
        self.assertEqual(f, [])

    def test_no_tracker_stop_is_reported_unknown_never_cleared_silently(self):
        f, unk = ma.software_stop_findings(gt_ok({"PLTR": self.DESIGN}), [pos("PLTR")], {}, {})
        self.assertEqual((f, unk), ([], ["PLTR"]))
        f, unk = ma.software_stop_findings(gt_ok({"PLTR": self.DESIGN}), [pos("PLTR")], None, {})
        self.assertEqual(unk, ["PLTR"])

    def test_other_classes_unheld_symbols_and_not_ok_are_ignored(self):
        trades = {"GE": {"direction": "long", "stop": 200.0}}
        f, unk = ma.software_stop_findings(gt_ok({"GE": {"class": "COVERED"}}), [pos("GE", price=100)], trades, {})
        self.assertEqual((f, unk), ([], []))
        f, unk = ma.software_stop_findings(gt_ok({"GE": self.DESIGN}), [], trades, {})
        self.assertEqual((f, unk), ([], []))
        f, unk = ma.software_stop_findings({"status": "UNKNOWN"}, [pos("GE", price=100)], trades, {})
        self.assertEqual((f, unk), ([], []))


class TestPositionsReadFailed(unittest.TestCase):
    """cold-2nd r2 FAIL: live positions read returns None while the ground truth is OK — the held set
    must fall back to the ground truth's own snapshot, never silence the checks."""
    @staticmethod
    def _rec(cls, owners, at_close, rejected=0):
        return {"class": cls, "owners": owners, "exposed_min": 60, "uncovered_min": 60 if at_close else 0,
                "lapsed_min": 0, "uncovered_at_close": at_close, "uncovered_windows_et": [],
                "uncovered_windows_pt": [], "rejected_stop_orders": rejected}

    G = gt_ok({"SOFI": _rec.__func__("SOFTWARE-ONLY-BY-DESIGN", ["core"], True),
               "GOOGL": _rec.__func__("COVERED", ["day"], True, 1),
               "AMD": _rec.__func__("SOFTWARE-ONLY-BY-DESIGN", ["core"], False)})

    def test_held_symbols_fallback(self):
        self.assertEqual(ma.held_symbols(self.G, None), {"SOFI", "GOOGL"})
        # live read is OLDER than the GT snapshot: it may only add symbols, never filter one out
        self.assertEqual(ma.held_symbols(self.G, []), {"SOFI", "GOOGL"})
        self.assertEqual(ma.held_symbols(self.G, [pos("AMD")]), {"SOFI", "GOOGL", "AMD"})

    def test_fill_after_positions_read_is_not_silenced(self):
        # cold-2nd r3: positions read 13:30:00 (AMD only), GOOGL day-tier fill 13:30:05 with REJECTED stop
        f = ma.uncovered_now_findings(self.G, [pos("AMD")])
        self.assertEqual([(x["title"].split()[0], x["severity"]) for x in f], [("GOOGL", "critical")])
        rep = {"SOFI": ["[SOFI] Hard stop close_position() FAILED — position may be naked"]}
        f, _ = ma.software_stop_findings(self.G, [pos("AMD")], {}, rep)
        self.assertEqual([x["severity"] for x in f], ["critical"])

    def test_self_report_still_critical_and_mark_check_unknown(self):
        rep = {"SOFI": ["[SOFI] Hard stop close_position() FAILED — position may be naked"]}
        f, unk = ma.software_stop_findings(self.G, None, {"SOFI": {"direction": "short", "stop": 18.0}}, rep)
        self.assertEqual([x["severity"] for x in f], ["critical"])
        f, unk = ma.software_stop_findings(self.G, None, {"SOFI": {"direction": "short", "stop": 18.0}}, {})
        self.assertEqual((f, unk), ([], ["SOFI"]))          # no mark → never cleared silently

    def test_uncovered_now_still_fires(self):
        f = ma.uncovered_now_findings(self.G, None)
        self.assertEqual([(x["title"].split()[0], x["severity"]) for x in f], [("GOOGL", "critical")])

    def test_main_adds_visible_high_alarm_and_names_the_positions(self):
        rep = {"SOFI": ["2026-09-24 17:05:10 | ERROR | x | [SOFI] Hard stop close_position() FAILED — position may be naked"]}
        sent = []
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ma, "LOGS_DIR", Path(d)), \
             mock.patch.object(ma, "REPORT_PATH", Path(d) / "r.json"), \
             mock.patch.object(ma, "read_today_trade_events", return_value=[]), \
             mock.patch.object(ma, "read_bot_log_tail", return_value=[]), \
             mock.patch.object(ma, "run_signal_postmortem", return_value=[]), \
             mock.patch.object(ma, "_fetch_live_positions", return_value=None), \
             mock.patch.object(ma, "_fetch_today_fills", return_value=[]), \
             mock.patch.object(ma, "collect_ground_truth", return_value=self.G), \
             mock.patch.object(ma, "scan_self_reports", return_value=rep), \
             mock.patch.object(ma, "_read_open_trades", return_value={}), \
             mock.patch.object(ma, "GEMINI_API_KEY", ""), \
             mock.patch.object(ma, "_slack", side_effect=lambda t, b, emoji="": sent.append((t, b))):
            ma.main()
            report = __import__("json").loads((Path(d) / "r.json").read_text())
        titles = [x["title"] for x in report["stop_coverage"]["alarms"]]
        self.assertTrue(any("Live positions unreadable" in t for t in titles))
        self.assertTrue(any(t.startswith("SOFI") for t in titles))
        self.assertTrue(any(t.startswith("GOOGL") for t in titles))
        self.assertIn("BROKER STOP CHECK", sent[0][1])


class TestSelfReportFloor(unittest.TestCase):
    def test_carried_position_scan_starts_at_todays_open_not_entry_day(self):
        # adversarial B1: a carried position's entry is days old — prior evenings' repaired failures
        # must not count; the scan floor is max(entry, today's session open)
        g = gt_ok({"SNOW": {"class": "SOFTWARE-ONLY-BY-DESIGN", "owners": ["core"], "exposed_min": 60,
                            "uncovered_min": 2, "lapsed_min": 0, "uncovered_at_close": False,
                            "uncovered_windows_et": [], "uncovered_windows_pt": [], "rejected_stop_orders": 0}})
        seen = {}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ma, "LOGS_DIR", Path(d)), \
             mock.patch.object(ma, "REPORT_PATH", Path(d) / "r.json"), \
             mock.patch.object(ma, "read_today_trade_events", return_value=[]), \
             mock.patch.object(ma, "read_bot_log_tail", return_value=[]), \
             mock.patch.object(ma, "run_signal_postmortem", return_value=[]), \
             mock.patch.object(ma, "_fetch_live_positions", return_value=[pos("SNOW", price=315.0)]), \
             mock.patch.object(ma, "_fetch_today_fills", return_value=[]), \
             mock.patch.object(ma, "collect_ground_truth", return_value=g), \
             mock.patch.object(ma, "scan_self_reports", side_effect=lambda since: seen.update(since) or {}), \
             mock.patch.object(ma, "_read_open_trades",
                               return_value={"SNOW": {"entry_time": "2026-08-01T07:00:00-07:00", "stop": 300.0}}), \
             mock.patch.object(ma, "GEMINI_API_KEY", ""), \
             mock.patch.object(ma, "_slack", side_effect=lambda t, b, emoji="": None):
            ma.main()
        open_utc = datetime.now(ma.ET).replace(hour=9, minute=30, second=0, microsecond=0).astimezone(ma.UTC)
        self.assertEqual(seen["SNOW"], open_utc)


class TestSelfReportScan(unittest.TestCase):
    def _scan(self, text, since):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mtf_bot.log"
            p.write_text(text)
            return ma.scan_self_reports(since, p)

    def test_only_warning_plus_stop_and_failure_words_after_entry(self):
        since = {"SOFI": datetime(2026, 9, 24, 14, 0, tzinfo=ma.UTC)}
        log = ("2026-09-24 13:00:00,1 | ERROR    | x | [SOFI] Hard stop close_position() FAILED — position may be naked\n"
               "2026-09-24 14:05:00,1 | INFO     | x | [SOFI] Short skipped — live shorting pre-flight failed\n"
               "2026-09-24 14:06:00,1 | WARNING  | x | [SOFI] GTC limit partial failed\n"
               "2026-09-24 14:07:00,1 | ERROR    | x | [SOFI] Hard stop close_position() FAILED — position may be naked\n"
               "2026-09-24 14:08:00,1 | ERROR    | x | [SOFIX] exit FAILED\n")
        out = self._scan(log, since)
        self.assertEqual(len(out["SOFI"]), 1)          # 13:00 is before entry; INFO/no stop-word/other symbol excluded
        self.assertIn("14:07:00", out["SOFI"][0])

    def test_entry_side_fail_closed_lines_are_not_exit_failures(self):
        since = {"DRAM": datetime(2026, 9, 24, 13, 30, tzinfo=ma.UTC)}
        log = ("2026-09-24 14:00:00,1 | WARNING  | x | [DRAM] No daily data — long/short entry blocked (fail-closed)\n"
               "2026-09-24 14:01:00,1 | WARNING  | x | [DRAM] PRICE SANITY FAIL vs prior close — skipping entry\n"
               "2026-09-24 14:02:00,1 | ERROR    | x | [DRAM] exit order FAILED: rejected\n"
               "2026-09-24 14:03:00,1 | ERROR    | x | [DRAM] #12c exit order submission failed — skipping entry.\n")
        out = self._scan(log, since)
        self.assertEqual(len(out["DRAM"]), 2)
        self.assertIn("exit order FAILED", out["DRAM"][0])
        self.assertIn("#12c exit order submission failed", out["DRAM"][1])

    def test_uncapped_beyond_2000_lines(self):
        since = {"SOFI": datetime(2026, 9, 24, 13, 30, tzinfo=ma.UTC)}
        early = "2026-09-24 13:35:00,1 | ERROR    | x | [SOFI] RTH DAY stop FAILED — position unprotected\n"
        filler = "".join(f"2026-09-24 15:{i // 60 % 60:02d}:{i % 60:02d},1 | INFO | x | noise\n" for i in range(3000))
        self.assertEqual(len(self._scan(early + filler, since)["SOFI"]), 1)

    def test_unreadable_log_returns_empty(self):
        out = ma.scan_self_reports({"SOFI": datetime(2026, 9, 24, tzinfo=ma.UTC)}, Path("/nonexistent/x.log"))
        self.assertEqual(out, {"SOFI": []})


class TestUncoveredNow(unittest.TestCase):
    def test_covered_but_uncovered_at_check_is_never_silent(self):
        # masked-loss R1: day-tier fill 13:29:10, stop REJECTED, checked 13:30:02 → within the 2-min tolerance
        g = gt_ok({"GOOGL": {"class": "COVERED", "uncovered_at_close": True, "rejected_stop_orders": 1,
                             "owners": ["day"]},
                   "INTC": {"class": "COVERED", "uncovered_at_close": True, "rejected_stop_orders": 0,
                            "owners": ["core"]},
                   "GE": {"class": "COVERED", "uncovered_at_close": False, "owners": ["qhm"]}})
        f = ma.uncovered_now_findings(g, [pos("GOOGL", "short", 3), pos("INTC"), pos("GE")])
        self.assertEqual({(x["title"].split()[0], x["severity"]) for x in f}, {("GOOGL", "critical"), ("INTC", "high")})
        # an empty (older) live read never filters out the GT's own uncovered-now symbols (cold-2nd r3)
        self.assertEqual(len(ma.uncovered_now_findings(g, [])), 2)
        self.assertEqual(ma.uncovered_now_findings({"status": "UNKNOWN"}, [pos("GOOGL")]), [])

    def test_day_holding_after_core_round_trip_is_never_silent(self):
        # cold-2nd r4: core GOOGL 10:00-12:00 (software stop by design, closed) then day-tier short 13:29:10
        # with its OCO stop REJECTED → classed BY-DESIGN (owners core+day) — must still alarm
        g = gt_ok({"GOOGL": {"class": "SOFTWARE-ONLY-BY-DESIGN", "owners": ["core", "day"],
                             "uncovered_at_close": True, "rejected_stop_orders": 1},
                   "SOFI": {"class": "SOFTWARE-ONLY-BY-DESIGN", "owners": ["core"],
                            "uncovered_at_close": True, "rejected_stop_orders": 0},
                   "AMD": {"class": "SOFTWARE-ONLY-BY-DESIGN", "owners": ["core", "day"],
                           "uncovered_at_close": True, "rejected_stop_orders": 0}})
        f = ma.uncovered_now_findings(g, [])
        self.assertEqual({(x["title"].split()[0], x["severity"]) for x in f},
                         {("GOOGL", "critical"), ("AMD", "high")})   # core-only BY-DESIGN SOFI: by design


class TestMainLegacyPath(unittest.TestCase):
    def test_ground_truth_alarms_reach_the_legacy_text_and_severity(self):
        # positions unreadable → card path raises → legacy text; the GT alarm must still be visible
        gt = {"status": "UNKNOWN", "reason": "HTTP 503", "positions": {}}
        sent = []
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ma, "LOGS_DIR", Path(d)), \
             mock.patch.object(ma, "REPORT_PATH", Path(d) / "r.json"), \
             mock.patch.object(ma, "read_today_trade_events", return_value=[]), \
             mock.patch.object(ma, "read_bot_log_tail", return_value=[]), \
             mock.patch.object(ma, "run_signal_postmortem", return_value=[]), \
             mock.patch.object(ma, "_fetch_live_positions", return_value=None), \
             mock.patch.object(ma, "_fetch_today_fills", return_value=[]), \
             mock.patch.object(ma, "_fetch_open_orders_nested", return_value=None), \
             mock.patch.object(ma, "collect_ground_truth", return_value=gt), \
             mock.patch.object(ma, "_read_open_trades", return_value={}), \
             mock.patch.object(ma, "GEMINI_API_KEY", ""), \
             mock.patch.object(ma, "_slack", side_effect=lambda t, b, emoji="": sent.append((t, b))):
            ma.main()
            report = __import__("json").loads((Path(d) / "r.json").read_text())
        self.assertIn("REVIEW", sent[0][0])
        self.assertIn("BROKER STOP CHECK", sent[0][1])
        self.assertIn("Degraded stop snapshot ALSO failed", sent[0][1])
        self.assertEqual(report["stop_coverage"]["schema"], "broker_ground_truth_v1")


class TestCollectRetries(unittest.TestCase):
    def test_retries_unknown_then_returns_ok(self):
        from reporting import broker_ground_truth as bgt
        seq = [{"status": "UNKNOWN", "reason": "fills changed"}, {"status": "OK", "positions": {}}]
        with mock.patch.object(bgt, "collect", side_effect=seq) as c, mock.patch("time.sleep") as sl:
            gt = ma.collect_ground_truth()
        self.assertEqual((gt["status"], c.call_count, sl.call_count), ("OK", 2, 1))

    def test_gives_up_unknown_after_all_attempts(self):
        from reporting import broker_ground_truth as bgt
        with mock.patch.object(bgt, "collect", return_value={"status": "UNKNOWN", "reason": "x"}) as c, \
             mock.patch("time.sleep"):
            gt = ma.collect_ground_truth()
        self.assertEqual((gt["status"], c.call_count), ("UNKNOWN", ma.GT_RETRIES))


class TestEscalation(unittest.TestCase):
    def test_only_raises(self):
        self.assertEqual(ma.escalate_card_verdict("PASS", [{"severity": "critical"}]), "FAIL")
        self.assertEqual(ma.escalate_card_verdict("PASS", [{"severity": "high"}]), "WARN")
        self.assertEqual(ma.escalate_card_verdict("FAIL", [{"severity": "high"}]), "FAIL")
        for v in ("PASS", "WARN", "FAIL", "UNKNOWN"):
            self.assertEqual(ma.escalate_card_verdict(v, [{"severity": "low"}]), v)
            self.assertEqual(ma.escalate_card_verdict(v, []), v)


class TestLogWindowIsUtc(unittest.TestCase):
    def test_cutoff_compares_utc_log_stamps(self):
        now = datetime.now(ma.UTC)
        recent = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        old = (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mtf_bot.log"
            p.write_text(f"{old},1 | INFO | old line\n{recent},1 | INFO | recent line\n")
            with mock.patch.object(ma, "BOT_LOG", p):
                out = ma.read_bot_log_tail(hours=4)
        self.assertEqual([ln.split("| ")[-1] for ln in out], ["recent line"])


class TestPrompt(unittest.TestCase):
    def _p(self, block):
        e = {"entry_count": 0, "exit_count": 0, "partial_count": 0, "stop_hit_rate": 0.0,
             "stop_hit_count": 0, "flagged_entries": []}
        with mock.patch.object(ma, "_build_config_constants_block", return_value=""):
            return ma._build_gemini_prompt(e, {}, {}, {}, [], block, {"available": False})

    def test_ground_truth_block_and_rules(self):
        p = self._p("BROKER GROUND TRUTH (x): GOOGL COVERED")
        self.assertIn("BROKER GROUND TRUTH (x): GOOGL COVERED", p)
        self.assertIn("STOP COVERAGE IS OWNED BY CODE", p)
        self.assertIn("DOES belong in\nCATASTROPHIC ALERT", p)
        self.assertNotIn("must appear in the CATASTROPHIC ALERT", p.lower())

    def test_missing_block_is_unverified_with_self_report_allowance(self):
        p = self._p("")
        self.assertIn("stop coverage UNVERIFIED", p)
        self.assertIn("broker-unverified", p)


if __name__ == "__main__":
    unittest.main()
