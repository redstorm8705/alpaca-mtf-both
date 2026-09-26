# ruff: noqa: E501
"""Paper-account frame gate (Rafael mandate 2026-09-26, CLAUDE.md "THIS IS A PAPER TRADING ACCOUNT").

1. preship_audit prefixes PAPER_FRAME (once) to every Gro / GAI / NVIDIA prompt.
2. bgg_prompt_bias_gate blocks a board-seat / design-fork Agent prompt that lacks the frame.
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bgg_prompt_bias_gate as gate  # noqa: E402
import preship_audit as pa  # noqa: E402


class FramePrefix(unittest.TestCase):
    def test_prefix_once(self):
        x = pa._framed("review this")
        self.assertTrue(x.startswith(pa.PAPER_FRAME))
        self.assertEqual(pa._framed(x), x)

    def test_frame_is_not_verdict_leading(self):
        self.assertEqual(pa._check_prompt_bias(pa.PAPER_FRAME), [])

    def test_every_bgg_call_is_framed(self):
        seen = []

        def fake_curl(url, headers, body, key, timeout=120):
            seen.append(json.dumps(body))
            if "groq" in url:
                return {"choices": [{"message": {"content": "VERDICT: APPROVE"}}]}
            return {"candidates": [{"content": {"parts": [{"text": "VERDICT: APPROVE"}]}}]}

        with mock.patch.object(pa, "_curl", fake_curl):
            pa._gro("hello-gro", "k")
            try:
                pa._gai("hello-gai", "k")
            except Exception:
                pass  # GAI ladder internals may reject the fake shape; the first call is enough
        self.assertTrue(seen)
        frame_head = pa.PAPER_FRAME[:40]
        for body in seen:
            self.assertIn(frame_head, body)


class HookFrameCheck(unittest.TestCase):
    def test_board_prompt_without_frame_is_flagged(self):
        self.assertTrue(gate.missing_paper_frame("Board seat: sizing. Vote on the fork."))
        self.assertTrue(gate.missing_paper_frame("Board seat. paper account only."))

    def test_board_prompt_with_frame_passes(self):
        ok = "Board seat: sizing. This is a PAPER account growing $2.5K to $25K. Vote."
        self.assertFalse(gate.missing_paper_frame(ok))

    def test_non_design_prompts_untouched(self):
        self.assertFalse(gate.missing_paper_frame(
            "You are a cold second-agent logic reviewer. Review this diff. VERDICT: PASS or FAIL"))
        self.assertFalse(gate.missing_paper_frame("Read file X and return it verbatim"))
        for p in ("Explore the strategy fork in run_cycle.py and report where it branches.",
                  "Check if this repo is a fork of upstream alpaca-py.",
                  "os.fork() is used here, read the file and report callers."):
            self.assertFalse(gate.missing_paper_frame(p), p)

    def test_board_phrasing_variants_require_frame(self):
        for p in ("Board: execution seat. Vote on the sizing change.",
                  "board-seat: sizing review. Vote A or B.",
                  "Spawn 2 cold board seats to review this sizing fork.",
                  "Board seat: risk asymmetry. Vote.",
                  "Run the board vote on option B.",
                  "Two design forks to decide."):
            self.assertTrue(gate.missing_paper_frame(p), p)
        for p in ("Open the dashboard review page and read the numbers.",
                  "keyboard seats and onboard votes are unrelated words"):
            self.assertFalse(gate.missing_paper_frame(p), p)

    def test_design_fork_prompt_requires_frame(self):
        self.assertTrue(gate.missing_paper_frame("DESIGN FORK: option A or B?"))
        self.assertFalse(gate.missing_paper_frame(
            "DESIGN FORK on a paper account ($2.5K to $25K goal): option A or B?"))

    def _run_hook(self, prompt):
        payload = json.dumps({"tool_input": {"prompt": prompt}})
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                gate.main()
        return cm.exception.code

    def test_hook_blocks_unframed_board_prompt(self):
        self.assertEqual(self._run_hook("Board seat: execution. Vote A or B on the fork."), 2)

    def test_hook_allows_framed_board_prompt(self):
        self.assertEqual(self._run_hook(
            "Board seat: execution. Paper account, $2.5K to $25K goal. Vote A or B on the fork."), 0)


if __name__ == "__main__":
    unittest.main()
