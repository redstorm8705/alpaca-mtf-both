#!/usr/bin/env python3
# ruff: noqa: E501
"""
earnings_preflight.py
Pre-flight earnings scanner — runs Sunday 5:55 PM ET before futures open.

Refreshes FMP earnings cache for all bot-tracked symbols. Sends Slack alert
for any symbol with earnings within 3 trading days so overnight / next-week
positions can be reviewed before entry gates open Monday.

Cron (via cron_tz_wrapper.py DST-safe pattern):
  55 21,22 * * 0 cd /home/ubuntu/mtf-bot && source .env &&
    python3 cron_tz_wrapper.py 17:55 earnings_preflight.py \
    >> logs/earnings_preflight_cron.log 2>&1
"""

import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Absolute paths — RC-2: never CWD-relative
# ---------------------------------------------------------------------------
_BASE  = Path(__file__).resolve().parent
_LOGS  = _BASE / "logs"
_STATE = _BASE / "data" / "state"
_CACHE = _BASE / "data" / "cache"

_TRADE_LOG       = _STATE / "trade_log.json"
_EARNINGS_CACHE  = _CACHE / "earnings_week.json"
_WATCHLIST_STATE = _STATE / "watchlist_state.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")


def _fmt_pt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(PT).strftime("%Y-%m-%d %I:%M %p PT")


# ---------------------------------------------------------------------------
# Load tracked symbols (open positions + watchlist if available)
# ---------------------------------------------------------------------------
def _load_tracked_symbols() -> set[str]:
    symbols: set[str] = set()

    if _TRADE_LOG.exists():
        try:
            data = json.loads(_TRADE_LOG.read_text())
            for sym in data.get("open_trades", {}).keys():
                symbols.add(sym.upper())
            logger.info(f"Open positions: {sorted(symbols) or 'none'}")
        except Exception as e:
            logger.warning(f"trade_log.json read error: {e}")

    if _WATCHLIST_STATE.exists():
        try:
            data = json.loads(_WATCHLIST_STATE.read_text())
            for sym in data.get("symbols", []):
                symbols.add(sym.upper())
        except Exception as e:
            logger.debug(f"watchlist_state.json read error (optional): {e}")

    return symbols


