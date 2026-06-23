"""
trade_logger.py
Structured trade lifecycle event logging — Guardrail 7.

Writes newline-delimited JSON to logs/trade_events.jsonl.
All timestamps are PT (America/Los_Angeles) per Guardrail 8.

Minimum fields per event (board-approved schema):
  ts, event, symbol, score, mri_level, data_source, price, size

Event types:
  entry         — position opened
  partial_exit  — partial close executed (T1/T2/T3 tranche)
  exit          — full position closed
  stop_hit      — stop-loss triggered (hard_stop, trail_stop, gtc_stop_triggered)
  signal        — confluence signal fired (logged before entry gate)
  mri_refresh   — MacroRiskIndex refreshed

Usage:
  from trade_logger import log_event
  log_event("entry", symbol="AMZN", price=236.78, size=4, score=11,
            mri_level="NORMAL", data_source="alpaca_data")
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PT    = ZoneInfo("America/Los_Angeles")
_JSONL = Path(__file__).resolve().parent / "logs" / "trade_events.jsonl"

# Exit reasons that map to "stop_hit" event type.
# Matched via substring in portfolio_tracker.py — do NOT name future exit
# reasons with these strings as substrings.
_STOP_REASONS = frozenset({
    "hard_stop", "trail_stop", "gtc_stop_triggered",
    "overnight_atr_buffer_exit", "breakeven_stop",
})


def log_event(
    event: str,
    symbol: str = "",
    price: float = 0.0,
    size: int = 0,
    score: int = 0,
    mri_level: str = "NORMAL",
    data_source: str = "alpaca_data",
    **extra,
) -> None:
    """
    Append one structured trade event to logs/trade_events.jsonl.

    Required fields:
      event       — one of: entry, partial_exit, exit, stop_hit, signal, mri_refresh
      symbol      — ticker (e.g. "AMZN")
      price       — fill/current price
      size        — shares / qty
      score       — confluence score (0 for non-signal events)
      mri_level   — MRI level at time of event ("NORMAL"/"ELEVATED"/…)
      data_source — "alpaca_data" or "yfinance_fallback"

    Any additional kwargs are written through for richer postmortem queries.
    """
    record = {
        "ts":          datetime.now(PT).isoformat(),
        "event":       event,
        "symbol":      symbol,
        "score":       int(score),
        "mri_level":   mri_level,
        "data_source": data_source,
        "price":       round(float(price), 4),
        "size":        int(size),
    }
    if extra:
        record.update(extra)

    try:
        os.makedirs(_JSONL.parent, exist_ok=True)
        with open(_JSONL, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning(f"trade_events.jsonl write failed: {e}")
