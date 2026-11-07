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
        self.assertTrue(any(x.startswith("Traceback") for x in r["new_error_signatures"]), r["new_error_signatures"])

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
        self.assertEqual(r["findings"][0]["kind"], "NEW_ERROR")
        self.assertTrue(r["findings"][0]["change"].startswith("error "))
        self.assertEqual(r["signatures"][r["new_ids"][0]]["count"], 1)
        self.assertEqual(len(r["signatures"]), 2)                               # every signature stored, not only new

    def test_dry_run_issue_never_calls_github(self):
        self.assertTrue(ple._upsert_issue({"change": "c", "finding": "F", "detail": "d"}, date(2026, 10, 12), True)
                        .startswith("DRY"))


FIX = json.loads((ROOT / "tests" / "fixtures" / "post_live_signatures" / "cases.json").read_text())


class SignatureV2(unittest.TestCase):
    """POST-LIVE-WEEKLY-1106 Diff A: versioned signature identity, masking by role (frozen fixtures)."""
    U = frozenset(FIX["universe"])

    def test_fixture_version_matches(self):
        self.assertEqual(FIX["normaliser_version"], ple.NORMALISER_VERSION)

    def test_merge_cases(self):
        for a, b in FIX["merge"]:
            self.assertEqual(ple.canonical(a, self.U), ple.canonical(b, self.U), (a, b))

    def test_split_cases(self):
        for a, b in FIX["split"]:
            self.assertNotEqual(ple.canonical(a, self.U), ple.canonical(b, self.U), (a, b))

    def test_words_are_never_masked_as_symbols(self):
        for msg, kept in FIX["unmasked"]:
            self.assertIn(kept, ple.canonical(msg, self.U), msg)

    def test_same_message_in_two_modules_is_two_signatures(self):
        log = ("2026-10-12 14:00:00 | ERROR    | execution.a | invalid state\n"
               "2026-10-12 14:00:01 | ERROR    | execution.b | invalid state\n")
        with tempfile.TemporaryDirectory() as t, mock.patch.object(ple, "ROOT", Path(t)):
            (Path(t) / "logs").mkdir()
            (Path(t) / "logs" / "mtf_bot.log").write_text(log)
            ple._DAY_CACHE.clear()
            sigs = ple.error_signatures(date(2026, 10, 12))
        self.assertEqual(len(sigs), 2)
        self.assertEqual(len({s["cluster"] for s in sigs.values()}), 2)

    def test_traceback_signature_uses_final_exception_and_deepest_repo_frame(self):
        root = "/srv/bot"
        tb = ["2026-07-24 00:13:57,460 | ERROR    | main | Loop error: boom",
              "Traceback (most recent call last):",
              '  File "/srv/bot/venv/lib/python3.10/site-packages/alpaca/common/rest.py", line 198, in _one_request',
              "    response.raise_for_status()",
              "requests.exceptions.HTTPError: 500 Server Error: Internal Server Error for url: https://p.a/v2/account",
              "",
              "During handling of the above exception, another exception occurred:",
              "",
              "Traceback (most recent call last):",
              '  File "/srv/bot/main.py", line 1041, in main',
              "    run_cycle(risk)",
              '  File "/srv/bot/execution/broker.py", line 141, in get_account',
              "    return client.get_account()",
              '  File "/srv/bot/venv/lib/python3.10/site-packages/alpaca/common/rest.py", line 207, in _one_request',
              "    raise APIError(error, http_error)",
              'alpaca.common.exceptions.APIError: {"code":50010000,"message":"internal server error occurred"}',
              "2026-07-24 00:14:57,523 | INFO     | strategy.run_cycle | next"]
        with mock.patch.object(ple, "ROOT", Path(root)):
            out = ple._tracebacks(tb)
        self.assertEqual(len(out), 1)
        kind, msg, frames = out[0]
        self.assertEqual(kind, "alpaca.common.exceptions.APIError")
        self.assertEqual(frames, ["main:main", "execution.broker:get_account"])
        self.assertIn("50010000", ple.canonical(msg))

    def test_timestamp_prefixed_traceback_is_parsed(self):
        out = ple._tracebacks(["2026-11-07 15:00:00 Traceback (most recent call last):",
                               '  File "x.py", line 1, in f', "KeyError: 'a'"])
        self.assertEqual([o[0] for o in out], ["KeyError"])

    def test_quoted_header_never_restarts_or_invents_a_block(self):
        root = "/srv/bot"
        with mock.patch.object(ple, "ROOT", Path(root)):
            out = ple._tracebacks(["Traceback (most recent call last):", '  File "/srv/bot/a.py", line 1, in f',
                                   '  File "/srv/bot/b.py", line 2, in g',
                                   '    if "Traceback (most recent call last)" in ln:', "ValueError: x"])
            self.assertEqual(out, [("ValueError", "x", ["a:f", "b:g"])])
            out = ple._tracebacks(["2026-11-06 10:00:00 | ERROR    | m | saw Traceback (most recent call last) text",
                                   "2026-11-06 10:00:01 | INFO     | m | next"])
            self.assertEqual(out, [])

    def test_two_different_crashes_are_two_signatures(self):
        log = ("2026-10-12 14:00:00 | INFO | x | start\n"
               "Traceback (most recent call last):\n  File \"x.py\", line 1, in f\nKeyError: 'a'\n"
               "2026-10-12 14:00:05 | INFO | x | again\n"
               "Traceback (most recent call last):\n  File \"x.py\", line 9, in g\nValueError: bad\n")
        with tempfile.TemporaryDirectory() as t, mock.patch.object(ple, "ROOT", Path(t)):
            (Path(t) / "logs").mkdir()
            (Path(t) / "logs" / "day_tier_runner_cron.log").write_text(log)
            ple._DAY_CACHE.clear()
            sigs = ple.error_signatures(date(2026, 10, 12))
        self.assertEqual(sorted(s["kind"] for s in sigs.values()), ["KeyError", "ValueError"])


