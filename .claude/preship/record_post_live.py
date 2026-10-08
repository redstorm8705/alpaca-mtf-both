#!/usr/bin/env python3
# ruff: noqa: E501
"""Record a post-live audit (Rafael mandate 2026-10-08; see post_live_due.py).

  python3 .claude/preship/record_post_live.py <pr> --session YYYY-MM-DD --verdict PASS|BUGS_FIXED|BUGS_OPEN \
      --notes "what was checked: the new code executed (log lines), its decisions replayed vs fills, findings"

Appends one JSON line to logs/post_live_audits.jsonl in THIS checkout; commit it with the session's docs so other
checkouts / accounts see it via origin/main. BUGS_OPEN is allowed (the audit happened) but the notes must name the fix
being built.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECORDS = os.path.join(REPO, "logs", "post_live_audits.jsonl")


def main(argv: "list | None" = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--session", required=True, help="the full trading session audited, YYYY-MM-DD")
    ap.add_argument("--verdict", required=True, choices=("PASS", "BUGS_FIXED", "BUGS_OPEN"))
    ap.add_argument("--notes", required=True)
    a = ap.parse_args(argv)
    try:
        session = date.fromisoformat(a.session)
    except ValueError:
        print("--session must be YYYY-MM-DD", file=sys.stderr)
        return 2
    if len(a.notes.strip()) < 40:
        print("--notes must say what was checked (>= 40 chars): executed-code evidence, replay, findings",
              file=sys.stderr)
        return 2
    rec = {"pr": a.pr, "session": session.isoformat(), "verdict": a.verdict, "notes": a.notes.strip(),
           "ts": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()}
    os.makedirs(os.path.dirname(RECORDS), exist_ok=True)
    with open(RECORDS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    print(f"recorded post-live audit PR #{a.pr} ({a.verdict}, session {session}) -> {RECORDS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
