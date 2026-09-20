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


# ═══════════════════════════════════════════════════════════════════════════════
# TIER-AGNOSTIC PER-TRADE RECORD BUILDERS — edge-discovery Step 1
# (design: logs/design_records/edge_discovery_2026-09-19.md, BGGN 5/5)
# ═══════════════════════════════════════════════════════════════════════════════
# ONE schema definition, shared by BOTH the offline backfill
# (research/trade_record_reducer.py, shipped WITH this) and the live emit path
# (portfolio_tracker / day_trade_manager — wired in a LATER, separately-gated
# increment) so the backfilled 3 months of history and every future live record
# are the SAME shape — the fuel research/ic_engine.py + deflated_sharpe.py have
# been starved of.
#
# SAFETY CONTRACT (why this is additive, not risk-path):
#   • These are PURE functions — no I/O, no order placement, no sizing, no gate.
#     Nothing here is called by any live trading decision in THIS increment.
#   • realized_pnl is passed IN (Alpaca-fills sourced upstream — the P&L system of
#     record per Guardrail 1). These builders NEVER compute, adjust, or MASK P&L;
#     they only arrange a value that already exists and derive R-multiples from
#     prices. A None/garbage input yields a None field (recorded honestly), never
#     a silent 0 or a crash into a caller.
#   • outcome_label is derived from WHICH barrier the exit reason names (triple-
#     barrier), NOT from the P&L sign — so a masked/mislabeled loss is impossible
#     here (the label and the P&L are independent, cross-checkable fields).

TRADE_RECORD_SCHEMA_V = 1
DEFAULT_SCORE_MAX = 12  # live 12-pt confluence; callers pass score_max explicitly

# Exit-reason substrings that mark a TARGET/take-profit barrier touch (LdP meta-
# labeling pre-wire). Stop barriers reuse _STOP_REASONS above + a generic "stop".
_TARGET_REASON_HINTS = ("take_profit", "target", "profit_target", "tp_hit")


def _f(x):
    """float(x) or None — never raises. A malformed numeric field becomes a recorded
    None (honest 'unknown'), never a crash into the caller and never a silent 0.0."""
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def outcome_from_reason(exit_reason: str) -> int:
    """Triple-barrier label from WHICH barrier the exit reason names (not P&L sign):
    +1 target/take-profit, -1 stop, 0 time/eod/external/manual. Pre-wires LdP
    meta-labeling. Independent of realized_pnl by design (cross-checkable)."""
    r = (exit_reason or "").lower()
    if any(h in r for h in _TARGET_REASON_HINTS):
        return 1
    if "stop" in r or any(s in r for s in _STOP_REASONS):
        return -1
    return 0


def compute_planned_R(entry_price, stop_price, target_price):
    """Planned reward:risk in R at entry. risk=|entry-stop|, reward=|target-entry|.
    None when any input is unknown or risk is zero (never a divide-by-zero)."""
    e, s, t = _f(entry_price), _f(stop_price), _f(target_price)
    if e is None or s is None or t is None:
        return None
    risk = abs(e - s)
    if risk <= 0:
        return None
    return round(abs(t - e) / risk, 4)


def compute_realized_R(entry_price, stop_price, exit_price, side):
    """Realized reward:risk in R, SIGNED by direction. long:(exit-entry)/risk,
    short:(entry-exit)/risk, risk=|entry-stop|. None on unknown input, zero risk,
    OR an unrecognized side (never silently assume a direction — a wrong sign would
    invert every downstream statistic, and P&L reconciliation would not catch it)."""
    e, s, x = _f(entry_price), _f(stop_price), _f(exit_price)
    if e is None or s is None or x is None:
        return None
    risk = abs(e - s)
    if risk <= 0:
        return None
    sd = str(side).lower()
    if sd.startswith("l"):
        reward = x - e
    elif sd.startswith("s"):
        reward = e - x
    else:
        return None
    return round(reward / risk, 4)


def compute_mae_mfe_R(entry_price, stop_price, min_price, max_price, side):
    """MAE/MFE in R from the price path's min & max while the trade was open.
    Returns (mae_R, mfe_R): mae_R = max ADVERSE excursion as a POSITIVE R-against,
    mfe_R = max FAVORABLE excursion as a POSITIVE R-for. (None, None) on unknown
    input / zero risk. Excursions are clamped to >= 0 (a trade that only moved one
    way has 0 on the other side)."""
    e, s = _f(entry_price), _f(stop_price)
    lo, hi = _f(min_price), _f(max_price)
    if e is None or s is None or lo is None or hi is None:
        return None, None
    risk = abs(e - s)
    if risk <= 0:
        return None, None
    sd = str(side).lower()
    if sd.startswith("l"):                      # long: best=highest, worst=lowest
        mfe = (hi - e) / risk
        mae = (e - lo) / risk
    elif sd.startswith("s"):                    # short: best=lowest, worst=highest
        mfe = (e - lo) / risk
        mae = (hi - e) / risk
    else:
        # unknown side -> cannot orient excursions
        return None, None
    return round(max(mae, 0.0), 4), round(max(mfe, 0.0), 4)


def make_components(conditions, weights=None):
    """Decompose a boolean-conditions dict into {factor:{value,weight,contribution}}
    using SCORE_WEIGHTS — the exact per-factor decomposition ic_engine.py calls its
    'Phase 1b' blocker. value coerced to bool; contribution = weight when truthy.
    config is imported lazily (it has no import-time trading side effects) to keep
    trade_logger's import chain minimal."""
    try:
        from config import SCORE_WEIGHTS
    except Exception:
        SCORE_WEIGHTS = {}
    w = weights if weights is not None else SCORE_WEIGHTS
    conditions = conditions or {}
    out = {}
    for factor, weight in w.items():
        v = bool(conditions.get(factor, False))
        out[factor] = {"value": v, "weight": weight, "contribution": weight if v else 0}
    return out


