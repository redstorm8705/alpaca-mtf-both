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
import fnmatch
import json
import os
import subprocess
import sys
import time as _time_mod
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
_LOGS = _ROOT / "logs"
ET = ZoneInfo("America/New_York")

# Load .env so SLACK_WEBHOOK_URL (and creds) are present even under cron, whose env is minimal.
# Historically the failure alerts here were [ALERT no-op] because the webhook was absent from the
# cron env — making the "fail LOUD" contract silently FALSE (board deploy-infra 2026-07-24).
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_ROOT / ".env")
except Exception:
    pass

# Ship channel (2026-07-24): branch protection blocks direct pushes to main, so report commits
# ship via an auto-merged PR. gh is NOT installed on OCI → GitHub REST API via the stored PAT.
_REPO_SLUG = "redstorm8705/alpaca-mtf-both"
_PR_POLL_TIMEOUT = 360   # max seconds to wait for the required `preship` check to go green
_PR_POLL_INTERVAL = 15   # seconds between mergeable-state polls

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


def _github_token():
    """PAT for the GitHub REST API. Prefers env (GITHUB_TOKEN/GH_TOKEN); falls back to the git
    credential store (credential.helper=store → ~/.git-credentials — the same secret git already
    uses to push). Returns the token string or None."""
    tok = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if tok:
        return tok.strip()
    try:
        cred = Path.home() / ".git-credentials"
        if cred.exists():
            for line in cred.read_text().splitlines():
                line = line.strip()
                if "github.com" not in line or "@" not in line or "//" not in line:
                    continue
                # forms: "<token>", "<user>:<token>", "x-access-token:<token>". Split on the
                # FIRST colon only (the user:token separator) so a token that itself contains a
                # ':' is preserved intact — the token is everything after the first colon.
                userinfo = line.split("//", 1)[1].rsplit("@", 1)[0]
                return userinfo.split(":", 1)[1] if ":" in userinfo else userinfo
    except Exception as e:
        print(f"[sync_reports] token read failed: {e}", file=sys.stderr)
    return None


def _api(method: str, path: str, token: str, body=None):
    """GitHub REST call. Returns (status_int, parsed_json_or_dict). Never raises — an HTTPError is
    captured WITH its status so callers can branch and fail LOUD (never silently)."""
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mtf-report-sync",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_error": str(e)}


def _commits_ahead_are_reports_only():
    """Return (ok, offending_paths). Verify every file THIS side introduced (origin/main...HEAD,
    three-dot = merge-base..HEAD) matches a report glob, so the auto-merged PR can NEVER carry a
    non-report (code/config) change to main — even if a stray commit landed on OCI's local main.
    Board deploy-infra 2026-07-24: trust the DIFF, not the PR's 'logs only' label.
    Three-dot (not two-dot) so an UPSTREAM code merge that OCI hasn't pulled yet is NOT mis-flagged
    as an offender (which would strand report syncing until OCI re-syncs) — it scans only what HEAD
    added since diverging, which is exactly what the PR introduces. A stray non-report commit on
    OCI's own side still shows and is still blocked. A git error is fail-CLOSED (returns not-ok)."""
    rc, out = _git("diff", "--name-only", "origin/main...HEAD")
    if rc != 0:
        return False, [f"(git diff failed: {out[:120]})"]
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    offending = [f for f in files
                 if not any(fnmatch.fnmatch(f, g) for g in _REPORT_GLOBS)]
    return (not offending), offending


