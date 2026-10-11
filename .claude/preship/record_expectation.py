#!/usr/bin/env python3
# ruff: noqa: E501  — dense help/log strings run long (project convention)
"""
record_expectation.py — register a change's POST-LIVE EXPECTATION before it ships (post-live engine increment 1;
design record logs/design_records/post_live_engine_2026-10-11.md; board Kim/Majors/López de Prado + Gro + GAI).

Every bot-code change states, BEFORE it ships, what will prove it ran in production, what it should change, and what
live result would prove it wrong. preship_gate.py refuses a bot-code commit/push without a fresh marker from this tool,
bound to both the code file's staged sha256 and the expectation record's staged sha256 (frozen at merge — an
expectation edited after seeing live results invalidates the marker). The OCI evaluator
(scripts/post_live_eval.py) reads the committed record after each session.

  record_expectation.py <expect.json> --covers <file> [<file> ...] [--ssh mtf-bot]

The record (logs/post_live/expect/<change>.json, committed WITH the change):
  {
    "change": "short-slug",                     # unique; also the record's file name
    "tier": 1 | 2,                              # 2 = risk-path or strategy change
    "paths": ["execution/x.py", ...],           # the code files this change touches
    "proof": [{"log": "logs/mtf_bot.log", "pattern": "<regex>"}],   # emitted ONLY by the new code
    "expected_rate": "how often the proof should fire (e.g. 'each co-hold entry; ~1/day on co-hold days')",
    "window_sessions": 1-10,                    # sessions to watch before NOT_EXERCISED expires into a finding
    "reversal": "the live result that would prove the change wrong",
    "metrics": [{"name": "...", "source": "...", "direction": "up|down|none", "bound": "..."}],  # tier 2: required
    "verification": "tests/...py or research/...py",   # tier 2: required, must exist
    "baseline_exempt_reason": "..."             # optional: why a proof pattern may already appear before ship
  }

UNIQUE-PROOF CHECK (board, Majors): each proof pattern is grepped in the CURRENT production log on --ssh (default
mtf-bot). A pattern that already matches would let the "it ran" check pass trivially, so the tool refuses it unless
baseline_exempt_reason explains why. Exit 0 + markers written iff the record is valid; exit 1 otherwise.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
MARKERS = os.path.join(HERE, "markers")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")


def _staged_sha(path: str) -> "str | None":
    r = subprocess.run(["git", "-C", REPO, "cat-file", "blob", f":{path}"], capture_output=True)
    if r.returncode != 0:
        return None
    return hashlib.sha256(r.stdout).hexdigest()


def validate(rec: object) -> list:
    """Schema problems (empty list = valid). Pure; never raises."""
    errs: list = []
    if not isinstance(rec, dict):
        return ["record is not a JSON object"]
    if not isinstance(rec.get("change"), str) or not _SLUG_RE.match(rec["change"]):
        errs.append("change: lower-case slug (3-81 chars, a-z 0-9 . _ -) required")
    if rec.get("tier") not in (1, 2):
        errs.append("tier: 1 or 2 required")
    paths = rec.get("paths")
    if not (isinstance(paths, list) and paths and all(isinstance(p, str) and p for p in paths)):
        errs.append("paths: non-empty list of file paths required")
    proof = rec.get("proof")
    if not (isinstance(proof, list) and proof):
        errs.append("proof: non-empty list of {log, pattern} required")
    else:
        for i, p in enumerate(proof):
            if not (isinstance(p, dict) and isinstance(p.get("log"), str) and p["log"].startswith("logs/")
                    and isinstance(p.get("pattern"), str) and len(p["pattern"].strip()) >= 8):
                errs.append(f"proof[{i}]: a logs/... path and a regex of at least 8 characters required")
                continue
            try:
                re.compile(p["pattern"])
            except re.error as e:
                errs.append(f"proof[{i}]: invalid regex ({e})")
    if not (isinstance(rec.get("expected_rate"), str) and rec["expected_rate"].strip()):
        errs.append("expected_rate: required")
    w = rec.get("window_sessions")
    if isinstance(w, bool) or not isinstance(w, int) or not 1 <= w <= 10:
        errs.append("window_sessions: integer 1-10 required")
    elif w < 5 and not (isinstance(rec.get("window_reason"), str) and rec["window_reason"].strip()):
        errs.append("window_reason: required when window_sessions < 5 (the default is 5; justify a shorter watch)")
    slug = rec.get("change") if isinstance(rec.get("change"), str) else ""
    exempt = str(rec.get("baseline_exempt_reason") or "").strip()
    if slug and isinstance(proof, list) and not exempt:
        for i, p in enumerate(proof):
            if isinstance(p, dict) and slug not in str(p.get("pattern", "")):
                errs.append(f"proof[{i}]: the pattern must contain the change slug {slug!r} — the new code logs a "
                            f"unique tag (board: a structured change_id event), or give baseline_exempt_reason")
    if not (isinstance(rec.get("reversal"), str) and len(rec["reversal"].strip()) >= 10):
        errs.append("reversal: a concrete live result that would prove the change wrong is required")
    if rec.get("tier") == 2:
        mets = rec.get("metrics")
        if not (isinstance(mets, list) and mets):
            errs.append("metrics: required for a tier-2 change")
        else:
            for i, m in enumerate(mets):
                if not (isinstance(m, dict) and all(isinstance(m.get(k), str) and m[k].strip()
                                                    for k in ("name", "source", "bound"))
                        and m.get("direction") in ("up", "down", "none")):
                    errs.append(f"metrics[{i}]: name, source, bound and direction (up|down|none) required")
        ver = rec.get("verification")
        if not (isinstance(ver, str) and ver.strip()):
            errs.append("verification: a test or replay script path is required for a tier-2 change")
        elif not os.path.exists(os.path.join(REPO, ver)):
            errs.append(f"verification: {ver} does not exist")
    return errs


_COUNT_SCRIPT = """
import json, os, re, sys
a = json.loads(sys.stdin.readline())
p = os.path.join("/home/ubuntu/mtf-bot", a["log"])
if not os.path.exists(p):
    print(0); sys.exit(0)