# Required keys (design record "required = *"). "Required" == the FIELD must be
# PRESENT from trade #1 (a value may be a dumb default or a null 'unknown' — e.g.
# planned_R with no target, or confidence before calibration). The invariant test
# asserts presence + JSON-serializability, and non-null only for the fields that
# are ALWAYS known (identity + entry price + qty).
REQUIRED_ENTRY_FIELDS = frozenset({
    "trade_id", "schema_v", "tier", "symbol", "side", "ts_entry", "confidence",
    "score_raw", "score_max", "components", "weights_version", "model_version",
    "regime", "mri_level", "gex_regime", "indicators", "entry_price",
    "stop_price", "target_price", "planned_R", "qty", "notional",
})
REQUIRED_EXIT_FIELDS = frozenset({
    "trade_id", "schema_v", "tier", "symbol", "ts_exit", "exit_price",
    "exit_reason", "realized_pnl", "realized_R", "mae_R", "mfe_R",
    "outcome_label", "qty",
})
# Always-non-null subset (identity + what is known at every entry).
ENTRY_NONNULL_FIELDS = frozenset({
    "trade_id", "schema_v", "tier", "symbol", "side", "ts_entry",
    "score_max", "entry_price", "qty",
})


def make_entry_record(*, trade_id, tier, symbol, side, entry_price, stop_price,
                      qty, score_raw, target_price=None,
                      score_max=DEFAULT_SCORE_MAX, confidence=None,
                      components=None, indicators=None, regime="UNKNOWN",
                      mri_level="UNKNOWN", gex_regime="UNKNOWN",
                      weights_version="", model_version="", notional=None,
                      ts_entry=None, **extra):
    """Build the ENTRY portion of a tier-agnostic trade record. Pure, never raises
    on normal inputs. confidence (Layer 0) defaults to score_raw/score_max clamped
    to [0,1] — the FIELD exists from trade #1 even before calibration."""
    e = _f(entry_price)
    q = _f(qty)
    sr = _f(score_raw)
    sm = _f(score_max)
    if sm is None:  # None/unparseable -> default (guards 0.0-swallow)
        sm = float(DEFAULT_SCORE_MAX)
    confidence = _f(confidence)          # explicit confidence via _f (never raises)
    if confidence is None and sr is not None and sm:
        confidence = max(0.0, min(1.0, sr / sm))
    n = _f(notional)
    if n is None and e is not None and q is not None:
        n = e * q
    sp = _f(stop_price)
    tp = _f(target_price)
    conf = round(float(confidence), 4) if confidence is not None else None
    rec = {
        "trade_id":        str(trade_id),
        "schema_v":        TRADE_RECORD_SCHEMA_V,
        "tier":            str(tier),
        "symbol":          str(symbol),
        "side":            str(side).lower(),
        "ts_entry":        ts_entry or datetime.now(PT).isoformat(),
        "confidence":      conf,
        "score_raw":       sr,
        "score_max":       sm,
        "components":      components if components is not None else {},
        "weights_version": str(weights_version),
        "model_version":   str(model_version),
        "regime":          str(regime),
        "mri_level":       str(mri_level),
        "gex_regime":      str(gex_regime),
        "indicators":      indicators if indicators is not None else {},
        "entry_price":     (round(e, 4) if e is not None else None),
        "stop_price":      (round(sp, 4) if sp is not None else None),
        "target_price":    (round(tp, 4) if tp is not None else None),
        "planned_R":       compute_planned_R(entry_price, stop_price, target_price),
        "qty":             q,
        "notional":        (round(n, 2) if n is not None else None),
    }
    if extra:
        rec.update(extra)
    return rec


def make_exit_record(*, trade_id, tier, symbol, side, entry_price, stop_price,
                     exit_price, qty, realized_pnl, exit_reason,
                     min_price=None, max_price=None, ts_exit=None, **extra):
    """Build the EXIT portion of a tier-agnostic trade record. realized_pnl is
    passed IN (Alpaca-fills sourced) — never computed or masked here. realized_R,
    MAE/MFE derive from prices; outcome_label from the exit reason (barrier),
    independent of P&L sign. Pure, never raises on normal inputs."""
    mae_R, mfe_R = compute_mae_mfe_R(
        entry_price, stop_price, min_price, max_price, side)
    pnl = _f(realized_pnl)
    xp = _f(exit_price)
    rec = {
        "trade_id":      str(trade_id),
        "schema_v":      TRADE_RECORD_SCHEMA_V,
        "tier":          str(tier),
        "symbol":        str(symbol),
        "ts_exit":       ts_exit or datetime.now(PT).isoformat(),
        "exit_price":    (round(xp, 4) if xp is not None else None),
        "exit_reason":   str(exit_reason),
        "realized_pnl":  (round(pnl, 2) if pnl is not None else None),
        "realized_R":    compute_realized_R(entry_price, stop_price, exit_price, side),
        "mae_R":         mae_R,
        "mfe_R":         mfe_R,
        "outcome_label": outcome_from_reason(exit_reason),
        "qty":           _f(qty),
    }
    if extra:
        rec.update(extra)
    return rec


def make_trade_record(entry_rec: dict, exit_rec: dict) -> dict:
    """Merge an entry record and an exit record into ONE closed-trade row (the
    trade_records.jsonl shape). Exit fields win for the few shared keys (trade_id/
    schema_v/tier/symbol/qty), which are identical by construction."""
    return {**(entry_rec or {}), **(exit_rec or {})}
