#!/usr/bin/env python3
# ruff: noqa: E501
"""behaviour_integrity.py — PostToolUse hook (every tool): undo any unapproved change to trading-behaviour or gate code.

Design record: logs/design_records/owner_approval_gates_2026-10-17.md (Approval-ID GATES-1017). The PreToolUse gate
(owner_approval_gate.py) reads commands and cannot see every route (a script written elsewhere then run, `cd` + a bare
file name, `git apply`, `find -exec`). This hook checks the RESULT instead: after every tool call it lists every file in
the behaviour/gate scope that differs from HEAD (staged, unstaged, or new) and, for any not covered by a valid owner
approval, restores it from HEAD (new files are moved to .claude/quarantine/, never deleted), then tells the agent and
pages the owner on Slack. The route used does not matter — only the state of the files.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)


def _git(root, *args):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    return r.returncode, r.stdout


def changed_paths(root: str) -> "tuple[list, list]":
    """(modified/deleted tracked paths vs HEAD, untracked new paths) — repo-relative."""
    _, diff = _git(root, "diff", "--name-only", "HEAD")
    _, untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    return ([p for p in diff.splitlines() if p.strip()], [p for p in untracked.splitlines() if p.strip()])


def _page(text: str) -> None:
    try:
        sys.path.insert(0, os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        from alerts import send_slack
        send_slack(text)
    except Exception:
        pass


_GIT_HOOK = """#!/bin/sh
# Installed by .claude/preship/behaviour_integrity.py (owner_approval_gates_2026-10-17.md): no commit or push of
# trading-behaviour or gate code without the owner's APPROVE <ID>. Runs at the moment git commits/pushes.
f="$(git rev-parse --show-toplevel)/.claude/preship/owner_approval_githook.py"
[ -f "$f" ] || exit 0    # a checkout from before this gate existed: nothing to run
exec python3 "$f" "$(basename "$0")" "$@"
"""


def _git_dir(root: str) -> str:
    _, d = _git(root, "rev-parse", "--git-common-dir")
    d = d.strip()
    return d if os.path.isabs(d) else os.path.join(root, d)


def ensure_git_hooks(root: str) -> list:
    """(Re)install pre-commit and pre-push hooks that refuse unapproved behaviour changes. Returns repaired names."""
    hooks = os.path.join(_git_dir(root), "hooks")
    os.makedirs(hooks, exist_ok=True)
    fixed = []
    for name in ("pre-commit", "pre-push"):
        p = os.path.join(hooks, name)
        try:
            cur = open(p).read()
        except OSError:
            cur = None
        if cur != _GIT_HOOK:
            with open(p, "w") as f:
                f.write(_GIT_HOOK)
            os.chmod(p, 0o755)
            if cur is not None:
                fixed.append(f"git {name} hook (was modified)")
    return fixed


def merge_in_progress(root: str) -> bool:
    _, gd = _git(root, "rev-parse", "--git-dir")
    gd = gd.strip()
    gd = gd if os.path.isabs(gd) else os.path.join(root, gd)
    return any(os.path.exists(os.path.join(gd, n)) for n in
               ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply"))


def clean_local_settings(root: str) -> list:
    """settings.local.json is git-ignored, so the diff never sees it: strip hook-disabling keys from it directly."""
    p = os.path.join(root, ".claude", "settings.local.json")
    if not os.path.exists(p):
        return []
    try:
        d = json.load(open(p))
    except Exception:
        return []
    bad = [k for k in ("hooks", "disableAllHooks", "allowManagedHooksOnly") if k in d]
    if bad:
        for k in bad:
            d.pop(k, None)
        with open(p, "w") as f:
            json.dump(d, f, indent=2)
        return [f".claude/settings.local.json keys {bad} (removed)"]
    return []


def enforce(root: str) -> list:
    import owner_approval_gate as oag
    undone = ensure_git_hooks(root) + clean_local_settings(root)
    if merge_in_progress(root):
        return undone          # a merge/rebase is resolving; the ship-time checks judge its result
    tracked, untracked = changed_paths(root)
    cands = [p for p in tracked + untracked if oag.is_behaviour(p) and p != ".claude/settings.local.json"
             and not p.startswith((".claude/preship/markers/", ".claude/approvals/", ".claude/quarantine/"))]
    if not cands:
        return undone
    recs = oag.approvals(root)
    for p in tracked:
        if p.startswith(".claude/preship/markers/") or not oag.is_behaviour(p) or oag.covered(p, recs):
            continue
        src = os.path.join(root, p)
        if os.path.exists(src):   # keep a copy of the undone version — work is never lost, only set aside
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = os.path.join(root, ".claude", "quarantine", stamp, p)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
        _git(root, "restore", "--staged", "--worktree", "--source=HEAD", "--", p)
        undone.append(p + " (copy kept in .claude/quarantine/)")
    for p in untracked:
        if p.startswith((".claude/preship/markers/", ".claude/approvals/", ".claude/quarantine/")) \
                or p == ".claude/settings.local.json":     # handled by clean_local_settings, never moved
            continue
        if not oag.is_behaviour(p) or oag.covered(p, recs):
            continue
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = os.path.join(root, ".claude", "quarantine", stamp, p)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(os.path.join(root, p), dest)
        undone.append(p + " (moved to .claude/quarantine/)")
    return undone


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    root = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    try:
        undone = enforce(root)
    except Exception as exc:   # warn, do not block every tool on a transient git error
        sys.stderr.write(f"BEHAVIOUR INTEGRITY: check failed ({exc}).\n")
        sys.exit(0)
    if undone:
        tool = (payload or {}).get("tool_name", "?")
        msg = (f"BEHAVIOUR INTEGRITY: the last {tool} call changed trading-behaviour or gate code with no owner "
               f"approval; the change was UNDONE: {undone}. Bring the design to the owner; he approves with 'APPROVE <ID>'.")
        sys.stderr.write(msg + "\n")
        _page(f"🚨 Claude changed trading-behaviour or gate code without your approval — the bot undid it automatically: "
              f"{', '.join(undone)}. Nothing was committed.")
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