rx = re.compile(a["pattern"]); n = 0
with open(p, encoding="utf-8", errors="replace") as fh:
    for ln in fh:
        if rx.search(ln):
            n += 1
print(n)
"""


def _baseline_count(log: str, pattern: str, host: str) -> "int | None":
    """Matches of `pattern` in the production log on `host`, counted with PYTHON's regex on the host (the engine the
    evaluator uses — cold-2nd: grep -E reads \\d as 'd' and bash expands $ inside double quotes). The script and its
    arguments go over stdin, never through a shell string. 0 when the log does not exist yet; None when unreadable."""
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", host,
                            "python3 -c " + shlex.quote(_COUNT_SCRIPT)], input=json.dumps({"log": log, "pattern": pattern}) + "\n",
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            return None
        return int(out.splitlines()[-1])
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def main(argv: "list | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or "--covers" not in argv:
        print(__doc__)
        return 1
    expect_path = argv[0]
    i = argv.index("--covers")
    covers = []
    for a in argv[i + 1:]:
        if a.startswith("--"):
            break                            # the next flag ends --covers (cold-2nd: --ssh's value was swallowed)
        covers.append(a)
    host = "mtf-bot"
    if "--ssh" in argv:
        j = argv.index("--ssh") + 1
        if j >= len(argv) or argv[j].startswith("--"):
            print("FAIL: --ssh needs a host")
            return 1
        host = argv[j]
    try:
        rec = json.load(open(os.path.join(REPO, expect_path), encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"FAIL: cannot read {expect_path}: {e}")
        return 1
    errs = validate(rec)
    if not errs and os.path.basename(expect_path) != f"{rec['change']}.json":
        errs.append(f"file name must be {rec['change']}.json")
    if not errs and not expect_path.startswith("logs/post_live/expect/"):
        errs.append("record must live under logs/post_live/expect/")
    missing = [c for c in covers if c not in rec.get("paths", [])] if not errs else []
    if missing:
        errs.append(f"--covers files not listed in paths: {missing}")
    if not covers:
        errs.append("--covers: at least one file required")
    if errs:
        print("FAIL: expectation record invalid:\n  - " + "\n  - ".join(errs))
        return 1
    exempt = str(rec.get("baseline_exempt_reason") or "").strip()
    for p in rec["proof"]:
        n = _baseline_count(p["log"], p["pattern"], host)
        if n is None:
            print(f"FAIL: could not read {p['log']} on {host} for the unique-proof check (no marker written).")
            return 1
        if n > 0 and not exempt:
            print(f"FAIL: proof pattern {p['pattern']!r} already matches {n} line(s) in production {p['log']} — it "
                  f"would pass without the new code running. Emit a unique tag, or give baseline_exempt_reason.")
            return 1
        print(f"  unique-proof check: {p['log']} :: {p['pattern']!r} -> {n} existing match(es)"
              f"{' (exempt: ' + exempt + ')' if n else ''}")
    exp_sha = _staged_sha(expect_path)
    if exp_sha is None:
        print(f"FAIL: {expect_path} is not staged (git add it first — the record ships with the change).")
        return 1
    os.makedirs(MARKERS, exist_ok=True)
    for c in covers:
        sha = _staged_sha(c)
        if sha is None:
            print(f"FAIL: {c} is not staged.")
            return 1
        marker = {"file": c, "sha256": sha, "expect_path": expect_path, "expect_sha256": exp_sha,
                  "change": rec["change"], "tier": rec["tier"],
                  "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        with open(os.path.join(MARKERS, c.replace("/", "__") + ".expect.json"), "w", encoding="utf-8") as fh:
            json.dump(marker, fh, indent=1)
        print(f"  marker: {c} -> {expect_path} (tier {rec['tier']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
