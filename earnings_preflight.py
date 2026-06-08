#!/usr/bin/env python3
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
# RTH block — 9:30 AM–4:00 PM ET weekdays
# ---------------------------------------------------------------------------
def _check_rth_block() -> None:
    _now = datetime.now(ET)
    if _now.weekday() < 5:
        _mins = _now.hour * 60 + _now.minute
        if (9 * 60 + 30) <= _mins < (16 * 60):
            logger.error("BLOCKED: Cannot run during RTH (9:30 AM–4:00 PM ET weekdays).")
            sys.exit(1)


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
    _check_rth_block()

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
        preload_earnings_week()
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
                for ed in dates:
                    if ed in trading_days:
                        imminent.append((sym, ed))
                        logger.warning(f"[{sym}] EARNINGS IMMINENT: {ed}")
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
            for sym, ed in imminent:
                lines.append(f"  • `{sym}` — reports {ed}")
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