# ---------------------------------------------------------------------------
# Next N trading days (weekdays only, excludes today)
# ---------------------------------------------------------------------------
def _next_n_trading_days(n: int = 3) -> list[str]:
    days: list[str] = []
    d = date.today()
    while len(days) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.isoformat())
    return days


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    # AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate). This
    # script is read-only (trade_log.json, watchlist_state.json) and only
    # refreshes its own FMP earnings cache (data/fmp_client.py owns that
    # write) -- no write-contention risk with the live bot's shared state.
    now_pt = _fmt_pt(datetime.now(ET))
    logger.info(f"=== Earnings Pre-Flight — {now_pt} ===")

    symbols = _load_tracked_symbols()
    logger.info(f"Total tracked symbols: {len(symbols)} — {sorted(symbols) or 'none'}")

    # Step 1 — Refresh FMP earnings cache
    try:
        sys.path.insert(0, str(_BASE))
        from data.fmp_client import preload_earnings_week, get_cached_earnings_dates
    except ImportError as e:
        logger.error(f"Cannot import fmp_client: {e} — aborting.")
        sys.exit(1)

    try:
        # AWP audit fix (2026-06-30): preload_earnings_week() requires a
        # `symbols: list[str]` positional argument (data/fmp_client.py:299-301)
        # -- this call was missing it entirely, raising TypeError every single
        # run and silently falling back to a stale/empty cache. Confirmed via
        # OCI's earnings_preflight_cron.log: identical "missing 1 required
        # positional argument: 'symbols'" error on every Sunday run since at
        # least 2026-06-07 (4+ consecutive weeks). Masked operationally because
        # tracked symbols happened to be 0 each of those weeks; would have
        # left the earnings cache permanently stale once positions existed.
        preload_earnings_week(sorted(symbols))
        logger.info("FMP earnings cache refreshed successfully.")
    except Exception as e:
        logger.error(f"preload_earnings_week() raised: {e} — will use stale cache if present.")

    # Log raw cache contents for audit trail
    if _EARNINGS_CACHE.exists():
        try:
            raw = json.loads(_EARNINGS_CACHE.read_text())
            _cached_syms = list(raw.get("data", {}).keys())
            logger.info(
                f"Cache contents after refresh — window: {raw.get('from')}→{raw.get('to')} "
                f"| {len(_cached_syms)} symbols: {_cached_syms}"
            )
        except Exception as e:
            logger.warning(f"Could not read earnings cache for audit log: {e}")
    else:
        logger.warning("earnings_week.json not found after refresh attempt.")

    # Step 2 — Check coverage for each tracked symbol
    trading_days = _next_n_trading_days(3)
    logger.info(f"Next 3 trading days: {trading_days}")

    imminent: list[tuple[str, str]] = []   # (symbol, earnings_date)
    covered:   list[str] = []
    uncovered: list[str] = []

    for sym in sorted(symbols):
        try:
            dates = get_cached_earnings_dates(sym)
            if dates:
                covered.append(sym)
                # AWP audit fix (2026-06-30): get_cached_earnings_dates()
                # returns date objects, but trading_days is a list[str] (from
                # _next_n_trading_days()'s .isoformat() calls). `ed in
                # trading_days` was comparing a date against strings -- this
                # is never True in Python, so the "EARNINGS IMMINENT" alert
                # has never fired correctly. Fixed to compare/store the ISO
                # string form, matching the declared imminent: list[tuple[str,
                # str]] type.
                for ed in dates:
                    ed_str = ed.isoformat()
                    if ed_str in trading_days:
                        imminent.append((sym, ed_str))
                        logger.warning(f"[{sym}] EARNINGS IMMINENT: {ed_str}")
                        break
                else:
                    logger.info(f"[{sym}] earnings dates: {dates} — none within 3 trading days")
            else:
                uncovered.append(sym)
                logger.warning(f"[{sym}] NOT IN FMP CACHE — no earnings coverage")
        except Exception as e:
            uncovered.append(sym)
            logger.warning(f"[{sym}] earnings check error: {e}")

    # Step 3 — Summary
    logger.info(
        f"Coverage summary — covered: {len(covered)}, "
        f"uncovered: {len(uncovered)}, imminent: {len(imminent)}"
    )

    # Step 4 — Slack alert
    try:
        from alerts import send_slack

        lines: list[str] = [f":calendar: *Earnings Pre-Flight — {now_pt}*"]

        if imminent:
            lines.append(
                f":rotating_light: *EARNINGS WITHIN 3 TRADING DAYS "
                f"({len(imminent)} symbol{'s' if len(imminent) != 1 else ''}):*"
            )
            for sym, ed_str in imminent:
                lines.append(f"  • `{sym}` — reports {ed_str}")
            lines.append(
                "_Review open/pending positions in these symbols before Monday open._"
            )

        if uncovered:
            lines.append(
                f":warning: *No FMP earnings coverage ({len(uncovered)} symbols):*"
            )
            for sym in uncovered:
                lines.append(f"  • `{sym}` — not in FMP calendar for this window")

        if not imminent and not uncovered and symbols:
            lines.append(
                f":white_check_mark: All {len(covered)} tracked symbols have earnings "
                f"coverage. None reporting within 3 trading days."
            )

        if not symbols:
            lines.append(":zzz: No open positions or watchlist symbols found — nothing to check.")

        send_slack("\n".join(lines))
        logger.info("Slack alert sent.")
    except Exception as e:
        logger.error(f"Slack alert failed: {e}")

    logger.info("=== Earnings Pre-Flight complete ===")


if __name__ == "__main__":
    main()
