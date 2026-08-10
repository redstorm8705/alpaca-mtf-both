#!/usr/bin/env python3
# ruff: noqa: E501  — dense doc/guidance strings run long (project convention)
"""
Record an ADVERSARIAL SELF-REVIEW verdict for a bot-code file about to ship (Self-QA gate #2,
Rafael 2026-08-10, BGG: separate marker, every bot-code ship).

    python3 .claude/preship/record_adversarial.py <file> PASS|FAIL --claims "<the ship's claims>"

WHAT IT ATTESTS (distinct from the cold-2nd, which reviews the DIFF's logic): an INDEPENDENT agent,
motivated to REFUTE, verified the ship's CLAIMS — the commit/PR wording ("fixes X", "dynamic",
"live", "verified", "self-heals") — against SOURCE (production logs, the code, runtime state), and
they hold (PASS) or were corrected before ship (record the corrected claims). It targets the two
failure modes documentation kept failing to stop:
  (a) labeling code live / proven / dynamic before it has ACTUALLY executed in production
      (honesty clause — "deployed, unexercised" until it runs);
  (b) a fix for a problem the logs do not actually show (the #104 class).

Binds the verdict to the sha256 of the file's STAGED content (`git show :<path>`), so a PASS cannot
survive an edit — every revision needs a fresh review, exactly like the cold-2nd. `preship_gate.py`
requires a fresh PASS on the exact bytes shipping, for every _is_bot_code file.

--claims is REQUIRED and stored in the marker: it is the specific assertion set that was verified,
so the record shows WHAT was checked (an empty attestation is not a review). A FAIL is recorded, not
discarded — a refuted claim must be visible.
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
            f"ERROR: '{relpath}' is not staged. Stage it first (git add), so the verdict "
            f"binds to the exact bytes that will ship.\n"
        )
        sys.exit(1)
    return hashlib.sha256(r.stdout).hexdigest()


_MIN_CLAIMS_LEN = 12   # a one-word truncation ("the") is not a review — demand a real assertion


def main():
    argv = sys.argv[1:]
    # Split at --claims: positionals (<file> <verdict>) come before it; the claims text is
    # EVERYTHING after it joined — so an UNQUOTED multi-word --claims is captured in full, not
    # silently truncated to its first token (cold-2nd threat 1). --claims must therefore be last.
    if "--claims" in argv:
        ci = argv.index("--claims")
        pos = [a for a in argv[:ci] if not a.startswith("--")]
        claims = " ".join(argv[ci + 1:]).strip()
    else:
        pos = [a for a in argv if not a.startswith("--")]
        claims = ""
    if len(pos) < 2 or pos[1].upper() not in ("PASS", "FAIL"):
        sys.stderr.write(__doc__ + "\n")
        sys.exit(2)
    relpath = pos[0]
    while relpath.startswith("./"):
        relpath = relpath[2:]
    verdict = pos[1].upper()
    if len(claims) < _MIN_CLAIMS_LEN:
        sys.stderr.write(
            "ERROR: --claims \"<the ship's claims that were verified>\" is REQUIRED and must be a "
            f"real assertion (>= {_MIN_CLAIMS_LEN} chars). An empty or one-word attestation is not "
            "a review — name what was checked against source (logs/code/runtime). Put it LAST.\n"
        )
        sys.exit(2)
    sha = _staged_sha(relpath)
    os.makedirs(MARKER_DIR, exist_ok=True)
    path = os.path.join(MARKER_DIR, relpath.replace("/", "__") + ".adversarial.json")
    with open(path, "w") as f:
        json.dump({"file": relpath, "verdict": verdict, "sha256": sha,
                   "ts": time.time(), "claims": claims}, f, indent=2)
    print(f"adversarial self-review {verdict} recorded for {relpath}")
    print(f"  sha256 {sha[:16]}...  claims: {claims[:80]}")
    if verdict == "FAIL":
        print("  NOTE: a FAIL blocks the ship — a claim was refuted. Correct the claim (or the "
              "code), re-stage, re-review, re-record.")


if __name__ == "__main__":
    main()
