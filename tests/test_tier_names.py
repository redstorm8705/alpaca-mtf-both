#!/usr/bin/env python3
# ruff: noqa: E501
"""Gate (CEO 2026-10-07): only four tier names appear in anything a person reads — Day, Swing, QHM, F6.

Scans every string literal in the bot's Python (docstrings excluded — they are developer documentation) for the retired
names "Core MTF", "main bot", "Forever-6"/"Forever 6" and "Day-Trade". A hit fails CI: route the name through
tier_names.tier_label() instead. Excluded: tests/, research/ (offline research artefacts), .claude/ (ship tooling),
strategy/movers/ (the retired Movers bot — forbidden to run), virtualenvs, logs/.

Run:  python3 tests/test_tier_names.py     (exit 0 = pass)
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_RETIRED = re.compile(r"(?i:Core MTF|\bmain[- ]bot\b|Forever[- ]6)|Day-Trade")   # "day-trade count" (PDT) is not a tier name
_EXCLUDED_DIRS = ("tests", "research", ".claude", "logs", "venv", ".venv", "node_modules", ".git")
_EXCLUDED_PREFIXES = ("strategy/movers/",)


def _docstring_ids(tree: ast.AST) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def scan() -> list:
    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.split("/", 1)[0] in _EXCLUDED_DIRS or rel.startswith(_EXCLUDED_PREFIXES) or "/worktrees/" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                m = _RETIRED.search(node.value)
                if m:
                    hits.append(f"{rel}:{node.lineno}: '{m.group(0)}' in {node.value[:90]!r}")
    return hits


class TierNames(unittest.TestCase):
    def test_no_retired_tier_name_in_person_facing_strings(self):
        hits = scan()
        self.assertEqual(hits, [], "retired tier names — use tier_names.tier_label():\n" + "\n".join(hits))

    def test_labels(self):
        from tier_names import TIER_NAMES, tier_label
        self.assertEqual(TIER_NAMES, ("Day", "Swing", "QHM", "F6"))
        self.assertEqual([tier_label(k) for k in ("daytrade", "intraday", "swing", "qhm", "forever6")],
                         ["Day", "Swing", "Swing", "QHM", "F6"])
        self.assertEqual(tier_label("unattributed", "Unattributed"), "Unattributed")
        self.assertEqual(tier_label(None), "")

    def test_scanner_catches_a_planted_name(self):
        tree = ast.parse('x = "QHM / Forever-6 holds"\ndef f():\n    """Core MTF docstring is fine."""\n')
        docs = _docstring_ids(tree)
        found = [n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and _RETIRED.search(n.value)]
        self.assertEqual(found, ["QHM / Forever-6 holds"])


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
