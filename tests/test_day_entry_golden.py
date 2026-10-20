#!/usr/bin/env python3
# ruff: noqa: E501
"""REPLAY-1019: the Day-tier entry must match its pinned golden master (tests/golden/day_entry_grid.json).
A difference fails unless the owner-approved design record lists it and the fixture was regenerated with it.
No test reaches the broker (see tests/golden_day_entry.py)."""
import json
import unittest

from tests import golden_day_entry as g


class DayEntryGolden(unittest.TestCase):
    def test_grid_matches_pinned_results(self):
        pinned = json.loads(g.FIXTURE.read_text())
        live = g.compute_all()
        self.assertEqual(sorted(pinned), sorted(live), "grid cases changed")
        diffs = [k for k in sorted(live) if live[k] != pinned[k]]
        self.assertEqual(diffs, [], "entry results differ from the pinned golden master: " + "; ".join(
            f"{k}: pinned {pinned[k]} != now {live[k]}" for k in diffs[:5]))

    def test_grid_exercises_every_outcome(self):
        # The grid must reach submitted entries and skips on every track, so a dropped rule shows as a difference.
        pinned = json.loads(g.FIXTURE.read_text())
        for track in g.TRACKS:
            rows = [v for k, v in pinned.items() if k.startswith(track + "|")]
            self.assertTrue(any(v["ok"] or v["submit"] for v in rows), f"track {track}: no submitted entry")
        self.assertTrue(any(v["submit"] is None for v in pinned.values()), "no skipped case in the grid")


if __name__ == "__main__":
    unittest.main()
