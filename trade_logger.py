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
  halt_eval     — Build F halt-observability discriminator (every RTH cycle):
                  {keyword_hit, spy_5m_pct, qqq_5m_pct, venue_status, verdict}

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


def _json_default(o):
    """Coerce a value json.dumps cannot natively serialize, so one odd type can
    never abort the whole event write.

    D1 (2026-07-20..07-27): a numpy.bool_ in the entry `conditions` payload (added
    by commit 0c2db0d) raised "Object of type bool is not JSON serializable", and
    the broad `except` in log_event() swallowed it as a WARNING -- so EVERY `entry`
    event was silently dropped for 7 days while `exit`/`stop_hit` (no numpy) kept
    writing. All three daily audits then re-derived a phantom "0 trades / accounting
    failure" from the entry-less log.

    numpy scalars expose `.item()` -> a native Python primitive; a non-JSON-native
    `.item()` result (e.g. numpy datetime) or an object with no usable `.item()`
    falls back to str(). The result is ALWAYS a JSON-native value, so json.dumps can
    never re-raise on what this returns. This is the event LOG only -- never P&L, a
    trading decision, or a risk value -- so a faithful coercion here cannot mask a loss.
    """
    item = getattr(o, "item", None)
    if callable(item):
        try:
            v = item()
            if v is None or isinstance(v, (bool, int, float, str)):
                return v
        except Exception:
            pass
    return str(o)


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
            f.write(json.dumps(record, default=_json_default) + "\n")
    except Exception as e:
        logger.warning(f"trade_events.jsonl write failed: {e}")