class DiffAState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_atomic_results_and_corrupt_file_is_backed_up(self):
        p = self.root / "r.json"
        ple._atomic_write(p, {"a": 1})
        self.assertEqual(json.loads(p.read_text()), {"a": 1})
        p.write_text("{not json")
        health: list = []
        self.assertIsNone(ple._read_state(p, health))
        self.assertTrue(health)
        self.assertFalse(p.exists())
        self.assertTrue(list(self.root.glob("r.json.corrupt.*")))

    def test_dry_run_never_moves_a_corrupt_file(self):
        p = self.root / "r.json"
        p.write_text("{not json")
        health: list = []
        self.assertIsNone(ple._read_state(p, health, backup=False))
        self.assertTrue(p.exists())

    def test_baseline_uses_stored_signatures(self):
        res_dir = self.root / "logs" / "post_live" / "results"
        res_dir.mkdir(parents=True)
        (self.root / "logs" / "post_live" / "expect").mkdir(parents=True)
        (self.root / "logs" / "mtf_bot.log").write_text("2026-10-12 14:00:00 | ERROR    | execution.x | seen before\n")
        sid = ple.sig_id("execution.x", "", "seen before")
        (res_dir / "2026-10-09.json").write_text(json.dumps({"schema_version": ple.RESULTS_SCHEMA,
                                                            "normaliser_version": ple.NORMALISER_VERSION,
                                                            "signatures": {sid: {}}, "symbol_universe": []}))
        (self.root / "logs" / "mtf_bot.log").write_text("2026-10-12 14:00:00 | ERROR    | execution.x | seen before\n")
        with mock.patch.object(ple, "ROOT", self.root), mock.patch.object(ple, "RESULTS_DIR", res_dir), \
                mock.patch.object(ple, "EXPECT_DIR", self.root / "logs" / "post_live" / "expect"), \
                mock.patch.object(ple, "error_signatures", wraps=ple.error_signatures) as es:
            r = ple.evaluate(date(2026, 10, 12), Evaluator.CAL)
        self.assertEqual(r["new_ids"], [])
        self.assertEqual(r["findings"], [])
        self.assertNotIn(date(2026, 10, 9), [c.args[0] for c in es.call_args_list])   # 10-09 came from the store

    def test_stored_baseline_with_a_smaller_universe_is_re_derived(self):
        res_dir = self.root / "logs" / "post_live" / "results"
        res_dir.mkdir(parents=True)
        (self.root / "logs" / "post_live" / "expect").mkdir(parents=True)
        (self.root / "logs" / "trade_events.jsonl").write_text('{"ts": "2026-10-12T10:00:00", "symbol": "AAPL"}\n')
        (self.root / "logs" / "mtf_bot.log").write_text(
            "2026-10-09 14:00:00 | ERROR    | execution.x | No bars returned for AAPL\n"
            "2026-10-12 14:00:00 | ERROR    | execution.x | No bars returned for AAPL\n")
        old = ple.sig_id("execution.x", "", "No bars returned for AAPL")     # stored before AAPL was in the universe
        (res_dir / "2026-10-09.json").write_text(json.dumps({"schema_version": ple.RESULTS_SCHEMA,
                                                            "normaliser_version": ple.NORMALISER_VERSION,
                                                            "signatures": {old: {}}, "symbol_universe": []}))
        with mock.patch.object(ple, "ROOT", self.root), mock.patch.object(ple, "RESULTS_DIR", res_dir), \
                mock.patch.object(ple, "EXPECT_DIR", self.root / "logs" / "post_live" / "expect"):
            r = ple.evaluate(date(2026, 10, 12), Evaluator.CAL)
        self.assertEqual(r["new_ids"], [])

    def test_pending_round_trip_and_merge(self):
        with mock.patch.object(ple, "PENDING_FILE", self.root / "p.json"):
            pend: list = []
            item = ple._item({"change": "error abc", "finding": "x", "detail": "d", "sig": "abc"}, date(2026, 10, 12))
            ple._merge_pending(pend, item, "issue lookup failed")
            ple._merge_pending(pend, item, "issue lookup failed")
            self.assertEqual(len(pend), 1)
            self.assertEqual(pend[0]["attempts"], 2)
            ple.save_pending(pend)
            self.assertEqual(ple.load_pending([])[0]["key"], "err-abc")

    def test_retry_finds_existing_issue_by_key_never_duplicates(self):
        item = ple._item({"change": "error abc", "finding": "x", "detail": "d", "sig": "abc"}, date(2026, 10, 12))
        index = [{"number": 7, "title": "renamed by hand", "body": "... " + ple._key_tag("err-abc"),
                  "html_url": "https://github.com/o/r/issues/7"}]
        calls = []

        def _api(method, path, tok, body=None):
            calls.append((method, path))
            return 201, {}
        fake = type(sys)("scripts.sync_reports")
        fake._api = _api
        with mock.patch.dict(sys.modules, {"scripts.sync_reports": fake}):
            left = ple.retry_pending([item], "tok", index)
        self.assertEqual(left, [])
        self.assertEqual(calls, [("POST", f"{ple.REPO}/issues/7/comments")])

    def test_pending_file_in_another_schema_is_kept_not_erased(self):
        pf = self.root / "p.json"
        pf.write_text(json.dumps({"schema_version": 99, "items": [{"key": "err-x"}]}))
        with mock.patch.object(ple, "PENDING_FILE", pf):
            health: list = []
            self.assertEqual(ple.load_pending(health), [])
        self.assertTrue(health)
        self.assertFalse(pf.exists())
        kept = list(self.root.glob("p.json.unsupported.*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(json.loads(kept[0].read_text())["schema_version"], 99)

    def test_unsaved_pending_outputs_fail_the_run(self):
        res = {"session": "2026-10-12", "changes": [], "findings": [], "signatures": {}, "new_ids": [],
               "partial_data": [], "health": [], "new_error_signatures": []}
        with mock.patch.object(ple, "sessions", return_value=Evaluator.CAL), \
                mock.patch.object(ple, "evaluate", return_value=res), \
                mock.patch.object(ple, "RESULTS_DIR", self.root), \
                mock.patch.object(ple, "load_pending", return_value=[]), \
                mock.patch.object(ple, "save_pending", side_effect=OSError("disk full")), \
                mock.patch.dict(sys.modules, {"scripts.sync_reports": type(sys)("x")}), \
                mock.patch.dict(sys.modules, {"alerts": type(sys)("alerts")}):
            sys.modules["scripts.sync_reports"]._github_token = lambda: None
            sys.modules["alerts"].send_slack_blocks = lambda b, f: True
            self.assertEqual(ple.main(["--session", "2026-10-12"]), 1)
        self.assertTrue((self.root / "2026-10-12.json").exists())

    def test_queued_recurrence_never_replaces_a_queued_create(self):
        create = ple._item({"change": "error abc", "finding": "x", "detail": "d", "sig": "abc"}, date(2026, 10, 12))
        recur = {"kind": "comment", "key": "err-abc-recur", "find_key": "err-abc", "title": "", "comment_only": True,
                 "comment": "Recurred", "queued": "t"}
        pend: list = []
        ple._merge_pending(pend, create, "issue lookup failed")
        ple._merge_pending(pend, recur, "issue lookup failed")
        self.assertEqual(sorted(p["kind"] for p in pend), ["comment", "issue"])
        self.assertTrue(any(p.get("body") and p["key"] == "err-abc" for p in pend))

    def test_retry_keeps_item_when_github_unreadable(self):
        item = ple._item({"change": "error abc", "finding": "x", "detail": "d", "sig": "abc"}, date(2026, 10, 12))
        self.assertEqual(len(ple.retry_pending([item], "tok", None)), 1)

    def test_expired_cause_is_classified(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "a.log").write_text("2026-10-01 10:00:00 | INFO | x | [cohold-lift] CO-HOLD BRACKET ENTRY placed\n")
        (logs / "u.log").write_text("no date here\n")
        rec = _rec(proof=[{"log": "logs/a.log", "pattern": r"\[cohold-lift\] CO-HOLD"},
                          {"log": "logs/missing.log", "pattern": "x"},
                          {"log": "logs/u.log", "pattern": "x"},
                          {"log": "logs/a.log", "pattern": "never there"},
                          {"log": "logs/a.log", "pattern": "(unclosed"}])
        with mock.patch.object(ple, "ROOT", self.root):
            causes = [d["cause"] for d in ple.proof_diagnosis(rec, datetime(2026, 10, 11, tzinfo=ET))]
        self.assertEqual(causes, ["matched_before_ship", "log_missing", "log_undated", "never_matches", "bad_regex"])


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
             {"change": "error 0123456789ab", "finding": "strategy.run_cycle: kill-switch failed", "kind": "NEW_ERROR"}]
        blocks, fallback = self._run(f, [f"opened {url}", "updated https://github.com/o/r/issues/777"])
        self.assertEqual(blocks[0]["type"], "header")
        self.assertIn("Tue Oct 27", blocks[0]["text"]["text"])
        texts = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
        self.assertIn(f"<{url}|Issue #770>", texts)
        self.assertIn("New error in run_cycle", texts)
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
        f = [{"change": f"error {n:012d}", "finding": "execution.broker: x", "kind": "NEW_ERROR"} for n in range(60)]
        i = [f"opened https://github.com/redstorm8705/alpaca-mtf-both/issues/{700 + n}" for n in range(60)]
        f.append({"change": "x" * 5000, "finding": "UNEXERCISED_EXPIRED"})
        i.append("opened https://github.com/o/r/issues/1")
        blocks, _ = self._run(f, i)
        for b in blocks:
            if b["type"] == "section":
                self.assertLess(len(b["text"]["text"]), 3000)
        self.assertEqual(sum(b["text"]["text"].count("New error in") for b in blocks if b["type"] == "section"), 60)

    def test_unfiled_issue(self):
        blocks, _ = self._run([{"change": "x", "finding": "UNEXERCISED_EXPIRED"}], ["issue create failed (500)"])
        self.assertIn("issue not filed", "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section"))


if __name__ == "__main__":
    unittest.main()
