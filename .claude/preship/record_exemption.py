#!/usr/bin/env python3
# ruff: noqa: E501  — dense help strings run long (project convention)
"""
record_exemption.py — declare ON THE RECORD that a gated bot-code change does NOT fix a logged
issue (so it needs no log-evidence). The audited opt-out of self-QA check #3 (Rafael mandate
2026-08-09; BGG Option A, Gro+GAI unanimous).

Why an opt-out and not a hard requirement: not every code change fixes a logged problem (a refactor,
a new signal not yet in the logs). Forcing log-evidence on all of them would over-block and teach
bypass. Instead, a change that does NOT claim a logged fix must carry a hash-bound exemption naming
a reason. This converts a SILENT skip (invisible negligence — the #104 failure mode) into an
EXPLICIT attestation committed to git history; a false exemption is a visible audit failure, not an
absent one.

Usage:
  record_exemption.py <gated_file> --reason "<why this is not a logged-issue fix>"

Writes .claude/preship/markers/<slug>.logexempt.json bound to the file's STAGED sha256 (any later
edit invalidates it — same binding as every other marker). The preship gate accepts EITHER a
verified log-evidence marker OR this exemption for a gated bot-code file.
"""
import sys
import os
import json
import hashlib
import subprocess
import datetime


HERE = os.path.dirname(os.path.abspath(__file__))
MARKERS = os.path.join(HERE, "markers")


def _staged_sha(path):
    r = subprocess.run(["git", "cat-file", "blob", f":{path}"], capture_output=True)
    if r.returncode != 0:
        return None
    return hashlib.sha256(r.stdout).hexdigest()


def _argval(argv, flag):
    """Value after `flag`, or None if the flag is absent OR is the last token (no value) —
    never IndexErrors on a trailing flag."""
    if flag not in argv:
        return None
    i = argv.index(flag)
    return argv[i + 1] if i + 1 < len(argv) else None


def main(argv):
    if not argv or argv[0].startswith("-"):
        sys.stderr.write(__doc__)
        return 2
    gated = argv[0]
    reason = _argval(argv, "--reason")
    if reason is None:
        sys.stderr.write('exemption: --reason "<why not a logged fix>" is required\n')
        return 2
    reason = reason.strip()
    if len(reason) < 8:
        sys.stderr.write("exemption: --reason must be a real explanation (>= 8 chars)\n")
        return 2
    sha = _staged_sha(gated)
    if not sha:
        sys.stderr.write(f"exemption: {gated} is not staged (git add it first)\n")
        return 1
    os.makedirs(MARKERS, exist_ok=True)
    marker = os.path.join(MARKERS, gated.replace("/", "__") + ".logexempt.json")
    with open(marker, "w") as f:
        json.dump({
            "file": gated, "sha256": sha, "reason": reason,
            "recorded_at": datetime.datetime.now().astimezone().isoformat(),
        }, f, indent=1)
    sys.stdout.write(
        f"exemption recorded (not-a-logged-fix): {gated} — {reason!r}\n  marker {marker}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
