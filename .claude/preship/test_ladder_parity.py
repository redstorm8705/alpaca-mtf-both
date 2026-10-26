#!/usr/bin/env python3
# ruff: noqa: E501
"""The Gemini model ladder is copied in four places on purpose (the CI gate and canary stay self-contained). This test
fails if the copies drift: gai_client.GAI_MODEL_LADDER, preship_audit._GAI_LADDER, .github/scripts/ci_audit.MODEL_LADDER,
gai_health.CANDIDATE_MODELS (the CI canary's probe list).
No network. Run: python3 .claude/preship/test_ladder_parity.py (exit 0 = pass). Signed: Claude, 2026-10-25.

Why: on 2026-10-25 gemini-3.6-flash served while every model in the ladders returned 429/503, so the gate reported
"Gemini down"; a ladder updated in one copy only would leave the other gate blind again.
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _tuple(path: str, name: str) -> tuple:
    tree = ast.parse(open(os.path.join(_ROOT, path), encoding="utf-8").read())
    for node in ast.walk(tree):
        targets: list = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is not None and any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return tuple(ast.literal_eval(value))
    raise AssertionError(f"{name} not found in {path}")


def main() -> int:
    canon = _tuple("gai_client.py", "GAI_MODEL_LADDER")
    copies = {".claude/preship/preship_audit.py": _tuple(".claude/preship/preship_audit.py", "_GAI_LADDER"),
              ".github/scripts/ci_audit.py": _tuple(".github/scripts/ci_audit.py", "MODEL_LADDER"),
              ".claude/preship/gai_health.py": _tuple(".claude/preship/gai_health.py", "CANDIDATE_MODELS")}
    fails = []
    for path, models in copies.items():
        ok = models == canon
        print(("  ok  " if ok else " FAIL ") + f"{path} matches gai_client ({len(models)} models)")
        if not ok:
            fails.append(path)
    if not canon or len(set(canon)) != len(canon):
        print(" FAIL gai_client ladder is empty or has duplicates")
        fails.append("gai_client")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
