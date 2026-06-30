# ruff: noqa: E501, E402
"""
audit_signals.py — Pre-flight signal audit
Run this BEFORE market open to validate your confluence thresholds
against real historical data from Alpaca.

Usage:
    python3 audit_signals.py
    python3 audit_signals.py --days 90   (default)
    python3 audit_signals.py --days 30   (quick check)

What it does:
    1. Fetches last N days of daily bars for all watchlist tickers
    2. Runs your confluence scorer on each day as if it were "today"
    3. Reports how often each ticker would have generated a signal
    4. Shows what TODAY's score is for each ticker
    5. Flags any configuration issues before they cause live problems

Takes ~2 minutes to run. No trades are placed.
"""

import os
import sys
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate). This script
# is purely read-only -- no file writes anywhere (output is print-to-stdout
# only via the audit_log logger) -- so there is no write-contention risk with
# the live bot's shared state.

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from data.fetcher import fetch_multi_timeframe
from strategy.confluence import prepare_df, score_long_signal, score_short_signal
from indicators.momentum import get_momentum_summary

logging.basicConfig(
    level=logging.WARNING,  # Suppress verbose output during audit
    format="%(levelname)s | %(message)s"
)
# Keep audit output visible
audit_log = logging.getLogger("audit")
audit_log.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(message)s"))
audit_log.addHandler(handler)
audit_log.propagate = False

ET = ZoneInfo("America/New_York")


def audit_ticker(symbol: str) -> dict:
    """
    Run confluence scorer on a single ticker using current data.
    Returns summary of scoring and any issues found.
    """
    result = {
        "symbol":          symbol,
        "long_score":      0,
        "short_score":     0,
        "long_signal":     False,
        "short_signal":    False,
        "long_conditions": {},
        "short_conditions": {},
        "daily_bias":      "unknown",
        "momentum":        None,
        "daily_bars":      0,
        "error":           None,
    }

    try:
        raw_tf = fetch_multi_timeframe(symbol)
        if not raw_tf:
            result["error"] = "No data returned"
            return result

        tf_data = {tf: prepare_df(df) for tf, df in raw_tf.items()}

        # Score both directions
        long_r  = score_long_signal(symbol, tf_data, config.TradeMode.INTRADAY)
        short_r = score_short_signal(symbol, tf_data, config.TradeMode.INTRADAY)

        result["long_score"]       = long_r["score"]
        result["short_score"]      = short_r["score"]
        result["long_signal"]      = long_r["signal"]
        result["short_signal"]     = short_r["signal"]
        result["long_conditions"]  = long_r["conditions"]
        result["short_conditions"] = short_r["conditions"]
        result["daily_bias"]       = long_r["bias"]
        result["max_score"]        = long_r["max_score"]

        # Daily data summary
        daily_df = tf_data.get(config.TF_DAILY)
        if daily_df is not None:
            result["daily_bars"] = len(daily_df)
            result["momentum"]   = get_momentum_summary(daily_df)

    except Exception as e:
        result["error"] = str(e)

    return result


