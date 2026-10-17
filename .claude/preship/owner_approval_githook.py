#!/usr/bin/env python3
# ruff: noqa: E501
"""owner_approval_githook.py — run BY GIT as the pre-commit / pre-push hook (installed by behaviour_integrity.py).

Design record: logs/design_records/owner_approval_gates_2026-10-17.md (Approval-ID GATES-1017). Git runs this at the
moment of the commit or push, so an edit + commit + push inside one shell command is still checked (the PreToolUse gate
ran before anything was staged). Refuses (exit 1) when a trading-behaviour or gate path in the commit / pushed range has
no owner approval recorded from the owner's own 'APPROVE <ID>' message.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _git(*args) -> "tuple[bool, str]":
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.returncode == 0, r.stdout


def uncovered(range_args: list) -> list:
    import owner_approval_gate as oag
    os.environ.setdefault("CLAUDE_PROJECT_DIR", _git("rev-parse", "--show-toplevel")[1].strip())
    ok, out = _git("diff", "--name-only", *range_args)
    if not ok:
        return ["(could not list changed files — refusing)"]
    root = oag._root()
    recs = oag.approvals(root)
    return [p for p in (ln.strip() for ln in out.splitlines() if ln.strip())
            if not p.startswith(".claude/preship/markers/") and oag.is_behaviour(p) and not oag.covered(p, recs)]


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    if kind == "pre-commit":
        ok, gd = _git("rev-parse", "--git-dir")
        gd = gd.strip()
        if ok and any(os.path.exists(os.path.join(gd, n)) for n in
                      ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply")):
            return 0   # completing a merge/rebase: main's code is not this branch's change; pre-push checks the branch
        bad = uncovered(["--cached"])
    elif kind == "pre-push":
        bad = []
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) >= 2 and set(parts[1]) != {"0"}:
                _git("fetch", "-q", "origin", "main")
                bad += uncovered([f"origin/main...{parts[1]}"])
    else:
        return 0
    if bad:
        sys.stderr.write(f"OWNER APPROVAL ({kind}): trading-behaviour or gate code with no owner approval: {sorted(set(bad))}. "
                         "Bring the design to the owner; he approves with 'APPROVE <ID>'.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
