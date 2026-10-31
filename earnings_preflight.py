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
import math
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
_OWNERSHIP_LEDGER = _STATE / "ownership_ledger.json"   # every tier's held shares (Swing, Day, QHM, F6)
_LOOKAHEAD_DAYS  = 35   # QHM's 21-day trim window + slack (the cache refresh and the per-symbol check share it)

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
def _load_tracked_symbols(problems: list[str]) -> set[str]:
    """Symbols to check; any ledger read problem is appended to `problems` (shown in Slack, never a silent all-clear)."""
    symbols: set[str] = set()

    if _TRADE_LOG.exists():
        try:
            data = json.loads(_TRADE_LOG.read_text())
            for sym in data.get("open_trades", {}).keys():
                symbols.add(sym.upper())
            logger.info(f"Open positions: {sorted(symbols) or 'none'}")
        except Exception as e:
            logger.warning(f"trade_log.json read error: {e}")

    # PREFLIGHT-LEDGER-1029: trade_log.json holds only the legacy tracker's trades, so this reported "0 tracked
    # symbols" every week while Swing/QHM lots were held (10-25 log). The ownership ledger lists every tier's shares.
    if _OWNERSHIP_LEDGER.exists():
        try:
            data = json.loads(_OWNERSHIP_LEDGER.read_text())
            held: list[str] = []
            for sym, rec in (data.get("positions") or {}).items():
                try:
                    qty = float(rec.get("alpaca_net_qty") or 0) if isinstance(rec, dict) else 0.0
                    if math.isfinite(qty) and qty != 0:
                        held.append(str(sym).upper())
                except (TypeError, ValueError) as e:   # one malformed row never drops the others
                    logger.warning(f"ownership_ledger.json row {sym!r} unreadable: {e}")
                    problems.append(f"ledger row {sym!r} unreadable")
            held.sort()
            symbols.update(held)
            logger.info(f"Ledger positions: {held or 'none'}")
        except Exception as e:
            logger.warning(f"ownership_ledger.json read error: {e}")
            problems.append(f"ownership ledger unreadable ({type(e).__name__})")
    else:
        logger.warning(f"{_OWNERSHIP_LEDGER} missing")
        problems.append("ownership ledger missing")

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
    # PREFLIGHT-ENV-1031: the cron line does not source .env, so FMP/Finnhub keys and the Slack webhook were unset
    # and every Sunday run was an "[ALERT no-op]" (earnings_preflight_cron.log 2026-07-19..10-25). Load it here,
    # before fmp_client and alerts read their keys at import; variables already set in the environment win.
    from dotenv import load_dotenv
    load_dotenv(_BASE / ".env")

    now_pt = _fmt_pt(datetime.now(ET))
    logger.info(f"=== Earnings Pre-Flight — {now_pt} ===")

    ledger_problems: list[str] = []
    symbols = _load_tracked_symbols(ledger_problems)
    logger.info(f"Total tracked symbols: {len(symbols)} — {sorted(symbols) or 'none'}")

    # Step 1 — Refresh FMP earnings cache
    try:
        sys.path.insert(0, str(_BASE))
        from data.fmp_client import (EARNINGS_DATED, EARNINGS_NONE_IN_WINDOW, get_earnings_info,
                                      get_symbol_earnings_info, preload_earnings_week)
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
        # 35 days (QHM's 21-day trim window + slack); the cache write is a per-symbol merge, never a narrowing overwrite.
        preload_earnings_week(sorted(symbols), lookahead_days=_LOOKAHEAD_DAYS)
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
            # UNKNOWN (plan-blocked or failed lookup) is not "no earnings"; only a per-symbol NONE_IN_WINDOW is.
            info = get_earnings_info(sym, min_horizon_days=_LOOKAHEAD_DAYS)
            if info.get("state") == EARNINGS_NONE_IN_WINDOW and info.get("source") == "fmp_batch":
                # The batch calendar is sparse on this plan, so its "none" is not authoritative for a held symbol;
                # the per-symbol answer is (fmp_client.get_symbol_earnings_info docstring).
                info = get_symbol_earnings_info(sym)
            state = info.get("state")
            dates = [d for d in (info.get("dates") or []) if isinstance(d, date) and d >= date.today()]
            if state == EARNINGS_DATED and dates:
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
            elif state in (EARNINGS_DATED, EARNINGS_NONE_IN_WINDOW):
                covered.append(sym)
                logger.info(f"[{sym}] FMP per-symbol: no upcoming earnings date (state={state})")
            else:   # UNKNOWN (plan block, lookup error) or any unexpected state
                uncovered.append(sym)
                logger.warning(f"[{sym}] earnings date {state} (source={info.get('source')}) — no earnings coverage")
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
        from alerts import send_slack_delivery

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
                lines.append(f"  • `{sym}` — earnings date unknown (FMP plan block or lookup error)")

        if not imminent and not uncovered and symbols:
            lines.append(
                f":white_check_mark: All {len(covered)} tracked symbols have earnings "
                f"coverage. None reporting within 3 trading days."
            )

        if ledger_problems:
            lines.append(":warning: *Holdings not fully checked:* " + "; ".join(ledger_problems))
        elif not symbols:
            lines.append(":zzz: No open positions or watchlist symbols found — nothing to check.")

        if send_slack_delivery("\n".join(lines)):
            logger.info("Slack alert sent.")
        else:
            logger.error("Slack alert NOT delivered (webhook unset or post failed).")
    except Exception as e:
        logger.error(f"Slack alert failed: {e}")

    logger.info("=== Earnings Pre-Flight complete ===")


if __name__ == "__main__":
    main()