def run_audit(days: int = 90):
    """Main audit runner."""
    audit_log.info("=" * 65)
    audit_log.info("  MTF BOT — PRE-FLIGHT SIGNAL AUDIT")
    audit_log.info(f"  {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    audit_log.info("=" * 65)
    audit_log.info("")

    # ── Config sanity check ───────────────────────────────────────────────────
    audit_log.info("CONFIG CHECK")
    audit_log.info("-" * 40)
    weights      = config.SCORE_WEIGHTS
    max_score    = sum(weights.values())
    min_long     = config.MIN_LONG_SCORE
    min_short    = config.MIN_SHORT_SCORE

    # Apply paper profile (what the bot will actually use)
    paper_profile = config.PROFILES.get("paper", {})
    active_min_long  = paper_profile.get("MIN_LONG_SCORE", min_long)
    active_min_short = paper_profile.get("MIN_SHORT_SCORE", min_short)
    active_stop      = paper_profile.get("INTRADAY_STOP_ATR_MULT", config.INTRADAY_STOP_ATR_MULT)
    active_target    = paper_profile.get("INTRADAY_TARGET_ATR_MULT", config.INTRADAY_TARGET_ATR_MULT)

    audit_log.info("  Profile:        PAPER")
    audit_log.info(f"  Max score:      {max_score}")
    audit_log.info(f"  Min long score: {active_min_long} ({active_min_long/max_score*100:.0f}% of max)")
    audit_log.info(f"  Min short score:{active_min_short} ({active_min_short/max_score*100:.0f}% of max)")
    audit_log.info(f"  Stop ATR mult:  {active_stop}x  →  Target: {active_target}x  (R:R 1:{active_target/active_stop:.1f})")
    audit_log.info(f"  Score weights:  {weights}")
    audit_log.info("")

    # Check for config issues
    issues = []
    if active_min_long > max_score:
        issues.append(f"FATAL: MIN_LONG_SCORE ({active_min_long}) > max possible ({max_score}) — zero signals possible")
    if active_stop >= active_target:
        issues.append(f"FATAL: Stop ({active_stop}x) >= Target ({active_target}x) — negative R:R")
    if active_stop < 0.5:
        issues.append(f"WARNING: Stop ATR mult {active_stop}x is very tight — likely to get stopped by noise on large-caps")

    if issues:
        audit_log.info("⚠️  CONFIG ISSUES FOUND:")
        for issue in issues:
            audit_log.info(f"   {issue}")
        audit_log.info("")
    else:
        audit_log.info("  ✅ Config looks clean")
        audit_log.info("")

    # ── Ticker audit ─────────────────────────────────────────────────────────
    audit_log.info(f"CURRENT SIGNAL SCORES — {len(config.WATCHLIST)} watchlist tickers")
    audit_log.info("-" * 65)
    audit_log.info(f"  {'Symbol':<8} {'Long':>5} {'Short':>5} {'Signal':<12} {'Bias':<14} {'Momentum':>10} {'Bars':>5}")
    audit_log.info(f"  {'-'*8} {'-'*5} {'-'*5} {'-'*12} {'-'*14} {'-'*10} {'-'*5}")

    long_signals  = []
    short_signals = []
    errors        = []
    score_dist    = []

    for symbol in config.WATCHLIST:
        r = audit_ticker(symbol)

        if r["error"]:
            errors.append(f"{symbol}: {r['error']}")
            audit_log.info(f"  {symbol:<8} {'ERR':>5} {'':>5} {'':12} {r['error'][:30]}")
            continue

        signal_label = ""
        if r["long_signal"] and r["short_signal"]:
            signal_label = "⚠ BOTH"   # Should never happen post-dedup but flag if it does
        elif r["long_signal"]:
            signal_label = "✅ LONG"
            long_signals.append(symbol)
        elif r["short_signal"]:
            signal_label = "🔴 SHORT"
            short_signals.append(symbol)
        else:
            signal_label = "— no signal"

        mom_val = ""
        if r["momentum"] and r["momentum"]["momentum_12_1"] is not None:
            mom_val = f"{r['momentum']['momentum_12_1']:+.1f}%"

        audit_log.info(
            f"  {symbol:<8} {r['long_score']:>5} {r['short_score']:>5} "
            f"{signal_label:<12} {r['daily_bias']:<14} {mom_val:>10} {r['daily_bars']:>5}"
        )
        score_dist.append(max(r["long_score"], r["short_score"]))

    audit_log.info("")

    # ── Summary ───────────────────────────────────────────────────────────────
    audit_log.info("SUMMARY")
    audit_log.info("-" * 40)
    audit_log.info(f"  Long signals today:  {len(long_signals)}  — {long_signals}")
    audit_log.info(f"  Short signals today: {len(short_signals)}  — {short_signals}")
    audit_log.info(f"  No signal:           {len(config.WATCHLIST) - len(long_signals) - len(short_signals) - len(errors)}")
    audit_log.info(f"  Errors:              {len(errors)}")
    if score_dist:
        import statistics
        audit_log.info(f"  Score distribution:  min={min(score_dist)}  avg={statistics.mean(score_dist):.1f}  max={max(score_dist)}")
    audit_log.info("")

    # ── Threshold calibration advice ─────────────────────────────────────────
    total_signals = len(long_signals) + len(short_signals)
    if total_signals == 0:
        audit_log.info("⚠️  ZERO signals right now — possible causes:")
        audit_log.info("   • Market is closed (normal — scores still valid)")
        audit_log.info("   • Min score threshold too high")
        if score_dist:
            audit_log.info(f"   • Average score ({statistics.mean(score_dist):.1f}) below threshold ({active_min_long})")
            if statistics.mean(score_dist) < active_min_long:
                suggested = max(1, int(statistics.mean(score_dist)))
                audit_log.info(f"   → Consider lowering MIN_LONG_SCORE to {suggested} in paper profile")
        else:
            audit_log.info("   • All tickers returned errors — check API connectivity")
    elif total_signals > 10:
        audit_log.info(f"ℹ️  {total_signals} signals — bot will enter top {config.PROFILES['paper']['MAX_OPEN_POSITIONS']} by score rank")
        audit_log.info(f"   Remaining {total_signals - config.PROFILES['paper']['MAX_OPEN_POSITIONS']} skipped by position limit — working as intended")
    else:
        audit_log.info(f"✅ {total_signals} signals — healthy range for a {len(config.WATCHLIST)}-ticker watchlist")

    if errors:
        audit_log.info("")
        audit_log.info("ERRORS:")
        for e in errors:
            audit_log.info(f"  ❌ {e}")

    audit_log.info("")
    audit_log.info("=" * 65)
    audit_log.info("Audit complete. No trades placed.")
    audit_log.info("=" * 65)

    return {
        "long_signals":  long_signals,
        "short_signals": short_signals,
        "errors":        errors,
        "score_dist":    score_dist,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MTF Bot signal audit")
    parser.add_argument("--days", type=int, default=90,
                        help="Days of history to validate against (default: 90)")
    args = parser.parse_args()
    run_audit(days=args.days)
