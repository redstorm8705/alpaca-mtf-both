#!/usr/bin/env python3
# ruff: noqa: E501
"""Post-live audit engine increment 1 (2026-10-11; design record logs/design_records/post_live_engine_2026-10-11.md;
board Kim/Majors/López de Prado + Gro + GAI):
  * record_expectation.validate — the expectation schema (tier 1 / tier 2);
  * preship_gate._expectation_ok — a bot-code change needs a marker bound to its sha AND the record's sha at the same ref;
  * scripts/post_live_eval — window position, proof counting, error signatures, NOT_EXERCISED / UNEXERCISED_EXPIRED;
  * post_live_due / record_post_live — NOT_EXERCISED keeps a PR open; a PASS that says "not exercised" is refused.
Run: PYTHONPATH=. python3 -m pytest -q tests/test_post_live_engine.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


rexp = _load("record_expectation", ".claude/preship/record_expectation.py")
gate = _load("preship_gate_mod", ".claude/preship/preship_gate.py")
due = _load("post_live_due_mod", ".claude/preship/post_live_due.py")
rpl = _load("record_post_live_mod", ".claude/preship/record_post_live.py")
sys.path.insert(0, str(ROOT / "scripts"))
import post_live_eval as ple  # noqa: E402


def _rec(**kw):
    r = {"change": "cohold-lift", "tier": 1, "paths": ["run_day_tier.py"],
         "proof": [{"log": "logs/day_tier_runner_cron.log", "pattern": r"\[cohold-lift\] CO-HOLD BRACKET ENTRY"}],
         "expected_rate": "each same-direction co-hold entry", "window_sessions": 5,
         "reversal": "a bracket entry leaves a Day lot without a stop"}
    r.update(kw)
    return r


class Validate(unittest.TestCase):
    def test_valid_tier1(self):
        self.assertEqual(rexp.validate(_rec()), [])

    def test_missing_and_bad_fields(self):
        self.assertTrue(rexp.validate({}))
        self.assertTrue(rexp.validate(_rec(change="Bad Slug")))
        self.assertTrue(rexp.validate(_rec(proof=[{"log": "/var/x.log", "pattern": "abcdefgh"}])))
        self.assertTrue(rexp.validate(_rec(proof=[{"log": "logs/x.log", "pattern": "short"}])))
        self.assertTrue(rexp.validate(_rec(proof=[{"log": "logs/x.log", "pattern": "unclosed(paren"}])))
        self.assertTrue(rexp.validate(_rec(window_sessions=0)))
        self.assertTrue(rexp.validate(_rec(window_sessions=True)))
        self.assertTrue(rexp.validate(_rec(reversal="bad")))
        self.assertTrue(rexp.validate(_rec(window_sessions=2)))                       # short window needs a reason
        self.assertEqual(rexp.validate(_rec(window_sessions=2, window_reason="runs every close")), [])
        self.assertTrue(rexp.validate(_rec(proof=[{"log": "logs/x.log", "pattern": "no slug in here"}])))
        self.assertEqual(rexp.validate(_rec(proof=[{"log": "logs/x.log", "pattern": "no slug in here"}],
                                             baseline_exempt_reason="config-only change")), [])

    def test_covers_stops_at_the_next_flag(self):
        with mock.patch.object(rexp, "validate", return_value=["stop here"]):
            self.assertEqual(rexp.main(["logs/post_live/expect/x.json", "--covers", "a.py", "--ssh", "mtf-bot"]), 1)

    def test_tier2_needs_metrics_and_existing_verification(self):
        self.assertTrue(rexp.validate(_rec(tier=2)))
        ok = _rec(tier=2, metrics=[{"name": "entries", "source": "day_tier_events", "direction": "up", "bound": "+0-2/day"}],
                  verification="tests/test_post_live_engine.py")
        self.assertEqual(rexp.validate(ok), [])
        self.assertTrue(rexp.validate({**ok, "verification": "tests/does_not_exist.py"}))
        self.assertTrue(rexp.validate({**ok, "metrics": [{"name": "x", "source": "y", "direction": "sideways", "bound": "z"}]}))


class GateExpectation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _check(self, marker, shas, on_main=False, main_ref=True):
        if marker is not None:
            with open(os.path.join(self.tmp.name, "run_day_tier.py.expect.json"), "w") as fh:
                json.dump(marker, fh)

        def _git(args, want_bytes=False):
            if args[:1] == ["rev-parse"]:
                return main_ref, ""
            if args[:2] == ["cat-file", "-e"]:
                return on_main, ""
            return False, ""
        with mock.patch.object(gate, "MARKER_DIR", self.tmp.name), \
                mock.patch.object(gate, "_git", side_effect=_git), \
                mock.patch.object(gate, "_content_sha", side_effect=lambda ref: shas.get(ref, "")):
            return gate._expectation_ok("run_day_tier.py", ":{}")

    def test_record_already_on_main_is_refused(self):
        m = {"sha256": "A", "expect_path": "logs/post_live/expect/x.json", "expect_sha256": "E"}
        shas = {":run_day_tier.py": "A", ":logs/post_live/expect/x.json": "E"}
        ok, why = self._check(m, shas, on_main=True)
        self.assertFalse(ok)
        self.assertIn("already on main", why)
        self.assertFalse(self._check(m, shas, main_ref=False)[0])               # unreadable main ref: fail closed

    def test_bound_marker_passes(self):
        m = {"sha256": "A", "expect_path": "logs/post_live/expect/x.json", "expect_sha256": "E"}
        ok, _ = self._check(m, {":run_day_tier.py": "A", ":logs/post_live/expect/x.json": "E"})
        self.assertTrue(ok)

    def test_no_marker_fails(self):
        ok, why = self._check(None, {":run_day_tier.py": "A"})
        self.assertFalse(ok)
        self.assertIn("record_expectation.py", why)

    def test_code_changed_fails(self):
        m = {"sha256": "A", "expect_path": "logs/post_live/expect/x.json", "expect_sha256": "E"}
        self.assertFalse(self._check(m, {":run_day_tier.py": "B", ":logs/post_live/expect/x.json": "E"})[0])

    def test_record_edited_or_not_shipped_fails(self):
        m = {"sha256": "A", "expect_path": "logs/post_live/expect/x.json", "expect_sha256": "E"}
        self.assertFalse(self._check(m, {":run_day_tier.py": "A", ":logs/post_live/expect/x.json": "F"})[0])
        self.assertFalse(self._check(m, {":run_day_tier.py": "A"})[0])

    def test_scope_is_code_that_runs_in_production(self):
        for f in ("run_day_tier.py", "execution/broker.py", "scripts/post_live_eval.py", "config.py", "auto_deploy.sh"):
            self.assertTrue(gate._runs_in_production(f), f)
        for f in ("tests/test_x.py", "research/r.py", ".gitignore", "logs/x.md", ".claude/preship/a.py", "docs/a.md"):
            self.assertFalse(gate._runs_in_production(f), f)

    def test_record_outside_expect_dir_fails(self):
        m = {"sha256": "A", "expect_path": "notes/x.json", "expect_sha256": "E"}
        self.assertFalse(self._check(m, {":run_day_tier.py": "A", ":notes/x.json": "E"})[0])
        m = {"sha256": "A", "expect_path": "logs/post_live/expect/../../x.json", "expect_sha256": "E"}
        self.assertFalse(self._check(m, {":run_day_tier.py": "A", ":logs/post_live/expect/../../x.json": "E"})[0])

    def test_file_identical_to_main_needs_no_expectation(self):
        # cold-2nd round 3: merging main into a branch brings main's files into the push range; they are not this
        # branch's change. _changed_gated skips a production file whose content equals origin/main's.
        def _git(args, want_bytes=False):
            if args[:2] == ["diff", "--name-status"]:
                return True, "M\texecution/foo.py\nM\texecution/bar.py\n"
            return False, ""
        shas = {"HEAD:execution/foo.py": "SAME", "origin/main:execution/foo.py": "SAME",
                "HEAD:execution/bar.py": "NEW", "origin/main:execution/bar.py": "OLD"}
        with mock.patch.object(gate, "_git", side_effect=_git), \
                mock.patch.object(gate, "_content_sha", side_effect=lambda ref: shas.get(ref, "")), \
                mock.patch.object(gate, "_is_gated", return_value=False):
            fails, _ = gate._changed_gated(["@{upstream}..HEAD"], "HEAD:{}")
        post = [f for f in fails if "[POST-LIVE]" in f]
        self.assertEqual(len(post), 1)
        self.assertIn("execution/bar.py", post[0])


class Evaluator(unittest.TestCase):
    CAL = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12, 13)]

    def test_window_position(self):
        shipped = datetime(2026, 10, 11, 15, 0, tzinfo=ET)                     # Sunday
        self.assertEqual(ple.window_position(shipped, date(2026, 10, 12), self.CAL), 1)
        self.assertEqual(ple.window_position(shipped, date(2026, 10, 13), self.CAL), 2)
        self.assertIsNone(ple.window_position(shipped, date(2026, 10, 9), self.CAL))
        midday = datetime(2026, 10, 12, 11, 0, tzinfo=ET)                      # shipped after the open
        self.assertEqual(ple.window_position(midday, date(2026, 10, 13), self.CAL), 1)

    def test_signature_normalises_ids_symbols_numbers(self):
        a = ple._signature("m", "[NVDA] stop 12.5 order 3f2a9c1e-1111-2222-3333-444455556666 ['MSFT', 'PLTU']")
        b = ple._signature("m", "[EWY] stop 7 order aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee ['AAPL']")
        self.assertEqual(a, b)

    def _with_logs(self, files: dict, records: dict, shipped: datetime, session: date):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "logs" / "post_live" / "expect").mkdir(parents=True)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        for name, rec in records.items():
            (root / "logs" / "post_live" / "expect" / f"{name}.json").write_text(json.dumps(rec))
        with mock.patch.object(ple, "ROOT", root), \
                mock.patch.object(ple, "EXPECT_DIR", root / "logs" / "post_live" / "expect"), \
                mock.patch.object(ple, "shipped_at", return_value=shipped):
            return ple.evaluate(session, self.CAL)

    def test_ran_vs_not_exercised_vs_expired(self):
        log = ("2026-10-12 14:00:00 | INFO | day_tier_runner | [cohold-lift] CO-HOLD BRACKET ENTRY placed\n"
               "2026-10-09 14:00:00 | INFO | day_tier_runner | [cohold-lift] CO-HOLD BRACKET ENTRY placed\n")
        shipped = datetime(2026, 10, 11, 15, 0, tzinfo=ET)
        r = self._with_logs({"logs/day_tier_runner_cron.log": log}, {"cohold-lift": _rec()}, shipped, date(2026, 10, 12))
        self.assertEqual(r["changes"][0]["verdict"], "RAN")
        self.assertEqual(r["changes"][0]["proof_matches_session"], 1)         # the 10/09 line is not this session
        r = self._with_logs({"logs/day_tier_runner_cron.log": ""}, {"cohold-lift": _rec(window_sessions=2)},
                            shipped, date(2026, 10, 12))
        self.assertEqual(r["changes"][0]["verdict"], "NOT_EXERCISED")
        self.assertFalse(r["findings"])                                        # window not spent yet
        r = self._with_logs({"logs/day_tier_runner_cron.log": ""}, {"cohold-lift": _rec(window_sessions=2)},
                            shipped, date(2026, 10, 13))
        self.assertEqual([f["finding"] for f in r["findings"]], ["UNEXERCISED_EXPIRED"])

    def test_ran_on_an_earlier_window_session_is_never_expired(self):
        # cold-2nd F1: ran on session 1 (10/12), silent on session 2 (10/13) -> RAN, no UNEXERCISED_EXPIRED
        log = "2026-10-12 14:00:00 | INFO | day_tier_runner | [cohold-lift] CO-HOLD BRACKET ENTRY placed\n"
        shipped = datetime(2026, 10, 11, 15, 0, tzinfo=ET)
        r = self._with_logs({"logs/day_tier_runner_cron.log": log},
                            {"cohold-lift": _rec(window_sessions=2, window_reason="x")}, shipped, date(2026, 10, 13))
        self.assertEqual(r["changes"][0]["verdict"], "RAN")
        self.assertEqual(r["findings"], [])

    def test_proof_written_between_sessions_counts(self):
        # 2026-10-13: the fill-timestamp canonicalisation ran once at 17:46 ET (after the close, before the next
        # session) and was reported UNEXERCISED_EXPIRED. Any calendar day since the ship counts.
        log = "2026-10-12 17:46:00 | INFO | x | [cohold-lift] CO-HOLD BRACKET ENTRY placed\n"
        shipped = datetime(2026, 10, 12, 17, 40, tzinfo=ET)
        r = self._with_logs({"logs/day_tier_runner_cron.log": log},
                            {"cohold-lift": _rec(window_sessions=1, window_reason="x")}, shipped, date(2026, 10, 13))
        self.assertEqual(r["changes"][0]["verdict"], "RAN")
        self.assertEqual(r["findings"], [])

    def test_window_days_waits_for_a_weekend_job(self):
        # A Saturday-only report shipped Sunday 10/11: two weekday sessions must not expire it.
        shipped = datetime(2026, 10, 11, 15, 0, tzinfo=ET)
        rec = _rec(window_sessions=2, window_reason="x", window_days=9)
        r = self._with_logs({"logs/day_tier_runner_cron.log": ""}, {"cohold-lift": rec}, shipped, date(2026, 10, 13))
        self.assertEqual(r["changes"][0]["verdict"], "NOT_EXERCISED")
        self.assertEqual(r["findings"], [])
        cal = self.CAL + [date(2026, 10, d) for d in (14, 15, 16, 19, 20)]
        with mock.patch.object(self, "CAL", cal):
            sat = "2026-10-17 09:00:00 | INFO | x | [cohold-lift] CO-HOLD BRACKET ENTRY placed\n"
            r = self._with_logs({"logs/day_tier_runner_cron.log": sat}, {"cohold-lift": rec}, shipped, date(2026, 10, 20))
            self.assertEqual(r["changes"][0]["verdict"], "RAN")
            r = self._with_logs({"logs/day_tier_runner_cron.log": ""}, {"cohold-lift": rec}, shipped, date(2026, 10, 20))
            self.assertEqual([f["finding"] for f in r["findings"]], ["UNEXERCISED_EXPIRED"])

    def test_undated_traceback_inherits_the_session_date(self):
        logs = {"logs/day_tier_runner_cron.log": "2026-10-09 14:00:00 | INFO | x | fine\n"
                                                 "2026-10-12 14:00:00 | INFO | x | start\n"
                                                 "Traceback (most recent call last):\n  File \"x.py\"\n"}
        r = self._with_logs(logs, {}, datetime(2026, 10, 11, tzinfo=ET), date(2026, 10, 12))
        self.assertIn("Traceback", r["new_error_signatures"])

    def test_outside_window_is_skipped(self):
        shipped = datetime(2026, 9, 28, 8, 0, tzinfo=ET)                       # far beyond window + grace
        r = self._with_logs({}, {"old": _rec(change="old", window_sessions=1)}, shipped, date(2026, 10, 12))
        self.assertEqual(r["changes"], [])

    def test_new_error_signature_is_a_finding(self):
        logs = {"logs/mtf_bot.log": "2026-10-09 14:00:00 | ERROR    | execution.x | [NVDA] old failure 1\n"
                                    "2026-10-12 14:00:00 | ERROR    | execution.x | [AAPL] old failure 2\n"
                                    "2026-10-12 14:01:00 | CRITICAL | execution.y | brand new failure\n"}
        r = self._with_logs(logs, {}, datetime(2026, 10, 11, tzinfo=ET), date(2026, 10, 12))
        self.assertEqual(r["new_error_signatures"], ["execution.y: brand new failure"])
        self.assertTrue(r["findings"][0]["finding"].startswith("NEW_ERRORS_"))

    def test_dry_run_issue_never_calls_github(self):
        self.assertTrue(ple._upsert_issue({"change": "c", "finding": "F", "detail": "d"}, date(2026, 10, 12), True)
                        .startswith("DRY"))


class TrackerVerdicts(unittest.TestCase):
    def test_not_exercised_keeps_pr_open_latest_record_wins(self):
        lines = [{"pr": 554, "verdict": "PASS", "notes": "not exercised: no exits", "ts": "2026-10-09T15:00"},
                 {"pr": 552, "verdict": "PASS", "notes": "ran fine", "ts": "2026-10-09T15:00"},
                 {"pr": 560, "verdict": "NOT_EXERCISED", "notes": "x" * 40, "ts": "2026-10-12T15:00"},
                 {"pr": 561, "verdict": "NOT_EXERCISED", "notes": "x" * 40, "ts": "2026-10-12T15:00"},
                 {"pr": 561, "verdict": "PASS", "notes": "ran on 10/13", "ts": "2026-10-13T15:00"}]
        text = "\n".join(json.dumps(x) for x in lines)
        with mock.patch.object(due, "_git", return_value=text), mock.patch("builtins.open", side_effect=OSError):
            self.assertEqual(due.audited_prs(), {552, 561})

    def test_record_refuses_pass_when_not_exercised(self):
        with mock.patch.object(rpl, "RECORDS", os.path.join(tempfile.mkdtemp(), "r.jsonl")):
            self.assertEqual(rpl.main(["554", "--session", "2026-10-12", "--verdict", "PASS",
                                       "--notes", "not exercised: no Swing tranche exits this session at all"]), 2)
            self.assertEqual(rpl.main(["554", "--session", "2026-10-12", "--verdict", "NOT_EXERCISED",
                                       "--notes", "not exercised: no Swing tranche exits this session at all"]), 0)


class SlackDigest(unittest.TestCase):
    """Owner 2026-10-27: the one-paragraph digest was unreadable on a phone. Block Kit, short lines, tappable links."""

    def _run(self, findings, issues, ran=5, idle=4):
        return ple._slack_digest(date(2026, 10, 27), ran, idle, findings, issues)

    def test_layout_and_links(self):
        url = "https://github.com/o/r/issues/770"
        f = [{"change": "alert-false-wording", "finding": "UNEXERCISED_EXPIRED"},
             {"change": "session", "finding": "NEW_ERRORS_2026-10-26"}]
        blocks, fallback = self._run(f, [f"opened {url}", "updated https://github.com/o/r/issues/777"])
        self.assertEqual(blocks[0]["type"], "header")
        self.assertIn("Tue Oct 27", blocks[0]["text"]["text"])
        texts = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
        self.assertIn(f"<{url}|Issue #770>", texts)
        self.assertIn("New error types in logs", texts)
        self.assertIn("⚠️ 2 need a look", fallback)
        for line in texts.split("\n"):
            self.assertNotIn(" | ", line)                       # SLK05: no packed fields
            shown = re.sub(r"<[^|>]+\|([^>]+)>", r"\1", line)   # what the phone shows: the link text, not the URL
            self.assertLessEqual(len(shown), 45, shown)

    def test_nothing_to_flag(self):
        blocks, fallback = self._run([], [], idle=0)
        self.assertIn("nothing needs attention", fallback)
        self.assertFalse(any(b["type"] == "divider" for b in blocks))

    def test_many_findings_split_under_slack_cap(self):
        f = [{"change": f"change-{n}", "finding": "UNEXERCISED_EXPIRED"} for n in range(40)]
        i = [f"opened https://github.com/o/r/issues/{700 + n}" for n in range(40)]
        blocks, _ = self._run(f, i)
        for b in blocks:
            if b["type"] == "section":
                self.assertLess(len(b["text"]["text"]), 3000)
        self.assertEqual(sum(b["text"]["text"].count("⚠️ change-") for b in blocks if b["type"] == "section"), 40)

    def test_many_error_findings_split_under_slack_cap(self):
        f = [{"change": "session", "finding": f"NEW_ERRORS_2026-10-{n:02d}"} for n in range(60)]
        i = [f"opened https://github.com/redstorm8705/alpaca-mtf-both/issues/{700 + n}" for n in range(60)]
        f.append({"change": "x" * 5000, "finding": "UNEXERCISED_EXPIRED"})
        i.append("opened https://github.com/o/r/issues/1")
        blocks, _ = self._run(f, i)
        for b in blocks:
            if b["type"] == "section":
                self.assertLess(len(b["text"]["text"]), 3000)
        self.assertEqual(sum(b["text"]["text"].count("New error types") for b in blocks if b["type"] == "section"), 60)

    def test_unfiled_issue(self):
        blocks, _ = self._run([{"change": "x", "finding": "UNEXERCISED_EXPIRED"}], ["issue create failed (500)"])
        self.assertIn("issue not filed", "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section"))


if __name__ == "__main__":
    unittest.main()
