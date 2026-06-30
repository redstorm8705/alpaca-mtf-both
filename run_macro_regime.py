# ruff: noqa: E501, E402
"""
run_macro_regime.py
Weekly wrapper for the macro-regime-detector skill.
Fetches ETF bars via Alpaca T1 (data/fetcher.py), runs regime calculations
using the skill's calculators directly, and writes regime label + score to
logs/macro_regime_latest.json.

main.py reads the cache at startup — Layer 6 uses it to adjust the MIN_SCORE
floor when the structural regime is Contraction or Concentration (late-cycle).

Data source: Alpaca T1 (RSP/SPY/IWM/TLT/SHY/HYG/LQD/XLY/XLP daily bars)
Treasury rates: yield_curve_calculator SHY/TLT fallback (treasury_rates=None).

Schedule: Sunday 5:00 PM PT (pre-open structural update).
  crontab: 0 17 * * 0 cd /path/to/bot && /usr/local/bin/python3.10 run_macro_regime.py >> /path/to/bot/logs/macro_regime_cron.log 2>&1

Output: logs/macro_regime_latest.json (overwritten each run)
        logs/macro_regime/<timestamped reports> (full detail)
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate). This script
# only writes its own dedicated cache file (macro_regime_latest.json) -- the
# live bot only reads it at startup, never writes to it, so there is no
# write-contention risk like reconcile_eod.py had with eod_{date}.json.
PT = ZoneInfo("America/Los_Angeles")
_now = datetime.now(PT)

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_macro_regime")

BASE_DIR      = Path(__file__).resolve().parent
LOGS_DIR      = BASE_DIR / "logs"
OUTPUT_DIR    = LOGS_DIR / "macro_regime"
LATEST_PATH   = LOGS_DIR / "macro_regime_latest.json"
SKILL_SCRIPTS = (BASE_DIR / "claude-trading-skills-main" / "skills"
                 / "macro-regime-detector" / "scripts")

REQUIRED_ETFS         = ["RSP", "SPY", "IWM", "TLT", "SHY", "HYG", "LQD", "XLY", "XLP"]
HISTORY_DAYS          = 600
HIGH_RISK_REGIMES     = {"Concentration", "Contraction"}
MODERATE_RISK_REGIMES = {"Transitional"}


def _fetch_alpaca_data(symbols: list, days: int) -> dict:
    """
    Fetch ETF daily bars from Alpaca T1 (data/fetcher.py).
    Returns {symbol: [dict, ...]} newest-first, FMP-compatible format.
    """
    sys.path.insert(0, str(BASE_DIR))
    import config as _cfg
    from data.fetcher import fetch_bars

    result: dict[str, list] = {}
    for sym in symbols:
        logger.info(f"  Fetching {sym} ({days} bars, Alpaca T1)...")
        try:
            df = fetch_bars(sym, _cfg.TF_DAILY, num_bars=days)
            if df is None or df.empty:
                logger.warning(f"  {sym}: empty response from Alpaca")
                result[sym] = []
                continue
            rows = [
                {
                    "date":   idx.strftime("%Y-%m-%d"),
                    "open":   float(r["open"]),
                    "high":   float(r["high"]),
                    "low":    float(r["low"]),
                    "close":  float(r["close"]),
                    "volume": int(r["volume"]),
                }
                for idx, r in df.iterrows()
            ]
            rows.reverse()   # newest-first (FMP-compatible format for skill calculators)
            result[sym] = rows
            logger.info(f"  {sym}: OK ({len(rows)} bars)")
        except Exception as e:
            logger.warning(f"  {sym}: FAILED — {e}")
            result[sym] = []
    return result


def run_analysis() -> bool:
    """
    Fetch ETF data via Alpaca T1, compute 6 regime components using skill
    calculators, write timestamped JSON + MD reports to OUTPUT_DIR.
    Returns True on success.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: Fetch market data (T1 — Alpaca, no FMP dependency)
    logger.info("Step 1/4: Fetching Market Data (Alpaca T1)")
    historical = _fetch_alpaca_data(REQUIRED_ETFS, HISTORY_DAYS)
    if not historical.get("SPY"):
        logger.error("SPY fetch failed — cannot proceed without SPY data")
        return False

    # Step 2: Import skill calculators (data-only modules, no FMP client)
    logger.info("Step 2/4: Importing skill calculators")
    if not SKILL_SCRIPTS.exists():
        logger.error(f"Skill scripts not found: {SKILL_SCRIPTS}")
        return False
    sys.path.insert(0, str(SKILL_SCRIPTS))
    from calculators.concentration_calculator     import calculate_concentration
    from calculators.credit_conditions_calculator import calculate_credit_conditions
    from calculators.equity_bond_calculator       import calculate_equity_bond
    from calculators.sector_rotation_calculator   import calculate_sector_rotation
    from calculators.size_factor_calculator       import calculate_size_factor
    from calculators.yield_curve_calculator       import calculate_yield_curve
    from report_generator import generate_json_report, generate_markdown_report
    from scorer import (calculate_composite_score, check_regime_consistency,
                        classify_regime)

    # Step 3: Calculate 6 components
    logger.info("Step 3/4: Calculating Components")
    comp1 = calculate_concentration(
        rsp_history=historical.get("RSP", []),
        spy_history=historical.get("SPY", []),
    )
    comp2 = calculate_yield_curve(
        treasury_rates=None,  # SHY/TLT fallback active (FMP treasury not required)
        shy_history=historical.get("SHY", []),
        tlt_history=historical.get("TLT", []),
    )
    comp3 = calculate_credit_conditions(
        hyg_history=historical.get("HYG", []),
        lqd_history=historical.get("LQD", []),
    )
    comp4 = calculate_size_factor(
        iwm_history=historical.get("IWM", []),
        spy_history=historical.get("SPY", []),
    )
    comp5 = calculate_equity_bond(
        spy_history=historical.get("SPY", []),
        tlt_history=historical.get("TLT", []),
    )
    comp6 = calculate_sector_rotation(
        xly_history=historical.get("XLY", []),
        xlp_history=historical.get("XLP", []),
    )

    component_results = {
        "concentration":    comp1,
        "yield_curve":      comp2,
        "credit_conditions": comp3,
        "size_factor":      comp4,
        "equity_bond":      comp5,
        "sector_rotation":  comp6,
    }
    component_scores = {k: v["score"] for k, v in component_results.items()}
    data_availability = {k: v.get("data_available", False)
                         for k, v in component_results.items()}

    composite = calculate_composite_score(component_scores, data_availability)
    regime    = classify_regime(component_results)
    regime["consistency"] = check_regime_consistency(
        regime["current_regime"], component_results
    )

    logger.info(f"  Regime: {regime['regime_label']} (confidence: {regime['confidence']})")
    logger.info(f"  Composite: {composite['composite_score']}/100")

    # Step 4: Generate reports
    logger.info("Step 4/4: Generating Reports")
    analysis = {
        "metadata": {
            "generated_at": _now.isoformat(),
            "data_source":  "Alpaca T1",   # T1 — not FMP
            "history_days": HISTORY_DAYS,
        },
        "composite":  composite,
        "regime":     regime,
        "components": component_results,
    }
    ts       = datetime.now(PT).strftime("%Y-%m-%d_%H%M%S")
    json_out = str(OUTPUT_DIR / f"macro_regime_{ts}.json")
    md_out   = str(OUTPUT_DIR / f"macro_regime_{ts}.md")
    generate_json_report(analysis, json_out)
    generate_markdown_report(analysis, md_out)
    logger.info(f"  JSON: {json_out}")
    logger.info(f"  MD:   {md_out}")
    return True


