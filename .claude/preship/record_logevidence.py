#!/usr/bin/env python3
# ruff: noqa: E501  — dense help/log strings run long (project convention)
"""
record_logevidence.py — VERIFY that a fix/feature addresses a REAL, currently-logged issue.

Self-QA check #3 ("am I fully reviewing the logs?") made mechanical (Rafael mandate 2026-08-09).
Prevents the #104 class: a change shipped for a NON-problem on a premise the logs refute (7 working
prod scripts swapped on a "gemini-2.5-flash 404" claim the production logs showed was false —
114/114 successes, 0 404s). This tool does NOT trust the author's claim — it GREPS the cited log and
REFUSES to write the evidence marker when the pattern is absent (or below the required count).
Fabricated or stale evidence is impossible: no real match -> no marker -> the claim has no backing.

Usage:
  record_logevidence.py <gated_file> --log <path> --pattern <regex> [--ssh <host>] [--min N]

    <gated_file>  the changed file whose "fixes a logged issue" claim this backs; the marker binds
                  to its STAGED sha256 (so it goes stale if the file changes — like every other marker)
    --log         path to the log file (local path, or a path on the --ssh host)
    --pattern     an egrep/regex the real issue leaves in that log
    --ssh <host>  grep the log on a remote host (e.g. the OCI box `mtf-bot`) instead of locally —
                  most live evidence (trade_events.jsonl, mtf_bot.log) lives on the box
    --min N       minimum match count required to count as evidence (default 1)

Exit 0 + marker written iff the log genuinely contains >= N matches. Exit 1 (no marker) otherwise.
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


def _grep(log, pattern, host):
    # count + one sample match; -a (treat binary as text), -E (regex). Never trusts the author.
    if host:
        cnt = subprocess.run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", host,
                              f"grep -acE {json.dumps(pattern)} {json.dumps(log)}"],
                             capture_output=True, text=True, timeout=60)
        sample = subprocess.run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", host,
                                 f"grep -aE {json.dumps(pattern)} {json.dumps(log)} | head -1"],
                                capture_output=True, text=True, timeout=60)
    else:
        cnt = subprocess.run(f"grep -acE {json.dumps(pattern)} {json.dumps(log)}",
                             shell=True, capture_output=True, text=True, timeout=60)
        sample = subprocess.run(f"grep -aE {json.dumps(pattern)} {json.dumps(log)} | head -1",
                                shell=True, capture_output=True, text=True, timeout=60)
    try:
        n = int((cnt.stdout or "0").strip() or "0")
    except ValueError:
        n = 0
    return n, (sample.stdout or "").strip()[:300]


def main(argv):
    if len(argv) < 1 or "--log" not in argv or "--pattern" not in argv:
        sys.stderr.write(__doc__)
        return 2
    gated = argv[0]
    log = argv[argv.index("--log") + 1]
    pattern = argv[argv.index("--pattern") + 1]
    host = argv[argv.index("--ssh") + 1] if "--ssh" in argv else ""
    min_n = int(argv[argv.index("--min") + 1]) if "--min" in argv else 1

    if host and not os.path.isabs(log):
        # Over ssh the remote working dir is the login home, NOT the repo — a relative path
        # silently 0-matches (fail-safe REFUSE, but a false negative). Force an absolute path so
        # a real match is never missed.
        sys.stderr.write(
            f"logevidence: with --ssh the --log path must be ABSOLUTE (got {log!r}); the remote "
            f"working dir is undefined and a relative path would silently under-count.\n")
        return 2

    sha = _staged_sha(gated)
    if not sha:
        sys.stderr.write(f"logevidence: {gated} is not staged (git add it first)\n")
        return 1

    try:
        n, sample = _grep(log, pattern, host)
    except Exception as e:
        sys.stderr.write(f"logevidence: grep failed ({e}) — NO marker written (evidence unverified).\n")
        return 1

    where = f"{host}:{log}" if host else log
    if n < min_n:
        sys.stderr.write(
            f"logevidence: REFUSED — pattern {pattern!r} matched {n} time(s) in {where} "
            f"(need >= {min_n}). The logs do NOT support this 'fixes a logged issue' claim — "
            f"do not ship it as one. (This is exactly the #104 guard.)\n")
        return 1

    os.makedirs(MARKERS, exist_ok=True)
    key = gated.replace("/", "__")
    marker = os.path.join(MARKERS, f"{key}.logevidence.json")
    with open(marker, "w") as f:
        json.dump({
            "file": gated, "sha256": sha,
            "log": where, "pattern": pattern, "match_count": n, "min_required": min_n,
            "sample": sample,
            "recorded_at": datetime.datetime.now().astimezone().isoformat(),
        }, f, indent=1)
    sys.stdout.write(
        f"logevidence VERIFIED: {pattern!r} -> {n} matches in {where} (>= {min_n}). "
        f"marker {marker}\n  sample: {sample[:140]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
