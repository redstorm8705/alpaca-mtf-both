# ruff: noqa: E402, E501  — E402: load_dotenv() must precede project imports
# (they read env keys at import time). E501: pre-existing long argparse/log lines.
# run_movers.py — Standalone Movers Bot runner
# Usage:
#   python run_movers.py                  → full universe, 5-min loop
#   python run_movers.py --once           → single scan + exit
#   python run_movers.py --mode swing     → hold overnight
#   python run_movers.py --top 5          → only top 5 movers
#   python run_movers.py --no-confluence  → pure momentum, no MTF filter

import os
import sys
import time
import argparse
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

from data.fetcher import DataFetcher
from events.calendar import EventCalendar
from events.earnings_fetcher import fetch_upcoming_earnings
from strategy.movers.scanner import MoversScanner
from strategy.movers.strategy import MoversStrategy
from execution.broker import AlpacaBroker
from execution.risk_manager import RiskManager
from execution.portfolio_tracker import PortfolioTracker
from config import TF_15M, TF_DAILY, TF_1H
import strategy.movers.strategy as movers_config

# ─── LOGGING ─────────────────────────────────────────────────────────────────
from pathlib import Path as _Path
LOG_DIR = str(_Path(__file__).resolve().parent / "logs")
LOG_LEVEL = "INFO"

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "movers.log")),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("run_movers")

TIMEFRAMES_FOR_CONFLUENCE = [TF_1H, TF_15M, TF_DAILY]
# TF_WEEKLY removed (board-approved 2026-05-10): unused by confluence scorer
# (confluence.py has zero TF_WEEKLY refs). Weekly bias filtering runs
# independently in signal_generator._get_weekly_bias().


def run_movers_cycle(
    scanner:   MoversScanner,
    strategy:  MoversStrategy,
    fetcher:   DataFetcher,
    broker:    AlpacaBroker,
    universe:  list,
    mode:      str,
    top_n:     int,
    use_confluence: bool,
):
    """One full scan-evaluate-exit cycle."""
    now = datetime.now(ZoneInfo("America/New_York"))
    logger.info(f"─── Movers cycle @ {now.strftime('%H:%M:%S')} ET ───")

    # ── 1. Check exits on open positions ──────────────────────────────────────
    closed = strategy.check_exits()
    if closed:
        logger.info(f"Exits: {closed}")

    # ── 2. Market hours check ─────────────────────────────────────────────────
    if not broker.is_market_open():
        logger.info("Market closed — skipping scan")
        return

    # Avoid first/last 15 minutes
    total_mins = now.hour * 60 + now.minute
    if total_mins < (9 * 60 + 45):
        logger.info("Waiting — first 15 mins (9:30-9:45)")
        return
    if total_mins > (15 * 60 + 45):
        logger.info("EOD window — flattening intraday")
        strategy.flatten_intraday()
        return

    # ── 3. Scan for movers ────────────────────────────────────────────────────
    if mode == "swing":
        movers = scanner.get_daily_movers(universe, top_n=top_n)
    else:
        # Intraday: blend of 30-min moves + daily movers
        intraday_movers = scanner.get_intraday_movers(universe, top_n=top_n, lookback_mins=30)
        daily_movers    = scanner.get_daily_movers(universe, top_n=top_n)

        # Merge and deduplicate, preferring intraday
        seen = set()
        merged_long  = []
        merged_short = []
        for m in intraday_movers["long"] + daily_movers["long"]:
            if m["symbol"] not in seen:
                merged_long.append(m)
                seen.add(m["symbol"])
        for m in intraday_movers["short"] + daily_movers["short"]:
            if m["symbol"] not in seen:
                merged_short.append(m)
                seen.add(m["symbol"])

        movers = {
            "long":      merged_long[:top_n],
            "short":     merged_short[:top_n],
            "scan_time": datetime.now(ZoneInfo("America/New_York")),
        }

    scanner.print_movers(movers)

    # ── 4. Optional: enrich with MTF confluence ───────────────────────────────
    if use_confluence:
        all_syms = [m["symbol"] for m in movers["long"] + movers["short"]]
        tf_cache = {}
        for sym in all_syms:
            try:
                tf_cache[sym] = fetcher.get_multi_timeframe(sym, TIMEFRAMES_FOR_CONFLUENCE)
            except Exception as e:
                logger.warning(f"TF fetch failed {sym}: {e}")
        movers = scanner.add_confluence_score(movers, tf_cache)
        logger.info("Confluence scores added")

    # ── 5. Evaluate entries ───────────────────────────────────────────────────
    executed = strategy.evaluate_entries(movers, mode=mode)
    if executed:
        logger.info(f"Entered: {executed}")

    # ── 6. Print current positions ────────────────────────────────────────────
    strategy.print_positions()