def write_latest_cache() -> bool:
    reports = sorted(OUTPUT_DIR.glob("macro_regime_*.json"), reverse=True)
    if not reports:
        logger.error("No macro_regime_*.json found after analysis run.")
        return False

    latest = reports[0]
    try:
        with open(latest) as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {latest}: {e}")
        return False

    regime    = data.get("regime", {})
    composite = data.get("composite", {})
    label      = regime.get("regime_label", "Unknown")
    confidence = regime.get("confidence", "unknown")
    score      = composite.get("composite_score", 0)

    if label in HIGH_RISK_REGIMES:
        risk_tier = 2
    elif label in MODERATE_RISK_REGIMES:
        risk_tier = 1
    else:
        risk_tier = 0

    cache = {
        "generated_at":  _now.isoformat(),
        "source_report": str(latest),
        "regime_label":  label,
        "confidence":    confidence,
        "composite_score": score,
        "risk_tier":     risk_tier,
    }

    _tmp = LATEST_PATH.with_suffix(".json.tmp")
    with open(_tmp, "w") as f:
        json.dump(cache, f, indent=2)
    _tmp.replace(LATEST_PATH)
    logger.info(
        f"macro_regime_latest.json written: label={label} confidence={confidence} "
        f"score={score}/100 risk_tier={risk_tier}"
    )
    return True


def main():
    logger.info(f"run_macro_regime.py starting — {_now.strftime('%Y-%m-%d %H:%M PT')}")
    if not run_analysis():
        logger.error("Analysis failed — macro_regime_latest.json not updated.")
        sys.exit(1)
    if not write_latest_cache():
        logger.error("Cache update failed.")
        sys.exit(1)
    logger.info("run_macro_regime.py complete.")


if __name__ == "__main__":
    main()
