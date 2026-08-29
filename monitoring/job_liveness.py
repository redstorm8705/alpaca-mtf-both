# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
monitoring/job_liveness.py — cross-process cron-JOB liveness watchdog (part 2/3).

Complements the two EXISTING watchdogs (neither of which could catch the weekly-audit death):
  - monitoring/watchdog.py  — an IN-PROCESS thread that restarts the LIVE bot if run_cycle() hangs
    during RTH. Blind to separate cron processes.
  - scripts/service_watchdog.sh + nightly_audit._check_watchdog_heartbeat() — watches the SERVICE
    watchdog's heartbeat. One file, one job.
This watches the SCHEDULED CRON JOBS (weekly/midday/nightly/meta/gex/score16/… reports), each of which
now emits a COMPLETION heartbeat via monitoring/job_heartbeat.beat(). weekly_perf_audit.py crashed
every run for ~2 months (PR #194) and no signal existed; this makes that a next-day alert.

TWO detection modes (a job is "unhealthy" if EITHER fires):
  STALE      — it succeeded before but its last heartbeat is older than its own declared cadence ×
               TOLERANCE. (The weekly-audit case: last success Jul 5, cadence weekly → stale.)
  NEVER_RAN  — the crontab tags it `# hb:<name>` (so it is EXPECTED to run) but it has NO heartbeat at
               all (crash-from-birth). Requires the crontab; degrades to STALE-only if unavailable.

DYNAMIC, not static: expected cadence comes from each job's own beat() cadence (which mirrors its
crontab schedule); the expected SET comes from the live crontab's `# hb:` tags. No hand-maintained
per-job threshold table. TOLERANCE is a single declared operational multiplier (not a risk/scoring
threshold), documented per the "1-in-10 static, declared" rule.
"""
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from monitoring.job_heartbeat import _HB_FILE, beat

logger = logging.getLogger("job_liveness")

_PT = ZoneInfo("America/Los_Angeles")
_ROOT = Path(__file__).resolve().parent.parent

TOLERANCE = 1.5                 # overdue if age > cadence × TOLERANCE (one missed run + slack)
_SELF_JOB = "job_liveness_watchdog"
_SELF_CADENCE_MIN = 1440        # this watchdog is expected to run at least daily


def _load_registry() -> dict:
    try:
        with _HB_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("job_heartbeats.json unreadable (%s) — treating as empty", exc)
        return {}


def _read_crontab_hb_names() -> set[str] | None:
    """Expected job names from the live crontab's `# hb:<name>` tags. None if crontab unavailable
    (then NEVER_RAN detection is skipped — STALE detection still works from the registry)."""
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        names: set[str] = set()
        for line in out.stdout.splitlines():
            i = line.find("# hb:")
            if i >= 0:
                tag = line[i + len("# hb:"):].strip().split()[0] if line[i + len("# hb:"):].strip() else ""
                if tag:
                    names.add(tag)
        return names
    except Exception as exc:
        logger.warning("crontab -l unavailable (%s) — NEVER_RAN detection skipped", exc)
        return None


def _age_min(last_ts: str, now: datetime) -> float | None:
    try:
        dt = datetime.fromisoformat(last_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_PT)
        return (now - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def scan(now: datetime | None = None, expected_names: set[str] | None = None) -> dict:
    """Return {'ok': [...], 'stale': [...], 'never_ran': [...], 'checked': N}. Pure — no I/O side
    effects except reading. `expected_names` overrides the crontab read (for tests)."""
    now = now or datetime.now(_PT)
    reg = _load_registry()
    names = expected_names if expected_names is not None else _read_crontab_hb_names()

    ok, stale = [], []
    for job, rec in reg.items():
        cadence = int(rec.get("expected_every_min", 0) or 0)
        age = _age_min(rec.get("last_success_ts", ""), now)
        if cadence <= 0 or age is None:
            # Unusable record — surface as stale so it is never silently ignored.
            stale.append({"job": job, "age_min": age, "cadence_min": cadence, "reason": "unparseable_record"})
            continue
        if age > cadence * TOLERANCE:
            stale.append({"job": job, "age_min": round(age, 1), "cadence_min": cadence,
                          "overdue_by_min": round(age - cadence, 1), "last_success": rec.get("last_success_ts", "")})
        else:
            ok.append({"job": job, "age_min": round(age, 1), "cadence_min": cadence})

    never_ran = []
    if names:
        for nm in sorted(names - set(reg.keys())):
            never_ran.append({"job": nm, "reason": "tagged in crontab but no completion heartbeat ever recorded"})

    return {"ok": ok, "stale": stale, "never_ran": never_ran, "checked": len(reg),
            "expected_from_crontab": (sorted(names) if names else None)}


def format_report(result: dict) -> str:
    lines = [f"JOB-LIVENESS: {result['checked']} job(s) with heartbeats, "
             f"{len(result['stale'])} STALE, {len(result['never_ran'])} NEVER_RAN"]
    for s in result["stale"]:
        if s.get("reason") == "unparseable_record":
            lines.append(f"  ⚠️ {s['job']}: unparseable heartbeat record")
        else:
            lines.append(f"  🔴 STALE {s['job']}: last success {s['age_min']:.0f}min ago "
                         f"(cadence {s['cadence_min']}min, overdue {s.get('overdue_by_min', 0):.0f}min)")
    for n in result["never_ran"]:
        lines.append(f"  🔴 NEVER_RAN {n['job']}: {n['reason']}")
    if not result["stale"] and not result["never_ran"]:
        lines.append("  🟢 all scheduled jobs fresh")
    return "\n".join(lines)


def check_and_alert() -> dict:
    """Scan + Slack-alert on any unhealthy job. Safe to call from nightly_audit. Never raises."""
    result = scan()
    report = format_report(result)
    logger.info(report)
    unhealthy = result["stale"] + result["never_ran"]
    if unhealthy:
        try:
            from alerts import send_slack  # reuse the existing alert channel
            names = ", ".join(u["job"] for u in unhealthy)
            send_slack(f":rotating_light: *SCHEDULED JOB(S) NOT REPORTING* :rotating_light:\n"
                       f"{len(unhealthy)} job(s) overdue/never-ran: *{names}*\n```{report}```\n"
                       f"A scheduled report/analysis job has stopped producing its completion "
                       f"heartbeat — it likely crashed or its cron was disabled. This is the exact "
                       f"silent-death that hid the weekly-audit crash for 2 months.")
        except Exception as exc:
            logger.warning("job_liveness Slack alert failed: %s", exc)
    return result


def main() -> int:
    result = check_and_alert()
    print(format_report(result))
    # Self-monitoring: this watchdog beats too, so nightly_audit (and this scan on its next run)
    # can flag it if IT stops — with the human reading the daily heartbeat as the final backstop.
    beat(_SELF_JOB, expected_every_min=_SELF_CADENCE_MIN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
