# ruff: noqa: E501
"""Tests for investigate_propose_gate.py (owner mandate 2026-11-13: an investigation always ships with a fix, a
prevention plan and a tracked bookmark)."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import investigate_propose_gate as g  # noqa: E402

_ASK = "Jensen resigned as nvda ceo today. Did the bot catch that? Did it review nvda?"
_REPORT_ONLY = ("The bot did not catch it. At 14:38 PT it filed the headline under 'powell'; QHM never reviewed "
                "NVDA, which came within $1.32 of its stop.")
_FULL = (_REPORT_ONLY + " Fix: HOLD-NEWS-REVIEW-1113 builds a whole-article review of every held name on news; "
         "it prevents a repeat because an alarm pages any material item left unreviewed. Design: "
         "logs/design_records/hold_news_review_2026-11-13.md")


def _run(owner, reply, active=False):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": owner}}) + "\n")
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "x"}]}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": reply}]}}) + "\n")
        path = fh.name
    err = io.StringIO()
    stdin = io.StringIO(json.dumps({"transcript_path": path, "stop_hook_active": active}))
    with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stderr", err):
        rc = g.main()
    os.unlink(path)
    return rc, err.getvalue()


class Gate(unittest.TestCase):
    def test_report_only_blocks_and_names_all_three(self):
        rc, err = _run(_ASK, _REPORT_ONLY)
        self.assertEqual(rc, 2)
        for word in ("FIX", "PREVENTION", "BOOKMARK"):
            self.assertIn(word, err)

    def test_full_answer_passes(self):
        self.assertEqual(_run(_ASK, _FULL)[0], 0)

    def test_missing_bookmark_only(self):
        rc, err = _run(_ASK, _FULL.split(" Design:")[0].replace("HOLD-NEWS-REVIEW-1113", "a review"))
        self.assertEqual(rc, 2)
        self.assertIn("BOOKMARK", err)
        self.assertNotIn("FIX (", err)

    def test_failure_words_trigger_without_an_ask(self):
        self.assertEqual(_run("ok continue", "Root cause: the stop was never placed.")[0], 2)

    def test_unrelated_turn_passes(self):
        self.assertEqual(_run("ok continue", "Shipped PR 967; the deploy is live.")[0], 0)

    def test_fires_once_per_turn(self):
        self.assertEqual(_run(_ASK, _REPORT_ONLY, active=True)[0], 0)

    def test_unreadable_input_never_blocks(self):
        with mock.patch.object(sys, "stdin", io.StringIO("not json")):
            self.assertEqual(g.main(), 0)
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"transcript_path": "/nonexistent"}))):
            self.assertEqual(g.main(), 0)

    def test_owner_text_skips_tool_results_and_notices(self):
        evs = [{"type": "user", "message": {"role": "user", "content": _ASK}},
               {"type": "user", "message": {"role": "user", "content": "<task-notification> SYSTEM NOTIFICATION"}},
               {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}]
        self.assertEqual(g.last_texts(evs), (_ASK, "x"))

    def test_owner_words_after_a_reminder_block_are_read(self):
        # cold review 11-14: the app can put <system-reminder> blocks ahead of the owner's typed words in one entry
        rem = "<system-reminder>\nYou are operating in a git worktree.\n</system-reminder>\n\n"
        self.assertEqual(_run(rem + _ASK, "At 14:38 PT it filed the headline under 'powell'; QHM ran no review.")[0], 2)
        evs = [{"type": "user", "message": {"role": "user", "content": "why did the bot miss NVDA?"}},
               {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": _FULL}]}},
               {"type": "user", "message": {"role": "user", "content": rem + "Yes, both stand"}},
               {"type": "assistant", "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "Recorded. Both items are queued."}]}}]
        owner, reply = g.last_texts(evs)
        self.assertEqual((owner, g.missing(owner, reply)), ("Yes, both stand", []))

    def test_a_reminder_only_entry_is_not_the_owner(self):
        evs = [{"type": "user", "message": {"role": "user", "content": _ASK}},
               {"type": "user", "message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}},
               {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": None}]}},
               {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}]
        self.assertEqual(g.last_texts(evs), (_ASK, "x"))

    def test_a_subagent_hand_back_is_not_the_owner(self):
        # cold review 11-14, round 2: a hand-back arrives as a user entry that does not start with "<"
        hb = ('Another Claude session sent a message:\n<agent-message from="a1">\n[Subagent hand-back] Log read '
              'complete: headline keyed to powell.\n</agent-message>')
        mk = lambda role, text: {"type": role, "message": {"role": role, "content": text}}  # noqa: E731
        evs = [mk("user", _ASK), mk("user", hb), mk("assistant", [{"type": "text", "text": _REPORT_ONLY}])]
        owner, reply = g.last_texts(evs)
        self.assertEqual(owner, _ASK)
        self.assertEqual(len(g.missing(owner, reply)), 3)
        evs = [mk("user", "ok continue"), mk("user", hb.replace("Log read complete", "Static audit of the file")),
               mk("assistant", [{"type": "text", "text": "Recorded. Both items are queued."}])]
        owner, reply = g.last_texts(evs)
        self.assertEqual((owner, g.missing(owner, reply)), ("ok continue", []))

    def test_an_approval_naming_the_gate_is_not_an_ask(self):
        self.assertEqual(g.missing("Approve investigate gate 1113", "Recorded. Building it now."), [])
        self.assertEqual(len(g.missing("Investigate why NVDA was skipped", "It was skipped at 14:38.")), 3)
        # round 3: an ask that follows the approval clause still counts
        self.assertEqual(len(g.missing("Approved. Now investigate the NVDA stop.", "It sat at $131.20 all day.")), 3)
        self.assertEqual(len(g.missing("Approve it, and why did NVDA miss?", "It was skipped at 14:38.")), 3)

    def test_app_written_entries_are_not_the_owner(self):
        # round 3: compaction summaries, skill bodies and hook feedback carry isCompactSummary / isMeta
        mk = lambda text, **kw: {"type": "user", "message": {"role": "user", "content": text}, **kw}  # noqa: E731
        end = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}
        summary = mk("This session is being continued from a previous one. PM audit root cause.", isCompactSummary=True)
        self.assertEqual(g.last_texts([mk("ok continue"), summary, end])[0], "ok continue")
        self.assertEqual(g.last_texts([mk(_ASK), mk("Base directory for this skill: audit", isMeta=True), end])[0], _ASK)
        # round 4: the app's usage-limit resume note carries no flag, only its fixed opening words
        note = mk("I hit my usage limit while you were working, but it has reset now. Please continue from where you "
                  "left off.")
        self.assertEqual(g.last_texts([mk(_ASK), note, end])[0], _ASK)

    def test_several_approvals_in_one_message_are_not_an_ask(self):
        self.assertEqual(g.missing("Approve regime size 1113\nApprove audit log 1113", "Recorded both."), [])
        self.assertEqual(len(g.missing("Approve regime size 1113\nApprove audit log 1113\nwhy did NVDA miss?",
                                       "It was skipped at 14:38.")), 3)


if __name__ == "__main__":
    unittest.main()
