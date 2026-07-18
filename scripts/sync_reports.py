#!/usr/bin/env python3
# ruff: noqa: E501
"""
scripts/sync_reports.py — durable cross-account persistence of routine AUDIT reports (Option B;
board 3-1 + Gro, 2026-07-18; design: logs/reports_durability_design_2026-07-18.md).

Runs on OCI as a SINGLE post-routine cron in a fixed late window (after the day's report
routines, before the 2am restart). It:
  1. stages ONLY the un-ignored audit-report globs (NEVER `git add -A` — it cannot commit code);
  2. commits them in ONE batch if any changed;
  3. pushes once to main with `pull --rebase` retry on non-fast-forward (bounded);
  4. runs an expected-vs-present reconciliation and Slack-alerts on any gap.
So any operator account's `git pull` sees 100% of routine audit reports. It NEVER force-pushes,
NEVER touches non-report files, and fails LOUD (Slack + non-zero exit) rather than masking a
sync failure — a silently-missing report is exactly what this exists to prevent.

The report globs here MUST stay in lockstep with the .gitignore negations; a mismatch means a
report is written but never un-ignored (invisible) — the reconciliation catches the symptom.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
_LOGS = _ROOT / "logs"
ET = ZoneInfo("America/New_York")

# Un-ignored audit-report globs — MUST match the .gitignore negations exactly.
_REPORT_GLOBS = [
    "logs/gemini_audit_*.txt",
    "logs/midday_audit_*.json",
    "logs/midday_gemini_*.txt",
    "logs/meta_audit_latest.json",
    "logs/ai_audit_meta_*.json",
    "logs/gex_daily_audit_*.json",
    "logs/wtp_*.md",
    "logs/weekly_audit_rollup_*.md",
    "logs/score16_report.json",
]

_PUSH_RETRIES = 3


def _slack(msg: str) -> None:
    """Best-effort Slack; never raises."""
    try:
        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))
        from alerts import send_slack
        send_slack(msg)
    except Exception as e:
        print(f"[sync_reports] slack send failed: {e}", file=sys.stderr)


def _git(*args, timeout: int = 120):
    """Run a git command in the repo. Returns (returncode, combined stdout+stderr)."""
    try:
        p = subprocess.run(
            ["git", "-C", str(_ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, f"git invocation failed: {e}"


def _stage_reports() -> list[str]:
    """git add ONLY the report globs (never -A → cannot stage code). A glob matching nothing
    is skipped (no empty `git add --` error). Returns the list of staged report paths."""
    for g in _REPORT_GLOBS:
        matches = [str(p.relative_to(_ROOT)) for p in _ROOT.glob(g)]
        if matches:
            _git("add", "--", *matches)
    rc, out = _git("diff", "--cached", "--name-only")
    if rc != 0:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def _push_with_rebase_retry() -> bool:
    """Push local commit(s) to main. On non-fast-forward, `pull --rebase` then retry (bounded).
    A rebase failure (e.g. a dirty OCI tree) is NEVER force-resolved — it Slack-alerts and aborts
    so the deploy channel is never corrupted. Returns True on a confirmed push."""
    for attempt in range(_PUSH_RETRIES):
        prc, pout = _git("push", "origin", "main")
        if prc == 0:
            return True
        rrc, rout = _git("pull", "--rebase", "origin", "main")
        if rrc != 0:
            # Restore a clean pre-rebase state so the NEXT run isn't wedged mid-rebase.
            # --abort keeps our local commit (returns to pre-pull HEAD); it is a harmless
            # no-op if no rebase is actually in progress (e.g. a network error before rebase
            # started). NEVER force-resolves — the deploy channel is never corrupted.
            _git("rebase", "--abort")
            _slack(
                f":rotating_light: sync_reports PULL-REBASE failed (attempt {attempt + 1}/"
                f"{_PUSH_RETRIES}) — conflict or dirty OCI tree; rebase aborted to clean state, "
                f"NOT force-pushing (deploy channel protected). Reports committed locally on OCI "
                f"only; investigate. {rout[:220]}"
            )
            return False
    _slack(
        f":rotating_light: sync_reports PUSH failed after {_PUSH_RETRIES} retries — reports are "
        f"committed on OCI but NOT on GitHub. Another account's `git pull` will miss them until "
        f"a manual push. Investigate the OCI→GitHub push path."
    )
    return False


def _reconcile_and_alert() -> None:
    """Expected-vs-present: on a weekday the core daily audit reports for TODAY's ET date should
    exist on disk. Any missing one → Slack (the affirmative 100%-capture invariant; a whole-day
    miss is loud, not silent). Weekend routines (weekly_postmortem) are not asserted here — they
    are date-stamped to Friday and covered by the weekday set the following run."""
    now = datetime.now(ET)
    d = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:  # Sat/Sun — no daily report routines run
        return
    # Only reconcile on a CONFIRMED trading day. eod_{d}.json is the bot's end-of-day marker,
    # written at the close on days it actually ran — absent on weekday market holidays (routines
    # produce nothing) and bot-down days. Gating on it kills the weekday-holiday false positive;
    # a genuine bot-down day is surfaced by the bot's own service alarms, not this report gate.
    if not (_LOGS / f"eod_{d}.json").exists():
        print(f"[sync_reports] no eod_{d}.json — not a confirmed trading day; skipping reconciliation.")
        return
    expected = [
        f"gemini_audit_{d}.txt",
        f"midday_audit_{d}.json",
        f"midday_gemini_{d}.txt",
        f"gex_daily_audit_{d}.json",
    ]
    missing = [f for f in expected if not (_LOGS / f).exists()]
    if missing:
        _slack(
            f":warning: sync_reports RECONCILIATION — {len(missing)} expected audit report(s) "
            f"MISSING for {d}: {missing}. A routine likely failed to run or write; investigate "
            f"its cron log."
        )
        print(f"[sync_reports] reconciliation gap for {d}: {missing}", file=sys.stderr)


def main() -> int:
    staged = _stage_reports()
    if staged:
        now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
        rc, out = _git(
            "-c", "user.name=mtf-report-sync",
            "-c", "user.email=reports@mtf-bot.local",
            "commit", "-m",
            f"reports: routine audit report sync ({now}) [{len(staged)} file(s)]",
        )
        if rc != 0:
            _slack(f":rotating_light: sync_reports COMMIT failed: {out[:280]}")
            return 1
        print(f"[sync_reports] committed {len(staged)} report file(s).")
    else:
        print("[sync_reports] no new report changes to commit.")

    # Push if we have any local commit ahead of origin (this run's, or a prior failed run's).
    # A FAILURE to even compute the ahead-count must NOT be treated as "0 ahead → skip" — that
    # would silently strand committed reports on OCI with a green exit (the exact silent-miss
    # this script prevents). Fail LOUD instead (cold-2nd 2026-07-18).
    arc, ahead = _git("rev-list", "--count", "origin/main..HEAD")
    if arc != 0:
        _slack(
            f":rotating_light: sync_reports could not compute commits-ahead of origin/main "
            f"(git error) — reports may be committed on OCI but their GitHub state is UNVERIFIED. "
            f"Not exiting green. {ahead[:200]}"
        )
        return 1
    have_ahead = ahead.strip().isdigit() and int(ahead) > 0
    if have_ahead and not _push_with_rebase_retry():
        return 1

    _reconcile_and_alert()
    print("[sync_reports] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
