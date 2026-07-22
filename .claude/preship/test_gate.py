#!/usr/bin/env python3
"""Regression suite for preship_gate ship-detection.

TESTS THE STAGED BLOB, NOT THE WORKING TREE (2026-07-22). An earlier version loaded
preship_gate.py from disk and reported 19/19 while the STAGED blob — the bytes actually
shipping — still contained both verified bypasses. A test that runs on a file that is
not shipping manufactures confidence; this one loads its module from
`git cat-file blob :.claude/preship/preship_gate.py`, so it can never again pass on
bytes that aren't in the commit. If the file is not staged, that is a hard FAIL. If the
working tree differs from the staged blob, that drift is a hard FAIL too — results for
the staged blob must never be mistaken for validation of unstaged edits.

Every case here is a real command that was either (a) a verified BYPASS the gate let
through, or (b) a verified FALSE POSITIVE that blocked ordinary read-only work. Both
failure directions matter: a missed ship is unaudited code, and a blocked read-only
command is an outage that gets the gate switched off.

Run:  python3 .claude/preship/test_gate.py
"""
import os
import shlex
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
REL = ".claude/preship/preship_gate.py"


def _staged_source() -> bytes:
    r = subprocess.run(["git", "-C", REPO, "cat-file", "blob", f":{REL}"],
                       capture_output=True, timeout=15)
    if r.returncode != 0:
        sys.exit(f"FAIL: {REL} is NOT STAGED — the shipping artifact cannot be "
                 f"tested.\n      git add {REL}  and re-run. (Testing the working "
                 f"tree instead is exactly how a 19/19 pass shipped a gate with "
                 f"both bypasses intact.)")
    return r.stdout


STAGED = _staged_source()
with open(os.path.join(HERE, "preship_gate.py"), "rb") as fh:
    WORKTREE = fh.read()
DRIFT = STAGED != WORKTREE

pg = types.ModuleType("pg_staged")
pg.__file__ = os.path.join(HERE, "preship_gate.py")   # keep REPO derivation correct
exec(compile(STAGED, f"{REL}<staged>", "exec"), pg.__dict__)

SHIP = {"commit", "push"}


def detect(cmd):
    """Mirror main()'s pipeline EXACTLY, including the heredoc strip.

    An earlier version of this test tokenised the raw command and so silently skipped
    _strip_heredocs — it reported a failure main() does not have. A test that does not
    reproduce the production path tests nothing.
    """
    argv = shlex.split(pg._strip_heredocs(cmd))
    return (bool(pg._git_subcommands(argv) & SHIP),
            pg._cmd_position(argv, "rsync") is not None)


def wt_mode(cmd):
    argv = shlex.split(pg._strip_heredocs(cmd))
    subs = pg._git_subcommands(argv)
    return pg._commit_worktree_mode(argv, subs)


HEREDOC = (
    "cat > /tmp/t.txt <<EOF\n"
    + "git " + "commit -m x and git " + "push are the ship verbs\n"
    + "EOF\n"
    + "git status"
)
PYC = 'python3 -c "print(\'git ' + 'commit -m x\')"'

CASES = [
    # (command, expect_ship, expect_rsync, note)
    ("git add -A && git " + "commit -m x && git " + "push", True, False,
     "VERIFIED BYPASS: chained add+commit+push took push branch, saw empty range"),
    ("git " + "commit -m push", True, False,
     "VERIFIED BYPASS: the word 'push' in the MESSAGE flipped it to the push branch"),
    ('bash -c "git ' + 'push"', True, False, "subshell payload"),
    ("(git " + "push)", True, False, "paren subshell"),
    ("v=$(git " + "push origin main)", True, False, "command substitution"),
    ("sudo git " + "push", True, False, "sudo prefix"),
    ("git -C /repo " + "commit -m x", True, False, "global -C option"),
    ("git " + "commit -m fix", True, False, "honest commit"),
    ("git " + "push origin main", True, False, "honest push"),
    ("git status --short", False, False, "read-only"),
    ("git add f.py", False, False, "stage only"),
    ("git diff --cached", False, False, "read-only"),
    ("git log --oneline -5", False, False, "read-only"),
    ("grep -rn rsync .", False, False,
     "VERIFIED FALSE POSITIVE: rsync as a grep PATTERN blocked read-only work"),
    (HEREDOC, False, False,
     "VERIFIED FALSE POSITIVE: heredoc body documenting the ship verbs"),
    (PYC, False, False,
     "VERIFIED FALSE POSITIVE: quoted test string inside python3 -c"),
    ("rsync -av ./x remote:/y", False, True, "real rsync"),
    ("cd /tmp && rsync -av a b", False, True, "rsync after &&"),
    ("sudo rsync -av a b", False, True, "sudo rsync"),
]

