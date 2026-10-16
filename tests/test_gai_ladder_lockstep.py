# ruff: noqa: E501
"""The Gemini model ladder lives in gai_client.GAI_MODEL_LADDER; three import-free gate scripts carry mirrors
(.claude/preship/preship_audit.py _GAI_LADDER, .github/scripts/ci_audit.py MODEL_LADDER, .claude/preship/gai_health.py
CANDIDATE_MODELS). 2026-10-16: the canonical tuple was updated and the mirrors were not, so the gates kept calling
only dead models. This test fails whenever a mirror differs from the canonical ladder. (Claude 2026-10-16)"""
import ast
import unittest
from pathlib import Path

from gai_client import GAI_MODEL_LADDER

ROOT = Path(__file__).resolve().parent.parent
MIRRORS = {".claude/preship/preship_audit.py": "_GAI_LADDER", ".github/scripts/ci_audit.py": "MODEL_LADDER",
           ".claude/preship/gai_health.py": "CANDIDATE_MODELS"}


def _assigned(path: Path, name: str):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in {path}")


class LadderLockstep(unittest.TestCase):
    def test_every_mirror_equals_the_canonical_ladder(self):
        for rel, name in MIRRORS.items():
            with self.subTest(file=rel):
                self.assertEqual(_assigned(ROOT / rel, name), tuple(GAI_MODEL_LADDER))


if __name__ == "__main__":
    unittest.main()
