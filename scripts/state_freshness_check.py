#!/usr/bin/env python3
# ruff: noqa: E501,E402  — E501: plain-English Slack text; E402: imports follow the sys.path bootstrap (cron)
"""
scripts/state_freshness_check.py — page Slack when a cron-written state file the live bot reads has gone stale
(Claude 2026-10-16; design: logs/design_records/state_freshness_alarm_2026-10-16.md).

Two files are written by weekday crons and read by live trading code; if a cron silently stops, the bot keeps using
the old values and nothing pages:

  data/state/dip_zones.json    run_dip_zones.py   -> execution/dip_zones.refresh()          cron 12:20 + 21:20 UTC
                               read by QHM through dip_zones.zones_for()
  data/state/overnight_k.json  run_overnight_k.py -> execution/overnight_capacity.refresh() cron 12:15 + 21:15 UTC
                               read by the tiers through overnight_capacity.overnight_k()

Each writer stamps "session" = the ET calendar date of the run (f"{datetime.now(ET):%Y-%m-%d}") and "computed_at" =
the PT ISO time. A failed refresh writes nothing, so the old "session" stays.

STALE = at least one trading session that is COMPLETED (its close has passed) falls strictly after the file's
"session" date — the file is older than one completed trading session. Derived from the Alpaca trading calendar
(T1, read-only, via execution.broker.get_trading_client; holiday and half-day aware). If the calendar is unreadable
or returns no rows, weekdays strictly before today stand in for completed sessions: a holiday then counts as a session
(may page early), and today never counts (no hour constant), so for a run BEFORE today's close — the installed cron —
it can page early but never late. A fallback run after the close sees today's session one run later. No fixed hour
thresholds.

KNOWN LIMIT: "session" is the run date, not the data date (both writers keep bars before today). If only the evening
runs succeed for days, the data stays one session behind and this check stays quiet; it catches a stopped job.

A missing, unreadable or unstamped file pages too. Read-only: never writes state, never touches orders.
Exit 0 = all fresh, 1 = at least one file stale or missing.

Cron: scripts/install_state_freshness_cron.sh (weekdays, after the morning refreshes).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("state_freshness_check")

ET = ZoneInfo("America/New_York")
STATE_DIR = ROOT / "data" / "state"

# name -> (file, what it controls, what the bot does meanwhile, writer script, cron log)
FILES = {
    "dip_zones": (
        STATE_DIR / "dip_zones.json",
        "the long-term-hold (QHM) dip-buy zones (how far each held stock must fall before the bot adds to it)",
        "QHM keeps buying dips on the old zones",
        "run_dip_zones.py",
        "logs/dip_zones_cron.log",
    ),
    "overnight_k": (
        STATE_DIR / "overnight_k.json",
        "the overnight holding cap (how much stock all tiers may hold overnight, as a multiple of equity)",
        "the tiers keep using the old overnight cap",
        "run_overnight_k.py",
        "logs/overnight_k_cron.log",
    ),
}


def read_session(path: Path) -> "tuple[date | None, str, str]":
    """(session date, computed_at text, problem text). session None = missing / unreadable / unstamped."""
    try:
        if not path.exists():
            return None, "", "the file does not exist"
        d = json.loads(path.read_text())
        if not isinstance(d, dict):
            return None, "", "the file is not a JSON object"
        raw = d.get("session")
        if not isinstance(raw, str):
            return None, "", "the file has no session date"
        return date.fromisoformat(raw[:10]), str(d.get("computed_at") or ""), ""
    except Exception as e:  # noqa: BLE001
        return None, "", f"the file could not be read ({type(e).__name__}: {e})"


def _as_et(dt: datetime) -> datetime:
    """A calendar close as an aware ET datetime (alpaca-py returns naive ET wall-clock times)."""
    return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)


def completed_sessions(after: date, now: datetime) -> "tuple[list, bool]":
    """Trading sessions strictly after `after` whose close has passed by `now` (aware).
    Returns (sorted dates, True if from the Alpaca calendar / False if the weekday fallback was used)."""
    today = now.astimezone(ET).date()
    if after >= today:
        return [], True
    try:
        from alpaca.trading.requests import GetCalendarRequest

        from execution.broker import get_trading_client
        cal = get_trading_client().get_calendar(GetCalendarRequest(start=after + timedelta(days=1), end=today))
        rows: "list[Any]" = list(cal or [])   # alpaca-py: list[Calendar] (typed as a raw-data union)
        out = set()
        for c in rows:
            d = c.date if isinstance(c.date, date) else date.fromisoformat(str(c.date)[:10])
            close = c.close if isinstance(c.close, datetime) else datetime.fromisoformat(str(c.close))
            if d > after and _as_et(close) <= now:
                out.add(d)
        if rows:
            return sorted(out), True
        # no rows at all is treated as unreadable, never as "no sessions" (adversarial review 2026-10-16; same guard
        # as scripts/post_live_eval.py sessions())
        logger.warning("trading calendar returned no rows for %s..%s — counting weekdays instead", after, today)
    except Exception as e:  # noqa: BLE001
        logger.warning("trading calendar unreadable (%s) — counting weekdays instead", e)
    out_wd, d = [], after + timedelta(days=1)
    while d < today:
        if d.weekday() < 5:
            out_wd.append(d)
        d += timedelta(days=1)
    return out_wd, False


def check(now: "datetime | None" = None) -> list:
    """One plain-English problem line per stale / missing file (empty list = all fresh)."""
    now = now or datetime.now(ET)
    problems = []
    for path, what, effect, writer, cron_log in FILES.values():
        session, computed_at, err = read_session(path)
        if session is None:
            problems.append(f"*{path.name}* — {what}: {err}. Check {writer} and {cron_log}.")
            continue
        if session > now.astimezone(ET).date():
            # the writers only stamp today's ET date: a later date is clock error or corruption, and would otherwise
            # read as fresh until that date passes (cold-2nd 2026-10-16)
            problems.append(f"*{path.name}* — {what}: its session date {session:%a %b %d %Y} is in the future (clock "
                            f"error or a damaged file), so its freshness cannot be trusted. Check {writer} and {cron_log}.")
            continue
        missed, from_cal = completed_sessions(session, now)
        if not missed:
            logger.info("%s fresh: session %s (computed %s)", path.name, session, computed_at or "?")
            continue
        n = len(missed)
        days = ", ".join(f"{d:%a %b %d}" for d in missed)
        basis = "" if from_cal else " (trading calendar unreadable or empty, so weekdays were counted; a holiday may be included)"
        problems.append(
            f"*{path.name}* — {what} was last refreshed for {session:%a %b %d}; {n} trading "
            f"session{'s have' if n != 1 else ' has'} closed since then ({days}){basis}. Its daily job ({writer}) "
            f"looks stopped or failing, so {effect}. Check {cron_log} and the crontab.")
    return problems


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception as e:  # noqa: BLE001
        logger.warning(".env not loaded (%s)", e)
    problems = check()
    # post-live proof line (logs/post_live/expect/state-freshness-alarm.json)
    logger.info("change=state-freshness-alarm checked %d file(s): %d stale or missing", len(FILES), len(problems))
    if not problems:
        logger.info("all state files fresh")
        return 0
    msg = ":warning: Data the live bot reads is stale or missing:\n" + "\n".join(f"• {p}" for p in problems)
    logger.warning(msg)
    try:
        from alerts import send_slack
        send_slack(msg)
    except Exception as e:  # noqa: BLE001
        logger.error("state freshness page not sent: %s", e)
    return 1


if __name__ == "__main__":
    sys.exit(main())