# Working-tree-mode classification: commits that ship WORKING-TREE bytes must be audited
# against the working tree, not `--cached`. `git commit -am` with three gated files
# modified-unstaged was a verified full bypass (2026-07-22).
WT_CASES = [
    ("git " + "commit -am x", True, "VERIFIED BYPASS: -a commits the working tree"),
    ("git " + "commit -a -m x", True, "-a split form"),
    ("git " + "commit --all -m x", True, "--all long form"),
    ("git " + "commit -m x file.py", True, "pathspec commit ships working-tree bytes"),
    ("git add f.py && git " + "commit -m x", True,
     "VERIFIED BYPASS: hook fires before add; --cached shows the PRE-add index"),
    ("git " + "commit -m x", False, "plain commit ships the index — --cached ok"),
    ("git " + "commit -m push", False, "message consumed by -m, not a pathspec"),
    ("git " + "commit --amend --no-edit", False, "amend without -a reuses the index"),
    # Redirect/pipe over-block regression (2026-07-22): `2>&1` was read as a pathspec.
    ("git " + "commit -m x 2>&1", False,
     "VERIFIED FALSE POSITIVE: 2>&1 redirect is not a pathspec"),
    ("git " + "commit -m x 2>&1 | tail -4", False,
     "VERIFIED FALSE POSITIVE: commit piped to tail for logging"),
    ("git " + "commit -m x > out.log", False, "stdout redirect is not a pathspec"),
    ("git " + "commit -am x 2>&1", True, "-a still wins before the redirect"),
    ("git " + "commit -m x file.py 2>&1", True,
     "a real pathspec precedes the redirect → still worktree mode"),
    ("git " + "commit -m x 2> err.log", False,
     "bare stderr redirect + spaced target — both skipped, no pathspec"),
    # Redirect BEFORE the working-tree indicator — must be SKIPPED, not break the scan
    # (cold-2nd 2026-07-22: `break` missed these = a working-tree ship slipping to index
    # mode, the dangerous direction).
    ("git " + "commit 2>&1 -am x", True,
     "redirect before -a must NOT hide it (skip, don't break)"),
    ("git " + "commit >log --all", True,
     "redirect before --all must NOT hide it"),
    ("git " + "commit 'weird>name.py'", True,
     "a pathspec containing '>' is NOT a redirect operator → still worktree mode"),
]


def main():
    bad = 0
    print(f"module under test: STAGED blob :{REL} ({len(STAGED)} bytes)")
    for cmd, exp_ship, exp_rs, note in CASES:
        ship, rs = detect(cmd)
        ok = (ship == exp_ship and rs == exp_rs)
        if not ok:
            bad += 1
        print(f"  {'OK  ' if ok else 'FAIL'} ship={ship!s:5} rsync={rs!s:5} {note}")
    for cmd, exp_wt, note in WT_CASES:
        wt = wt_mode(cmd)
        ok = (wt == exp_wt)
        if not ok:
            bad += 1
        print(f"  {'OK  ' if ok else 'FAIL'} wt_mode={wt!s:5}          {note}")
    print()
    if DRIFT:
        print(f"FAIL: DRIFT — working tree {REL} ({len(WORKTREE)} bytes) differs from "
              f"the staged blob ({len(STAGED)} bytes).")
        print("      The results above describe the STAGED bytes only. `git add` the "
              "file so the shipping artifact is the one you edited, then re-run.")
        sys.exit(1)
    if bad:
        print(f"RESULT: {bad}/{len(CASES) + len(WT_CASES)} MISMATCHES")
        sys.exit(1)
    print(f"RESULT: all {len(CASES) + len(WT_CASES)} correct (staged blob, no drift)")


if __name__ == "__main__":
    main()
