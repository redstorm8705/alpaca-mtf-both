#!/usr/bin/env python3
# ruff: noqa: E501
"""Regression suite for preship_audit's STATIC-FACTS block and the automatic name-premise counter-prompt
(Rafael 2026-10-08: three Gro false REJECTs in one session were "undefined variable" claims on chunked diffs —
the definition sat outside the lines the reviewer saw).

Run:  python3 .claude/preship/test_static_facts.py     (exit 0 = pass)
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("preship_audit", os.path.join(HERE, "preship_audit.py"))
assert _spec is not None and _spec.loader is not None
pa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pa)

CLEAN = b"def f(x):\n    pos = x + 1\n    return pos\n"
UNDEF = b"def f(x):\n    return pos + x\n"


class StaticFacts(unittest.TestCase):
    def test_clean_file_names_ok(self):
        txt, ok = pa._static_facts("execution/x.py", CLEAN)
        if ok is None:
            self.skipTest("ruff not available here")
        self.assertTrue(ok)
        self.assertIn("ruff F821/F822/F823 undefined-name check: PASS", txt)
        self.assertIn("py_compile: PASS", txt)

    def test_undefined_name_reported(self):
        txt, ok = pa._static_facts("execution/x.py", UNDEF)
        if ok is None:
            self.skipTest("ruff not available here")
        self.assertFalse(ok)
        self.assertIn("F821", txt)
        self.assertIn("execution/x.py", txt)          # temp path replaced by the repo path

    def test_non_python_makes_no_claim(self):
        self.assertEqual(pa._static_facts("handoff.md", b"# x"), ("", None))

    def test_ruff_unavailable_makes_no_claim(self):
        with mock.patch.object(pa.subprocess, "run", side_effect=FileNotFoundError("no ruff")):
            self.assertEqual(pa._static_facts("execution/x.py", CLEAN), ("", None))


class NamePremise(unittest.TestCase):
    def test_matches_only_when_static_check_clean(self):
        t = "VERDICT: REJECT — `pos` is an undefined variable, leading to a runtime NameError."
        self.assertTrue(pa._name_premise_reject(t, True))
        self.assertFalse(pa._name_premise_reject(t, False))   # the static check found a real undefined name
        self.assertFalse(pa._name_premise_reject(t, None))    # the check did not run -> no claim
        self.assertFalse(pa._name_premise_reject("VERDICT: REJECT — off-by-one in the loop bound", True))

    def test_attribute_import_and_module_claims_are_not_countered(self):
        for t in ("VERDICT: REJECT — config.DAY_TIER_MAX is not defined in config.py, AttributeError at runtime",
                  "VERDICT: REJECT — `nonexistent_thing` is not defined: from os import nonexistent_thing fails",
                  "VERDICT: REJECT — the module helper is undefined"):
            self.assertFalse(pa._name_premise_reject(t, True), t)

    def test_only_the_reject_reason_is_matched(self):
        t = ("The static analysis shows no undefined name. However the stop comparison is inverted.\n"
             "VERDICT: REJECT — inverted stop comparison")
        self.assertFalse(pa._name_premise_reject(t, True))
        self.assertFalse(pa._name_premise_reject("`pos` is undefined\nVERDICT: APPROVE", True))
        self.assertFalse(pa._name_premise_reject("no verdict line, `pos` is undefined", True))

    def test_path_dependent_unbound_claims_are_not_countered(self):
        # ruff cannot prove every path assigns a name — such claims stay with the reviewer
        for t in ("VERDICT: REJECT — `pos` may be unbound when the branch is skipped (UnboundLocalError)",
                  "VERDICT: REJECT — `px` is referenced before assignment on the error path"):
            self.assertFalse(pa._name_premise_reject(t, True), t)


class AuditFlow(unittest.TestCase):
    """End-to-end audit_file with the reviewers mocked: an undefined-name REJECT contradicted by the static check
    gets exactly ONE counter-prompt carrying the evidence; a genuine second REJECT still blocks."""

    def _run(self, gro_replies, names_ok=True):
        calls = []

        def _gro(prompt, key):
            calls.append(prompt)
            return gro_replies[len(calls) - 1]
        with mock.patch.object(pa, "_git", side_effect=[(True, CLEAN, ""), (True, "", ""),
                                                        (True, "@@ -1 +1 @@\n-a\n+b\n", "")]), \
                mock.patch.object(pa, "_static_facts", return_value=("\nSTATIC ANALYSIS block", names_ok)), \
                mock.patch.object(pa, "_gai", return_value="VERDICT: APPROVE"), \
                mock.patch.object(pa, "_gro", side_effect=_gro), \
                mock.patch("builtins.open", mock.mock_open()), \
                mock.patch.object(pa.os, "makedirs"):
            ok, msg = pa.audit_file("execution/x.py", False, {"GROQ_API_KEY": "k", "GEMINI_API_KEY": "g"})
        return ok, msg, calls

    def test_name_reject_then_approve_after_counter(self):
        ok, msg, calls = self._run(["VERDICT: REJECT — `pos` is undefined (NameError)", "VERDICT: APPROVE"])
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 2)
        self.assertIn("AUTOMATIC COUNTER-PROMPT EVIDENCE", calls[1])
        self.assertIn("Your previous reason, quoted", calls[1])
        self.assertIn("`pos` is undefined", calls[1])

    def test_second_reject_still_blocks(self):
        ok, msg, calls = self._run(["VERDICT: REJECT — `pos` is undefined (NameError)",
                                    "VERDICT: REJECT — the reprice compares bid to the wrong limit"])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 2)

    def test_no_counter_when_static_check_found_the_name_missing(self):
        ok, msg, calls = self._run(["VERDICT: REJECT — `pos` is undefined (NameError)"], names_ok=False)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)

    def test_other_reject_gets_no_counter(self):
        ok, msg, calls = self._run(["VERDICT: REJECT — inverted comparison on the stop"])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)

    def test_static_block_reaches_the_prompt(self):
        ok, msg, calls = self._run(["VERDICT: APPROVE"])
        self.assertIn("STATIC ANALYSIS block", calls[0])


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
