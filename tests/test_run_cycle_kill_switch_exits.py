"""P0 (audit 2026-09-24): a tripped kill switch blocks NEW ENTRIES only, never exits.

run_cycle() is a ~2,000-line orchestrator driven by main.py module state, so this
is a structural regression guard over its source (AST), not a behavioural replay:
  1. the kill-switch branch no longer returns (it used to skip every exit, stop
     and reconcile path);
  2. every entry path is gated on the flag: the entries-only HALT return (before
     QHM entries, run_scan and execute_entries), the overnight entry check and the
     F6 starter;
  3. the exit paths it used to skip are still reached after the flag is set.
"""

import ast
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "strategy" / "run_cycle.py").read_text()
TREE = ast.parse(SRC)
FLAG = "_kill_block_entries"


def _run_cycle_fn() -> ast.FunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "run_cycle":
            return node
    raise AssertionError("run_cycle not found")


def _names(node: ast.AST) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _call_lines(fn: ast.AST, attr: str) -> list:
    return sorted(
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
    )


class KillSwitchKeepsExits(unittest.TestCase):
    def setUp(self):
        self.fn = _run_cycle_fn()
        assigns = [
            n
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == FLAG for t in n.targets)
        ]
        self.assertEqual(len(assigns), 1, "flag must be assigned exactly once")
        self.flag_line = assigns[0].lineno
        # P0-4a: the flag is the kill result OR a control fault (fail-closed)
        self.assertTrue(
            {"_kill_tripped", "_control_fault"} <= _names(assigns[0].value)
        )

    def _try_wrapping(self, attr: str) -> ast.Try:
        for n in ast.walk(self.fn):
            if isinstance(n, ast.Try):
                body_calls = [
                    c
                    for b in n.body
                    for c in ast.walk(b)
                    if isinstance(c, ast.Call)
                    and (
                        (isinstance(c.func, ast.Attribute) and c.func.attr == attr)
                        or (isinstance(c.func, ast.Name) and c.func.id == attr)
                    )
                ]
                if body_calls:
                    return n
        raise AssertionError(f"{attr} is not inside a try")

    def test_kill_evaluation_fails_closed(self):
        t = self._try_wrapping("check_kill_switch")
        self.assertLess(t.lineno, self.flag_line)
        handler_names = {x for h in t.handlers for x in _names(h)}
        self.assertIn("_control_fault", handler_names)

    def test_sustained_fault_pages_once_in_rth(self):
        src = ast.unparse(self.fn)
        self.assertIn("_control_fault_streak = 0", src)
        self.assertIn("if _in_rth_now():", src)
        self.assertIn("_control_fault_streak == _CONTROL_FAULT_PAGE_AFTER", src)
        mod = ast.parse(SRC)
        consts = {
            t.id: n.value.value
            for n in mod.body
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        self.assertEqual(consts.get("_CONTROL_FAULT_PAGE_AFTER"), 3)

    def test_account_read_fails_closed(self):
        t = self._try_wrapping("get_portfolio_value")
        self.assertLess(t.lineno, self.flag_line)
        handler_names = {x for h in t.handlers for x in _names(h)}
        self.assertIn("_control_fault", handler_names)
        self.assertFalse(
            any(isinstance(r, ast.Return) for h in t.handlers for r in ast.walk(h)),
            "an account-read failure must not end the cycle",
        )

    def test_kill_branch_does_not_return(self):
        ifs = [
            n
            for n in ast.walk(self.fn)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Name)
            and n.test.id == FLAG
        ]
        self.assertEqual(len(ifs), 1)
        self.assertFalse(
            any(isinstance(n, ast.Return) for n in ast.walk(ifs[0])),
            "kill-switch branch must not return (it would skip every exit path)",
        )

    def test_no_other_kill_switch_return(self):
        for n in ast.walk(self.fn):
            if isinstance(n, ast.If) and "check_kill_switch" in ast.unparse(n.test):
                self.assertFalse(
                    any(isinstance(r, ast.Return) for r in ast.walk(n)),
                    f"early return on check_kill_switch at L{n.lineno}",
                )

    def _entries_halt(self) -> ast.If:
        for n in ast.walk(self.fn):
            if (
                isinstance(n, ast.If)
                and FLAG in _names(n.test)
                and any(isinstance(r, ast.Return) for r in n.body)
            ):
                return n
        raise AssertionError("no entries-only return gated on the kill flag")

    def test_entry_calls_sit_after_the_entries_halt(self):
        halt = self._entries_halt()
        self.assertGreater(halt.lineno, self.flag_line)
        for attr in ("maybe_enter_positions", "execute_entries"):
            lines = _call_lines(self.fn, attr)
            self.assertTrue(lines, attr)
            self.assertTrue(
                all(ln > halt.lineno for ln in lines), f"{attr} reachable before halt"
            )
        scans = [
            n.lineno
            for n in ast.walk(self.fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "run_scan"
        ]
        self.assertTrue(scans and all(ln > halt.lineno for ln in scans))

    def test_exit_paths_run_before_the_entries_halt(self):
        halt = self._entries_halt()
        for attr in (
            "check_partial_exits",
            "check_exits",
            "_check_exits_extended_hours",
        ):
            lines = _call_lines(self.fn, attr)
            self.assertTrue(lines, attr)
            self.assertTrue(
                any(self.flag_line < ln < halt.lineno for ln in lines),
                f"{attr} not reached between the kill flag and the entries halt",
            )

    def _enclosing_ifs(self, target_line: int) -> list:
        out = []
        for n in ast.walk(self.fn):
            if isinstance(n, ast.If):
                body_lines = [
                    c.lineno
                    for b in n.body
                    for c in ast.walk(b)
                    if hasattr(c, "lineno")
                ]
                if body_lines and min(body_lines) <= target_line <= max(body_lines):
                    out.append(n)
        return out

    def test_overnight_entry_check_is_gated(self):
        lines = _call_lines(self.fn, "_overnight_entry_check")
        self.assertEqual(len(lines), 1)
        gates = [
            n
            for n in self._enclosing_ifs(lines[0])
            if isinstance(n.test, ast.UnaryOp)
            and isinstance(n.test.op, ast.Not)
            and FLAG in _names(n.test)
        ]
        self.assertTrue(gates, "overnight entry check not gated on the kill flag")

    def test_f6_starter_gated_but_trims_still_run(self):
        starts = _call_lines(self.fn, "maybe_start_accumulation")
        self.assertEqual(len(starts), 1)
        ifexp = [
            n
            for n in ast.walk(self.fn)
            if isinstance(n, ast.IfExp)
            and FLAG in _names(n.test)
            and "maybe_start_accumulation" in ast.unparse(n.orelse)
        ]
        self.assertEqual(len(ifexp), 1, "F6 starter not gated on the kill flag")
        trims = _call_lines(self.fn, "evaluate_and_execute_trims")
        self.assertEqual(len(trims), 1)
        for n in self._enclosing_ifs(trims[0]):
            self.assertNotIn(
                FLAG, _names(n.test), "F6 trims (exits) must not be kill-gated"
            )


if __name__ == "__main__":
    unittest.main()
