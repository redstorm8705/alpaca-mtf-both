#!/usr/bin/env python3
"""
Record a COLD SECOND-AGENT verdict for a file about to ship.

    python3 .claude/preship/record_cold2.py <file> PASS|FAIL

Binds the verdict to the sha256 of the file's STAGED content (`git show :<path>`), so a
PASS cannot be reused after the file changes. `preship_gate.py` requires a fresh PASS on
the exact bytes being shipped.

WHY THIS EXISTS (2026-07-22): nothing enforced the cold-2nd step — it was invoked
voluntarily. On one ~200-line DISPLAY-ONLY diff it caught four ship-blocking defects
across three rounds, all of which ruff/mypy/py_compile passed clean. Two of the four
were introduced by the previous round's fix, which is why the sha binding matters more
than the verdict.

A FAIL is recorded, not discarded — it must be visible that a review rejected the work.
"""
import sys
import os
import json
import time
import hashlib
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MARKER_DIR = os.path.join(REPO, ".claude", "preship", "markers")


def _staged_sha(relpath: str) -> str:
    r = subprocess.run(["git", "-C", REPO, "cat-file", "blob", f":{relpath}"],
                       capture_output=True, timeout=15)
    if r.returncode != 0:
        sys.stderr.write(
            f"ERROR: '{relpath}' is not staged. Stage it first (git add), so the "
            f"verdict binds to the exact bytes that will ship.\n"
            f"  (Recording against an unstaged file is how a marker ends up describing "
            f"content that never shipped.)\n"
        )
        sys.exit(1)
    return hashlib.sha256(r.stdout).hexdigest()


def main():
    if len(sys.argv) != 3 or sys.argv[2].upper() not in ("PASS", "FAIL"):
        sys.stderr.write(__doc__ + "\n")
        sys.exit(2)
    # NOT .lstrip("./") — lstrip strips CHARACTERS, so ".claude/x" became "claude/x" and
    # no self-gated (dot-leading) path could ever be audited: the gate's own remediation
    # command was proven to fail. Strip only a literal "./" PREFIX.
    relpath = sys.argv[1]
    while relpath.startswith("./"):
        relpath = relpath[2:]
    verdict = sys.argv[2].upper()
    sha = _staged_sha(relpath)
    os.makedirs(MARKER_DIR, exist_ok=True)
    path = os.path.join(MARKER_DIR, relpath.replace("/", "__") + ".cold2.json")
    with open(path, "w") as f:
        json.dump({"file": relpath, "verdict": verdict, "sha256": sha,
                   "ts": time.time()}, f, indent=2)
    print(f"cold-2nd {verdict} recorded for {relpath}")
    print(f"  sha256 {sha[:16]}...  marker {os.path.relpath(path, REPO)}")
    if verdict == "FAIL":
        print("  NOTE: a FAIL blocks the ship. Fix, re-stage, re-review, re-record.")


if __name__ == "__main__":
    main()
