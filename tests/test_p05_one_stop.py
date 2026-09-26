"""P0-5 (2026-09-26): one stop for sizing and enforcement.

1A — after a material fill gap, entry_logic shifts the SIZED stop/target
     distances to the fill (`_shift_sized_levels`) instead of recomputing
     (the recompute dropped the H2 scalar).
2A — the after-hours GTC block no longer re-applies VIX widening to the
     stored stop.

entry_logic imports the whole trading stack, so the pure helper is compiled
out of the source with ast and executed in isolation; the run_cycle check is
structural.
"""

import ast
import math
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_EL = (_ROOT / "execution" / "entry_logic.py").read_text()
_RC = (_ROOT / "strategy" / "run_cycle.py").read_text()


def _load_helper():
    tree = ast.parse(_EL)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_shift_sized_levels"
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ns: dict = {"math": math}
    exec(compile(mod, "entry_logic_helper", "exec"), ns)
    return ns["_shift_sized_levels"]


shift = _load_helper()


class ShiftSizedLevels(unittest.TestCase):
    def test_long_keeps_sized_distances(self):
        # sized at 100.00: stop 97.50 (2.50), target 105.00 (5.00); fill 100.40
        stop, target = shift("long", 100.40, 100.00, 2.50, 105.00)
        self.assertEqual((stop, target), (97.90, 105.40))
        self.assertAlmostEqual(100.40 - stop, 2.50, places=6)

    def test_short_keeps_sized_distances(self):
        stop, target = shift("short", 49.80, 50.00, 1.20, 47.60)
        self.assertEqual((stop, target), (51.00, 47.40))
        self.assertAlmostEqual(stop - 49.80, 1.20, places=6)

    def test_rounding_never_wider_than_sized(self):
        # distances with sub-cent components round TOWARD the fill
        for fill, dist in ((101.237, 2.3333), (57.111, 1.0049), (12.345, 0.777)):
            ls, _ = shift("long", fill, fill, dist, fill + 2 * dist)
            ss, _ = shift("short", fill, fill, dist, fill - 2 * dist)
            self.assertLessEqual(fill - ls, dist + 1e-9, f"long wider @ {fill}/{dist}")
            self.assertLessEqual(ss - fill, dist + 1e-9, f"short wider @ {fill}/{dist}")
            self.assertLess(dist - (fill - ls), 0.01 + 1e-9)
            self.assertLess(dist - (ss - fill), 0.01 + 1e-9)

    def test_float_noise_exact_cent(self):
        stop, _ = shift("long", 100.10, 100.10, 2.10, 104.30)
        self.assertEqual(stop, 98.00)

    def test_degenerate_inputs_return_none(self):
        self.assertIsNone(shift("long", 100.0, 100.0, 0.0, 105.0))
        self.assertIsNone(shift("long", 0.0, 100.0, 2.0, 105.0))
        self.assertIsNone(shift("sideways", 100.0, 100.0, 2.0, 105.0))
        self.assertIsNone(shift("long", 1.00, 1.00, 1.50, 4.0))  # stop would be <= 0
        self.assertIsNone(shift("short", 1.00, 1.00, 0.5, -1.0))  # target would be <= 0


class PostFillUsesShift(unittest.TestCase):
    def test_no_recompute_after_fill(self):
        # only the pre-sizing get_stop_and_target call remains (with the override)
        calls = [
            n
            for n in ast.walk(ast.parse(_EL))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get_stop_and_target"
        ]
        self.assertEqual(len(calls), 1)
        self.assertIn("atr_mult_override", [k.arg for k in calls[0].keywords])
        self.assertEqual(_EL.count("_shift_sized_levels("), 2)  # def + one call

    def test_news_adjustment_not_reapplied_after_fill(self):
        self.assertEqual(_EL.count("get_news_adjusted_stop("), 1)


class AfterHoursNoSecondWidening(unittest.TestCase):
    def test_no_vix_rewidening_in_run_cycle(self):
        for token in ("_vix_mult_gtc", "_gdist_gtc", "_yf_gtc", "stop widened"):
            self.assertNotIn(token, _RC, token)
        self.assertIn("NO second VIX widening", _RC)


if __name__ == "__main__":
    unittest.main()
