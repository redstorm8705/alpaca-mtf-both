#!/usr/bin/env python3
# ruff: noqa: E501
"""
scripts/post_live_eval.py — post-live audit EVALUATOR (increment 1; design record
logs/design_records/post_live_engine_2026-10-11.md; board Kim/Majors/López de Prado + Gro + GAI).

Runs on OCI after the close (cron, weekdays). For every committed expectation record in logs/post_live/expect/ whose
watch window covers the session that just ended, it checks:
  * RAN?  each proof pattern's matches on lines of that session's date in its log. Zero for all patterns ->
          NOT_EXERCISED (never a pass); still zero once the window is spent -> finding UNEXERCISED_EXPIRED.
  * NEW ERROR SIGNATURES: ERROR/CRITICAL/Traceback signatures (digits, ids and symbols normalised away) in the
          session's mtf_bot.log and day_tier_runner_cron.log that did not appear in the prior 5 sessions -> finding
          NEW_ERRORS (listed with the changes live in their window; whole files, not only the changed modules — a
          caller can fail).
Metric evaluation is increment 2.

OUTPUTS (no code, no commits, no config, no tracked files on OCI):
  * logs/post_live/results/<session>.json (untracked — .gitignore keeps logs/post_live/* except expect/);
  * a GitHub issue per finding, DEDUPLICATED by its title "[post-live] <change>: <finding>" (an open issue with the same
    title gets a comment instead of a duplicate), label "post-live";
  * one Slack card for the session.
READ-ONLY on the market: Alpaca is read only for the trading calendar (T1). Never raises past main (exit 0/1).

  python3 scripts/post_live_eval.py [--session YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
EXPECT_DIR = ROOT / "logs" / "post_live" / "expect"
RESULTS_DIR = ROOT / "logs" / "post_live" / "results"
ERROR_LOGS = ("logs/mtf_bot.log", "logs/day_tier_runner_cron.log")
_PRIOR_SESSIONS = 5
_EXPIRY_GRACE = 3          # sessions after the window in which a missed expiry is still raised (issues are deduped)
_ERR_RE = re.compile(r"\| (ERROR|CRITICAL) +\| ([\w.]+) \| (.*)$")


def sessions(until: date, n_back: int = 40) -> list:
    """Trading session dates up to and including `until` (Alpaca calendar, T1). Weekdays if the calendar is
    unreadable (holidays then count as sessions — the strict direction for the window)."""
    try:
        from alpaca.trading.requests import GetCalendarRequest

        from execution.broker import get_trading_client
        cal = get_trading_client().get_calendar(GetCalendarRequest(start=until - timedelta(days=n_back), end=until))
        out = sorted({c.date if isinstance(c.date, date) else date.fromisoformat(str(c.date)[:10]) for c in cal})
        if out:
            return out
    except Exception as e:  # noqa: BLE001
        print(f"[post-live] calendar unreadable ({e}); using weekdays", file=sys.stderr)
    d, out = until - timedelta(days=n_back), []
    while d <= until:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def shipped_at(path: Path) -> "datetime | None":
    """When the record reached main: the FIRST-PARENT commit (the PR merge) that added it, ET (cold-2nd: the branch
    commit's date can be days before the merge/deploy)."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "log", "--first-parent", "-m", "--diff-filter=A",
                              "--format=%cI", "HEAD", "--", str(path.relative_to(ROOT))],
                             capture_output=True, text=True, timeout=20).stdout
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return datetime.fromisoformat(lines[-1]).astimezone(ET) if lines else None
    except Exception:  # noqa: BLE001
        return None