def main():
    parser = argparse.ArgumentParser(description="Alpaca Top Movers Bot")
    parser.add_argument("--mode",           default="intraday", choices=["intraday", "swing"])
    parser.add_argument("--top",            type=int, default=10,  help="Top N movers to scan")
    parser.add_argument("--scan-interval",  type=int, default=300, help="Seconds between scans")
    parser.add_argument("--once",           action="store_true",   help="Single scan and exit")
    parser.add_argument("--no-confluence",  action="store_true",   help="Skip MTF filter (pure momentum)")
    parser.add_argument("--symbols",        nargs="+",             help="Override universe")
    parser.add_argument("--min-score",      type=int, default=None,help="Override confluence min score")
    args = parser.parse_args()

    # Apply overrides
    if args.no_confluence:
        movers_config.MIN_CONFLUENCE = 0
        logger.info("Running in pure momentum mode (no confluence filter)")

    if args.min_score is not None:
        movers_config.MIN_CONFLUENCE = args.min_score
        logger.info(f"Confluence min score overridden to {args.min_score}")

    logger.info("=" * 60)
    logger.info(f"  TOP MOVERS BOT | Mode: {args.mode.upper()} | Top {args.top}")
    logger.info("=" * 60)

    # ── Init components ───────────────────────────────────────────────────────
    fetcher    = DataFetcher()
    broker     = AlpacaBroker()
    account    = broker.get_account()
    logger.info(f"Connected | Equity: ${float(account.equity):,.2f}")
    equity     = float(account.equity)
    try:
        sod_equity = float(account.last_equity)
    except (AttributeError, TypeError, ValueError):
        sod_equity = equity
    risk       = RiskManager(equity, daily_start_value=sod_equity)
    tracker    = PortfolioTracker()
    calendar   = EventCalendar()
    scanner    = MoversScanner(fetcher, calendar)
    strategy   = MoversStrategy(broker, risk, tracker, calendar, scanner)

    # ── Check today's events ──────────────────────────────────────────────────
    day_risk = calendar.get_day_risk()
    if not calendar.is_tradeable():
        logger.warning(f"TODAY IS A MARKET HOLIDAY — bot will not trade: {day_risk['note']}")
    elif day_risk["risk"].value in ("high_risk", "caution", "opportunity"):
        logger.warning(f"⚠ RISK DAY — trading at 50% size: {day_risk['note']}")

    # ── Print week ahead ──────────────────────────────────────────────────────
    week = calendar.get_week_ahead()
    if week:
        logger.info("EVENTS THIS WEEK:")
        for e in week:
            logger.info(f"  {e['date']} | {e['risk'].value.upper()} | {e['note']}")

    # ── Fetch earnings calendar ───────────────────────────────────────────────
    try:
        added = fetch_upcoming_earnings(calendar)
        logger.info(f"Loaded {added} upcoming earnings dates")
    except Exception as e:
        logger.warning(f"Could not fetch earnings calendar: {e}")

    # ── Load universe ─────────────────────────────────────────────────────────
    if args.symbols:
        universe = [s.upper() for s in args.symbols]
    else:
        universe = fetcher.get_universe()

    # S58c: was risk.reset_day() — method never existed (AttributeError crash on
    # every cron run; flagged P5-C2). RiskManager API is reset_daily(portfolio_value).
    risk.reset_daily(sod_equity)
    use_confluence = not args.no_confluence

    if args.once:
        run_movers_cycle(scanner, strategy, fetcher, broker, universe,
                         args.mode, args.top, use_confluence)
        tracker.print_summary()
        return

    # ── Main loop ─────────────────────────────────────────────────────────────
    logger.info(f"Movers bot running — scanning every {args.scan_interval}s")

    while True:
        try:
            _now_et = datetime.now(ZoneInfo("America/New_York"))
            if _now_et.weekday() < 5 and _now_et.hour * 60 + _now_et.minute >= 9 * 60 + 28:
                logger.info("9:28 AM ET — terminating before RTH open, handing off to main.py")
                strategy.flatten_intraday()
                tracker.print_summary()
                break
            run_movers_cycle(scanner, strategy, fetcher, broker, universe,
                             args.mode, args.top, use_confluence)
            time.sleep(args.scan_interval)
        except KeyboardInterrupt:
            logger.info("Movers bot stopped")
            strategy.flatten_intraday()
            tracker.print_summary()
            break
        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            time.sleep(60)


if __name__ == "__main__":
    main()
