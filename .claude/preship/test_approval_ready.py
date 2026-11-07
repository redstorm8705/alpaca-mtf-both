# ruff: noqa: E501
"""Tests for approval_ready.py (APPROVAL-READY-1106, logs/design_records/approval_ready_2026-11-06.md).

Each test builds a throwaway git repository holding one design record, so nothing reads or writes the real approval
store. No broker or network call is made anywhere."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import approval_ready as ar  # noqa: E402
import owner_approval_capture as oac  # noqa: E402

with open(os.path.join(HERE, "required_questions.json"), encoding="utf-8") as _f:
    _NQ = len(json.load(_f)["questions"])
ANSWERS = "".join(f"Q{i}: answered fully for this unit test case\n" for i in range(1, _NQ + 1))


def record(aid="TESTREADY-901", covers="execution/thing.py", body="", answers=ANSWERS):
    return (f"# Test design\n\nApproval-ID: {aid}\nCovers: {covers}\n\n{body}\n\n## Required questions\n{answers}")


class Repo:
    def __init__(self, text, extra_files=()):
        self.dir = tempfile.mkdtemp()
        run = lambda *a: subprocess.run(["git", "-C", self.dir, *a], capture_output=True, check=True)  # noqa: E731
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        for rel in ("execution/thing.py", "execution/other.py", *extra_files):
            p = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x = 1\n")
        os.makedirs(os.path.join(self.dir, "logs", "design_records"))
        self.path = os.path.join(self.dir, "logs", "design_records", "t.md")
        open(self.path, "w").write(text)
        run("add", "-A")
        run("commit", "-qm", "init")
        oac._worktree_roots.cache_clear()
        oac.common_dir.cache_clear()
        if hasattr(oac, "_DECL_CACHE"):
            oac._DECL_CACHE.clear()


ASK = "PROPOSAL: test design.\n\nDo you approve TESTREADY-901? Reply APPROVE TESTREADY-901.\n"


class TestChecks(unittest.TestCase):
    def test_complete_record_is_ready(self):
        r = Repo(record())
        self.assertEqual(ar.evaluate(r.dir, ASK), {"TESTREADY-901": []})

    def test_unanswered_questions_block(self):
        r = Repo(record(answers="Q1: answered fully for this unit test case\n"))
        probs = ar.evaluate(r.dir, ASK)["TESTREADY-901"]
        self.assertTrue(any("required questions unanswered" in p for p in probs), probs)

    def test_malformed_owner_numbers_block(self):
        r = Repo(record(body="Owner numbers: config.py K = 1.9x"))
        probs = ar.evaluate(r.dir, ASK)["TESTREADY-901"]
        self.assertTrue(any("Owner numbers" in p for p in probs), probs)

    def test_empty_covers_block(self):
        r = Repo(record(covers=""))
        probs = ar.check(r.dir, "TESTREADY-901", ASK)
        self.assertTrue(probs, probs)

    def test_named_gated_file_outside_covers_blocks(self):
        r = Repo(record(body="This changes execution/other.py as well."))
        probs = ar.evaluate(r.dir, ASK)["TESTREADY-901"]
        self.assertTrue(any("execution/other.py" in p for p in probs), probs)

    def test_citation_is_evidence_not_a_change(self):
        r = Repo(record(body="Evidence: execution/other.py:12 already does this."))
        self.assertEqual(ar.evaluate(r.dir, ASK)["TESTREADY-901"], [])

    def test_not_changed_line_passes(self):
        r = Repo(record(body="Reads execution/other.py.\n\nNot changed: execution/other.py"))
        self.assertEqual(ar.evaluate(r.dir, ASK)["TESTREADY-901"], [])

    def test_not_changed_but_modified_blocks(self):
        r = Repo(record(body="Reads execution/other.py.\n\nNot changed: execution/other.py"))
        subprocess.run(["git", "-C", r.dir, "branch", "-q", "-f", "origin/main"], check=True)
        subprocess.run(["git", "-C", r.dir, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        open(os.path.join(r.dir, "execution", "other.py"), "w").write("x = 2\n")
        probs = ar.evaluate(r.dir, ASK)["TESTREADY-901"]
        self.assertTrue(any("Not changed" in p for p in probs), probs)

    def test_new_marker_in_covers_is_normalised(self):
        r = Repo(record(covers="execution/thing.py, run_new_tool.py (new)", body="Adds run_new_tool.py (new)."))
        self.assertEqual(ar.evaluate(r.dir, ASK)["TESTREADY-901"], [])

    def test_new_file_missing_from_covers_blocks(self):
        r = Repo(record(body="Adds run_new_tool.py (new)."))
        probs = ar.evaluate(r.dir, ASK)["TESTREADY-901"]
        self.assertTrue(any("run_new_tool.py" in p for p in probs), probs)

    def test_owner_choice_must_be_in_ask(self):
        r = Repo(record(body="Owner choices: low_gate = minimum | maximum"))
        probs = ar.evaluate(r.dir, ASK)["TESTREADY-901"]
        self.assertTrue(any("owner choice" in p for p in probs), probs)
        ask2 = ASK + "\nChoose low_gate: minimum or maximum.\n"
        self.assertEqual(ar.evaluate(r.dir, ask2)["TESTREADY-901"], [])

    def test_reply_that_does_not_record_blocks(self):
        r = Repo(record())
        two = "Do you approve TESTREADY-901? Reply APPROVE TESTREADY-901 or APPROVE OTHER-902.\n"
        probs = ar.simulate_replies(r.dir, ["TESTREADY-901"], two)
        self.assertTrue(probs, probs)

    def test_report_of_recorded_approval_is_not_an_ask(self):
        r = Repo(record())
        self.assertEqual(ar.asked_ids(r.dir, "TESTREADY-901 approved and recorded on the first message."), [])

    def test_history_table_is_not_an_ask(self):
        r = Repo(record())
        table = "| Design | extra messages |\n|---|---|\n| TESTREADY-901 | 1 |\n"
        self.assertEqual(ar.asked_ids(r.dir, table), [])

    def test_disagreeing_copies_block(self):
        r = Repo(record())
        with mock.patch.object(oac, "_declarations", return_value={"TESTREADY-901": [
                (r.path, ["execution/thing.py"], 0), ("/elsewhere/t.md", ["execution/thing.py", "x.py"], 0)]}):
            probs = ar.check(r.dir, "TESTREADY-901", ASK)
        self.assertTrue(any("copies disagree" in p for p in probs), probs)


class TestReviewFixes(unittest.TestCase):
    """Cold-review round 1 threats (2026-11-06)."""

    def _approve(self, r, valid=True):
        os.makedirs(oac.approvals_dir(r.dir), exist_ok=True)
        open(os.path.join(oac.approvals_dir(r.dir), "TESTREADY-901.json"), "w").write("{}")
        p = mock.patch.object(ar, "_on_record", return_value=valid)
        p.start()
        self.addCleanup(p.stop)

    def test_quoted_past_question_is_not_an_ask(self):
        r = Repo(record())
        self._approve(r)
        msg = "Earlier I asked: Do you approve TESTREADY-901? You replied yes, and it shipped."
        self.assertEqual(ar.asked_ids(r.dir, msg), [])

    def test_reapproval_after_covers_change_is_askable(self):
        r = Repo(record())
        self._approve(r, valid=False)                   # store file exists but no longer matches the design
        self.assertEqual(ar.asked_ids(r.dir, ASK), ["TESTREADY-901"])
        self.assertEqual(ar.evaluate(r.dir, ASK)["TESTREADY-901"], [])
        bare = "PROPOSAL (TESTREADY-901): test.\n\nYOUR DECISION: APPROVE / REJECT / DEFER"
        self.assertEqual(ar.asked_ids(r.dir, bare), ["TESTREADY-901"])
        self.assertTrue(any("earlier approval" in p for p in ar.evaluate(r.dir, bare)["TESTREADY-901"]))

    def test_absolute_path_in_nested_worktree_maps_by_longest_root(self):
        r = Repo(record())
        inner = os.path.join(r.dir, ".claude", "worktrees", "wt")
        with mock.patch.object(oac, "worktree_roots", return_value=[r.dir, inner]):
            self.assertEqual(ar._repo_rel(os.path.join(inner, "execution", "x.py"), r.dir), "execution/x.py")

    def test_report_of_recorded_decision_is_not_an_ask(self):
        r = Repo(record())
        self._approve(r)
        self.assertEqual(ar.asked_ids(r.dir, "Your decision on TESTREADY-901 is recorded."), [])

    def test_explicit_reask_of_recorded_id_is_an_ask(self):
        r = Repo(record())
        self._approve(r)
        self.assertEqual(ar.asked_ids(r.dir, ASK), ["TESTREADY-901"])

    def test_report_quoting_the_reply_is_not_an_ask(self):
        r = Repo(record())
        self._approve(r)
        msg = "Done. Your reply APPROVE TESTREADY-901 was recorded and the change is shipped."
        self.assertEqual(ar.asked_ids(r.dir, msg), [])

    def test_corrupt_store_file_does_not_raise(self):
        r = Repo(record())
        os.makedirs(oac.approvals_dir(r.dir), exist_ok=True)
        for body in ("", "[]"):
            open(os.path.join(oac.approvals_dir(r.dir), "TESTREADY-901.json"), "w").write(body)
            self.assertIsInstance(ar.check(r.dir, "TESTREADY-901", ASK), list)

    def test_disagreeing_copies_still_detected_as_ask(self):
        r = Repo(record())
        decl = {"TESTREADY-901": [(r.path, ["execution/thing.py"], 0), ("/elsewhere/t.md", ["x.py"], 0)]}
        with mock.patch.object(oac, "_declarations", return_value=decl):
            self.assertEqual(ar.asked_ids(r.dir, ASK), ["TESTREADY-901"])

    def test_reply_regex_needs_an_id(self):
        r = Repo(record())
        text = "Do you approve TESTREADY-901? Say APPROVE if you agree, or reject it.\nReply APPROVE TESTREADY-901.\n"
        self.assertEqual([m.group(1) for m in ar._REPLY.finditer(text)], ["TESTREADY-901"])
        self.assertIsNotNone(r)

    def test_absolute_path_is_checked(self):
        r = Repo(record(body="Changes PATH and ./execution/other.py too.".replace("PATH", "x")))
        probs = ar.evaluate(r.dir, ASK)["TESTREADY-901"]
        self.assertTrue(any("execution/other.py" in p for p in probs), probs)
        r2 = Repo(record())
        body = f"Changes {os.path.realpath(r2.dir)}/execution/other.py too."
        open(r2.path, "w").write(record(body=body))
        probs2 = ar.check(r2.dir, "TESTREADY-901", ASK)
        self.assertTrue(any("execution/other.py" in p for p in probs2), probs2)


class TestHook(unittest.TestCase):
    def _run(self, text, root):
        payload = json.dumps({"transcript_path": "/x", "cwd": root, "stop_hook_active": True})
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), mock.patch("sys.stderr", err), \
                mock.patch("execute_dont_ask_gate._last_assistant_text", return_value=text):
            code = ar._hook()
        return code, err.getvalue()

    def test_incomplete_ask_blocks_even_on_second_attempt(self):
        r = Repo(record(answers="Q1: answered fully for this unit test case\n"))
        code, err = self._run(ASK, r.dir)
        self.assertEqual(code, 2)
        self.assertIn("Remove the approval ask, or fix the record", err)

    def test_complete_ask_passes(self):
        r = Repo(record())
        with mock.patch.object(oac, "register_designs", return_value={}):
            code, err = self._run(ASK, r.dir)
        self.assertEqual(code, 0, err)
        self.assertIn("READY", err)

    def test_message_without_ask_passes(self):
        r = Repo(record(answers=""))
        self.assertEqual(self._run("All done; nothing pending.", r.dir)[0], 0)

    def test_detection_error_never_blocks(self):
        r = Repo(record())
        with mock.patch.object(ar, "asked_ids", side_effect=OSError("disk")):
            code, _err = self._run("Approval recorded for TESTREADY-901.", r.dir)
        self.assertEqual(code, 0)

    def test_internal_error_blocks_an_ask(self):
        r = Repo(record())
        with mock.patch.object(ar, "evaluate", side_effect=OSError("disk")):
            code, err = self._run(ASK, r.dir)
        self.assertEqual(code, 2)
        self.assertIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