def window_position(shipped: datetime, session: date, cal: list) -> "int | None":
    """1-based index of `session` among the sessions that OPENED (09:30 ET) after `shipped`; None if before."""
    live = [d for d in cal if datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) >= shipped]
    return live.index(session) + 1 if session in live else None


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _lines_on(log: Path, day: date) -> list:
    """Lines of `log` dated `day` (the 'YYYY-MM-DD ' text-log and '"ts": "YYYY-MM-DD' JSONL forms, date within the first
    60 characters). An undated line (a raw traceback, a print) inherits the date of the last dated line before it
    (cold-2nd: an uncaught crash dumped into a cron log has no date). Missing log -> []."""
    if not log.exists():
        return []
    tag = day.isoformat()
    out, cur = [], ""
    with log.open("r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            m = _DATE_RE.search(ln[:60])
            if m:
                cur = m.group(1)
            if cur == tag:
                out.append(ln.rstrip("\n"))
    return out


def proof_count(rec: dict, day: date) -> int:
    total = 0
    for p in rec.get("proof") or []:
        try:
            rx = re.compile(p["pattern"])
        except (re.error, KeyError, TypeError):
            continue
        total += sum(1 for ln in _lines_on(ROOT / p["log"], day) if rx.search(ln))
    return total


def _signature(module: str, msg: str) -> str:
    s = re.sub(r"\[(?:'[A-Z0-9.\-]{1,12}',? ?)+\]", "<SYMS>", msg)  # ['MSFT', 'PLTU']
    s = re.sub(r"\[[A-Z0-9.\-]{1,12}\]", "[SYM]", s)              # [NVDA]
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<id>", s)          # uuids
    s = re.sub(r"\b[A-Z]{2}-[A-Z0-9.]+-[bs]-\d+-[0-9a-f]+\b", "<coid>", s)
    s = re.sub(r"\d+(\.\d+)?", "N", s)
    return f"{module}: {s[:160]}"


def error_signatures(day: date) -> set:
    sigs = set()
    for name in ERROR_LOGS:
        for ln in _lines_on(ROOT / name, day):
            m = _ERR_RE.search(ln)
            if m:
                sigs.add(_signature(m.group(2), m.group(3)))
            elif "Traceback" in ln:
                sigs.add("Traceback")
    return sigs


def evaluate(session: date, cal: list) -> dict:
    prior = [d for d in cal if d < session][-_PRIOR_SESSIONS:]
    old_sigs: set = set()
    for d in prior:
        old_sigs |= error_signatures(d)
    new_sigs = sorted(error_signatures(session) - old_sigs)
    changes, findings = [], []
    for path in sorted(EXPECT_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            findings.append({"change": path.stem, "finding": "UNREADABLE_RECORD", "detail": str(e)})
            continue
        shipped = shipped_at(path)
        if shipped is None:
            continue
        pos = window_position(shipped, session, cal)
        live = [d for d in cal if datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) >= shipped and d <= session]
        try:
            window = int(rec.get("window_sessions") or 5)
            # window_days (calendar days after the ship): for a job that runs outside trading sessions, e.g. a
            # weekend report. The window then ends at the first session on/after ship date + window_days; until
            # that session exists it has not ended (2026-10-13: a Saturday-only job expired after two weekdays).
            window_days = int(rec["window_days"]) if rec.get("window_days") is not None else None
        except (TypeError, ValueError):
            findings.append({"change": rec.get("change", path.stem), "finding": "BAD_WINDOW",
                             "detail": f"window unreadable: sessions={rec.get('window_sessions')!r} "
                                       f"days={rec.get('window_days')!r}"})
            continue
        if pos is None:
            continue
        if window_days is not None:
            deadline = shipped.date() + timedelta(days=window_days)
            ends = [i + 1 for i, d in enumerate(live) if d >= deadline]
            window = ends[0] if ends else pos + 1
        if pos > window + _EXPIRY_GRACE:
            continue
        # Cumulative over the window so far (cold-2nd F1), counted on EVERY calendar day from the ship date — a proof
        # line written in the evening or on a weekend is dated between sessions (2026-10-13: the fill-timestamp
        # canonicalisation ran once at 17:46 ET and was never counted).
        last_day = session if pos <= window else live[window - 1]
        days = [shipped.date() + timedelta(days=i) for i in range((last_day - shipped.date()).days + 1)]
        n_today = proof_count(rec, session) if pos <= window else 0
        n_total = sum(proof_count(rec, d) for d in days)
        if pos > window and n_total > 0:
            continue                         # ran within its window; nothing more to report
        verdict = "RAN" if n_total > 0 else "NOT_EXERCISED"
        changes.append({"change": rec.get("change", path.stem), "tier": rec.get("tier"), "session_in_window": pos,
                        "window": window, "proof_matches_session": n_today, "proof_matches_window": n_total,
                        "verdict": verdict})
        if n_total == 0 and pos >= window:   # the window's last session, or a missed run inside the grace
            findings.append({"change": rec.get("change", path.stem), "finding": "UNEXERCISED_EXPIRED",
                             "detail": f"no proof match since it shipped ({window} session(s) checked); expected rate: "
                                       f"{rec.get('expected_rate')}. Needs a historical replay proving the code fires, "
                                       f"or a remove / kill-flag decision."})
    if new_sigs:
        findings.append({"change": "session", "finding": f"NEW_ERRORS_{session.isoformat()}",
                         "detail": "New error signatures (absent in the prior 5 sessions):\n- " + "\n- ".join(new_sigs[:25])
                                   + f"\nChanges live in their window: {[c['change'] for c in changes] or 'none'}"})
    return {"session": session.isoformat(), "changes": changes, "new_error_signatures": new_sigs,
            "findings": findings}


def _upsert_issue(finding: dict, session: date, dry: bool) -> str:
    title = f"[post-live] {finding['change']}: {finding['finding']}"
    body = f"Session {session.isoformat()} (post_live_eval.py)\n\n{finding['detail']}"
    if dry:
        return f"DRY {title}"
    from scripts.sync_reports import _api, _github_token
    tok = _github_token()
    if not tok:
        return f"NO_TOKEN {title}"
    repo = "/repos/redstorm8705/alpaca-mtf-both"
    # Dedup over EVERY open issue (not only the label, which can be removed), paginated. A failed lookup never falls
    # through to a create (cold-2nd: that duplicates); the finding stays in the results file and retries next run.
    for page in range(1, 11):
        st, items = _api("GET", f"{repo}/issues?state=open&per_page=100&page={page}", tok)
        if st != 200 or not isinstance(items, list):
            return f"issue lookup failed ({st}) — not created: {title}"
        for it in items:
            if it.get("title") == title and "pull_request" not in it:
                cst, _ = _api("POST", f"{repo}/issues/{it['number']}/comments", tok, {"body": body})
                url = it.get("html_url") or "#" + str(it["number"])
                return f"updated {url}" if cst in (200, 201) else f"comment failed ({cst}) on {url}"
        if len(items) < 100:
            break
    st, made = _api("POST", f"{repo}/issues", tok, {"title": title, "body": body, "labels": ["post-live"]})
    return f"opened {made.get('html_url') or '#' + str(made.get('number'))}" if st in (200, 201) \
        else f"issue create failed ({st})"


def main(argv: "list | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    dry = "--dry-run" in argv
    today = datetime.now(ET).date()
    session = date.fromisoformat(argv[argv.index("--session") + 1]) if "--session" in argv else today
    cal = sessions(session)
    if session not in cal:
        print(f"post-live eval: {session} is not a trading session — nothing to do")
        return 0
    res = evaluate(session, cal)
    res["issues"] = [_upsert_issue(f, session, dry) for f in res["findings"]]
    if not dry:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / f"{session.isoformat()}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    ran = [c["change"] for c in res["changes"] if c["verdict"] == "RAN"]
    expired = {f["change"] for f in res["findings"] if f["finding"] == "UNEXERCISED_EXPIRED"}
    idle = [c["change"] for c in res["changes"] if c["verdict"] != "RAN" and c["change"] not in expired]
    # PLAIN ENGLISH (owner 2026-10-16): what worked, what is still waiting, what needs a look — names in the results file.
    def _plain(f: dict, link: str) -> str:
        url = next((w for w in str(link).split() if w.startswith("http")), "issue not filed")
        if f["finding"] == "UNEXERCISED_EXPIRED":
            return f"'{f['change']}' never ran in its check window — needs a replay or a decision ({url})"
        if str(f["finding"]).startswith("NEW_ERRORS"):
            return f"new kinds of errors showed up in today's logs ({url})"
        return f"{f['change']}: {f['finding']} ({url})"
    msg = (f"New-code check for {session:%a %b %d}: {len(ran)} recent change(s) have run since they shipped"
           + (f"; {len(idle)} are still waiting for the situation they handle (normal)" if idle else "")
           + (". Needs a look: " + "; ".join(_plain(f, i) for f, i in zip(res["findings"], res["issues"], strict=True))
              if res["findings"] else ". Nothing needs attention."))
    # Dated + tagged with this change's slug: the evaluator's own unique proof line (cold-2nd F2; board change_id tag).
    print(f"{session.isoformat()} post-live eval [post-live-evaluator]: {msg}")
    if not dry:
        try:
            from alerts import send_slack
            send_slack(f":clipboard: {msg}")
        except Exception as e:  # noqa: BLE001
            print(f"post-live eval: Slack failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"post-live eval FAILED: {e!r}", file=sys.stderr)
        sys.exit(1)
