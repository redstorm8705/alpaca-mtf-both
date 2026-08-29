# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
monitoring/job_heartbeat.py — per-job COMPLETION heartbeat (job-liveness watchdog, part 1/3).

WHY: weekly_perf_audit.py crashed on every run for ~2 months and NOTHING detected it — the
operator did (PR #194). The project already solves this for ONE thing: scripts/service_watchdog.sh
touches logs/svc_watchdog.heartbeat every */5 run and nightly_audit._check_watchdog_heartbeat()
alerts if that file goes stale ("every watchdog's failure mode is SILENCE", board 2026-07-16). This
EXTENDS that exact proven pattern from the service to every SCHEDULED CRON JOB (the weekly/midday/
nightly/meta/gex/score16/… reports), which today emit no liveness signal.

DESIGN (Gro SRE review 2026-08-29): watch the PROCESS, not the OUTPUT. A job that writes its output
file early then crashes in post-processing LOOKS fresh — so health is signalled by a heartbeat the
job emits as the LAST thing it does on SUCCESS, not by an output-file mtime. A crash ANYWHERE (before
or after output) leaves no fresh heartbeat → detected. "Ran but produced nothing" (heartbeat present,
output absent) is distinguishable from "never ran" (no heartbeat).

USAGE (add as the last successful line of a scheduled script):
    from monitoring.job_heartbeat import beat
    ...
    beat("weekly_perf_audit", expected_every_min=10080)   # weekly = 7*24*60
    return 0

The `expected_every_min` is the job's OWN declaration of its cadence (mirrors its crontab schedule) —
the watchdog (monitoring/job_liveness.py) uses it to decide "overdue". It is operational cadence
config, NOT a risk/scoring decision threshold, so it is outside the no-static-regimes mandate.

Robustness contract (this runs at the tail of OTHER jobs — it must NEVER take one down):
  - never raises (all failures logged + swallowed — the ONE justified swallow, since a heartbeat that
    crashes its host would be worse than a missing heartbeat);
  - atomic write (tmp→os.replace, RC-5);
  - an advisory file lock serializes the read-modify-write so two jobs finishing together cannot
    clobber each other's entry (last-writer-wins on the shared file would lose a job otherwise);
  - tz-aware PT timestamps (RC-1); path anchored to __file__ (RC-2).
"""
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("job_heartbeat")

_PT = ZoneInfo("America/Los_Angeles")
_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _ROOT / "data" / "state"
_HB_FILE = _STATE_DIR / "job_heartbeats.json"
_LOCK_FILE = _STATE_DIR / "job_heartbeats.lock"


def _load(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt registry must not wedge every future beat — start fresh, but say so loudly.
        logger.warning("job_heartbeats.json unreadable (%s) — starting a fresh registry", exc)
        return {}


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".hb_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # atomic
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def beat(job_name: str, expected_every_min: int, duration_s: float | None = None) -> bool:
    """Record a SUCCESSFUL completion of `job_name`. Call as the last successful action.

    Returns True on success, False on any failure (never raises — see the robustness contract).
    `expected_every_min` is the job's cadence (how often it is scheduled to run).
    """
    if not job_name or expected_every_min <= 0:
        logger.warning("beat: invalid args job_name=%r expected_every_min=%r — skipped", job_name, expected_every_min)
        return False
    lock_fh = None
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Advisory lock so concurrent finishers serialize their read-modify-write.
        # fcntl is POSIX (OCI is Linux); if it is unavailable the write still happens
        # (last-writer-wins), which is strictly no worse than having no heartbeat at all.
        try:
            import fcntl
            lock_fh = open(_LOCK_FILE, "w")
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            lock_fh = None
        reg = _load(_HB_FILE)
        now = datetime.now(_PT)
        prev = reg.get(job_name, {})
        reg[job_name] = {
            "last_success_ts": now.isoformat(),
            "prev_success_ts": prev.get("last_success_ts", ""),
            "run_count": int(prev.get("run_count", 0)) + 1,
            "expected_every_min": int(expected_every_min),
            "last_duration_s": round(duration_s, 2) if duration_s is not None else None,
        }
        _atomic_write(_HB_FILE, reg)
        logger.info("job heartbeat: %s (run #%d, cadence %dmin)", job_name, reg[job_name]["run_count"], expected_every_min)
        return True
    except Exception as exc:
        # THE justified swallow: a heartbeat failure must not crash the job it monitors.
        logger.warning("beat(%s) failed (non-fatal, job unaffected): %s", job_name, exc)
        return False
    finally:
        if lock_fh is not None:
            try:
                lock_fh.close()
            except Exception:
                pass


if __name__ == "__main__":
    # Manual smoke: `python3 -m monitoring.job_heartbeat <name> <every_min>`
    import sys
    nm = sys.argv[1] if len(sys.argv) > 1 else "manual_test"
    ev = int(sys.argv[2]) if len(sys.argv) > 2 else 1440
    print("beat ->", beat(nm, ev), "| registry:", _HB_FILE)
