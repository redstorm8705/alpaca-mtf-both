#!/usr/bin/env python3
# ruff: noqa: E501  — dense help/doc strings run long (project convention)
"""
record_design_waiver.py — the audited escape valve for Self-QA gate #4 ("BGG design-record").

    python3 .claude/preship/record_design_waiver.py <new_file_path> --reason "<why this is not
        new capability needing a design pass>"

WHO MAY RUN THIS (procedural, not cryptographic — read before using): per the Q3 board/GAI
consensus in logs/design_records/self_qa_gate4_design_record.md, this is Rafael's own escape
valve for when the gate misclassifies a small change as "new capability." A Claude session must
NOT self-invoke this script without Rafael's explicit go-ahead in the current chat message —
same bar as any other "explicit permission required" action. Nothing in this script can verify
WHO ran it; the control is procedural discipline plus the git-committed audit trail this marker
creates, identical to every other marker script in this repo (record_exemption.py,
record_cold2.py) which have the same honest limitation. A false or lazy waiver is a visible
audit-trail entry, not a silent bypass — that is the point of recording it at all.

Records the STAGED (or, absent that, working-tree) content sha256 of the target path when one is
available, for the audit trail — mirrors record_cold2.py / record_adversarial.py. THIS IS NOT A
HARD REQUIREMENT (fixed 2026-08-12, GAI preship finding 3C, verified real): the gate's PRIMARY use
case is waiving a file that does not exist ANYWHERE yet — design_record_gate.py blocks the Write
before the file is ever created, so it can never be staged, so an earlier version of this script
that REQUIRED a staged hash created a genuine deadlock (block the write -> can't create the file
-> can't stage it -> can't record the waiver -> still blocked, forever). The gate itself
(design_record_gate.py::_waiver_covers) only checks marker presence + freshness + a non-empty
reason — it does NOT check this sha — so requiring one here served no protective purpose and only
produced the deadlock. A sha is now recorded when one is obtainable (e.g. waiving an EXISTING
file) and omitted (null) when the path doesn't exist yet, which is the common case.
"""
import sys
import os
import json
import time
import hashlib
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MARKER_DIR = os.path.join(REPO, ".claude", "preship", "markers")
_MIN_REASON_LEN = 8


def _best_effort_sha(relpath: str):
    """Staged content sha256 if the path is in the git index; else working-tree bytes if the
    file exists on disk; else None (the primary case — a brand-new, not-yet-created file)."""
    r = subprocess.run(["git", "-C", REPO, "cat-file", "blob", f":{relpath}"],
                        capture_output=True, timeout=15)
    if r.returncode == 0:
        return hashlib.sha256(r.stdout).hexdigest()
    abspath = os.path.join(REPO, relpath)
    try:
        with open(abspath, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _argval(argv, flag):
    if flag not in argv:
        return None
    i = argv.index(flag)
    return argv[i + 1] if i + 1 < len(argv) else None


def main(argv):
    if not argv or argv[0].startswith("-"):
        sys.stderr.write(__doc__)
        return 2
    relpath = argv[0]
    while relpath.startswith("./"):
        relpath = relpath[2:]
    reason = _argval(argv, "--reason")
    if reason is None:
        sys.stderr.write('record_design_waiver: --reason "<why not new capability>" is required\n')
        return 2
    reason = reason.strip()
    if len(reason) < _MIN_REASON_LEN:
        sys.stderr.write(
            f"record_design_waiver: --reason must be a real explanation "
            f"(>= {_MIN_REASON_LEN} chars), not a placeholder\n")
        return 2
    # Best-effort only (see module docstring, GAI finding 3C): the PRIMARY case is a file that
    # does not exist anywhere yet, so `sha` is commonly None here — that is expected, not an
    # error, and must NOT block recording the waiver (the gate doesn't check this hash anyway).
    sha = _best_effort_sha(relpath)
    os.makedirs(MARKER_DIR, exist_ok=True)
    marker_path = os.path.join(MARKER_DIR, relpath.replace("/", "__") + ".designwaiver.json")
    # Atomic write (Peterffy board-seat review, 2026-08-12) — matches record_design.py's fix and
    # this project's RC-5 atomic-write convention for state files.
    tmp_path = marker_path + f".tmp{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump({
            "file": relpath,
            "sha256": sha,
            "reason": reason,
            "ts": time.time(),
        }, f, indent=2)
    os.replace(tmp_path, marker_path)
    print(f"design-record waiver recorded for {relpath}")
    print(f"  sha256 {(sha[:16] + '...') if sha else '(none — file does not exist yet)'}  reason: {reason!r}")
    print(f"  marker: {os.path.relpath(marker_path, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
