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
import time
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

# ── Write-failure operator alert throttle (observability follow-up to D1) ──────
# A persistent write failure fires on ~every event, so escalate LOUDLY (ERROR +
# Slack) but at most once per window — this bounds BOTH operator spam AND blocking
# of the run_cycle trading thread on the synchronous (urllib, ~4s) Slack send.
# In-memory (NOT a state file) on purpose: this runs inside the disk-write failure
# handler, so a throttle-state file write could itself fail. 3600s is an operator-
# cadence choice — no historical data to derive it from (flagged per no-static rule);
# it surfaces a persistent outage within the first trading hour without spamming.
_WRITE_FAIL_ALERT_THROTTLE_S = 3600.0
_last_write_fail_alert = 0.0


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
            # DURABILITY (day-tier live build, data-integrity seat 2026-09-02):
            # flush+fsync so the event reaches stable storage before the call
            # returns. Without it, page-cache data is lost on a host hard-stop or
            # OCI reboot — a weekend-gap vector for a multi-day paper-live run (the
            # day-tier's canonical P&L flows through here). Cost is ms-scale at a
            # few events per 5-min cycle, so fsync-per-event is safe on the
            # run_cycle thread; inside the try, so an fsync failure hits the same
            # throttled write-fail alert below (never stalls the trading thread).
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        # Observability follow-up to D1 (2026-07-28): escalate the previously-silent
        # WARNING to ERROR + a throttled operator Slack. A silent WARNING let a 7-day
        # entry-write outage hide. ERROR is free/non-blocking. The Slack send is
        # synchronous (urllib ~4s) and log_event runs on the run_cycle trading thread,
        # so it is BOTH throttled (≤1 send/window bounds trading-thread blocking) AND
        # fully guarded (a failed/blocked alert must never crash or stall the caller).
        # Event LOG path only — never P&L or a trade decision — so alerting cannot mask
        # a loss. `event`/`symbol` are function parameters, always in scope here.
        logger.error(f"trade_events.jsonl write FAILED ({event} {symbol}): {e}")
        global _last_write_fail_alert
        _now = time.monotonic()
        if _now - _last_write_fail_alert >= _WRITE_FAIL_ALERT_THROTTLE_S:
            # stamp BEFORE send: caps trading-thread blocking at ≤1×4s per window,
            # even if Slack is down (a failed send does not reset the throttle).
            _last_write_fail_alert = _now
            try:
                # Lazy import (only on this rare, throttled path) ON PURPOSE: a
                # top-level alerts import would put alerts.py in trade_logger's import
                # chain; any alerts import failure would trip portfolio_tracker's
                # `_log_event(*a,**kw): pass` fallback — silently disabling ALL trade
                # logging (the exact D1 silent-drop class this change exists to end).
                from alerts import send_slack
                send_slack(
                    "⚠️ WARNING — trade_events.jsonl WRITE FAILING\n"
                    f"Last error ({event} {symbol}): {e}\n"
                    "Structured trade log is not recording; daily audits will "
                    "under-report until fixed. Position stops are UNAFFECTED."
                )
            except Exception as _alert_e:
                logger.error(
                    f"trade_logger: write-fail alert itself failed: {_alert_e}"
                )
