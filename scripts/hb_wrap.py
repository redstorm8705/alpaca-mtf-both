#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
scripts/hb_wrap.py — COMPLETION-HEARTBEAT WRAPPER (job-liveness watchdog, part 2/3).

Runs a scheduled job as a child process and, ONLY IF it exits 0, records a completion heartbeat via
monitoring.job_heartbeat.beat(). The child's exit code is propagated so cron sees the true result.

WHY A WRAPPER (not editing every script): instruments ALL ~20 cron jobs via a one-line-per-job crontab
change instead of 20 gated file edits. And it catches ANY crash — a non-zero exit for ANY reason, incl.
the #194 f-string `ValueError` (which exits 1) — regardless of where in the script it happens. That is
stronger than an in-script beat() call, which only fires if execution actually REACHES it.
(cron_tz_wrapper.py can't host the beat: it uses os.execvp and never returns.)

USAGE (in the crontab, after any cron_tz_wrapper):
    cd /home/ubuntu/mtf-bot && venv/bin/python3 scripts/hb_wrap.py weekly_perf_audit 10080 -- \
        venv/bin/python3 weekly_perf_audit.py    # hb:weekly_perf_audit

    hb_wrap.py <job_name> <cadence_min> -- <command> [args...]

`cadence_min` = how often the job is scheduled (daily=1440, weekly=10080, weekdays≈2880). It is
operational cadence config mirroring the crontab schedule, NOT a risk/scoring decision threshold.

CONTRACT: the heartbeat is best-effort and must NEVER change the job's outcome — beat() already
swallows its own errors, and this wrapper always exits with the CHILD's exit code (even if beat fails).
A no-op job that exits 0 will beat (liveness ≠ output-validation); crash-death detection is the goal,
and output correctness is a separate concern handled per-job later.
"""
import subprocess
import sys
import time
from pathlib import Path

# Repo root on path so `monitoring.job_heartbeat` imports whether cwd is repo root or scripts/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _usage() -> int:
    sys.stderr.write("usage: hb_wrap.py <job_name> <cadence_min> -- <command> [args...]\n")
    return 2


def main(argv: list[str]) -> int:
    if "--" not in argv:
        return _usage()
    sep = argv.index("--")
    head = argv[:sep]
    command = argv[sep + 1:]
    if len(head) != 2 or not command:
        return _usage()
    job_name = head[0]
    try:
        cadence_min = int(head[1])
    except ValueError:
        return _usage()
    if cadence_min <= 0:
        return _usage()

    start = time.monotonic()
    try:
        # Inherit stdio so the child's logging/output flows through exactly as an un-wrapped cron run.
        rc = subprocess.call(command)
    except FileNotFoundError:
        sys.stderr.write(f"hb_wrap: command not found: {command[0]!r} — no heartbeat\n")
        return 127
    except Exception as exc:                       # pragma: no cover — defensive
        sys.stderr.write(f"hb_wrap: failed to launch {command[0]!r}: {exc} — no heartbeat\n")
        return 1
    duration_s = time.monotonic() - start

    if rc == 0:
        # Only a clean completion earns a heartbeat. A crash/circuit-breaker (non-zero) leaves the
        # heartbeat stale so monitoring/job_liveness.py flags it. beat() never raises.
        try:
            from monitoring.job_heartbeat import beat
            beat(job_name, cadence_min, duration_s=duration_s)
        except Exception as exc:                   # pragma: no cover — beat is already guarded
            sys.stderr.write(f"hb_wrap: heartbeat emit failed (job unaffected): {exc}\n")
    else:
        sys.stderr.write(f"hb_wrap: {job_name} exited {rc} — NO heartbeat (watchdog will flag if it persists)\n")

    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