def _ship_via_pr() -> bool:
    """Ship local report commit(s) to main via an AUTO-MERGED PR (2026-07-24). Direct pushes to
    main are blocked by branch protection (2026-07-22); report commits are non-code, so the
    required `preship` check passes trivially. Fails LOUD at every step (never silently strands a
    committed report). Returns True only on a CONFIRMED merge to origin/main.

    Flow: push HEAD to a temp branch → REST create PR → poll until mergeable_state=='clean'
    (required check green) → REST merge → OCI fast-forward to the merged main → delete temp branch."""
    token = _github_token()
    if not token:
        _slack(":rotating_light: sync_reports: no GitHub token (env or credential store) — cannot "
               "open a PR. Reports committed on OCI only; another account's git pull will miss them.")
        return False

    # Refresh origin/main so the report-only scope check AND the branch base are computed against
    # the CURRENT remote (a stale ref would mis-scope the diff and mis-base the PR).
    _git("fetch", "origin", "main")

    # SAFETY (board deploy-infra 2026-07-24): this auto-merger holds merge rights to `main`, so it
    # must ship ONLY report logs — never trust the PR's "logs only" label. If any commit ahead of
    # origin/main touches a non-report path (operator hotfix, config tweak, rebase artifact), REFUSE
    # to ship and alert, so code can never be auto-merged to the deploy channel unreviewed.
    _ok_scope, _offending = _commits_ahead_are_reports_only()
    if not _ok_scope:
        _slack(":rotating_light: sync_reports: REFUSING to ship — commit(s) ahead of origin/main "
               f"include NON-report file(s): {_offending[:10]}. This auto-merger ships report logs "
               "ONLY; a code/config change must go through the normal reviewed PR path. Aborting.")
        return False

    ts = datetime.now(ET).strftime("%Y%m%d-%H%M%S")
    branch = f"reports/auto-sync-{ts}"
    num = None
    branch_pushed = False
    try:
        prc, pout = _git("push", "origin", f"HEAD:refs/heads/{branch}")
        if prc != 0:
            _slack(f":rotating_light: sync_reports: push to temp branch {branch} failed — "
                   f"{pout[:200]}. Reports on OCI only.")
            return False
        branch_pushed = True
        st, pr = _api("POST", f"/repos/{_REPO_SLUG}/pulls", token, {
            "title": f"reports: routine audit sync ({datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')})",
            "head": branch, "base": "main",
            "body": "Automated routine-report sync (logs only, no code). Auto-merged by sync_reports.py.",
        })
        num = pr.get("number")
        if not num:
            _slack(f":rotating_light: sync_reports: PR create failed (HTTP {st}) — {str(pr)[:200]}. "
                   f"Branch {branch} pushed but not merged; reports not on main.")
            return False

        mstate = "unknown"
        last_status = None
        waited = 0
        while waited < _PR_POLL_TIMEOUT:
            _time_mod.sleep(_PR_POLL_INTERVAL)
            waited += _PR_POLL_INTERVAL
            last_status, prv = _api("GET", f"/repos/{_REPO_SLUG}/pulls/{num}", token)
            mstate = prv.get("mergeable_state", "unknown")
            # Merge ONLY on 'clean' (no conflicts AND all required checks passed). 'blocked' /
            # 'unstable' / 'behind' / 'unknown' keep polling to the timeout — never merged early.
            if mstate == "clean":
                break
            if mstate == "dirty":  # a real merge conflict — it will not become mergeable
                _slack(f":rotating_light: sync_reports: PR #{num} CONFLICTED (mergeable_state=dirty) "
                       f"— not merging. Branch {branch}; investigate.")
                return False
        if mstate != "clean":
            _hint = " (last GitHub HTTP=0 → network error while polling)" if last_status == 0 else ""
            _slack(f":rotating_light: sync_reports: PR #{num} not mergeable within {_PR_POLL_TIMEOUT}s "
                   f"(mergeable_state={mstate}, last HTTP={last_status}){_hint} — required `preship` "
                   f"check pending/failed. Branch {branch}; reports on OCI + branch, NOT on main.")
            return False

        mst, mr = _api("PUT", f"/repos/{_REPO_SLUG}/pulls/{num}/merge", token,
                       {"merge_method": "merge"})
        if not mr.get("merged"):
            _slack(f":rotating_light: sync_reports: PR #{num} merge call failed (HTTP {mst}) — "
                   f"{str(mr)[:200]}. Branch {branch}; reports not on main.")
            return False

        # Reports are now on GitHub main (SHIP SUCCEEDED). Re-sync OCI local main so the NEXT run
        # starts clean and drift never re-accumulates. OCI is at commit C; origin/main is the merge
        # commit M, which descends from C, so this fast-forwards cleanly in the normal case.
        _git("fetch", "origin", "main")
        frc, fout = _git("merge", "--ff-only", "origin/main")
        if frc != 0:
            # The SHIP is done (reports ARE on main), but OCI local main did NOT fast-forward — it
            # is now diverged and will RE-ACCUMULATE the exact drift this fix removes until synced.
            # Do NOT auto-`reset --hard` production (could discard an unrelated local change); fail
            # LOUD and return False so the run exits non-zero and the operator re-syncs OCI
            # (`git fetch && git reset --hard origin/main` once verified clean). GAI pre-ship 2026-07-24.
            _slack(f":rotating_light: sync_reports: PR #{num} MERGED to main (reports ARE on GitHub), "
                   f"but OCI local main FAILED to fast-forward — {fout[:160]}. OCI is out of sync and "
                   f"will re-drift until a manual `git reset --hard origin/main`. Returning non-zero.")
            return False
        return True
    finally:
        # Delete the temp branch whenever it was PUSHED — not only when a PR number was obtained.
        # A PR-create failure leaves num=None but the branch already exists on the remote; keying
        # cleanup off `num` orphaned one branch per failed run (board + cold-2nd 2026-07-24).
        if branch_pushed:
            # best-effort (harmless no-op if the repo auto-deleted the merged branch)
            _api("DELETE", f"/repos/{_REPO_SLUG}/git/refs/heads/{branch}", token)


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
    # Visibility (board deploy-infra 2026-07-24): if the webhook is STILL unset after the .env
    # load, every :rotating_light: alert below is a no-op and the run's exit code is the ONLY
    # surviving failure signal — say so LOUDLY in the cron log so the gap is not invisible.
    if not os.getenv("SLACK_WEBHOOK_URL", "").strip():
        print("[sync_reports] WARNING: SLACK_WEBHOOK_URL unset even after .env load — failure "
              "alerts will be no-ops; this run's non-zero exit code is the only failure signal. "
              "Ensure the cron surfaces exit codes (MAILTO / systemd OnFailure).", file=sys.stderr)
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
    if have_ahead and not _ship_via_pr():
        return 1

    _reconcile_and_alert()
    print("[sync_reports] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
