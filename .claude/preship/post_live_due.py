#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
POST-LIVE AUDIT TRACKER (Rafael mandate 2026-10-08): every diff that goes live is AUDITED and BACKTESTED (replayed
against that day's real logs / fills) after its first FULL trading session live, and any bug found is fixed at once.

Why a mechanism: the same day's top-to-bottom day-tier audit found a live bug no pre-ship gate could see (Track B read
one empty IEX 5-min bar as a halt and benched AMD all day). Per CLAUDE.md DOCUMENTATION-IS-NOT-ENFORCEMENT, the rule is
carried by this tracker, not by a note.

  * DUE: a PR merged to origin/main (number >= FIRST_PR) that changed bot code, once one full regular session
    (Mon-Fri 09:30-16:00 ET) has started after the merge and ended. Holidays are not modelled: a holiday only makes an
    audit come due one session early (the strict direction).
  * DONE: a record for that PR in logs/post_live_audits.jsonl — read from origin/main AND this checkout's working file
    (written by record_post_live.py, then committed with the session's docs).

Modes: `--hook` (UserPromptSubmit: prints only when something is overdue; never fails the prompt) and the default
report (prints every tracked PR and its status). Read-only; never fetches (uses the local origin/main ref).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECORDS = "logs/post_live_audits.jsonl"
FIRST_PR = 531          # the mandate applies from the first PR shipped after it (Track A ETF routing)
_MERGE_RE = re.compile(r"Merge pull request #(\d+)")
_NOT_BOT = ("tests/", ".claude/", ".github/", "research/", "logs/", "docs/")


def _git(*args: str) -> "str | None":
    try:
        r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, timeout=10)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a tracker read never raises into the hook
        return None


def is_bot_code(path: str) -> bool:
    """A file that changes what the bot runs: a .py / .sh outside tests and tooling, or config."""
    p = path.strip()
    if not p or p.startswith(_NOT_BOT):
        return False
    return p.endswith((".py", ".sh")) or p in ("config.py", "requirements.txt")


def first_full_session_end(merged_et: datetime) -> datetime:
    """16:00 ET of the first weekday session that OPENS (09:30 ET) at or after `merged_et`."""
    d = merged_et.date()
    if merged_et.time() > time(9, 30):
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, time(16, 0), tzinfo=ET)


def merged_prs() -> list:
    """[(pr, merged_et, files)] for origin/main first-parent merges of PRs >= FIRST_PR that touched bot code."""
    out = _git("log", "origin/main", "--first-parent", "--merges", "--format=%H|%cI|%s", "-n", "200")
    rows = []
    for line in (out or "").splitlines():
        try:
            sha, iso, subj = line.split("|", 2)
        except ValueError:
            continue
        m = _MERGE_RE.search(subj)
        if not m or int(m.group(1)) < FIRST_PR:
            continue
        files = [f for f in (_git("diff", "--name-only", f"{sha}^1", sha) or "").splitlines() if is_bot_code(f)]
        if files:
            rows.append((int(m.group(1)), datetime.fromisoformat(iso).astimezone(ET), files))
    return rows


def audited_prs() -> set:
    texts = [_git("show", f"origin/main:{RECORDS}") or ""]
    try:
        with open(os.path.join(REPO, RECORDS), encoding="utf-8") as fh:
            texts.append(fh.read())
    except OSError:
        pass
    done = set()
    for text in texts:
        for line in text.splitlines():
            try:
                done.add(int(json.loads(line)["pr"]))
            except (ValueError, KeyError, TypeError):
                continue
    return done


def status(now: "datetime | None" = None) -> list:
    """[(pr, state, due_et, files)] with state OVERDUE / pending / done."""
    now = (now or datetime.now(ET)).astimezone(ET)
    done = audited_prs()
    out = []
    for pr, merged, files in merged_prs():
        due = first_full_session_end(merged)
        state = "done" if pr in done else ("OVERDUE" if now >= due else "pending")
        out.append((pr, state, due, files))
    return out


def main() -> int:
    rows = status()
    if "--hook" in sys.argv:
        over = [r for r in rows if r[1] == "OVERDUE"]
        if over:
            items = "; ".join(f"PR #{pr} ({', '.join(files[:3])}) — live since a full session ended {due:%Y-%m-%d}"
                              for pr, _s, due, files in over)
            print(f"[post-live audit OVERDUE] {items}. Audit + replay each against that session's real logs/fills, "
                  f"fix any bug found, then: python3 .claude/preship/record_post_live.py <pr> --session YYYY-MM-DD "
                  f"--verdict PASS|BUGS_FIXED|BUGS_OPEN --notes \"...\" (Rafael mandate 2026-10-08).")
        return 0
    for pr, state, due, files in rows:
        print(f"PR #{pr}: {state} (due after {due:%Y-%m-%d %H:%M} ET) — {', '.join(files)}")
    if not rows:
        print("no tracked PRs")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — never break the user's prompt
        print(f"[post-live audit tracker error: {e!r}]")
        sys.exit(0)
