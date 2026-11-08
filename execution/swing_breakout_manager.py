# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/swing_breakout_manager.py — Confluence 2.0 swing tier: megacap 55-day breakout (RISK-PATH).

Design record: logs/design_records/c2_swing_breakout_tier_2026-09-29.md (evidence: labs 3-9 in
logs/design_records/entry_rebuild_2026-09-26.md; live since 2026-04-06 the 12-point swing lost -$126.00).
Owner design SWING-OWNER-1025 (logs/design_records/swing_owner_design_2026-10-25.md): top-50 universe, pending
breakout setups that must be recovered, stop-based fractional-Kelly sizing, Invariant #11 limb 1 retired for Swing.

WHAT IT TRADES (long only, one lot per symbol):
  Universe  = the SWING_BREAKOUT_TOP_N names of the pool (S&P 500 / NDX names that reached the top 50 by 60-day
              median dollar volume in the last year) with the highest 60-day median dollar volume, ranked each day
              on CLOSED daily bars (T1 Alpaca via data.fetcher.fetch_closed_bars). Setups are tried in rank order,
              so the top 10 go first.
  Setup     = the last closed daily bar closes above the highest close of the prior 55 sessions AND above its
              200-day SMA → a PENDING setup at that prior 55-day high (the breakout level). It is dropped when a
              closed bar closes below the prior 20-session low; one entry per setup.
  Entry     = a pending setup whose session open, live ask AND fresh last trade are all at or above its breakout level
              → a marketable DAY limit (first cycle >= 10:05 ET). Any of them below the level, or no fresh last trade,
              waits (never entered below it).
  Protection= a GTC sell stop at fill − the stop distance (2.5 × ATR(14, daily), floored at the stock's own
              low-percentile ATR% × price) placed immediately after the fill; if it cannot be
              placed after 3 tries the lot is flattened. Every cycle _reconcile repairs ANY held lot to "open with a
              resting stop for min(lot qty, broker long qty)": a stop cancelled/expired/rejected by anyone, never
              placed, or lost to a restart is re-placed (paged, retried each cycle); a stop is never placed on
              shares the broker does not show (no accidental short); a lot missing at the broker is booked closed.
  Exit      = in the last 10 minutes before the real close: last trade price below the lowest low of the prior 20 completed sessions, or the 30th
              session held (entry day = session 1). Exit = cancel the stop (confirmed) then close ONLY this tier's
              quantity (broker.partial_close_position(tier=TIER)) — never a whole-symbol close.

SIZING (SWING-OWNER-1025, owner-approved 2026-10-25; board Thorp/Shaw/Taleb + Gro + GAI): up to
  SWING_BREAKOUT_MAX_SLOTS lots, each sized by its OWN stop: shares = r × (K / K ceiling) × equity / (stop distance ×
  loss multiple). r = SWING_BREAKOUT_KELLY_FRACTION × max(0, mean R − SE) / var R from this tier's closed lots (R =
  the lot's fill P&L / its risk at the stop) once KELLY_MIN_SAMPLE_SIZE lots exist, before that from the replay prior
  (research/swing_owner_prior_2026-10-25.json); loss multiple = the mean loss of stopped lots in R (gap-throughs), never
  below 1. Bounds: the per-lot loss at the stop <= SWING_BREAKOUT_MAX_RISK_PCT of equity (a single share may risk up to
  SWING_BREAKOUT_MIN1_RISK_PCT when that buys none — CEO 2026-10-14); total overnight notional (all tiers) <= K ×
  equity (Invariant #11 limb 2; limb 1, swing/day <= 100%, no longer binds this tier). Gap check: the open Swing book
  plus the new lot, each × its own 99th-percentile one-day loss, must fit the margin buffer (E − m·L)/(1 − m); the new
  lot is resized down to fit, never below one share. No room in the K limb → skip + log (block-only, never a sale).

ISOLATION: a symbol that already has ANY Alpaca position or open order, or that is a quarterly-hold name,
  is skipped (no co-holds, no wash-trade collisions with another tier's resting sell stop). Orders carry the
  swing owner tag ("intraday"; the allocator lease's IN- client_order_id); this tier's own lots are identified
  by its state file (orphan/drift exclusion reads get_breakout_symbols()).
CAPITAL: every entry is admitted by execution.tier_capital_allocator.live_admit("swing", ...) before submit;
  a denial is a logged skip. Entries are admitted with overnight=False (the same as entry_logic's RTH swing
  path): the allocator's overnight flag caps TOTAL account gross — QHM and Forever-6 included — at 40%, and
  Rafael excluded those buy-and-hold tiers from swing exposure limits (CLAUDE.md, 2026-09-26). This tier's own
  overnight bound is the K-limb sizing cap above (Invariant #11). While TIER_CAPITAL_ALLOCATOR_ENABLED is
  False (since 2026-10-02) live_admit approves without a lease and the sizing cap is the binding bound.

PROFIT LOCK (SWING-RATCHET-1105, owner design logs/design_records/swing_ratchet_2026-11-05.md rev 9/9.1): once a lot's
  completed daily close reaches entry + trigger × R (R = the lot's own entry stop distance per share, fixed at entry),
  its own IN- GTC stop is replaced IN PLACE at max(current stop, min(entry + lock × R rounded up to the cent, bid − one
  tick)). trigger / lock / tick come only from the replay-produced logs/research/swing_pm/ratchet_params.json. Only
  ever raised; never cancelled; an owner (untagged) or other-tier stop is never touched. A per-lot state machine
  (idle | pending | reprotect_pending | done) reconciles every regular-hours cycle from broker truth.

LOGGING: every entry, skip and exit writes its decision stack to trade_events.jsonl (Rule D).
FAIL-SAFE: SWING_BREAKOUT_ENABLED False → no new entries; lots already held keep their stops and managed exits. Any error in one symbol aborts THAT
  symbol only and never raises into run_cycle.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
# Ownership: breakout lots ARE the swing tier (Rafael 2026-09-27: the swing tier's entries), so they carry the
# swing owner tag "intraday" (IN- client_order_id) and draw from the live tier capital allocator's "swing"
# budget — the same path as execution/entry_logic.py. A separate ledger tier would break the allocator's
# four-tier reconciliation (tier_capital_allocator._ledger_tier_gross) and block every tier's entries.
TIER = "intraday"
ALLOC_TIER = "swing"
FAMILY_ID = "swing_breakout_55d_v1"
HYPOTHESIS_VERSION = "c2-swing-breakout-2026-09-29"
_STATE = Path(__file__).resolve().parent.parent / "data" / "state" / "swing_breakout.json"

# Lot statuses that may still hold shares at the broker: counted as this tier's (slots, notional, orphan/drift
# exclusion) so no other system adopts or closes them. Only "open" lots are actively managed.
_ENTRY_PENDING = ("submitting", "submitted", "submit_unknown", "entry_unverified")
_HOLDING = ("open", "exit_unverified") + _ENTRY_PENDING
_ENTRY_START_MIN = 10 * 60 + 5     # first entry cycle: 10:05 ET — after run_cycle's 09:30-10:00 opening return
                                   # and after the QHM entry hook's own >= 10:05 gate (QHM picks go first)
_PAGE_EVERY_S = 1800               # repeat an unresolved-lot page at most every 30 minutes

CHANGE = "swing-owner-1025"
RATCHET_CHANGE = "swing-ratchet-1105"
_RATCHET_PARAMS = Path(__file__).resolve().parent.parent / "logs" / "research" / "swing_pm" / "ratchet_params.json"
_TRADE_EVENTS = Path(__file__).resolve().parent.parent / "logs" / "trade_events.jsonl"
_ALPACA_CLIENT_ID_API_CHARS = 48   # Alpaca's documented client_order_id length limit (API plumbing)
_RP_HASH_DIGITS = 24               # hex chars of sha256(lot coid) in a re-protection client id (design rev 9)
_LIVE_STOP = ("new", "held", "accepted", "pending_new")     # a stop order that is in force
_REPLACEABLE = ("new", "held")                              # Alpaca refuses a replace in accepted / pending_*
_PRIOR = Path(__file__).resolve().parent.parent / "research" / "swing_owner_prior_2026-10-25.json"
# The pool (SWING-OWNER-1025): every current S&P 500 / NDX member that ranked in the top 50 by 60-day median dollar
# volume on any session of the last 252 (lab cache, last bar 2026-09-25; research/swing_owner_prior.py). The daily
# rank on live Alpaca bars picks the top SWING_BREAKOUT_TOP_N of these.
_DEFAULT_POOL = ["AAPL", "ABBV", "ACN", "ADBE", "AMAT", "AMD", "AMZN", "APP", "ARM", "ASML", "AVGO", "BA", "BAC",
                 "BE", "BKNG", "BRK.B", "C", "CAT", "COHR", "COIN", "COST", "CRM", "CRWD", "CRWV", "CSCO", "CVX",
                 "DELL", "GE", "GEV", "GLW", "GOOG", "GOOGL", "GS", "HD", "HOOD", "IBM", "INTC", "INTU", "JNJ",
                 "JPM", "KLAC", "LITE", "LLY", "LRCX", "MA", "META", "MRK", "MRVL", "MSFT", "MSTR", "MU", "NBIS",
                 "NFLX", "NOW", "NVDA", "ORCL", "PANW", "PFE", "PG", "PLTR", "QCOM", "RKLB", "SHOP", "SMCI", "SNDK",
                 "SPCX", "STX", "TSLA", "TXN", "UBER", "UNH", "V", "VRT", "WDC", "WFC", "WMT", "XOM"]


def _cfg(name: str, default: Any) -> Any:
    return getattr(config, name, default)


def enabled() -> bool:
    return bool(_cfg("SWING_BREAKOUT_ENABLED", False))


# ── pure decision functions (unit-tested) ───────────────────────────────────────────────────────
def median_dollar_volume(df, window: int = 60) -> Optional[float]:
    """60-day median of close × volume on closed daily bars; None when fewer than 40 bars."""
    try:
        if df is None or len(df) < 40:
            return None
        dv = (df["close"].astype(float) * df["volume"].astype(float)).tail(window)
        v = float(dv.median())
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def rank_universe(bars: dict, top_n: int) -> list[str]:
    """Top `top_n` symbols by 60-day median dollar volume (ties broken by symbol for determinism)."""
    scored = [(v, s) for s, df in bars.items() if (v := median_dollar_volume(df)) is not None]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s for _, s in scored[:top_n]]


def atr14(df) -> Optional[float]:
    """Simple 14-day average true range on closed daily bars (the lab's base.atr definition)."""
    try:
        if df is None or len(df) < 15:
            return None
        h, lo, c = (df[k].astype(float).to_numpy() for k in ("high", "low", "close"))
        trs = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(len(c) - 14, len(c))]
        a = sum(trs) / 14.0
        return a if math.isfinite(a) and a > 0 else None
    except Exception:
        return None


def breakout_signal(df, lookback: int = 55) -> Optional[dict]:
    """The lab-8c rule on the LAST closed bar: close > highest close of the prior `lookback` sessions and
    close > SMA(200). Returns {close, prior_high, sma200, atr} or None (also None on insufficient data)."""
    try:
        if df is None or len(df) < max(200, lookback + 1):
            return None
        c = df["close"].astype(float)
        last = float(c.iloc[-1])
        prior_high = float(c.iloc[-(lookback + 1):-1].max())
        sma200 = float(c.tail(200).mean())
        a = atr14(df)
        if a is None or not all(math.isfinite(x) for x in (last, prior_high, sma200)):
            return None
        if last > prior_high and last > sma200:
            return {"close": last, "prior_high": prior_high, "sma200": sma200, "atr": a}
        return None
    except Exception:
        return None


def prior_low(df, n: int = 20) -> Optional[float]:
    """Lowest low of the last `n` COMPLETED sessions (the exit reference checked against the live price)."""
    try:
        if df is None or len(df) < n:
            return None
        v = float(df["low"].astype(float).tail(n).min())
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def sessions_held(df, entry_date: str) -> Optional[int]:
    """Sessions held INCLUDING today (entry day = 1): completed sessions on/after the entry date, plus today.
    `df` holds completed daily bars only, so today's session is added explicitly."""
    try:
        idx = [str(t)[:10] for t in df.index]
        return sum(1 for d in idx if d >= entry_date) + 1
    except Exception:
        return None


def exit_reason(live_price: float, lo20: Optional[float], held: Optional[int], max_hold: int) -> Optional[str]:
    """'trend_break' when the live price is below the prior 20-session low; 'time' on the max_hold-th session;
    None otherwise. Unknown inputs never force an exit (the GTC stop still protects the lot)."""
    if lo20 is not None and math.isfinite(live_price) and live_price > 0 and live_price < lo20:
        return "trend_break"
    if held is not None and held >= max_hold:
        return "time"
    return None


def _finite(*xs) -> bool:
    try:
        return all(math.isfinite(float(x)) for x in xs)
    except (TypeError, ValueError):
        return False


def stop_distance(df, price: float, atr: float) -> Optional[tuple[float, float]]:
    """(stop distance in $, ATR% floor) for a lot entered at `price`: SWING_BREAKOUT_STOP_ATR × ATR14, floored at the
    stock's OWN low (10th) percentile of daily ATR14 / close over the bars it has, so a stale or near-zero ATR cannot
    pull the stop distance below that stock's own quietest-regime level (board Thorp, SWING-OWNER-1025). The floor binds
    only when ATR falls under ~40% of that level; above it the stop is 2.5 x ATR. None on any bad input (→ no entry)."""
    try:
        if df is None or not (_finite(price, atr) and price > 0 and atr > 0):
            return None
        import pandas as pd
        h, lo, c = (df[k].astype(float) for k in ("high", "low", "close"))
        prev = c.shift(1)
        tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
        atr_pct = (tr.rolling(14).mean() / c).dropna()
        floor_pct = float(atr_pct.quantile(0.10)) if len(atr_pct) else 0.0
        if not (math.isfinite(floor_pct) and floor_pct >= 0):
            floor_pct = 0.0
        dist = max(float(config.SWING_BREAKOUT_STOP_ATR) * atr, floor_pct * price)
        return (dist, floor_pct) if math.isfinite(dist) and dist > 0 else None
    except Exception:
        return None


def p99_down(df) -> Optional[float]:
    """The stock's own empirical 99th-percentile one-day loss (a fraction >= 0) from its closed daily closes; None when
    it cannot be measured."""
    try:
        r = df["close"].astype(float).pct_change().dropna()
        if not len(r):
            return None
        v = -float(r.quantile(0.01))
        return (v if v > 0 else 0.0) if math.isfinite(v) else None
    except Exception:
        return None


def lot_r(rec: dict) -> Optional[float]:
    """A closed lot's R = its realized P&L (from its own Alpaca order fills) / its risk at the stop at entry. None when
    either is unknown (an estimated entry, an unverified close, a legacy lot whose qty shrank by a partial)."""
    try:
        pnl = rec.get("realized_pnl")
        if pnl is None or rec.get("entry_px_estimated"):
            return None
        risk = rec.get("risk_usd")
        if risk is None:
            if rec.get("partial_pnl") is not None or rec.get("partial_pnl_unknown"):
                return None
            risk = _entry_risk_ps(rec) * float(rec.get("qty") or 0)
        risk = float(risk)
        if not (_finite(pnl, risk) and risk > 0):
            return None
        return float(pnl) / risk
    except (TypeError, ValueError):
        return None


def _risk_usd_ps(rec: dict) -> Optional[float]:
    """risk_usd / qty, only while the lot's qty is still its entry qty (no partial booked or unknown); else None."""
    try:
        risk, qty = rec.get("risk_usd"), float(rec.get("qty") or 0)
        if risk is None or qty <= 0 or rec.get("partial_pnl") is not None or rec.get("partial_pnl_unknown"):
            return None
        v = float(risk) / qty
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def _entry_risk_ps(rec: dict) -> float:
    """Risk per share at ENTRY: init_risk_ps (written once at fill), else risk_usd / entry qty, else entry −
    init_stop_px, else entry − stop_px ONLY for a lot whose stop the ratchet never raised. Never a raised stop
    (SWING-RATCHET-1105 rev 4 item 10c, cold review M2). 0.0 when unknown."""
    try:
        v = rec.get("init_risk_ps")
        if v is not None and _finite(v) and float(v) > 0:
            return float(v)
        rps = _risk_usd_ps(rec)
        if rps is not None:
            return rps
        if rec.get("init_stop_px") is not None:
            stop = rec.get("init_stop_px")
        elif (rec.get("ratchet") or {}).get("order_ids") or (rec.get("ratchet") or {}).get("new_order_id"):
            return 0.0                                   # the stop was raised: stop_px is not the entry risk
        else:
            stop = rec.get("stop_px")
        v = float(rec.get("entry_px") or 0) - float(stop or 0)
        return v if math.isfinite(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


# every exit at or through a stop level, measured against the stop_px in force (R6-2: a market close because the price
# was already through an unplaceable stop is a stop loss, and its gap counts)
_STOP_FAMILY = ("protective_stop", "ratchet_stop", "stop_breached_unprotected")
_STOP_PARTIALS = tuple(f"{r}_partial" for r in _STOP_FAMILY)


def _gap_mult(stop: Any, fill: Any, risk: Any) -> Optional[float]:
    """1 + max(0, stop − fill) / risk for one stop fill; None when any input is unusable."""
    try:
        stop, fill, risk = float(stop or 0), float(fill or 0), float(risk or 0)
    except (TypeError, ValueError):
        return None
    if not (_finite(stop, fill, risk) and stop > 0 and fill > 0 and risk > 0):
        return None
    return 1.0 + max(0.0, stop - fill) / risk


def stop_gap_multiple(rec: dict) -> Optional[float]:
    """A lot's stop loss multiple: the qty-weighted mean, over EVERY stop leg (each *_stop_partial leg booked by
    _book_partial, plus a stop-family final exit), of 1 + max(0, stop − fill) / entry risk per share (cold review M1,
    R3-1): the gap EXCESS of each stop that filled, so a fill at or above its stop counts 1.0 and a ratchet or relabelled
    fill can never count below 1. A leg whose prices are unknown is left out and the KNOWN legs decide (R4-2); a lot with
    a stop leg but none known returns None (the caller excludes it, never a guessed 1.0). The one exact fallback: a
    legacy single-leg protective stop at its entry stop with no prices recorded, where the gap is −R − 1, so the
    multiple is max(1, −R)."""
    raw_legs = rec.get("stop_legs")
    legs: dict = raw_legs if isinstance(raw_legs, dict) else {}
    lq, lmq = float(legs.get("qty") or 0), float(legs.get("mult_qty") or 0)
    if not (_finite(lq, lmq) and lq > 0):
        lq, lmq = 0.0, 0.0
    final_q, final_m = 0.0, None
    if rec.get("exit_reason") in _STOP_FAMILY:
        final_m = _gap_mult(rec.get("stop_px"), rec.get("exit_px"), _entry_risk_ps(rec))
        final_q = float(rec.get("qty") or 0) if final_m is not None else 0.0
    known_q = lq + final_q
    if known_q > 0:
        return (lmq + final_q * (final_m or 0.0)) / known_q
    if final_m is not None:
        return final_m                                    # a final leg with no qty recorded: its own multiple
    if (not legs and rec.get("exit_reason") == "protective_stop"
            and not (rec.get("ratchet") or {}).get("order_ids")):
        x = lot_r(rec)
        return max(1.0, -x) if x is not None and math.isfinite(x) else None
    return None


def _net_fill(rec: dict, oid: str, cum_qty: Any, cum_avg: Any) -> tuple[int, float]:
    """The part of an order's CUMULATIVE fill not yet booked by the ratchet (ratchet.booked_fills), priced
    incrementally: (cum qty × cum avg − booked qty × booked avg) / new qty. (0, 0.0) when nothing is new; price 0.0 when
    it cannot be derived (the caller books it as unknown, never fabricated)."""
    try:
        cq, ca = int(float(cum_qty or 0)), float(cum_avg or 0)
    except (TypeError, ValueError):
        return 0, 0.0
    b = ((rec.get("ratchet") or {}).get("booked_fills") or {}).get(oid) if oid else None
    if not b:
        return cq, ca
    bq = int(float((b.get("qty") if isinstance(b, dict) else b) or 0))
    ba = float((b.get("avg") if isinstance(b, dict) else ca) or 0)
    nq = cq - bq
    if nq <= 0:
        return 0, 0.0
    px = (cq * ca - bq * ba) / nq if ca > 0 and ba > 0 else 0.0
    return nq, (px if math.isfinite(px) and px > 0 else 0.0)


def _mark_booked(rec: dict, oid: str, cum_qty: Any, cum_avg: Any) -> None:
    rec.setdefault("ratchet", {}).setdefault("booked_fills", {})[oid] = {"qty": int(float(cum_qty or 0)),
                                                                         "avg": float(cum_avg or 0)}


def closed_lots(state: dict) -> list[dict]:
    """Every closed lot this tier booked: the durable archive plus closed records still in `positions` (legacy lots
    booked before the archive existed), de-duplicated by client order id."""
    out, seen = [], set()
    for rec in list(state.get("closed_lots") or []) + [p for p in (state.get("positions") or {}).values()
                                                        if isinstance(p, dict) and p.get("status") == "closed"]:
        key = rec.get("coid") or (rec.get("entry_date"), rec.get("entry_px"), rec.get("exit_date"))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def _archive_closed(state: dict, rec: Optional[dict]) -> None:
    """Append a closed lot to the durable archive (state["closed_lots"]) once, keyed by client order id, so its R is
    not lost when a later lot on the same symbol replaces its record in `positions`."""
    if not isinstance(rec, dict) or rec.get("status") != "closed":
        return
    arch = state.setdefault("closed_lots", [])
    key = rec.get("coid") or (rec.get("entry_date"), rec.get("entry_px"), rec.get("exit_date"))
    if any((a.get("coid") or (a.get("entry_date"), a.get("entry_px"), a.get("exit_date"))) == key for a in arch):
        return
    arch.append({k: rec.get(k) for k in ("coid", "entry_date", "exit_date", "exit_reason", "realized_pnl", "entry_px",
                                          "stop_px", "exit_px", "qty", "risk_usd", "partial_pnl", "partial_pnl_unknown",
                                          "entry_px_estimated", "init_risk_ps", "init_stop_px", "ratchet",
                                          "stop_legs")}
               | {"status": "closed"})


def kelly_risk(lots: list[dict], prior: Optional[dict]) -> dict:
    """Risk per lot r (fraction of equity) and the loss multiple, from this tier's closed lots once
    KELLY_MIN_SAMPLE_SIZE have a known R, else from the replay prior. r = SWING_BREAKOUT_KELLY_FRACTION × max(0,
    mean R − SE) / var R, never above SWING_BREAKOUT_MAX_RISK_PCT. Loss multiple = the mean stop_gap_multiple of lots
    that exited on a stop (gap-throughs cost more than 1R), never below 1. No live sample and no prior → r 0
    (the caller's one-share floor still trades the setup)."""
    rs = [x for x in (lot_r(rec) for rec in lots) if x is not None and math.isfinite(x)]
    # SWING-RATCHET-1105 (masked-loss seat, cold review M1): the loss multiple measures each stop fill's GAP EXCESS
    # (1 + max(0, stop − fill) / entry risk), for every stop-family exit — protective or ratchet, whatever the label —
    # so a profit-lock fill can never shrink it and up-size new lots; a gap through a raised stop counts in full.
    stop_ms, detail, excluded = [], [], []
    for rec in lots:
        legs = rec.get("stop_legs") if isinstance(rec.get("stop_legs"), dict) else {}
        if rec.get("exit_reason") not in _STOP_FAMILY and not legs:
            continue
        m = stop_gap_multiple(rec)
        if m is None or not math.isfinite(m):
            # a stop lot whose stop prices are unknown is left out of the loss multiple, never counted as 1.0 (R4-2)
            excluded.append(rec.get("coid"))
            logger.info("swing kelly: lot %s (%s) excluded from the loss multiple — no stop leg with known prices",
                        rec.get("coid"), rec.get("exit_reason"))
            continue
        stop_ms.append(m)
        detail.append({"coid": rec.get("coid"), "reason": rec.get("exit_reason"), "mult": round(m, 4)})
    out: dict[str, Any] = {"n_live": len(rs), "n_live_stopped": len(stop_ms), "stop_multiples": detail,
                           "stop_excluded": excluded}
    try:
        import statistics
        if len(rs) >= int(config.KELLY_MIN_SAMPLE_SIZE):
            n, mean, sd, src = len(rs), statistics.fmean(rs), statistics.stdev(rs), "fills"
            lm = statistics.fmean(stop_ms) if stop_ms else float((prior or {}).get("loss_mult_stopped") or 1.0)
        elif prior:
            n, mean, sd, src = int(prior["n"]), float(prior["mean_r"]), float(prior["sd_r"]), "replay_prior"
            lm = float(prior["loss_mult_stopped"])
        else:
            out.update(r=0.0, loss_mult=1.0, source="none")
            return out
        if not (n > 1 and _finite(mean, sd, lm) and sd > 0):
            out.update(r=0.0, loss_mult=1.0, source=f"{src} (unusable)")
            return out
        se = sd / math.sqrt(n)
        kelly = max(0.0, mean - se) / sd ** 2
        r = min(float(config.SWING_BREAKOUT_KELLY_FRACTION) * kelly, float(config.SWING_BREAKOUT_MAX_RISK_PCT))
        out.update(r=r, loss_mult=max(1.0, lm), source=src, n=n, mean_r=mean, sd_r=sd, se=se, kelly=kelly)
    except Exception as e:
        out.update(r=0.0, loss_mult=1.0, source=f"error {e!r}")
    return out


def _load_prior() -> Optional[dict]:
    try:
        d = json.loads(_PRIOR.read_text())
        if isinstance(d, dict) and all(k in d for k in ("n", "mean_r", "sd_r", "loss_mult_stopped")):
            return d
        logger.warning("swing_breakout: replay prior %s is missing fields", _PRIOR)
    except Exception as e:
        logger.warning("swing_breakout: replay prior unreadable (%s)", e)
    return None


def size_order(equity: float, price: float, stop_dist: float, r: float, loss_mult: float, k: float, k_ceiling: float,
               other_notional: float, tier_notional: float, protected_notional: float = 0.0) -> tuple[int, dict]:
    """Whole shares for one lot, sized by its own stop. Every bound is a floor; any bad input → 0 (skip).
    shares = r × (k / k_ceiling) × equity / (stop_dist × loss_mult), bounded by the per-lot loss at the stop <=
    SWING_BREAKOUT_MAX_RISK_PCT × equity and by the total overnight limb: (other + QHM/F6 + this tier + this lot) <=
    k × equity (Invariant #11 limb 2; limb 1 no longer binds this tier — SWING-OWNER-1025). `other_notional` = every
    non-tier position except the buy-and-hold tiers; `protected_notional` = QHM + F6."""
    try:
        if not _finite(equity, price, stop_dist, r, loss_mult, k, k_ceiling, other_notional, tier_notional,
                       protected_notional):
            return 0, {"why": "non-finite sizing input"}
        if equity <= 0 or price <= 0 or stop_dist <= 0 or k <= 0 or k_ceiling <= 0 or protected_notional < 0 \
                or r < 0 or loss_mult <= 0:
            return 0, {"why": "non-positive sizing input"}
        k_scalar = min(k / k_ceiling, 1.0)                    # tighten-only: K below its ceiling shrinks r
        risk_budget = r * k_scalar * equity
        q_risk = math.floor(risk_budget / (stop_dist * loss_mult))
        risk_cap = float(config.SWING_BREAKOUT_MAX_RISK_PCT) * equity
        q_cap = math.floor(risk_cap / stop_dist)
        room_total = k * equity - (other_notional + protected_notional + tier_notional)
        q_room = math.floor(room_total / price) if room_total > 0 else 0
        qty = max(0, min(q_risk, q_cap, q_room))
        floor_note = ""
        # A single share may risk up to SWING_BREAKOUT_MIN1_RISK_PCT of equity at its stop when the bounds buy zero
        # shares (CEO 2026-10-14; AMD 10/07 was refused: $63.30 stop distance vs a $50 cap). Never above it.
        one_risk_cap = max(float(config.SWING_BREAKOUT_MIN1_RISK_PCT) * equity, risk_cap)
        if qty == 0 and stop_dist <= one_risk_cap:
            # MIN-1-SHARE floor (BGGN 2026-09-29): one share when it is <= min(SWING_BREAKOUT_MIN1_MAX_PCT × equity,
            # the K-limb room) and its stop risk <= one_risk_cap. Never fewer trades for a high-priced name.
            one_cap = min(float(config.SWING_BREAKOUT_MIN1_MAX_PCT) * equity, room_total)
            if price <= one_cap:
                qty = 1
                floor_note = f", 1-share floor (≤ ${one_cap:.0f}, stop risk ${stop_dist:.0f} ≤ ${one_risk_cap:.0f})"
        info = {"r": r, "k": k, "k_scalar": k_scalar, "loss_mult": loss_mult, "stop_dist": stop_dist,
                "risk_budget": round(risk_budget, 2), "risk_cap": round(risk_cap, 2), "room_total": round(room_total, 2),
                "q_risk": q_risk, "q_cap": q_cap, "q_room": q_room,
                "why": (f"risk ${risk_budget:.2f} (r {r:.4f} × K {k:.2f}/{k_ceiling:.2f}) / (stop ${stop_dist:.2f} × "
                        f"loss {loss_mult:.2f}) → {q_risk} sh; 2% cap {q_cap} sh; K-limb room ${room_total:.0f} "
                        f"(tier ${tier_notional:.0f}, other ${other_notional:.0f}, QHM/F6 ${protected_notional:.0f}) → "
                        f"{q_room} sh → {qty} sh{floor_note}")}
        return int(qty), info
    except Exception as e:
        return 0, {"why": f"sizing error: {e!r}"}


def gap_resize(qty: int, price: float, p99_new: Optional[float], book: list, equity: float, long_value: float,
               maint: Optional[float]) -> tuple[int, dict]:
    """Gap check (SWING-OWNER-1025): the open Swing book plus the new lot, each × its own 99th-percentile one-day loss,
    must fit the margin buffer. A margin call needs equity − D < m × (L − D), so the largest survivable one-day loss is
    D = (E − m × L) / (1 − m). With the new lot of q shares (L = L0 + q·P, D = book loss + q·P·p):
        q <= ((E − m·L0) / (1 − m) − book loss) / (P × (p + m / (1 − m)))
    The lot is resized down to fit, NEVER below one share (never a skip — owner rule); every resize is logged.
    `book` = [(notional, p99 or None)]; a lot with an unknown p99 takes the largest known one; any unknown input →
    one share."""
    info: dict[str, Any] = {"qty_before": int(qty), "maint": maint, "p99_new": p99_new, "long_value": long_value}
    if qty <= 1:
        info["qty_after"] = int(qty)
        return int(qty), info
    known = [p for _, p in book if p is not None] + ([p99_new] if p99_new is not None else [])
    worst = max(known) if known else None
    try:
        if p99_new is None or worst is None or maint is None or not (0 < maint < 1) \
                or not _finite(price, equity, long_value, worst) or price <= 0:
            info.update(qty_after=1, note="gap inputs unknown → 1 share")
            return 1, info
        book_loss = sum(float(n) * (p if p is not None else worst) for n, p in book)
        buffer = (equity - maint * long_value) / (1 - maint)
        denom = price * (p99_new + maint / (1 - maint))
        q_max = math.floor((buffer - book_loss) / denom) if denom > 0 else 0
        new = max(1, min(int(qty), q_max))
        info.update(book_loss=round(book_loss, 2), buffer=round(buffer, 2), q_max=q_max, qty_after=new)
        return new, info
    except Exception as e:
        info.update(qty_after=1, note=f"gap check error {e!r} → 1 share")
        return 1, info


def _lot_notional(rec: dict) -> float:
    """Notional a lot holds or may hold: filled qty × entry price when known, else the requested qty × limit
    (an in-flight / unverified entry claims its full request — never 0). Non-finite or bad input → 0."""
    try:
        qty, px = float(rec.get("qty") or 0), float(rec.get("entry_px") or 0)
        if not (qty > 0 and px > 0):
            qty, px = float(rec.get("req_qty") or 0), float(rec.get("limit") or 0)
        v = qty * px
        return v if math.isfinite(v) and v > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _lot_risk(rec: dict) -> float:
    """A held lot's loss at its stop in $ (risk_usd recorded at entry, else the entry risk per share × qty — never a
    raised stop); 0 when unknown."""
    try:
        v = rec.get("risk_usd")
        if v is None:
            v = _entry_risk_ps(rec) * float(rec.get("qty") or 0)
        v = float(v)
        return v if math.isfinite(v) and v > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _protected_notional(positions: list) -> float:
    """Market value of the QHM + Forever-6 share of `positions`, from the ownership ledger's protected tiers
    (per symbol: |market_value| × min(protected qty, |broker qty|) / |broker qty|). FAIL-SAFE: an unreadable
    ledger or position returns 0.0 for it, so for a caller that still applies the 100% swing/day limb
    (execution/day_promotion.py) those shares stay in that stricter limb (fewer entries, never more). The Swing tier's
    own sizing counts QHM/F6 in the K limb either way (SWING-OWNER-1025), so the split does not change its room."""
    try:
        from execution.ownership_guard import load_ledger, protected_floor
        led = load_ledger()
    except Exception as e:
        logger.warning("swing_breakout: ownership ledger unreadable (%s) — QHM/F6 not excluded (fail-safe)", e)
        return 0.0
    total = 0.0
    for p in positions or []:
        try:
            qty = abs(float(getattr(p, "qty", 0) or 0))
            mv = abs(float(getattr(p, "market_value", 0.0) or 0.0))
            if qty <= 0 or not math.isfinite(mv):
                continue
            prot = min(max(float(protected_floor(led, getattr(p, "symbol", ""))), 0.0), qty)
            total += mv * prot / qty
        except Exception as e:
            logger.warning("swing_breakout: protected share of %s unreadable (%s) — counted as swing (fail-safe)",
                           getattr(p, "symbol", "?"), e)
    return total if math.isfinite(total) else 0.0


# ── state (atomic) ──────────────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        if _STATE.exists():
            d = json.loads(_STATE.read_text())
            if isinstance(d, dict):
                d.setdefault("positions", {})
                return d
            raise ValueError("state is not a dict")
    except Exception as e:
        # Unreadable state must NOT look like "no positions" (that would re-enter and orphan stops):
        # raise so the caller skips the cycle.
        raise RuntimeError(f"swing_breakout state unreadable: {e}") from e
    return {"positions": {}}


def _save_state(state: dict) -> bool:
    try:
        os.makedirs(_STATE.parent, exist_ok=True)
        tmp = _STATE.with_suffix(f".tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STATE)
        return True
    except Exception as e:
        logger.error("swing_breakout state write FAILED: %s", e)
        return False


_last_good_symbols: frozenset = frozenset()
_SESSION_CACHE: dict = {}          # SWING-RATCHET-1105: Alpaca calendar session opens per (start, end) date


def get_breakout_symbols_checked() -> tuple[set, bool]:
    """Return (confirmed holding symbols, state_readable).

    Unlike :func:`get_breakout_symbols`, this never substitutes the configured
    candidate pool for confirmed holdings.  It is for observability callers
    that must distinguish "owned" from "ownership could not be proved".
    """
    global _last_good_symbols
    try:
        syms = frozenset(s for s, p in _load_state().get("positions", {}).items() if p.get("status") in _HOLDING)
        _last_good_symbols = syms
        return set(syms), True
    except Exception as e:
        logger.critical(
            "swing_breakout: state unreadable (%s) — confirmed holdings unavailable",
            e,
        )
        return set(), False


def get_breakout_symbols() -> set:
    """Symbols this tier currently holds or may hold (for orphan/drift exclusion). On an unreadable state file
    returns the LAST GOOD set (never empty-on-error, which would let a restart adopt this tier's lots)."""
    syms, readable = get_breakout_symbols_checked()
    if readable:
        return syms
    # FAIL CLOSED (as QHM does): last good set, else the whole configured pool (over-protective — a restart
    # must never adopt this tier's lots as intraday orphans because its own state file is unreadable)
    fallback = set(_last_good_symbols) or set(_cfg("SWING_BREAKOUT_POOL", _DEFAULT_POOL) or _DEFAULT_POOL)
    logger.critical(
        "swing_breakout: excluding %d symbol(s) from orphan adoption",
        len(fallback),
    )
    return fallback


def _page(msg: str) -> None:
    logger.critical(msg)
    try:
        from alerts import send_slack
        send_slack("🚨 SWING-BREAKOUT ALERT\n" + msg)
    except Exception as e:  # pragma: no cover
        logger.error("swing_breakout: page send failed: %s", e)


def _page_throttled(rec: dict, msg: str) -> None:
    """Page for an unresolved lot at most every _PAGE_EVERY_S (the throttle timestamp lives in the lot record,
    which the caller saves)."""
    now = time.time()
    if now - float(rec.get("last_page_ts") or 0.0) >= _PAGE_EVERY_S:
        rec["last_page_ts"] = now
        _page(msg)
    else:
        logger.critical(msg)


def _log(event: str, symbol: str, **kw) -> None:
    try:
        import trade_logger
        change = kw.pop("change", CHANGE)
        trade_logger.log_event(event, symbol=symbol, data_source="alpaca_data", tier=TIER,
                               setup="c2_breakout_55d", family_id=FAMILY_ID,
                               hypothesis_version=HYPOTHESIS_VERSION, change=change, **kw)
    except Exception as e:
        logger.warning("[%s] swing_breakout trade_logger write failed: %s", symbol, e)


_TERMINAL = ("filled", "canceled", "cancelled", "rejected", "expired", "done_for_day")


def _is_terminal(status: Any) -> bool:
    """True for a final order status. 'partially_filled' is NOT terminal (it also ends in 'filled')."""
    st = str(status or "").lower().split(".")[-1]
    return st in _TERMINAL


# ── SWING-RATCHET-1105: profit-lock stop (pure functions, unit-tested) ─────────────────────────────
def _ord_status(o: Any) -> str:
    st = getattr(o, "status", "")
    return str(getattr(st, "value", st) or "").lower().split(".")[-1]


def _ord_field(o: Any, name: str) -> str:
    v = getattr(o, name, "")
    return str(getattr(v, "value", v) or "").lower()


def _is_sell_stop(o: Any) -> bool:
    typ = _ord_field(o, "order_type") or _ord_field(o, "type")
    return _ord_field(o, "side").endswith("sell") and "stop" in typ


def _validate_ratchet_params(d: Any) -> Optional[dict]:
    """Schema check of the replay-produced parameter record: version 1, finite trigger > 0, 0 <= lock < trigger, finite
    tick > 0. None on any failure (→ the last valid version, else no ratchet)."""
    if not isinstance(d, dict) or d.get("version") != 1:
        return None
    raw = [d.get("trigger_r"), d.get("lock_r"), d.get("tick_usd")]
    nums = [float(x) for x in raw if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if len(nums) != len(raw) or not _finite(*nums):
        return None
    trig, lock, tick = nums
    if not (trig > 0 and 0 <= lock < trig and tick > 0):
        return None
    return {"trigger_r": trig, "lock_r": lock, "tick_usd": tick, "variant": d.get("variant"),
            "produced": d.get("produced"), "trial_count": d.get("trial_count")}


def _ratchet_last_valid_path() -> Path:
    return _STATE.parent / "swing_ratchet_params.last_valid.json"


def load_ratchet_params() -> tuple[Optional[dict], str]:
    """(params, source). The replay file when valid (its copy is kept as the last valid version, written atomically);
    else the last valid version; else (None, reason) — no ratchet (today's behaviour), the caller pages."""
    why = "parameter file missing"
    try:
        raw = json.loads(_RATCHET_PARAMS.read_text())
        p = _validate_ratchet_params(raw)
        if p is not None:
            try:
                lv = _ratchet_last_valid_path()
                if not lv.exists() or json.loads(lv.read_text()) != raw:
                    os.makedirs(lv.parent, exist_ok=True)
                    tmp = lv.with_suffix(f".tmp{os.getpid()}")
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(raw, f, indent=1)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, lv)
            except Exception as e:
                logger.warning("swing ratchet: last-valid parameter copy not written (%s)", e)
            return p, "file"
        why = "parameter file invalid"
    except Exception as e:
        why = f"parameter file unreadable ({e})"
    try:
        p = _validate_ratchet_params(json.loads(_ratchet_last_valid_path().read_text()))
        if p is not None:
            return p, f"last_valid ({why})"
    except Exception as e:
        logger.warning("swing ratchet: last-valid parameters unreadable (%s)", e)
    return None, why


def tick_ceil(x: float, tick: float) -> float:
    """Round UP to the tick (a lock level never sits below entry + lock × R), to the cent."""
    return round(math.ceil(round(x / tick, 6)) * tick, 2)


def tick_floor(x: float, tick: float) -> float:
    """Round DOWN to the tick (a level set under the bid), to the cent."""
    return round(math.floor(round(x / tick, 6)) * tick, 2)


def reprotect_coid(lot_coid: Any, seq: Any) -> Optional[str]:
    """Deterministic re-protection client_order_id (design rev 9): IN-rp-<first 24 hex of sha256(lot coid)>-<seq>.
    Only [A-Za-z0-9-], keeps the tier's IN- tag. None (→ no order, paged) when the sequence is not a non-negative int,
    the lot coid is empty, or the id would exceed Alpaca's 48-character limit."""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return None
    if not isinstance(lot_coid, str) or not lot_coid:
        return None
    cid = f"IN-rp-{hashlib.sha256(lot_coid.encode('utf-8')).hexdigest()[:_RP_HASH_DIGITS]}-{seq}"
    return cid if len(cid) <= _ALPACA_CLIENT_ID_API_CHARS else None


def ratchet_armed(df, entry_date: str, entry_px: float, risk_ps: float, trigger_r: float,
                  tick: float) -> Optional[dict]:
    """Armed when a COMPLETED daily close on/after the entry date reached entry + trigger × R, that level rounded UP
    to the tick (cold review L1: 142.58 + 2 × 19.85 must arm on a 182.28 close despite float error). `df` holds
    completed bars only (fetch_closed_bars). An empty entry date never arms. None on unreadable input (→ retried,
    never armed blind)."""
    try:
        if df is None or not _finite(entry_px, risk_ps, trigger_r, tick) or risk_ps <= 0 or tick <= 0:
            return None
        trigger_px = tick_ceil(float(entry_px) + float(trigger_r) * float(risk_ps), tick)
        if not entry_date:
            return {"armed": False, "max_close": None, "trigger_px": trigger_px, "armed_session": None,
                    "last_session": None, "why": "no entry date"}
        rows = [(str(t)[:10], float(c)) for t, c in zip(df.index, df["close"].astype(float), strict=True) if str(t)[:10] >= entry_date]
        rows = [(d, c) for d, c in rows if math.isfinite(c)]
        if not rows:
            return {"armed": False, "max_close": None, "trigger_px": trigger_px, "armed_session": None,
                    "last_session": None}
        hit = next((d for d, c in rows if c >= trigger_px), None)
        return {"armed": hit is not None, "max_close": max(c for _, c in rows), "trigger_px": trigger_px,
                "armed_session": hit, "last_session": rows[-1][0]}
    except Exception:
        return None


def ratchet_target(entry_px: float, risk_ps: float, cur_stop: float, bid: float, lock_r: float, tick: float) -> dict:
    """target = max(current stop, min(lock_px, bid − one tick)), lock_px = entry + lock × R rounded UP to the tick; the
    level under the bid rounded DOWN. `raise_` only when the target is strictly above the current stop (never down)."""
    lock_px = tick_ceil(float(entry_px) + float(lock_r) * float(risk_ps), tick)
    under = tick_floor(float(bid) - tick, tick)
    cur = round(float(cur_stop), 2)
    target = round(max(cur, min(lock_px, under)), 2)
    return {"lock_px": lock_px, "under_bid_px": under, "target": target, "cur_stop": cur,
            "raise_": target > cur and target > 0}


def _stop_exit_label(rec: dict, order_id: str, partial: bool = False) -> str:
    """Exit label by PROVENANCE (rev 4 item 1): a fill of an order the ratchet placed or moved to is a ratchet stop;
    every other stop fill stays a protective stop. One helper for every booking site (rev 4 item 10b)."""
    rt = rec.get("ratchet") or {}
    ids = list(rt.get("order_ids") or []) + list(rt.get("chain_ids") or []) + [str(rt.get("new_order_id") or "")]
    base = "ratchet_stop" if order_id and order_id in ids else "protective_stop"
    return f"{base}_partial" if partial else base


class SwingBreakoutManager:
    """Owns the breakout tier's lots. All methods are called from run_cycle (single-threaded)."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    # ── data ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _bars(symbol: str):
        from data.fetcher import fetch_closed_bars
        return fetch_closed_bars(symbol, getattr(config, "TF_DAILY", "1Day"), num_bars=260).df

    @staticmethod
    def _today_open(symbol: str, today: str, n_closed: int) -> Optional[float]:
        """Today's session open from today's (still forming) daily bar — T1 Alpaca via fetch_bars, with the same
        request size fetch_closed_bars used for `n_closed` closed bars, so it is normally a cache hit. None when today's
        bar is absent or unreadable (the setup then waits)."""
        try:
            from data.fetcher import fetch_bars
            raw = fetch_bars(symbol, getattr(config, "TF_DAILY", "1Day"), num_bars=int(n_closed) + 1)
            if raw is None or raw.empty or str(raw.index[-1])[:10] != today:
                return None
            v = float(raw["open"].iloc[-1])
            return v if math.isfinite(v) and v > 0 else None
        except Exception as e:
            logger.warning("[%s] swing_breakout: today's open unreadable: %s", symbol, e)
            return None

    @staticmethod
    def _maint_ratio(acct: Any, positions: list) -> Optional[float]:
        """The account's live maintenance ratio m: the stricter of maintenance_margin / gross and / long value (as
        overnight_capacity.refresh derives it), else the ratio in the last overnight-K state; None when neither is
        readable (the gap check then sizes one share)."""
        try:
            mm = float(getattr(acct, "maintenance_margin", 0.0) or 0.0)
            mvs = [float(getattr(p, "market_value", 0.0) or 0.0) for p in positions or []]
            gross = sum(abs(v) for v in mvs)
            long_mv = sum(v for v in mvs if v > 0)
            if mm > 0 and gross > 0:
                m = mm / gross
                if long_mv > 0:
                    m = max(m, mm / long_mv)
                if 0 < m < 1:
                    return m
        except (TypeError, ValueError) as e:
            logger.warning("swing_breakout: maintenance ratio unreadable from the account (%s)", e)
        try:
            from execution import overnight_capacity as oc
            st = oc._load() or {}
            m = float((st.get("inputs") or {}).get("maint_ratio") or 0.0)
            return m if 0 < m < 1 else None
        except Exception as e:
            logger.warning("swing_breakout: maintenance ratio unreadable from the overnight-K state (%s)", e)
            return None

    # ── order / position reads (None = UNREADABLE, never "zero") ─────────────
    @staticmethod
    def _read_order(order_id: str) -> Optional[tuple[int, float, bool]]:
        """(filled_qty, avg_price, is_terminal) from one order read; None when the order is UNREADABLE — never
        read an unreadable order as "zero filled" (that would orphan a filled lot or mask a stop fill)."""
        from execution import broker
        if not order_id:
            return None
        try:
            o = broker.get_order(order_id)
            if o is None:
                return None
            return (int(float(getattr(o, "filled_qty", 0) or 0)), float(getattr(o, "filled_avg_price", 0) or 0),
                    _is_terminal(getattr(o, "status", "")))
        except Exception as e:
            logger.warning("swing_breakout: order %s unreadable: %s", order_id, e)
            return None

    def _final_fill(self, order_id: str) -> Optional[tuple[int, float]]:
        r = self._read_order(order_id)
        return None if r is None else (r[0], r[1])

    @staticmethod
    def _broker_qty(symbol: str) -> Optional[int]:
        """Long shares held at the broker for `symbol` (0 = no position); None when unreadable."""
        from execution import broker
        try:
            pos = broker.get_open_position(symbol)
            return 0 if pos is None else max(int(float(getattr(pos, "qty", 0) or 0)), 0)
        except Exception as e:
            logger.warning("[%s] swing_breakout: position unreadable: %s", symbol, e)
            return None

    def _await_fill(self, order_id: str) -> tuple[int, float]:
        """Poll until terminal or SWING_BREAKOUT_FILL_WAIT_S; keeps the HIGHEST fill seen (an unreadable read
        never lowers it). Always reads at least once."""
        deadline = time.monotonic() + float(_cfg("SWING_BREAKOUT_FILL_WAIT_S", 15.0))
        filled, px = 0, 0.0
        while True:
            r = self._read_order(order_id)
            if r is not None:
                if r[0] >= filled:
                    filled, px = r[0], (r[1] or px)
                if r[2]:
                    break
            if time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        return filled, px

    def _settle(self, order_id: str, wait_s: float = 3.0) -> Optional[tuple[int, float, bool]]:
        """Bring an order to a FINAL status before its fill count is trusted: read it; if still working, cancel it
        and poll (0.25 s) up to wait_s for a terminal status. A fill that lands while the cancel is pending is
        therefore counted. Returns the last read (terminal flag False = still working), or None if unreadable."""
        from execution import broker
        r = self._read_order(order_id)
        if r is not None and r[2]:
            return r
        try:
            broker.cancel_order(order_id)      # also when the first read failed: every caller wants it dead
        except Exception as e:
            logger.warning("swing_breakout: cancel of %s failed: %s", order_id, e)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
            r2 = self._read_order(order_id)
            if r2 is not None:
                r = r2
                if r2[2]:
                    break
        return r

    # ── entries ───────────────────────────────────────────────────────────────
    def run_entries(self, now: Optional[datetime] = None) -> list[str]:
        """Once per ET day, from 10:05 ET: evaluate the universe on the prior closed bar and enter."""
        if not enabled() or self.dry_run:
            return []
        now = now or datetime.now(ET)
        if now.hour * 60 + now.minute < _ENTRY_START_MIN:
            return []
        today = now.strftime("%Y-%m-%d")
        try:
            state = _load_state()
        except RuntimeError as e:
            _page(f"entries skipped — {e}")
            return []
        if state.get("last_entry_scan") == today:
            return []
        # latch FIRST so an exception mid-scan never re-fires entries later the same day
        state["last_entry_scan"] = today
        if not _save_state(state):
            _page("entries skipped — could not persist the daily latch (fail-closed)")
            return []
        from execution import broker
        from execution.quarterly_hold_manager import get_quarterly_hold_symbols
        entered: list[str] = []
        pool = list(_cfg("SWING_BREAKOUT_POOL", _DEFAULT_POOL) or _DEFAULT_POOL)
        bars = {}
        for s in pool:
            try:
                df = self._bars(s)
                if df is not None and not df.empty:
                    bars[s] = df
            except Exception as e:
                logger.warning("[%s] swing_breakout bars fetch failed: %s", s, e)
        if not bars:
            # a total data outage must not burn the day: un-latch so the next cycle retries
            state["last_entry_scan"] = None
            _save_state(state)
            logger.warning("swing_breakout: no daily bars for any pool symbol — entries retry next cycle")
            return []
        ranked = rank_universe(bars, len(bars))
        rank_of = {s: i + 1 for i, s in enumerate(ranked)}
        universe = ranked[: int(config.SWING_BREAKOUT_TOP_N)]
        _log("signal", "", note="breakout universe", universe=universe, pool_n=len(pool), bars_n=len(bars))
        try:
            positions = broker.get_open_positions() or []
            open_orders = broker.get_open_orders()
            acct = broker.get_account()
            equity = float(getattr(acct, "equity", 0.0) or 0.0)
            buying_power = float(getattr(acct, "buying_power", 0.0) or 0.0)
        except Exception as e:
            state["last_entry_scan"] = None      # transient read failure must not burn the day: retry next cycle
            _save_state(state)
            _page(f"entries skipped — live book unreadable (fail-closed, retry next cycle): {e!r}")
            return []
        if open_orders is None:
            state["last_entry_scan"] = None
            _save_state(state)
            _page("entries skipped — open orders unreadable (fail-closed, retry next cycle)")
            return []
        held_syms = {getattr(p, "symbol", None) for p in positions}
        order_syms = {getattr(o, "symbol", None) for o in open_orders}
        mine = {s for s, p in state["positions"].items() if p.get("status") in _HOLDING}
        added = 0.0
        try:
            qhm = set(get_quarterly_hold_symbols())
        except Exception as e:
            logger.warning("swing_breakout: QHM symbols unreadable (%s) — entries retry next cycle (fail-closed)", e)
            state["last_entry_scan"] = None
            _save_state(state)
            return []
        try:
            # QHM's configured picks, INCLUDING ones still pending entry: QHM may buy them later, and a co-held
            # symbol would collide on the wash-trade rule and on whole-symbol stop accounting
            from execution.quarterly_hold_manager import _configured_qhm_symbols
            qhm |= set(_configured_qhm_symbols())
        except Exception as e:
            logger.warning("swing_breakout: QHM configured picks unreadable: %s", e)
        # Active F6 names plus the explicit reserved names: a resting breakout sell stop would make Alpaca reject
        # F6's buy on the same symbol (wash-trade rule) for up to 30 sessions.  The union means a runtime/new F6
        # common-stock candidate is excluded immediately, while the reserved list retains retired ETF candidates
        # DRAM/EWY so narrowing F6 cannot silently expand Swing exposure (cold-2nd 2026-10-21).
        # Runtime configuration can replace either list.  Keep the retired ETF pair as an immutable floor so a
        # configuration override cannot silently release them into Swing (adversarial review 2026-10-21).
        qhm |= {"DRAM", "EWY"}
        qhm |= {
            str(x).strip().upper()
            for x in (_cfg("FOREVER6_UNIVERSE", []) or [])
            if str(x).strip()
        }
        qhm |= {
            str(x).strip().upper()
            for x in (_cfg("SWING_BREAKOUT_F6_RESERVED_SYMBOLS", []) or [])
            if str(x).strip()
        }
        # Pending setups (SWING-OWNER-1025): a breakout on the last closed bar of a top-N name creates a setup at its
        # breakout level (the prior 55-day closing high), kept across days until entered once or until a closed bar
        # closes below the prior 20-session low. The first level is kept (one entry per setup).
        setups = state.setdefault("setups", {})
        for s in universe:
            sig = breakout_signal(bars.get(s))
            if sig is not None and s not in mine and s not in setups:
                setups[s] = {"level": sig["prior_high"], "created": today, "rank": rank_of.get(s), "signal": sig}
                _log("signal", s, note="breakout setup", level=sig["prior_high"], rank=rank_of.get(s), signal=sig)
        for s in list(setups):
            df = bars.get(s)
            if s in mine:
                setups.pop(s, None)
                continue
            if df is None or df.empty:
                continue                                     # no bars today: the setup waits (never dropped blind)
            # any closed bar AFTER the setup's creation that closed below its prior 20-session low drops it — not only
            # the newest bar, so a session the scan did not run (halt, restart) is still seen (cold-2nd 2026-10-25)
            closes = df["close"].astype(float)
            lo20s = df["low"].astype(float).rolling(20).min().shift(1)
            # the setup is created during session `created` from the bar before it; that session's own bar and later
            # ones closed after it was created
            created = str((setups.get(s) or {}).get("created") or "")
            since = [i for i, t in enumerate(df.index) if str(t)[:10] >= created]
            broke = [i for i in since if math.isfinite(float(lo20s.iloc[i])) and float(closes.iloc[i]) < float(lo20s.iloc[i])]
            if broke:
                setups.pop(s, None)
                i = broke[0]
                _log("skip", s, reason="setup dropped: close below the prior 20-session low",
                     close=float(closes.iloc[i]), lo20=float(lo20s.iloc[i]), bar=str(df.index[i])[:10])
        _save_state(state)
        for s in sorted(setups, key=lambda x: rank_of.get(x, math.inf)):
            setup = setups.get(s) or {}
            rank = rank_of.get(s)
            if rank is None:
                _log("skip", s, reason="no daily bars today — setup waits", level=setup.get("level"))
                continue                                     # retried next session
            if len({x for x, p in state["positions"].items() if p.get("status") in _HOLDING}) >= int(config.SWING_BREAKOUT_MAX_SLOTS):
                _log("skip", s, reason="all slots full", rank=rank)
                continue
            df = bars[s]
            sig = breakout_signal(df) or dict(setup.get("signal") or {})
            a = atr14(df)
            if a is None:
                _log("skip", s, reason="ATR unavailable", rank=rank)
                continue
            sig = dict(sig, atr=a)
            if s in mine or s in held_syms or s in order_syms or s in qhm:
                _log("skip", s, reason="symbol already held/ordered/QHM", signal=sig, rank=rank)
                continue
            level = float(setup.get("level") or 0.0)
            op = self._today_open(s, today, len(df))
            if op is None:
                _log("skip", s, reason="today's open unavailable — setup waits", level=level, rank=rank)
                continue
            if not (level > 0 and op >= level):
                _log("skip", s, reason="open below the breakout level — setup waits", open=op, level=level, rank=rank)
                continue
            try:
                if self._enter(s, sig, state, positions, equity, buying_power, today, added,
                               bars=bars, rank=rank, level=level, today_open=op, acct=acct):
                    entered.append(s)
            except Exception as e:
                _page(f"[{s}] entry raised (symbol aborted): {e!r}")
                _log("skip", s, reason=f"entry raised: {e!r}", rank=rank, level=level)
            r_ = state["positions"].get(s) or {}
            if r_.get("status") in _HOLDING or (r_.get("entry_date") == today and r_.get("change") == CHANGE
                                                and r_.get("status") != "unfilled"):
                # one entry per setup: a lot that filled and was then closed the same scan (stop not placeable →
                # flattened, or price already through the stop) also used the setup (cold-2nd 2026-10-25)
                setups.pop(s, None)
                _save_state(state)
            # Count EVERY lot this scan left holding or possibly holding (open, in-flight, unverified, or an
            # unconfirmed flatten) — not only clean entries — so the next symbol never sizes into room a
            # late fill already uses (board risk seat 2026-10-02).
            r = state["positions"].get(s) or {}
            if r.get("status") in _HOLDING:
                added += _lot_notional(r)
        return entered

    def _enter(self, symbol: str, sig: dict, state: dict, positions: list, equity: float,
               buying_power: float, today: str, added_notional: float = 0.0, *, bars: Optional[dict] = None,
               rank: Optional[int] = None, level: float = 0.0, today_open: Optional[float] = None,
               acct: Any = None) -> bool:
        from data.alpaca_data import get_latest_quote
        from execution import broker
        from execution.overnight_capacity import ceiling, overnight_k
        bars = bars or {}
        ctx = {"rank": rank, "level": level, "open": today_open, "signal": sig}
        q = get_latest_quote(symbol)
        if not isinstance(q, dict) or not (0 < float(q.get("bid") or 0) <= float(q.get("ask") or 0)):
            _log("skip", symbol, reason="quote unreadable", **ctx)
            return False
        ask = float(q["ask"])
        if level > 0 and ask < level:
            # never entered below the breakout level (owner rule): the setup waits for the next session
            _log("skip", symbol, reason="price below the breakout level — setup waits", ask=ask, **ctx)
            return False
        # SWING-LEVEL-LAST-1103: the IEX ask alone can sit far above where the stock trades (BRK.B 2026-10-27: a
        # $4.28-wide IEX quote put the ask over the $546.97 level while the fill came at $544.60). The last trade,
        # fresh by the same age rule the exits use, must also be at or above the level.
        last = self._last_trade(symbol)
        if not math.isfinite(last):
            _log("skip", symbol, reason="last trade unavailable or stale — setup waits", ask=ask, **ctx)
            return False
        if level > 0 and last < level:
            _log("skip", symbol, reason="last trade below the breakout level — setup waits", ask=ask, last=last,
                 **ctx)
            return False
        mine_notional = 0.0
        other_notional = 0.0
        long_value = 0.0
        tier_syms = {s for s, p in state["positions"].items() if p.get("status") in _HOLDING}
        pos_syms = {getattr(p, "symbol", None) for p in positions}
        book: list[tuple[float, Optional[float]]] = []         # (notional, own p99 one-day loss) per Swing lot
        for p in positions:
            mv_signed = float(getattr(p, "market_value", 0.0) or 0.0)
            mv = abs(mv_signed)
            long_value += max(mv_signed, 0.0)
            if getattr(p, "symbol", None) in tier_syms:
                mine_notional += mv
                book.append((mv, p99_down(bars.get(getattr(p, "symbol", None)))))
            else:
                other_notional += mv
        # held/pending lots NOT yet visible as a broker position (in-flight or unverified entries from earlier
        # cycles) still claim their requested notional; lots added in THIS scan arrive via added_notional
        for s_, r_ in state["positions"].items():
            if r_.get("status") in _HOLDING and s_ not in pos_syms and s_ != symbol and r_.get("entry_date") != today:
                mine_notional += _lot_notional(r_)
                long_value += _lot_notional(r_)
                book.append((_lot_notional(r_), p99_down(bars.get(s_))))
        # buy-and-hold (QHM/F6) share of the other notional — counted in the K limb
        protected_notional = min(_protected_notional([p for p in positions
                                                      if getattr(p, "symbol", None) not in tier_syms]), other_notional)
        other_notional -= protected_notional
        # positions is the start-of-scan snapshot: lots entered earlier in THIS scan are added explicitly
        for s_, r_ in state["positions"].items():
            if r_.get("status") in _HOLDING and r_.get("entry_date") == today and s_ != symbol and s_ not in pos_syms:
                book.append((_lot_notional(r_), p99_down(bars.get(s_))))
        mine_notional += added_notional
        long_value += added_notional
        buying_power -= added_notional
        sd = stop_distance(bars.get(symbol), ask, float(sig.get("atr") or 0.0))
        if sd is None:
            _log("skip", symbol, reason="stop distance unavailable", ask=ask, **ctx)
            return False
        stop_dist, atr_floor_pct = sd
        kel = kelly_risk(closed_lots(state), _load_prior())
        k, k_ceiling = overnight_k(), ceiling()
        qty, info = size_order(equity, ask, stop_dist, float(kel["r"]), float(kel["loss_mult"]), k, k_ceiling,
                               other_notional, mine_notional, protected_notional)
        p99_new = p99_down(bars.get(symbol))
        qty, gap = gap_resize(qty, ask, p99_new, book, equity, long_value, self._maint_ratio(acct, positions))
        if gap.get("qty_after") != gap.get("qty_before"):
            logger.info("[%s] breakout gap check resized %s → %s sh (%s)", symbol, gap.get("qty_before"), qty, gap)
        agg_risk = sum(_lot_risk(r_) for s_, r_ in state["positions"].items()
                       if r_.get("status") in _HOLDING and s_ != symbol) + qty * stop_dist
        sizing = dict(info, kelly=kel, atr=sig.get("atr"), atr_floor_pct=atr_floor_pct, gap=gap,
                      aggregate_risk=round(agg_risk, 2), aggregate_risk_pct=(round(agg_risk / equity, 4) if equity > 0 else None),
                      ask=ask, last=last, qty=int(qty))
        why = sizing
        if qty < 1:
            _log("skip", symbol, reason="no size", sizing=why, **ctx)
            return False
        limit = round(ask * (1 + float(_cfg("SWING_BREAKOUT_LIMIT_SLIP_PCT", 0.002))), 2)
        if buying_power < qty * limit * 1.10:
            _log("skip", symbol, reason="buying power", bp=buying_power, sizing=why, **ctx)
            return False
        stop_px = round(ask - stop_dist, 2)
        # FOREIGN-STOP DEFER (Option A, design record cohold_buys_vs_foreign_stops_2026-10-11.md): Alpaca rejects this
        # buy while another tier's sell stop rests on the symbol (wash rule) -> defer this scan, logged.
        from execution.foreign_stop_guard import clear_deferral, foreign_opposite_stop, note_deferral
        _fstop = foreign_opposite_stop(symbol, "buy", TIER)
        if _fstop is not None:
            note_deferral(symbol, TIER, _fstop)
            _log("skip", symbol, reason="foreign stop resting (wash rule) — deferred", sizing=why, signal=sig)
            return False
        clear_deferral(symbol, TIER)
        from execution.tier_capital_allocator import live_admit, live_bind, live_order_id, live_release
        capital = live_admit("swing", TIER, symbol, "buy", int(qty), limit,
                             stop_price=stop_px, risk_price_bound=limit)
        if not capital.approved:
            _log("skip", symbol, reason=f"allocator: {capital.reason}", sizing=why, signal=sig)
            return False
        coid = live_order_id(capital.lease) or ""
        if not coid:
            # allocator OFF (no lease): mint our own IN- tagged client id and STORE it, so an ambiguous submit is
            # recoverable by client id and its working remainder can be cancelled (board execution seat 2026-10-02)
            import uuid
            from execution.ownership_guard import make_coid
            coid = make_coid(TIER, symbol, "buy", int(time.time() * 1000), uuid.uuid4().hex[:8])
        rec: dict[str, Any] = {"status": "submitting", "entry_date": today, "coid": coid, "qty": 0, "signal": sig,
               "sizing": why, "limit": limit, "req_qty": int(qty), "ts": datetime.now(PT).isoformat(),
               "stop_dist": stop_dist, "level": level, "rank": rank, "change": CHANGE}
        _archive_closed(state, state["positions"].get(symbol))   # keep the old lot's R before this record replaces it
        state["positions"][symbol] = rec
        if not _save_state(state):
            live_release(capital.lease, "state_write_failed")
            state["positions"].pop(symbol, None)
            _page(f"[{symbol}] entry ABORTED — could not persist the pre-submit record")
            return False
        order = broker.submit_limit_order(symbol, qty, "buy", limit, tier=TIER, client_order_id=coid or None)
        if order is None:
            order = live_release(capital.lease, "submit_none")   # recovers an ambiguous submit by client id
        else:
            live_bind(capital.lease, order)
        if order is None or not getattr(order, "id", None):
            # ambiguous: the order may be live at Alpaca. Kept as a HELD status; _reconcile resolves it by client id.
            rec["status"] = "submit_unknown"
            _page_throttled(rec, f"[{symbol}] breakout entry submit AMBIGUOUS — resolving by client id each cycle")
            _save_state(state)
            return False
        rec["entry_order_id"] = str(getattr(order, "id", ""))
        rec["status"] = "submitted"
        _save_state(state)
        self._await_fill(rec["entry_order_id"])
        self._resolve_entry(symbol, rec, state)
        if rec.get("status") != "open":
            return False
        for _ in range(2):                                   # 3 stop attempts in total (one inside _resolve_entry)
            if rec.get("status") != "open" or not rec.get("unprotected"):
                break                                        # protected, or already closed by _reprotect
            time.sleep(1.0)
            self._reprotect(symbol, rec, state, int(rec.get("qty") or 0))
        if rec.get("status") != "open":
            return False
        if rec.get("unprotected") and self._broker_qty(symbol) == 0:
            # the position read still lags the fill: selling now could act on a stale read — leave the lot open +
            # unprotected; _reconcile re-places the stop next cycle (paged by _reprotect)
            return False
        if rec.get("unprotected"):
            _page(f"[{symbol}] breakout stop NOT placed after 3 tries — flattening the {rec.get('qty')}-sh lot")
            self._close_lot(symbol, rec, state, "stop_unconfirmed_flatten")
            return False
        logger.info("[%s] breakout ENTERED %s sh @ %.2f, GTC stop %.2f", symbol, rec.get("qty"),
                    float(rec.get("entry_px") or 0), float(rec.get("stop_px") or 0))
        return True

    def _resolve_entry(self, symbol: str, rec: dict, state: dict) -> None:
        """Drive an in-flight entry (submitting / submitted / submit_unknown / entry_unverified — also after a
        restart) to a final answer: settle the order (cancel any working remainder, count late fills), then
        either promote the filled qty to "open" with a stop, or mark it unfilled. Unresolvable → stays held
        (excluded from orphan adoption) and paged."""
        from execution import broker
        oid = str(rec.get("entry_order_id") or "")
        if not oid and rec.get("coid"):
            try:
                o = broker.get_order_by_client_order_id(rec["coid"])
            except Exception as e:
                logger.warning("[%s] breakout client-id lookup failed: %s", symbol, e)
                o = None
            if o is not None and getattr(o, "id", None):
                oid = str(getattr(o, "id", ""))
                rec["entry_order_id"] = oid
                _save_state(state)
        r = self._settle(oid) if oid else None
        if r is None:
            # no readable order: decide from the broker. This symbol had NO position or order when entry began,
            # so shares held now are this lot's. Held 0 with no open order for it → the entry never happened.
            held = self._broker_qty(symbol)
            try:
                orders = broker.get_open_orders(symbol)
            except Exception as e:
                logger.warning("[%s] breakout open-orders read failed: %s", symbol, e)
                orders = None
            if held == 0 and orders is not None and not any(
                    str(getattr(o, "side", "")).lower().endswith("buy") for o in orders):
                rec["status"] = "unfilled"
                _save_state(state)
                _log("skip", symbol, reason="entry not placed/filled (no order, no position)", signal=rec.get("signal"))
                return
            buy_working = orders is None or any(str(getattr(o, "side", "")).lower().endswith("buy") for o in orders)
            if held is None or held == 0 or buy_working:
                # never promote while a buy may still be filling (later fills would sit outside the lot's stop)
                rec["status"] = "entry_unverified"
                _page_throttled(rec, f"[{symbol}] breakout entry UNVERIFIED — order unreadable, position "
                                     f"{'unreadable' if held is None else ('flat' if held == 0 else f'{held} sh')}"
                                     f"{' with a working/unknown buy' if buy_working else ''}; retrying each cycle")
                _save_state(state)
                return
            # at most what this entry asked for: any excess belongs to another tier that bought meanwhile
            filled, px, terminal = min(held, int(rec.get("req_qty") or held)), 0.0, True
        else:
            filled, px, terminal = r
        if not terminal:
            rec["status"] = "submitted"          # cancel still pending: re-settled next cycle
            _page_throttled(rec, f"[{symbol}] breakout entry order {oid} still working after cancel — re-checking each cycle")
            _save_state(state)
            return
        if filled < 1:
            rec["status"] = "unfilled"
            _save_state(state)
            _log("skip", symbol, reason="entry not filled", limit=rec.get("limit"), sizing=rec.get("sizing"),
                 signal=rec.get("signal"))
            return
        sig = rec.get("signal") or {}
        if px <= 0:
            px = float(rec.get("limit") or sig.get("close") or 0.0)   # estimate for the stop anchor only
            rec["entry_px_estimated"] = True
        # the stop distance the lot was SIZED with (SWING-OWNER-1025); a lot recorded before it keeps STOP_ATR × ATR
        stop_dist: Any = rec.get("stop_dist")
        if not (_finite(stop_dist) and float(stop_dist) > 0):
            stop_dist = float(config.SWING_BREAKOUT_STOP_ATR) * float(sig.get("atr") or 0.0)
        stop_px = round(px - float(stop_dist), 2)
        rec.update(status="open", qty=int(filled), entry_px=px, stop_px=stop_px, stop_order_id="",
                   opened_ts=time.time(), risk_usd=round(int(filled) * (px - stop_px), 2),
                   init_stop_px=stop_px, init_risk_ps=round(px - stop_px, 6))   # immutable per lot (RATCHET rev 4)
        _save_state(state)
        _log("entry", symbol, price=px, size=int(filled), direction="long", stop=stop_px,
             limit=rec.get("limit"), sizing=rec.get("sizing"), signal=sig, trade_id=rec.get("coid"),
             entry_px_estimated=bool(rec.get("entry_px_estimated")))
        self._reprotect(symbol, rec, state, int(filled))

    # ── protection ────────────────────────────────────────────────────────────
    def _reprotect(self, symbol: str, rec: dict, state: dict, want_qty: int, depth: int = 0) -> bool:
        """Place a fresh GTC stop for min(want_qty, the broker's long qty) — never on shares the broker does not
        show (a sell stop with nothing behind it could open a short). Broker shows 0 → the lot is booked gone.
        Unreadable broker / failed stop → the lot stays "open" flagged unprotected, paged (throttled) and retried
        every cycle. A price already through the stop → the lot is closed instead (depth guard: once)."""
        from execution import broker
        if rec.get("status") == "closed":
            return False
        if self._ratchet_owns(rec, state, symbol=symbol):
            # SWING-RATCHET-1105 rev 7 item 3: the ratchet's own reconcile owns this lot's stop until broker truth is
            # proven — placing a second stop here could duplicate it (only within the session it started, R3-3)
            logger.warning("[%s] breakout re-protect deferred: ratchet %s owns the stop", symbol,
                           (rec.get("ratchet") or {}).get("status"))
            return False
        held = self._broker_qty(symbol)
        if held is None:
            rec.update(unprotected=True, stop_order_id="")
            _page_throttled(rec, f"[{symbol}] breakout lot UNPROTECTED — position unreadable, stop not placed; retrying each cycle")
            _save_state(state)
            return False
        qty = min(max(int(want_qty), 0), held)
        if qty < 1:
            if not self._confirmed_gone(symbol, rec):
                # a zero read that is not corroborated (position-endpoint lag right after a fill) is UNREADABLE,
                # not "gone": keep the lot open + unprotected and retry (cold-2nd 2026-10-02)
                rec.update(unprotected=True, stop_order_id="")
                _page_throttled(rec, f"[{symbol}] breakout lot: broker shows 0 sh but not confirmed flat — "
                                     f"stop not placed; retrying each cycle")
                _save_state(state)
                return False
            self._book_gone(symbol, rec, state)
            return False
        if qty < int(want_qty):
            # shares this lot expected are not at the broker and were never booked: flag the P&L as unknown
            rec["partial_pnl_unknown"] = True
            _page(f"[{symbol}] breakout lot expected {want_qty} sh but the broker holds {held} — protecting {qty}; "
                  f"{int(want_qty) - qty} sh unaccounted (P&L marked unknown); check fills")
        ls = self._lot_stops(symbol, rec)
        stop_qty = qty                               # the lot keeps `qty`; the stop may cover only part of it (R7-2)
        if ls is not None:
            # R10-2: ONE own stop per lot. An own stop of this lot already live (other than the dead id) is adopted —
            # _topup_cover sizes it — never a second stop next to it
            dead = str(rec.get("stop_order_id") or "")
            own_live = [x for x in ls["own"] if str(getattr(x, "id", "")) != dead]
            if len(own_live) == 1:
                px0 = float(getattr(own_live[0], "stop_price", 0) or 0)
                rec.update(status="open", unprotected=False, stop_order_id=str(getattr(own_live[0], "id", "")),
                           stop_px=round(px0, 2) if px0 > 0 else rec.get("stop_px"))
                _save_state(state)
                logger.info("[%s] breakout: this lot's live stop %s adopted (no second stop)", symbol,
                            rec["stop_order_id"])
                return True
            if len(own_live) > 1 or ls["own_trans"]:
                rec.update(unprotected=True)
                _page_throttled(rec, f"CRITICAL [{symbol}] lot {rec.get('coid')}: {len(own_live)} own stop(s) live / "
                                     f"{len(ls['own_trans'])} in transition — no further stop placed; review")
                _save_state(state)
                return False
        if ls is not None and ls["owner"]:
            covers, uncovered = self._owner_cover(symbol, ls["owner"], held, qty, rec)
            if covers or uncovered < 1:
                # R6-1: an owner (untagged) stop already holds these shares — protection, never re-protected over
                self._owner_protects(symbol, rec, state, ls["owner"], held)
                return True
            self._clear_owner_snapshot(rec)          # R10-4: full cover has ended
            stop_qty = min(qty, uncovered)           # R7-2: this lot's own stop for what the owner stop leaves
            # R8-1a: a PARTIAL owner cover is never recorded as this lot's protection (owner_stop_ids); it is tracked
            # apart, only so a real broker reduction behind it can be booked under the R8-1b cap
            rec["owner_partial_ids"] = sorted(str(getattr(o, "id", "")) for o in ls["owner"])
        elif ls is not None:
            self._clear_owner_snapshot(rec)          # R10-4: no owner stop at all — no cover snapshot
        stop_qty = max(min(stop_qty, qty), 0)        # never above min(lot qty, broker qty)
        stop_px = float(rec.get("stop_px") or 0.0)
        o: Any = None
        if stop_px > 0:
            try:
                o = broker.submit_gtc_stop_order(symbol, stop_qty, "sell", stop_px, tier=TIER,
                                                 allow_cancel_blocking=False)
            except Exception as e:
                logger.warning("[%s] breakout stop submit raised: %s", symbol, e)
                o = None
        held_sentinel = getattr(broker, "PROTECTION_ALREADY_HELD", None)
        if o is not None and held_sentinel is not None and o is held_sentinel:
            # a stable live reducing order already holds these shares (typically this lot's own stop that a read
            # could not see) — protected; nothing cancelled. The next cycle re-checks.
            rec.update(status="open", qty=qty, unprotected=False,
                       stop_order_id=self._find_resting_stop(symbol, stop_qty))
            _save_state(state)
            logger.info("[%s] breakout stop not placed: shares already held by a live reducing order (%s)",
                        symbol, rec["stop_order_id"] or "id not found")
            return True
        if o is not None and getattr(o, "id", None):
            rec.update(status="open", qty=qty, unprotected=False, stop_order_id=str(getattr(o, "id", "")))
            rt = rec.get("ratchet") or {}
            if rt.get("ratchet_stop_px") is not None and round(stop_px, 2) == round(float(rt["ratchet_stop_px"]), 2):
                # re-placed at the ratchet's own level: a fill is still a ratchet stop (provenance, masked-loss seat)
                rt.setdefault("order_ids", []).append(rec["stop_order_id"])
            _save_state(state)
            logger.info("[%s] breakout lot protected: %d of %d sh, GTC stop %.2f", symbol, stop_qty, qty, stop_px)
            return True
        unknown_sentinel = getattr(broker, "PROTECTION_UNKNOWN", None)
        if o is not None and unknown_sentinel is not None and o is unknown_sentinel:
            # the order book is unreadable: protection status UNKNOWN (not proven missing) — never close on it
            rec.update(status="open", qty=qty, unprotected=True, stop_order_id="")
            _page_throttled(rec, f"[{symbol}] breakout lot protection UNKNOWN (order book unreadable) — re-checking each cycle")
            _save_state(state)
            return False
        # failure (None / no id / no stop price)
        if depth == 0 and self._market_open() and self._price_through_stop(symbol, stop_px):
            # regular hours only: after hours a market close only queues for the open (and would be cancelled)
            _page_throttled(rec, f"[{symbol}] breakout stop could not be placed and the price is already through "
                                 f"${stop_px:.2f} — closing the lot")
            rec.update(status="open", qty=qty, unprotected=True, stop_order_id="")
            _save_state(state)
            self._close_lot(symbol, rec, state, "stop_breached_unprotected", _depth=1)
            return False
        rec.update(status="open", qty=qty, unprotected=True, stop_order_id="")
        _page_throttled(rec, f"[{symbol}] breakout lot of {qty} sh is UNPROTECTED — GTC stop placement failed; retrying each cycle")
        _save_state(state)
        return False

    def _confirmed_gone(self, symbol: str, rec: dict) -> bool:
        """True only when a FLAT broker position is corroborated: the lot is older than
        SWING_BREAKOUT_FLAT_GRACE_S (a just-filled lot's position read can lag) AND two more reads ~1 s and ~2 s
        apart both show 0. Any non-zero or unreadable read → False (the caller treats the lot as still held)."""
        opened = float(rec.get("opened_ts") or 0.0)
        if opened and time.time() - opened < float(_cfg("SWING_BREAKOUT_FLAT_GRACE_S", 300)):
            return False
        for wait in (1.0, 2.0):
            time.sleep(wait)
            if self._broker_qty(symbol) != 0:
                return False
        return True

    @staticmethod
    def _find_resting_stop(symbol: str, qty: int) -> str:
        """Id of a live SELL stop for exactly `qty` shares of `symbol` with this tier's owner tag, else ""."""
        from execution import broker
        try:
            for o in broker.get_open_orders(symbol) or []:
                if (str(getattr(o, "side", "")).lower().endswith("sell")
                        and "stop" in str(getattr(o, "order_type", getattr(o, "type", ""))).lower()
                        and int(float(getattr(o, "qty", 0) or 0)) == int(qty)
                        and str(getattr(o, "client_order_id", "") or "").startswith("IN-")):
                    return str(getattr(o, "id", "") or "")
        except Exception as e:
            logger.warning("[%s] breakout resting-stop lookup failed: %s", symbol, e)
        return ""

    @staticmethod
    def _market_open() -> bool:
        """Alpaca clock is_open; False when unreadable (then no market close is attempted — the lot stays
        flagged unprotected, paged and retried)."""
        try:
            from execution import broker
            return bool(broker.get_clock().get("is_open"))
        except Exception as e:
            logger.warning("swing_breakout: clock read failed (%s) — treating the market as closed", e)
            return False

    @classmethod
    def _last_trade(cls, symbol: str) -> float:
        return cls._last_trade_and_age(symbol)[0]

    @staticmethod
    def _last_trade_and_age(symbol: str) -> tuple[float, float]:
        """(last trade or NaN, the tier's price-age bound in seconds, NaN when not reached). The last trade price (T1 Alpaca Data), or NaN when unavailable or older than SWING_LAST_TRADE_MAX_AGE_S.
        Price-movement decisions (stop breached, trend break) use the last trade, never the IEX bid/ask, which swings
        wildly (CEO 2026-10-09; 10/07-08 Day-tier logs: MU's IEX spread went $3.50 -> $37.86 two minutes apart). A
        stale print (board Harris+Taleb: a thin name's last IEX print can be hours old) counts as unavailable, so the
        caller retries next cycle instead of deciding — and latching the day — on an old price."""
        try:
            from datetime import timezone
            from data.alpaca_data import get_latest_trade_with_time
            r = get_latest_trade_with_time(symbol)
            if r is None:
                return float("nan"), float("nan")
            px, ts = float(r[0]), r[1]
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            max_age = float(_cfg("SWING_LAST_TRADE_MAX_AGE_S", 180))  # PROV:swing-last-trade-2026-10-09
            if not (-5.0 <= age <= max_age):
                logger.warning("[%s] breakout last trade %.2f is %.0fs old (> %.0fs) — treated as unavailable",
                               symbol, px, age, max_age)
                return float("nan"), max_age
            return (px if math.isfinite(px) and px > 0 else float("nan")), max_age
        except Exception as e:
            logger.warning("[%s] breakout last-trade read failed: %s", symbol, e)
            return float("nan"), float("nan")

    @classmethod
    def _price_through_stop(cls, symbol: str, stop_px: float) -> bool:
        last = cls._last_trade(symbol)
        return stop_px > 0 and math.isfinite(last) and last <= stop_px

    # ── exits ─────────────────────────────────────────────────────────────────
    def run_exit_check(self, now: Optional[datetime] = None) -> list[str]:
        """Every cycle: reconcile every held lot. Near the real close: trend-break / max-hold exits, latched per
        lot per day only once DECIDED (a deferred/unconfirmed exit retries on the next cycle).
        Runs even when SWING_BREAKOUT_ENABLED is False: the flag stops NEW entries only, and lots already held
        keep their managed exits (a no-op when the state file holds no lots)."""
        if self.dry_run:
            return []
        try:
            state = _load_state()
        except RuntimeError as e:
            _page(f"exit check skipped — {e}")
            return []
        self._reconcile(state)
        now = now or datetime.now(ET)          # call-time clock by default (the hook runs mid-cycle)
        self._ratchet(state, now)              # SWING-RATCHET-1105: after reconcile, before the near-close return
        if not self._near_close(now):
            return []
        today = now.strftime("%Y-%m-%d")
        closed: list[str] = []
        for s, rec in list(state["positions"].items()):
            if rec.get("status") != "open" or rec.get("exit_checked") == today:
                continue
            if self._ratchet_owns(rec, state, now=now, symbol=s):
                # an unconfirmed ratchet replace from THIS session owns the stop; an older one is released and never
                # defers the trend / max-hold exits (R3-3, R4-1b)
                logger.warning("[%s] breakout exit check deferred: ratchet %s unconfirmed", s,
                               rec["ratchet"].get("status"))
                continue
            try:
                df = self._bars(s)
                live = self._last_trade(s)          # last trade, not the bid/ask midpoint (CEO 2026-10-09)
                lo20 = prior_low(df, 20)
                held = sessions_held(df, str(rec.get("entry_date")))
                why = exit_reason(live, lo20, held, int(_cfg("SWING_BREAKOUT_MAX_HOLD", 30)))
                if why is None:
                    if math.isfinite(live) and lo20 is not None and held is not None:
                        rec["exit_checked"] = today      # decided: hold (unknown inputs retry next cycle)
                        _save_state(state)
                    continue
                if self._close_lot(s, rec, state, why, live=live, lo20=lo20, held=held):
                    closed.append(s)
            except Exception as e:
                _page(f"[{s}] exit check raised (lot kept, GTC stop still rests): {e!r}")
        return closed

    @staticmethod
    def _near_close(now: datetime) -> bool:
        """Within SWING_BREAKOUT_EXIT_MINUTES of the REAL close (Alpaca clock next_close — half-day aware);
        falls back to 15:50 ET when the clock is unreadable."""
        mins = float(_cfg("SWING_BREAKOUT_EXIT_MINUTES", 10))
        try:
            from execution import broker
            nc = broker.get_clock().get("next_close")
            if nc is not None:
                left = (nc - now).total_seconds() / 60.0
                return 0 < left <= mins
        except Exception as e:
            logger.warning("swing_breakout: clock read failed (%s) — 15:50 ET fallback", e)
        return 16 * 60 - mins <= now.hour * 60 + now.minute < 16 * 60

    def _close_lot(self, symbol: str, rec: dict, state: dict, reason: str, _depth: int = 0, **ctx) -> bool:
        """Cancel this tier's stop (confirmed), book any stop fill, then close ONLY this tier's remaining qty and
        settle the close order to a final status. Unconfirmed → book what sold, re-protect what the broker still
        holds. Never a whole-symbol close; never a stop on shares that are not held."""
        from execution import broker
        if rec.get("status") == "closed":
            return True                          # already closed and booked — never sell or book it again
        stop_id = str(rec.get("stop_order_id") or "")
        if stop_id and not broker.cancel_stop_confirmed(symbol, stop_id):
            _page(f"[{symbol}] breakout exit ({reason}) deferred — stop cancel not confirmed; GTC stop still protects the lot")
            return False
        if stop_id:
            stop_id = self._resolve_stop_id(stop_id)   # a stop moved in place leaves its old id "replaced"
            final = self._final_fill(stop_id)
            if final is None:
                # the stop is confirmed cancelled but its fill is unreadable: never sell blind (a fired stop plus
                # a close would short the name). _reconcile resolves it next cycle.
                rec["status"] = "exit_unverified"
                _page_throttled(rec, f"[{symbol}] breakout exit ({reason}) HALTED — stop cancelled but its fill is "
                                     f"unreadable; the lot may be UNPROTECTED — resolving each cycle")
                _save_state(state)
                return False
            filled, px = _net_fill(rec, stop_id, *final)   # never re-book what the ratchet booked (R3-2)
            qty = int(rec.get("qty") or 0)
            if filled >= qty > 0 and px > 0:   # the stop fired during the cancel
                _mark_booked(rec, stop_id, *final)        # every owner dedupes on this (R4-1a)
                self._book_exit(symbol, rec, state, self._exit_label(rec, stop_id), px, filled)
                return True
            if 0 < filled < qty:
                _mark_booked(rec, stop_id, *final)
                self._book_partial(symbol, rec, state, px, filled, self._exit_label(rec, stop_id, partial=True),
                                   clear_key="stop_order_id")
            rec["stop_order_id"] = ""           # cancelled and fully accounted
            _save_state(state)
        qty = int(rec.get("qty") or 0)
        if qty < 1:
            rec["status"] = "closed"
            _save_state(state)
            return True
        order = broker.partial_close_position(symbol, qty, tier=TIER, _return_order=True)
        # True = the broker found no position to close; False/None = submit failed. Neither carries an order:
        # _reprotect below checks the broker's actual long qty before any stop is placed.
        oid = str(getattr(order, "id", "") or "") if order not in (None, False, True) else ""
        filled, px = 0, 0.0
        if oid:
            rec["close_order_id"] = oid
            rec["exit_reason_pending"] = reason      # lets a restart book this close by its real fill
            _save_state(state)
            filled, px = self._await_fill(oid)
            if not (filled >= qty and px > 0):
                r = self._settle(oid)
                if r is None or not r[2]:
                    rec["status"] = "exit_unverified"
                    _page_throttled(rec, f"[{symbol}] breakout close ({reason}) order {oid} unreadable/still working — "
                                         f"resolving each cycle")
                    _save_state(state)
                    return False
                filled, px = max(filled, r[0]), (r[1] or px)
            if filled >= qty and px > 0:
                _mark_booked(rec, oid, filled, px)
                self._book_exit(symbol, rec, state, reason, px, filled, **ctx)
                return True
            if 0 < filled < qty and px > 0:
                _mark_booked(rec, oid, filled, px)
                self._book_partial(symbol, rec, state, px, filled, f"{reason}_partial", clear_key="close_order_id")
            rec["close_order_id"] = ""
            _save_state(state)
        _page_throttled(rec, f"[{symbol}] breakout close ({reason}) UNCONFIRMED ({filled}/{qty}) — re-protecting the remainder")
        self._reprotect(symbol, rec, state, int(rec.get("qty") or 0), depth=_depth + 1)
        return False

    def _book_partial(self, symbol: str, rec: dict, state: dict, px: float, filled: int, reason: str,
                      clear_key: str = "") -> None:
        """Book a PARTIAL sale of this lot: its P&L is added to the lot's running realized P&L (so the final
        exit never books only the remainder) and the lot's qty shrinks by what sold. `clear_key` (the order-id
        field that produced the fill) is cleared in the SAME save, so a crash can never re-book the leg."""
        lot_q = int(rec.get("qty") or 0)
        if int(filled) > lot_q:
            _page(f"CRITICAL [{symbol}] lot {rec.get('coid')}: partial fill of {filled} sh exceeds the lot's {lot_q} — "
                  f"only {lot_q} booked (R10-3)")
            filled = lot_q
        entry = float(rec.get("entry_px") or 0.0)
        # an ESTIMATED entry price never produces a P&L number (never fabricate — booked unknown instead)
        pnl = (round((px - entry) * filled, 2) if entry > 0 and px > 0 and not rec.get("entry_px_estimated")
               else None)
        if pnl is not None:
            rec["partial_pnl"] = round(float(rec.get("partial_pnl") or 0.0) + pnl, 2)
        else:
            rec["partial_pnl_unknown"] = True
        if reason.removesuffix("_partial") in _STOP_FAMILY:     # every stop-family leg, *_unprotected_partial too
            # SWING-RATCHET-1105 R3-1: every stop leg's gap excess (the stop level at that moment vs this leg's fill),
            # qty-weighted, so a deep gap on an early leg is never lost when a later leg fills at its stop
            legs = rec.setdefault("stop_legs", {"qty": 0, "mult_qty": 0.0})
            m = _gap_mult(rec.get("stop_px"), px, _entry_risk_ps(rec))
            if m is None:
                legs["unknown"] = True
            else:
                legs["qty"] = int(legs.get("qty") or 0) + int(filled)
                legs["mult_qty"] = round(float(legs.get("mult_qty") or 0.0) + int(filled) * m, 6)
        rec["qty"] = max(int(rec.get("qty") or 0) - int(filled), 0)
        if clear_key:
            rec[clear_key] = ""
        _save_state(state)
        _log("partial_exit", symbol, price=px, size=int(filled), exit_reason=reason, realized_pnl=pnl,
             trade_id=rec.get("coid"), entry_px_estimated=bool(rec.get("entry_px_estimated")))

    def _book_exit(self, symbol: str, rec: dict, state: dict, reason: str, px: float, qty: int, **ctx) -> None:
        """Final exit. realized_pnl = this fill's P&L + every partial booked earlier (None if any part is unknown).
        R10-3: never more shares than the lot holds — any excess is paged CRITICAL and never booked to the lot."""
        lot_q = int(rec.get("qty") or 0)
        if lot_q > 0 and int(qty) > lot_q:
            _page(f"CRITICAL [{symbol}] lot {rec.get('coid')}: exit fill of {qty} sh exceeds the lot's {lot_q} — only "
                  f"{lot_q} booked to the lot; check the other {int(qty) - lot_q}")
            qty = lot_q
        entry = float(rec.get("entry_px") or 0.0)
        leg = (round((px - entry) * qty, 2) if entry > 0 and px > 0 and not rec.get("entry_px_estimated")
               else None)
        pnl = (None if leg is None or rec.get("partial_pnl_unknown")
               else round(leg + float(rec.get("partial_pnl") or 0.0), 2))
        rec.update(status="closed", exit_px=px, exit_reason=reason, exit_date=datetime.now(ET).strftime("%Y-%m-%d"),
                   realized_pnl=pnl, stop_order_id="", close_order_id="", unprotected=False)
        _archive_closed(state, rec)
        _save_state(state)
        # board Taleb (SWING-OWNER-1025): the fill vs the stop level, so a gap-through shows as the real loss
        stop_level = float(rec.get("stop_px") or 0.0)
        slip = round(px - stop_level, 4) if stop_level > 0 and px > 0 else None
        _log("exit", symbol, price=px, size=int(qty), exit_reason=reason, realized_pnl=pnl,
             entry_px_estimated=bool(rec.get("entry_px_estimated")), stop_level=stop_level or None,
             slippage_vs_stop=slip, r_multiple=lot_r(rec),
             trade_id=rec.get("coid"), **{k: v for k, v in ctx.items()
                                             if v is not None and not (isinstance(v, float) and not math.isfinite(v))})
        logger.info("[%s] breakout EXIT %s %d sh @ %.2f (P&L %s)", symbol, reason, qty, px, pnl)

    def _book_gone(self, symbol: str, rec: dict, state: dict) -> None:
        """The broker shows no position for this lot. Cancel any resting stop first (a sell stop on a flat
        symbol could open a short); if that stop turns out to have filled, book it as the protective stop."""
        from execution import broker
        stop_id = str(rec.get("stop_order_id") or "")
        if stop_id:
            if not broker.cancel_stop_confirmed(symbol, stop_id):
                _page_throttled(rec, f"[{symbol}] breakout lot gone at the broker but its stop {stop_id} could not be "
                                     f"confirmed cancelled — a resting sell stop on a flat symbol; retrying each cycle")
                _save_state(state)
                return
            qty = int(rec.get("qty") or 0)
            # the stop actually in force (a moved stop's old id reads "replaced"), then the ratchet's own new order
            for fid in dict.fromkeys([self._resolve_stop_id(stop_id), stop_id,
                                      str((rec.get("ratchet") or {}).get("new_order_id") or "")]):
                raw = self._final_fill(fid) if fid else None
                final = _net_fill(rec, fid, *raw) if raw is not None else None
                if raw is not None and final is not None and final[0] >= qty > 0 and final[1] > 0:
                    _mark_booked(rec, fid, *raw)                  # every owner dedupes on this (R4-1a)
                    self._book_exit(symbol, rec, state, self._exit_label(rec, fid), final[1], final[0])
                    return
        if rec.get("owner_stop_ids") or rec.get("owner_partial_ids"):
            # R7-1: the owner's stop protecting this lot may be what sold it — book its fill, never "unknown"
            got = self._book_owner_fills(symbol, rec, state)
            if got is None or got or rec.get("status") == "closed":
                return                               # booked, or an owner fill unreadable: stays unverified, paged
        close_id = str(rec.get("close_order_id") or "")
        if close_id:
            # a restart between our close submit and its booking: book the REAL close fill, not "unknown"
            # (board execution seat 2026-10-02)
            r = self._settle(close_id)
            if r is not None and r[2] and r[0] > 0 and r[1] > 0:
                reason = str(rec.get("exit_reason_pending") or "close")
                nq, npx = _net_fill(rec, close_id, r[0], r[1])
                if nq > 0 and npx > 0:
                    _mark_booked(rec, close_id, r[0], r[1])
                    if nq >= int(rec.get("qty") or 0):
                        self._book_exit(symbol, rec, state, reason, npx, nq)
                        return
                    self._book_partial(symbol, rec, state, npx, nq, f"{reason}_partial", clear_key="close_order_id")
        _page(f"[{symbol}] breakout lot of {rec.get('qty')} sh is GONE at the broker without a stop fill — "
              f"booked as an unverified external close (P&L unknown); check fills")
        rec.update(status="closed", exit_reason="external_close_unverified", realized_pnl=None, stop_order_id="",
                   close_order_id="", unprotected=False, exit_date=datetime.now(ET).strftime("%Y-%m-%d"))
        _save_state(state)
        # price 0.0 + price_unknown: trade_logger float()s the price, so None would drop this event (Rule D)
        _log("exit", symbol, price=0.0, price_unknown=True, size=int(rec.get("qty") or 0), exit_reason="external_close_unverified",
             realized_pnl=None, trade_id=rec.get("coid"), entry_px_estimated=bool(rec.get("entry_px_estimated")))

    # ── reconcile (every cycle) ───────────────────────────────────────────────
    def _reconcile(self, state: dict) -> None:
        """Bring every held lot to a known state: resolve in-flight entries and unverified exits; for open lots
        book a filled stop, book a partial fill, RE-PROTECT a lot whose stop is gone (cancelled/expired/rejected
        by anyone, never placed, lost to a restart) and book a lot missing at the broker."""
        for s, rec in list(state["positions"].items()):
            try:
                st = rec.get("status")
                if st in _ENTRY_PENDING:
                    self._resolve_entry(s, rec, state)
                    continue
                if st == "exit_unverified":
                    self._resolve_exit_unverified(s, rec, state)
                    continue
                if st != "open":
                    continue
                if self._ratchet_owns(rec, state, symbol=s):
                    continue                       # the ratchet's reconcile owns this lot's stop and its fills
                if rec.get("owner_stop_ids") or rec.get("owner_partial_ids"):
                    # R7-1 / R8-4: an owner stop's fill is booked to the lot only for lot shares that actually left
                    # (R8-1b); an unreadable owner order books nothing (paged) and protection still runs below
                    got = self._book_owner_fills(s, rec, state)
                    if got or rec.get("status") == "closed":
                        continue
                qty = int(rec.get("qty") or 0)
                stop_id = str(rec.get("stop_order_id") or "")
                if not stop_id or rec.get("unprotected"):
                    self._reprotect(s, rec, state, qty)
                    continue
                # R5-1: read the stop actually in force — a stop moved in place leaves its old id "replaced" (filled 0,
                # never terminal), which would hide the new order's fills and leave the shares without a stop
                stop_id, so = self._resolve_lot_stop(s, rec, state)
                if so is not None and _ord_status(so) == "replaced":
                    self._unresolved_chain(s, rec, state, qty)
                    continue
                r = self._order_fill(so) if so is not None else self._read_order(stop_id)
                if r is None:
                    continue                       # unreadable this cycle: no action (stop presumed resting)
                cum_filled, cum_px, terminal = r
                filled, px = _net_fill(rec, stop_id, cum_filled, cum_px)   # never re-book a ratchet-booked fill (R3-2)
                if filled >= qty > 0 and px > 0:
                    _mark_booked(rec, stop_id, cum_filled, cum_px)   # every owner dedupes on this (R4-1a)
                    # R10-3: never more than the lot (an excess is paged by _book_exit)
                    self._book_exit(s, rec, state, self._exit_label(rec, stop_id), px, filled)
                    continue
                if terminal:
                    # the stop ended without covering the lot (cancelled by another path, expired, rejected)
                    if 0 < filled < qty and px > 0:
                        _mark_booked(rec, stop_id, cum_filled, cum_px)
                        self._book_partial(s, rec, state, px, filled, self._exit_label(rec, stop_id, partial=True),
                                           clear_key="stop_order_id")
                    rec["stop_order_id"] = ""
                    _page_throttled(rec, f"[{s}] breakout stop {stop_id} ENDED with {filled}/{qty} filled — re-protecting")
                    self._reprotect(s, rec, state, int(rec.get("qty") or 0))
                    continue
                if so is not None:
                    self._topup_cover(s, rec, state, so)      # R9-1: a part-size own stop re-checks its cover
                if 0 < filled < qty and not rec.get("partial_stop_paged"):
                    # a resting stop that partly filled still covers the rest; flag once for review
                    rec["partial_stop_paged"] = True
                    _save_state(state)
                    _page(f"[{s}] breakout stop PARTIALLY filled ({filled}/{qty}) — remainder still stopped; review")
                if self._broker_qty(s) == 0 and self._confirmed_gone(s, rec):
                    self._book_gone(s, rec, state)
            except Exception as e:
                logger.warning("[%s] breakout reconcile failed (no action this cycle): %s", s, e)

    def _resolve_exit_unverified(self, symbol: str, rec: dict, state: dict) -> None:
        """An exit whose stop fill or close fill could not be read: once BOTH are readable and final, book what
        sold (full or partial), then re-protect whatever the broker still holds and return the lot to "open"."""
        qty = int(rec.get("qty") or 0)
        for key, label in (("stop_order_id", "protective_stop"), ("close_order_id", "close")):
            oid = str(rec.get(key) or "")
            if not oid:
                continue
            if key == "stop_order_id":
                oid = self._resolve_stop_id(oid)         # follow a moved stop to the order in force
                label = self._exit_label(rec, oid)
            r = self._settle(oid)
            if r is None or not r[2]:
                return                                   # still unreadable / working: retry next cycle
            filled, px = _net_fill(rec, oid, r[0], r[1])
            qty = int(rec.get("qty") or 0)
            if filled >= qty > 0 and px > 0:
                _mark_booked(rec, oid, r[0], r[1])           # every owner dedupes on this (R4-1a)
                self._book_exit(symbol, rec, state, label, px, filled)
                return
            if 0 < filled < qty and px > 0:
                _mark_booked(rec, oid, r[0], r[1])
                self._book_partial(symbol, rec, state, px, filled, f"{label}_partial", clear_key=key)
            rec[key] = ""
            _save_state(state)
        if self._reprotect(symbol, rec, state, int(rec.get("qty") or 0), depth=1):
            _page(f"[{symbol}] breakout lot re-protected ({rec.get('qty')} sh, GTC stop {rec.get('stop_px')}) after an unverified exit")

    # ── SWING-RATCHET-1105: profit-lock stop (regular hours, every cycle) ───────
    @staticmethod
    def _resolve_stop_id(order_id: str) -> str:
        """The id of the order in force at the end of `order_id`'s replaced_by chain; `order_id` itself when the chain
        cannot be read (the caller then reads that id, as before this change)."""
        from execution import broker
        fn = getattr(broker, "resolve_live_order", None)
        if not order_id or fn is None:
            return order_id
        try:
            o, oid = fn(order_id)
            return str(oid or order_id) if o is not None else order_id
        except Exception as e:
            logger.warning("swing_breakout: replaced-chain read for %s failed: %s", order_id, e)
            return order_id

    def _resolve_lot_stop(self, s: str, rec: dict, state: Optional[dict]) -> tuple[str, Any]:
        """(id, order) of the lot's stop actually in force: its replaced_by chain followed to the end (R5-1). A moved
        id is persisted to stop_order_id together with that order's stop level (broker truth). (state id, None) when
        the chain cannot be read or the broker module has no chain reader — the caller then reads the id as before."""
        from execution import broker
        sid = str(rec.get("stop_order_id") or "")
        fn = getattr(broker, "resolve_live_order", None)
        if not sid or fn is None:
            return sid, None
        try:
            o, oid = fn(sid)
        except Exception as e:
            logger.warning("[%s] breakout stop chain read for %s failed: %s", s, sid, e)
            return sid, None
        if o is None:
            return sid, None
        oid = str(oid or sid)
        if oid != sid:
            rec["stop_order_id"] = oid
            px = float(getattr(o, "stop_price", 0) or 0)
            if _finite(px) and px > 0:
                rec["stop_px"] = round(px, 2)
            if state is not None:
                _save_state(state)
            logger.info("[%s] breakout stop %s was moved in place — now reading %s (%s)", s, sid, oid, _ord_status(o))
        return oid, o

    @staticmethod
    def _order_fill(o: Any) -> Optional[tuple[int, float, bool]]:
        """(filled_qty, avg_price, is_terminal) from an order already read; None when it is unparseable."""
        try:
            return (int(float(getattr(o, "filled_qty", 0) or 0)), float(getattr(o, "filled_avg_price", 0) or 0),
                    _is_terminal(getattr(o, "status", "")))
        except (TypeError, ValueError):
            return None

    def _unresolved_chain(self, s: str, rec: dict, state: dict, qty: int) -> None:
        """The stop chain ends at a "replaced" order whose successor cannot be read (R5-1). Never left silently
        unprotected: shares held and no live own stop → re-protect now (allow_cancel_blocking=False inside _reprotect);
        exactly one live own stop for the qty → adopted; anything else → paged, re-checked next cycle."""
        held = self._broker_qty(s)
        ls = self._lot_stops(s, rec)
        if held is None or ls is None:
            _page_throttled(rec, f"[{s}] breakout stop chain unreadable and the position / orders too — re-checking")
            _save_state(state)
            return
        if held == 0:
            if self._confirmed_gone(s, rec):
                self._book_gone(s, rec, state)
            return
        want = self._tier_held(s, rec, held)
        trans = ls["own_trans"] + ls["owner_trans"]
        if trans:
            _page_throttled(rec, f"[{s}] breakout stop chain unreadable; a sell stop is {_ord_status(trans[0])} — waiting")
            _save_state(state)
            return
        own = ls["own"]
        if own:
            # R8-3: this lot's own stop may be the partial-cover size (what the owner stop leaves uncovered)
            ok_qty = {want}
            if ls["owner"]:
                ok_qty.add(self._owner_cover(s, ls["owner"], held, want, rec)[1])
            if len(own) == 1 and int(float(getattr(own[0], "qty", 0) or 0)) in ok_qty:
                px = float(getattr(own[0], "stop_price", 0) or 0)
                rec.update(stop_order_id=str(getattr(own[0], "id", "")), unprotected=False,
                           stop_px=round(px, 2) if px > 0 else rec.get("stop_px"))
                _save_state(state)
                return
            _page_throttled(rec, f"[{s}] breakout stop chain unreadable; {len(own)} own sell stop(s) not matching "
                                 f"{want} sh — review")
            _save_state(state)
            return
        if ls["owner"] and self._owner_cover(s, ls["owner"], held, want, rec)[0]:
            self._owner_protects(s, rec, state, ls["owner"], held)
            return
        lot_q = int(rec.get("qty") or 0)
        maybe = [o for o in ls["other"] if int(float(getattr(o, "qty", 0) or 0)) == lot_q]
        if maybe:
            # R6-4: with the chain unresolved, an IN- stop for exactly the lot's qty may be this lot's own successor —
            # never place a second stop over it
            _page_throttled(rec, f"[{s}] lot {rec.get('coid')}: stop chain unreadable; IN- stop "
                                 f"{getattr(maybe[0], 'id', '?')} for {lot_q} sh may be this lot's — no second stop placed")
            _save_state(state)
            return
        # an owner stop that covers only the owner's own shares leaves this lot's shares to this lot's stop (R7-2):
        # _reprotect sizes it to the uncovered qty
        _page_throttled(rec, f"[{s}] lot {rec.get('coid')}: stop chain unreadable and NO live stop for {want} sh — "
                             f"re-protecting now")
        rec.update(stop_order_id="", unprotected=True)
        _save_state(state)
        self._reprotect(s, rec, state, want, depth=1)  # depth 1: never a market close from an unresolved chain (R6-3)

    @staticmethod
    def _rt_status(rt: dict, status: str, reason: str = "") -> None:
        """Move the lot's ratchet state; any transition resets the per-(lot, status) page dedupe (rev 8 item 3)."""
        if rt.get("status") != status:
            rt.pop("page_key", None)
            rt.pop("page_session", None)
        rt["status"] = status
        if reason:
            rt["reason"] = reason

    @staticmethod
    def _rt_page(rec: dict, key: str, msg: str, today: str) -> None:
        """Page once per (lot, key) per session; repeats inside the session go to the log only."""
        rt = rec.setdefault("ratchet", {})
        if rt.get("page_key") == key and rt.get("page_session") == today:
            logger.critical(msg)
            return
        rt["page_key"], rt["page_session"] = key, today
        _page(msg)

    @staticmethod
    def _rt_log(symbol: str, rec: dict, action: str, reason: str, **kw) -> None:
        """Decision record (rev 2 item 8, rev 9 item 3): every input of a ratchet decision to trade_events.jsonl."""
        rt = rec.get("ratchet") or {}
        price = kw.pop("price", None)
        _log("stop_ratchet", symbol, price=float(price) if _finite(price) else 0.0, size=int(rec.get("qty") or 0),
             change=RATCHET_CHANGE, action=action, reason=reason, trade_id=rec.get("coid"),
             entry_px=rec.get("entry_px"), init_stop_px=rec.get("init_stop_px"), init_risk_ps=rec.get("init_risk_ps"),
             state_stop_px=rec.get("stop_px"), stop_order_id=rec.get("stop_order_id"),
             ratchet_status=rt.get("status"), armed_session=rt.get("armed_session"), armed_close=rt.get("armed_close"),
             trigger_px=rt.get("trigger_px"), target_px=rt.get("target_px"), lock_px=rt.get("lock_px"),
             old_order_id=rt.get("old_order_id"), old_stop_px=rt.get("old_stop_px"),
             new_order_id=rt.get("new_order_id"), reprotect_seq=rt.get("reprotect_seq"),
             reprotect_coid=rt.get("reprotect_coid"), reprotect_px=rt.get("reprotect_px"), **kw)

    def _fresh_bid(self, symbol: str) -> tuple[Optional[float], dict]:
        """(bid or None, quote facts). Valid only if positive, finite, not crossed, and its own timestamp is within the
        price-age bound the Swing tier already uses for its last trade (no new number)."""
        from data.alpaca_data import get_latest_quote
        facts: dict[str, Any] = {}
        try:
            q = get_latest_quote(symbol)
            if not isinstance(q, dict):
                return None, {"quote": "unreadable"}
            bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
            facts.update(bid=bid, ask=ask, quote_ts=q.get("ts"))
            if not (_finite(bid, ask) and 0 < bid <= ask):
                return None, dict(facts, quote="crossed or non-positive")
            _px, max_age = self._last_trade_and_age(symbol)
            from datetime import timezone
            # Alpaca stamps nanoseconds ("...:00.123456789Z"); Python 3.10 parses at most 6 fractional digits
            raw_ts = re.sub(r"(\.\d{6})\d+", r"\1", str(q.get("ts") or "")).replace("Z", "+00:00")
            ts = datetime.fromisoformat(raw_ts)
            if ts.tzinfo is None:
                return None, dict(facts, quote="timestamp without zone")
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            facts.update(quote_age_s=round(age, 1), max_age_s=max_age)
            if not (_finite(age, max_age) and abs(age) <= max_age):
                return None, dict(facts, quote="stale")
            return bid, facts
        except Exception as e:
            return None, dict(facts, quote=f"unreadable ({e!r})")

    @staticmethod
    def _entry_event(symbol: str, coid: str) -> Optional[tuple[float, float]]:
        """(entry price, entry stop) from this lot's own entry event in trade_events.jsonl; None when absent."""
        if not coid:
            return None
        found = None
        try:
            with open(_TRADE_EVENTS, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if coid not in line or '"entry"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("event") == "entry" and ev.get("trade_id") == coid and ev.get("symbol") == symbol \
                            and _finite(ev.get("price"), ev.get("stop")):
                        found = (float(ev["price"]), float(ev["stop"]))
        except OSError as e:
            logger.warning("[%s] swing ratchet: trade events unreadable (%s)", symbol, e)
            return None
        return found

    def _init_risk(self, symbol: str, rec: dict, state: dict, today: str) -> Optional[float]:
        """The lot's immutable entry risk per share. Written at the fill for new lots; an older lot's is rebuilt from
        its entry event (price and stop only, never the current qty) and must agree with state; else None (no ratchet,
        paged once per session) — rev 4 item 2, rev 5 item 3."""
        v = rec.get("init_risk_ps")
        if v is not None and _finite(v) and float(v) > 0:
            return float(v)
        rps = _risk_usd_ps(rec)                      # entry-time $ risk / the unchanged entry qty (cold review M2)
        if rps is not None:
            rec["init_risk_ps"] = round(rps, 6)
            _save_state(state)
            self._rt_log(symbol, rec, "init_risk", "rebuilt from risk_usd / entry qty", price=rps)
            return float(rec["init_risk_ps"])
        rt = rec.setdefault("ratchet", {"status": "idle"})
        if rt.get("risk_unknown") == today:
            return None                              # one rebuild attempt per session (the event log is large)
        rt["risk_unknown"] = today
        ev = self._entry_event(symbol, str(rec.get("coid") or ""))
        if ev is None:
            self._rt_page(rec, "no_entry_record", f"[{symbol}] swing ratchet: entry risk unknown (no entry record for "
                                                   f"{rec.get('coid')}) — no profit-lock for this lot", today)
            _save_state(state)
            return None
        price, stop = ev
        ids = (rec.get("ratchet") or {}).get("order_ids") or []
        stop_state = rec.get("init_stop_px") if rec.get("init_stop_px") is not None else (None if ids else rec.get("stop_px"))
        if round(price, 2) != round(float(rec.get("entry_px") or 0), 2) or \
                (stop_state is not None and round(stop, 2) != round(float(stop_state), 2)) or not price > stop:
            self._rt_page(rec, "entry_record_disagrees",
                          f"[{symbol}] swing ratchet: entry record (price {price:.2f}, stop {stop:.2f}) disagrees with "
                          f"state (entry {rec.get('entry_px')}, stop {stop_state}) — no profit-lock until resolved", today)
            _save_state(state)
            return None
        rec.update(init_risk_ps=round(price - stop, 6), init_stop_px=round(stop, 2))
        rt.pop("risk_unknown", None)
        _save_state(state)
        self._rt_log(symbol, rec, "init_risk", "rebuilt from the entry event", price=stop)
        return float(rec["init_risk_ps"])

    def _ratchet(self, state: dict, now: datetime) -> None:
        """Regular hours only (Alpaca clock is_open). Missing / invalid parameters (no last valid version) → no ratchet,
        paged once per session."""
        if not any(isinstance(r, dict) and r.get("status") == "open" for r in state.get("positions", {}).values()):
            return
        if not self._market_open():
            return
        today = now.strftime("%Y-%m-%d")
        params, src = load_ratchet_params()
        if params is None:
            if state.get("ratchet_params_paged") != today:
                state["ratchet_params_paged"] = today
                _save_state(state)
                _page(f"swing ratchet OFF this session — {src}; Swing stops keep their current levels")
            return
        for s, rec in list(state["positions"].items()):
            if not isinstance(rec, dict) or rec.get("status") != "open":
                continue
            try:
                self._ratchet_lot(s, rec, state, params, src, today, now)
            except Exception as e:
                _page_throttled(rec, f"[{s}] swing ratchet raised (stop unchanged): {e!r}")
                _save_state(state)

    def _ratchet_lot(self, s: str, rec: dict, state: dict, params: dict, src: str, today: str, now: datetime) -> None:
        from execution import broker
        rt = rec.get("ratchet")
        if not isinstance(rt, dict):
            rt = rec["ratchet"] = {"status": "idle"}
        status = rt.get("status") or "idle"
        if status in ("pending", "reprotect_pending") and not self._ratchet_owns(rec, state, now=now, symbol=s):
            status = rt.get("status") or "idle"     # released as stale (R4-1b): never reconciled by the ratchet again
        if status == "pending":
            self._ratchet_reconcile(s, rec, state, rt, params, today)
            return
        if status == "reprotect_pending":
            self._ratchet_reprotect_check(s, rec, state, rt, params, today, allow_submit=True)
            return
        if status == "done":
            return
        if status == "stale_released":
            # once-a-session reminder; the lot is under the normal reconcile / exits, and the ratchet may start a NEW
            # replace (its own fresh episode) once the stop at the broker reads replaceable again
            due = rt.get("page_key") != "stale_reminder" or rt.get("page_session") != today
            self._rt_page(rec, "stale_reminder", f"[{s}] lot {rec.get('coid')}: swing ratchet still released after a "
                                                 f"stale {rt.get('released_from')} since {rt.get('attempt_ts')} — "
                                                 f"check the stop at the broker", today)
            if due:
                _save_state(state)                   # persist the once-a-session dedupe key
        if rec.get("close_order_id") or rec.get("exit_reason_pending") or rec.get("unprotected") \
                or rec.get("partial_stop_paged"):
            return                                   # an exit / repair owns the lot this cycle
        qty = int(rec.get("qty") or 0)
        if qty < 1:
            return
        if rec.get("entry_px_estimated"):
            self._rt_page(rec, "entry_estimated", f"[{s}] swing ratchet: entry price is estimated — no profit-lock", today)
            _save_state(state)
            return
        entry = float(rec.get("entry_px") or 0)
        if not (_finite(entry) and entry >= 1):
            return                                   # sub-$1 lots: no sub-penny stops (rev 4 item 7)
        risk_ps = self._init_risk(s, rec, state, today)
        if risk_ps is None:
            return
        if not rt.get("armed_session"):
            if rt.get("checked") == today:
                return                               # one completed-bar check per session
            a = ratchet_armed(self._bars(s), str(rec.get("entry_date") or ""), entry, risk_ps, params["trigger_r"],
                              params["tick_usd"])
            if a is None:
                return                               # bars unreadable: retried next cycle, never armed blind
            rt["checked"] = today
            rt["trigger_px"] = round(a["trigger_px"], 4)
            if not a["armed"]:
                _save_state(state)
                self._rt_log(s, rec, "not_armed", "no completed close at the trigger", price=a["max_close"],
                             max_close=a["max_close"], last_session=a["last_session"], trigger_r=params["trigger_r"],
                             params_source=src)
                return
            rt.update(armed_session=a["armed_session"], armed_close=a["max_close"])
            _save_state(state)
            self._rt_log(s, rec, "armed", "completed close reached the trigger", price=a["max_close"],
                         trigger_r=params["trigger_r"], params_source=src)
        tick = params["tick_usd"]
        orders = broker.get_open_orders(s)
        if orders is None:
            logger.warning("[%s] swing ratchet: open orders unreadable — stop unchanged this cycle", s)
            return
        stops = [o for o in orders if _is_sell_stop(o)]
        lock_px = tick_ceil(entry + params["lock_r"] * risk_ps, tick)
        foreign = [o for o in stops if not str(getattr(o, "client_order_id", "") or "").startswith("IN-")]
        if foreign:
            # an owner (untagged) or other-tier stop is never touched (rev 3 item 4)
            hi = max((float(getattr(o, "stop_price", 0) or 0) for o in foreign), default=0.0)
            rt["lock_px"] = lock_px
            if hi >= lock_px:
                if rt.get("foreign_logged") != today:
                    rt["foreign_logged"] = today
                    _save_state(state)
                    self._rt_log(s, rec, "no_op", "a higher stop not placed by this tier rests — left alone", price=hi,
                                 foreign_stop_px=hi, foreign_ids=[str(getattr(o, "id", "")) for o in foreign])
            else:
                self._rt_page(rec, "foreign_stop", f"[{s}] swing ratchet: a stop not placed by this tier rests at "
                                                   f"${hi:.2f}, below the ${lock_px:.2f} lock — left alone; review", today)
                _save_state(state)
            return
        qty_ok = [o for o in stops if int(float(getattr(o, "qty", 0) or 0)) == qty]
        sid = str(rec.get("stop_order_id") or "")
        live_id = self._resolve_stop_id(sid) if sid else ""
        live = next((o for o in qty_ok if str(getattr(o, "id", "")) == live_id), None)
        if live is None:
            if len(qty_ok) != 1 or len(stops) != 1:
                self._rt_page(rec, "stop_candidates", f"[{s}] swing ratchet: {len(qty_ok)} IN- sell stop(s) for "
                                                      f"{qty} sh ({len(stops)} sell stops) — stop unchanged; review", today)
                _save_state(state)
                return
            live = qty_ok[0]
            rec["stop_order_id"] = str(getattr(live, "id", ""))   # resolved by tag, saved before any replace (rev 3 #11)
            _save_state(state)
        live_id = str(getattr(live, "id", ""))
        ost = _ord_status(live)
        if ost not in _REPLACEABLE or int(float(getattr(live, "filled_qty", 0) or 0)) != 0:
            logger.info("[%s] swing ratchet: stop %s is %s — not replaceable this cycle", s, live_id, ost)
            return
        cur = float(getattr(live, "stop_price", 0) or 0)
        if not (_finite(cur) and cur > 0):
            return
        if round(cur, 2) != round(float(rec.get("stop_px") or 0), 2):
            # broker is truth for reading; never lowered (rev 4 item 6)
            self._rt_page(rec, "stop_mismatch", f"[{s}] swing ratchet: broker stop ${cur:.2f} differs from state "
                                                f"${float(rec.get('stop_px') or 0):.2f} — broker level used", today)
        bid, qf = self._fresh_bid(s)
        if bid is None:
            logger.info("[%s] swing ratchet: no valid bid (%s) — stop unchanged this cycle", s, qf.get("quote"))
            return
        t = ratchet_target(entry, risk_ps, cur, bid, params["lock_r"], tick)
        rt["lock_px"] = t["lock_px"]
        if not t["raise_"]:
            if cur >= t["lock_px"]:
                self._rt_status(rt, "done", "stop already at or above the lock")
                _save_state(state)
                self._rt_log(s, rec, "no_op", "stop already at or above the lock", price=cur, cur_stop=cur, **qf)
            else:
                logger.info("[%s] swing ratchet: target %.2f not above the stop %.2f (bid %.2f) — unchanged", s,
                            t["target"], cur, bid)
            return
        if not t["target"] > 0:
            self._rt_page(rec, "bad_target", f"[{s}] swing ratchet: target ${t['target']} is not positive — no order",
                          today)
            _save_state(state)
            return
        held = self._broker_qty(s)
        rt.update(target_px=t["target"], old_order_id=live_id, old_stop_px=cur, new_order_id="",
                  attempt_ts=now.isoformat(), attempts=int(rt.get("attempts") or 0) + 1)
        self._rt_status(rt, "pending", "replace sent")
        if not _save_state(state):
            self._rt_status(rt, "idle", "state write failed before the replace")
            _page(f"[{s}] swing ratchet: state not saved — no replace sent (stop unchanged)")
            return
        new = broker.replace_stop_order(s, live_id, t["target"])
        rt["new_order_id"] = str(getattr(new, "id", "") or "") if new is not None else ""
        _save_state(state)
        self._rt_log(s, rec, "replace_sent", "completed close reached the trigger", price=t["target"],
                     cur_stop=cur, under_bid_px=t["under_bid_px"], old_status=ost,
                     old_qty=int(float(getattr(live, "qty", 0) or 0)), position_qty=held,
                     new_status=_ord_status(new) if new is not None else None, trigger_r=params["trigger_r"],
                     lock_r=params["lock_r"], tick=tick, params_source=src, **qf)
        self._ratchet_reconcile(s, rec, state, rt, params, today)

    @staticmethod
    def _session_opens(start: date, end: date) -> Optional[list]:
        """Alpaca-calendar session opens (ET) from `start` to `end`; None when the calendar cannot be read. Cached per
        (start, end) for the current end date."""
        key = (start, end)
        if key in _SESSION_CACHE:
            hit = _SESSION_CACHE[key]
            if not (isinstance(hit, tuple) and hit and hit[0] == "fail"):
                return hit
            if time.monotonic() - hit[-1] < _PAGE_EVERY_S:
                return None              # a failed read is cached too: never re-called per lot on the trading thread
        if any(k[1] != end for k in _SESSION_CACHE):
            _SESSION_CACHE.clear()
        try:
            from execution.swing_overnight import TNBroker
            opens = [op for _d, op, _c in TNBroker().calendar(start, end)]
        except Exception as e:
            logger.warning("swing ratchet: Alpaca calendar unreadable (%s) — ET-date fallback", e)
            _SESSION_CACHE[key] = ("fail", time.monotonic())
            return None
        _SESSION_CACHE[key] = opens
        return opens

    def _pending_stale(self, rt: dict, now: Optional[datetime] = None) -> bool:
        """True when the pending state began BEFORE the open of the current session — a session open from the Alpaca
        calendar lies between attempt_ts and now (R3-3). Unknown age → True (never left unmanaged); an unreadable
        calendar falls back to the ET date."""
        now = now or datetime.now(ET)
        try:
            t = datetime.fromisoformat(str(rt.get("attempt_ts") or ""))
            t = t.replace(tzinfo=ET) if t.tzinfo is None else t
        except ValueError:
            return True
        opens = self._session_opens(t.astimezone(ET).date(), now.astimezone(ET).date())
        if opens is None:
            return t.astimezone(ET).date() < now.astimezone(ET).date()
        return any(t < o <= now for o in opens)

    def _ratchet_owns(self, rec: dict, state: Optional[dict] = None, now: Optional[datetime] = None,
                      symbol: str = "") -> bool:
        """True while an unconfirmed ratchet replace / re-protection from the CURRENT session owns the lot's stop
        (_reconcile, _reprotect and the near-close exits stand aside). One from an earlier session is RELEASED the first
        time it is seen (R4-1b): status → "stale_released", which _ratchet_lot never reconciles, so only the normal
        paths book this lot's fills from then on. One CRITICAL page per stale episode; _ratchet_lot repeats it once a
        session while the lot stays released."""
        rt = rec.get("ratchet") or {}
        if rt.get("status") not in ("pending", "reprotect_pending"):
            return False
        now = now or datetime.now(ET)
        if not self._pending_stale(rt, now):
            return True
        was = rt.get("status")
        try:
            self._resolve_lot_stop(symbol, rec, None)  # hand the normal paths the stop actually in force (R5-1)
        except Exception as e:
            logger.warning("[%s] swing ratchet: stop chain read at release failed (%s)", symbol, e)
        self._rt_status(rt, "stale_released", f"{was} since {rt.get('attempt_ts')} — before the current session")
        rt.update(released_from=was, released_ts=now.isoformat(), page_key="stale_reminder",
                  page_session=now.strftime("%Y-%m-%d"))
        if state is not None:
            _save_state(state)
        self._rt_log(symbol, rec, "stale_released", f"{was} since {rt.get('attempt_ts')}", price=rec.get("stop_px"))
        _page(f"[{symbol or '?'}] lot {rec.get('coid')}: swing ratchet {was} since {rt.get('attempt_ts')} (before the "
              f"current session) — RELEASED to the normal reconcile / exits; check the stop at the broker")
        return False

    @staticmethod
    def _own_stop_ids(rec: dict) -> set:
        rt = rec.get("ratchet") or {}
        ids = {str(rec.get("stop_order_id") or ""), str(rt.get("old_order_id") or ""), str(rt.get("new_order_id") or "")}
        ids |= {str(x) for x in (rt.get("order_ids") or [])} | {str(x) for x in (rt.get("chain_ids") or [])}
        ids.discard("")
        return ids

    def _chain_ids(self, start_id: str) -> list[str]:
        """Ids AFTER `start_id` along its replaced_by chain (each read once; a repeated id ends the walk), capped at
        resolve_live_order's own hop limit."""
        import inspect
        from execution import broker
        try:
            cap = inspect.signature(broker.resolve_live_order).parameters["max_hops"].default
            cap = cap if isinstance(cap, int) and not isinstance(cap, bool) else None
        except Exception:
            cap = None
        out: list[str] = []
        seen, oid = {start_id}, start_id
        while oid and (cap is None or len(out) < cap):
            o = broker.get_order(oid)
            nxt = str(getattr(o, "replaced_by", "") or "") if o is not None and _ord_status(o) == "replaced" else ""
            if not nxt or nxt in seen:
                break
            out.append(nxt)
            seen.add(nxt)
            oid = nxt
        return out

    def _exit_label(self, rec: dict, order_id: str, partial: bool = False) -> str:
        """Provenance label (cold review L3): ratchet_stop when the filled order is the ratchet's new order, any order
        in the replaced_by chain from the ratchet's old order, or an order the ratchet placed; else protective_stop.
        The loss multiple counts every stop-family fill by its gap excess, so a label can never up-size (M1)."""
        label = _stop_exit_label(rec, order_id, partial)
        old = str((rec.get("ratchet") or {}).get("old_order_id") or "")
        if label.startswith("ratchet_stop") or not (order_id and old):
            return label
        try:
            if order_id in self._chain_ids(old):
                return "ratchet_stop_partial" if partial else "ratchet_stop"
        except Exception as e:
            logger.warning("swing ratchet: chain read for %s failed (%s) — labelled protective", old, e)
        return label

    def _lot_stops(self, s: str, rec: dict) -> Optional[dict]:
        """The sell stops on `s`, by who they belong to (R3-3, R6-1): "own" — this lot's own / the ratchet's order ids,
        their successors (`replaces`) and the IN-rp- re-protection prefix (the only stops ever ADOPTED); "owner" —
        untagged (the owner's): protection, never adopted, never cancelled; "other" — another tier's or another IN-
        system's. "*_trans" = own / owner stops in a transitional status (pending_cancel / pending_replace). None
        when the open orders are unreadable."""
        from execution import broker
        from execution.ownership_guard import tier_of_coid
        orders = broker.get_open_orders(s)
        if orders is None:
            return None
        own_ids = self._own_stop_ids(rec)
        out: dict[str, list] = {"own": [], "own_trans": [], "owner": [], "owner_trans": [], "other": []}
        for o in orders:
            if not _is_sell_stop(o):
                continue
            coid = str(getattr(o, "client_order_id", "") or "")
            if (str(getattr(o, "id", "")) in own_ids or str(getattr(o, "replaces", "") or "") in own_ids
                    or coid.startswith("IN-rp-")):
                key = "own"
            elif tier_of_coid(coid) is None:
                key = "owner"
            else:
                key = "other"
            if _ord_status(o) not in _LIVE_STOP:
                if key == "other":
                    continue
                key += "_trans"
            out[key].append(o)
        return out

    def _only_live_stop(self, s: str, rec: dict, oid: str) -> Optional[bool]:
        """True only when exactly ONE live sell stop of this lot rests on `s`, it is `oid`, and no owner stop also
        rests on the shares (rev 8); None when unreadable."""
        ls = self._lot_stops(s, rec)
        if ls is None:
            return None
        one = (not ls["own_trans"] and not ls["owner_trans"] and len(ls["own"]) == 1
               and str(getattr(ls["own"][0], "id", "")) == oid)
        if not one:
            return False
        if not ls["owner"]:
            return True
        if (rec.get("ratchet") or {}).get("reprotect_qty_cap") is None:
            return False
        # R8-2: a partial-cover re-protection confirms when this lot's one stop plus the owner stops cover the need
        held = self._broker_qty(s)
        if held is None:
            return None
        need = int(held) - int(self._other_tiers_qty(s))
        return self._stops_qty(ls["own"]) + self._stops_qty(ls["owner"]) >= need

    @staticmethod
    def _stops_qty(orders: list) -> int:
        try:
            return sum(int(float(getattr(o, "qty", 0) or 0)) for o in orders)
        except (TypeError, ValueError):
            return 0

    def _owner_protects(self, s: str, rec: dict, state: dict, owner: list, held: Optional[int] = None) -> None:
        """An owner (untagged) stop already covers the lot's shares (R6-1): counted as protection, NEVER saved as the
        lot's stop_order_id (so no exit path ever cancels it) and never re-protected over; paged once per stop set.
        R9-3: the first cover snapshots the broker qty and the owner's own shares (held − other tiers − lot qty)."""
        ids = sorted(str(getattr(o, "id", "")) for o in owner)
        keys = ("unprotected", "stop_order_id", "owner_stop_ids", "owner_stop_paged", "owner_cover_held",
                "owner_qty_at_cover")
        before = [rec.get(k) for k in keys]
        if (rec.get("owner_stop_ids") or []) != ids:
            self._clear_owner_snapshot(rec)          # R10-4: a different owner stop set — a fresh snapshot below
        rec.update(unprotected=False, stop_order_id="", owner_stop_ids=ids)
        if held is not None and rec.get("owner_cover_held") is None:
            rec["owner_cover_held"] = int(held)
            rec["owner_qty_at_cover"] = max(int(held) - int(self._other_tiers_qty(s)) - int(rec.get("qty") or 0), 0)
            rec["owner_fill_owner_qty"] = 0
        if rec.get("owner_stop_paged") != ids:
            rec["owner_stop_paged"] = ids
            _page(f"[{s}] lot {rec.get('coid')}: an owner (untagged) stop {', '.join(ids)} protects the shares — left "
                  f"alone, not adopted, never cancelled by this tier; this tier places no stop over it")
        if before != [rec.get(k) for k in keys]:
            _save_state(state)                       # written only when something changed (R7-4)

    def _book_owner_fills(self, s: str, rec: dict, state: dict) -> Optional[bool]:
        """Book what the owner stops protecting this lot (owner_stop_ids) filled (R7-1): each read with get_order only
        (never cancelled), its unbooked part via _net_fill / _mark_booked, labelled protective_stop and measured against
        the lot's own stop level in force (stop_px) — so the loss stays in the Kelly sample. True when the lot closed,
        False when nothing new, None when an owner order or the position is unreadable (paged CRITICAL; nothing booked).
        R8-1b: an owner fill is this lot's only for the lot shares the broker NO LONGER holds — leg = min(new fill, lot
        qty, lot qty − max(0, broker qty − other tiers' ledger shares − owner shares still behind a live owner stop)).
        The rest of the fill was the owner's own shares: marked seen, never booked to the lot."""
        from execution import broker
        ids = list(dict.fromkeys(str(x) for x in (rec.get("owner_stop_ids") or []) + (rec.get("owner_partial_ids") or [])
                                 if x))
        if not ids:
            return False
        orders = []
        for oid in ids:
            o = broker.get_order(oid)
            if o is None:
                _page_throttled(rec, f"CRITICAL [{s}] lot {rec.get('coid')}: owner stop {oid} is unreadable — its "
                                     f"fills are not booked yet; re-checking each cycle")
                _save_state(state)
                return None
            orders.append((oid, o))
        held = self._broker_qty(s)
        if held is None:
            _page_throttled(rec, f"CRITICAL [{s}] lot {rec.get('coid')}: position unreadable — owner stop fills not "
                                 f"booked this cycle")
            _save_state(state)
            return None
        reserved = 0
        for _oid, o in orders:
            if _ord_status(o) in _LIVE_STOP:
                try:
                    reserved += max(int(float(getattr(o, "qty", 0) or 0)) - int(float(getattr(o, "filled_qty", 0) or 0)), 0)
                except (TypeError, ValueError):
                    continue
        # R9-3: the owner's still-held shares are at least those still behind a live owner stop, and at least those he
        # held at the cover snapshot less the owner fills already attributed to him (shares he bought later without a
        # stop never count as the lot's)
        snap = rec.get("owner_qty_at_cover")
        owner_still = reserved
        if isinstance(snap, int) and not isinstance(snap, bool):
            owner_still = max(reserved, snap - int(rec.get("owner_fill_owner_qty") or 0))
        lot_held = max(0, int(held) - int(self._other_tiers_qty(s)) - owner_still)
        missing = max(0, int(rec.get("qty") or 0) - lot_held)
        drop: list[str] = []
        for oid, o in orders:
            cum, avg = getattr(o, "filled_qty", 0), getattr(o, "filled_avg_price", 0)
            nq, npx = _net_fill(rec, oid, cum, avg)
            qty = int(rec.get("qty") or 0)
            if _is_terminal(getattr(o, "status", "")):
                drop.append(oid)                     # R9-2: ended — never read again once its fills are seen below
            if nq <= 0 or qty < 1:
                continue
            _mark_booked(rec, oid, cum, avg)
            leg = min(nq, qty, missing)              # never more than the lot shares that actually left (R8-1b)
            rec["owner_fill_owner_qty"] = int(rec.get("owner_fill_owner_qty") or 0) + (nq - max(leg, 0))
            if leg < 1:
                _save_state(state)
                self._rt_log(s, rec, "owner_fill_not_lot", f"owner stop {oid} filled {nq} sh of the owner's own",
                             price=npx, position_qty=held, lot_held=lot_held)
                continue
            missing -= leg
            if npx <= 0:
                rec.update(qty=max(qty - leg, 0), partial_pnl_unknown=True)
                rec.setdefault("stop_legs", {"qty": 0, "mult_qty": 0.0})["unknown"] = True
                _save_state(state)
                _page(f"CRITICAL [{s}] lot {rec.get('coid')}: owner stop {oid} filled {leg} sh at an unreadable price — "
                      f"P&L unknown, excluded from the Kelly loss multiple; check fills")
                continue
            if leg >= qty:
                self._book_exit(s, rec, state, "protective_stop", npx, leg)
                return True
            self._book_partial(s, rec, state, npx, leg, "protective_stop_partial")
        if drop:
            before_full = list(rec.get("owner_stop_ids") or [])
            for key in ("owner_stop_ids", "owner_partial_ids"):
                if rec.get(key):
                    rec[key] = [x for x in rec[key] if str(x) not in drop]
            if list(rec.get("owner_stop_ids") or []) != before_full:
                self._clear_owner_snapshot(rec)      # R10-4: the full-cover owner stop set changed
            _save_state(state)
        return False

    @staticmethod
    def _other_tiers_qty(s: str) -> float:
        """Shares of `s` that OTHER tiers own per the ownership ledger (all tiers − this tier); 0.0 when unreadable."""
        try:
            from execution.ownership_guard import get_combined_symbol_exposure, load_ledger, tier_qty
            led = load_ledger()
            v = float(get_combined_symbol_exposure(led, s)) - float(tier_qty(led, s, TIER))
            return v if _finite(v) and v > 0 else 0.0
        except Exception as e:
            logger.warning("[%s] breakout: ownership ledger unreadable (%s) — other tiers' shares taken as 0", s, e)
            return 0.0

    def _topup_cover(self, s: str, rec: dict, state: dict, so: Any) -> None:
        """R9-1 / R10-1: while the lot's own live stop covers fewer shares than the lot, re-run the owner cover every
        cycle and keep ONE own stop sized to the uncovered qty — never a second stop:
          (a) replace the lot's own stop to that qty at the SAME stop price;
          (b) a replace whose outcome is unknown (None) adds nothing: `topup_pending` is recorded, the next cycle re-reads
              the chain (a successor is adopted by _resolve_lot_stop; the old stop still live at the old qty with nothing
              in transition → the replace is retried);
          (c) a stop Alpaca cannot replace yet (accepted / pending_new — overnight GTC stops sit in accepted for hours)
              is NEVER cancelled (R11-1): the partial stop stays, `topup_pending` records reason not_replaceable, a
              CRITICAL page names the uncovered qty once per change and once per session, and the replace (a) runs on
              the first cycle the stop reads replaceable;
          (d) a CRITICAL page while any share of the lot is uncovered."""
        from execution import broker
        try:
            own_q = int(float(getattr(so, "qty", 0) or 0))
        except (TypeError, ValueError):
            return
        lot = int(rec.get("qty") or 0)
        sid = str(getattr(so, "id", "") or "")
        tp = rec.get("topup_pending") if isinstance(rec.get("topup_pending"), dict) else None
        if tp is not None and str(tp.get("old_id") or "") != sid:
            rec.pop("topup_pending", None)           # (b) the chain moved: the successor is the stop in force
            _save_state(state)
            logger.info("[%s] breakout top-up resolved: %s → %s (%s sh)", s, tp.get("old_id"), sid, own_q)
            tp = None
        if own_q >= lot:
            if tp is not None:
                rec.pop("topup_pending", None)
                _save_state(state)
            return
        held = self._broker_qty(s)
        ls = self._lot_stops(s, rec)
        if held is None or ls is None:
            return
        cap_total = min(lot, int(held))
        covers, uncovered = self._owner_cover(s, ls["owner"], int(held), cap_total, rec)
        if not covers:
            self._clear_owner_snapshot(rec)          # R10-4: full cover has ended
        target = 0 if covers else min(int(uncovered), cap_total)
        own_total = self._stops_qty(ls["own"])
        if own_total >= target:
            if tp is not None:
                rec.pop("topup_pending", None)
                _save_state(state)
            return
        short = target - own_total
        if ls["owner"]:
            rec["owner_partial_ids"] = sorted(str(getattr(o, "id", "")) for o in ls["owner"])
        else:
            rec.pop("owner_partial_ids", None)
        ost = _ord_status(so)
        if ls["own_trans"] or ost not in _LIVE_STOP:
            _page_throttled(rec, f"CRITICAL [{s}] lot {rec.get('coid')}: {short} sh uncovered; its stop is {ost} — "
                                 f"waiting for broker truth")
            _save_state(state)
            return
        px = float(getattr(so, "stop_price", 0) or rec.get("stop_px") or 0)
        new_q = min(own_q + short, cap_total)
        if ost not in _REPLACEABLE:
            # (c) R11-1: never cancel — the partial stop keeps protecting `own_q` shares; replaced once replaceable
            today = datetime.now(ET).strftime("%Y-%m-%d")
            key = [sid, ost, int(short), today]
            changed = rec.get("topup_wait_paged") != key
            if changed:
                rec["topup_wait_paged"] = key            # once per (stop, status, uncovered qty), then once a session
                _page(f"CRITICAL [{s}] lot {rec.get('coid')}: {short} sh uncovered — its stop {sid} is {ost} and cannot "
                      f"be replaced yet; it still covers {own_q} sh and is raised to {new_q} once it reads replaceable")
            if tp is None or tp.get("reason") != "not_replaceable" or tp.get("qty") != new_q:
                rec["topup_pending"] = {"old_id": sid, "old_qty": own_q, "qty": new_q, "reason": "not_replaceable",
                                        "status": ost, "ts": datetime.now(ET).isoformat()}
                changed = True
            if changed:
                _save_state(state)
            return
        new = broker.replace_stop_order(s, sid, px, qty=new_q) if px > 0 and sid else None
        if new is not None and getattr(new, "id", None):
            rec.pop("topup_pending", None)
            rec.update(stop_order_id=str(getattr(new, "id", "")), stop_px=round(px, 2), unprotected=False)
            _save_state(state)
            _page(f"[{s}] lot {rec.get('coid')}: the owner's stop no longer covers it — own stop raised from {own_q} to "
                  f"{new_q} sh at ${px:.2f}")
            return
        # (b) outcome unknown: nothing added; the next cycle reads the chain and adopts or retries
        rec["topup_pending"] = {"old_id": sid, "old_qty": own_q, "qty": new_q, "ts": datetime.now(ET).isoformat()}
        _page_throttled(rec, f"CRITICAL [{s}] lot {rec.get('coid')}: {short} sh uncovered — stop replace {sid} to "
                             f"{new_q} sh unconfirmed; re-checking each cycle")
        _save_state(state)

    @staticmethod
    def _clear_owner_snapshot(rec: dict) -> None:
        """R10-4: forget the owner-cover snapshot (a new full cover takes a fresh one)."""
        for k in ("owner_cover_held", "owner_qty_at_cover", "owner_fill_owner_qty", "cover_surplus_paged"):
            rec.pop(k, None)

    def _owner_cover(self, s: str, owner: list, held: int, want: int,
                     rec: Optional[dict] = None) -> tuple[bool, int]:
        """(owner stops cover this lot, this lot's uncovered qty). An owner stop protects this lot only when it covers
        more than the owner's own shares: owner stop qty ≥ broker qty − other tiers' ledger shares (R7-2). Otherwise
        this lot places its own stop for the shares the owner stop leaves uncovered. R9-3: shares the broker gained
        after a full-cover snapshot are the owner's — paged, never counted as needing this lot's cover."""
        oq = self._stops_qty(owner)
        if not owner:
            return False, int(want)
        snap = (rec or {}).get("owner_cover_held")
        if isinstance(snap, int) and not isinstance(snap, bool) and int(held) > snap:
            if rec is not None and rec.get("cover_surplus_paged") != int(held):
                rec["cover_surplus_paged"] = int(held)
                _page(f"[{s}] lot {rec.get('coid')}: broker holds {held} sh, above the {snap} at the owner-stop cover — "
                      f"the new shares are the owner's; no stop placed on them")
            held = snap
        need = int(held) - int(self._other_tiers_qty(s))
        if oq >= need:
            return True, 0
        return False, max(min(int(want), need - oq), 0)

    def _tier_held(self, s: str, rec: dict, held: int) -> int:
        """Shares this lot protects: min(lot qty, broker qty) — never more than the lot (no stop on another tier's or
        the owner's surplus shares, R6-3) and never fewer than that because of the ledger (R7-3). Paged when the broker
        holds more than the lot, and when this tier's ledger qty reads below the lot."""
        lot = int(rec.get("qty") or 0)
        want = min(lot, int(held))
        try:
            from execution.ownership_guard import load_ledger, tier_qty
            led = float(tier_qty(load_ledger(), s, TIER))
            if _finite(led) and led < lot and rec.get("ledger_low_paged") != [led, lot]:
                rec["ledger_low_paged"] = [led, lot]          # once per (ledger qty, lot qty); the caller saves
                _page(f"[{s}] lot {rec.get('coid')}: ownership ledger shows {led:g} {TIER} sh, fewer than the lot's "
                      f"{lot} — still protecting {want}; check the ledger")
        except Exception as e:
            logger.warning("[%s] breakout: ownership ledger unreadable (%s) — sized from the lot and the broker", s, e)
        if int(held) > lot and rec.get("broker_surplus_paged") != [int(held), lot]:
            rec["broker_surplus_paged"] = [int(held), lot]
            _page(f"[{s}] lot {rec.get('coid')}: broker holds {held} sh, more than the lot's {lot} — protecting "
                  f"only {want}")
        return want

    def _successor(self, s: str, old_id: str, rt: dict) -> Optional[Any]:
        """The order that replaced `old_id`: the ratchet's saved new id (read by id, ANY status), else an open order
        whose `replaces` is `old_id`. The design's 'list the symbol's orders since attempt_ts, all statuses' needs a
        broker.py function that does not exist (no public all-status listing); this is the closest using public calls."""
        from execution import broker
        nid = str(rt.get("new_order_id") or "")
        if nid:
            o = broker.get_order(nid)
            if o is not None and str(getattr(o, "replaces", "") or old_id) == old_id:
                return o
        orders = broker.get_open_orders(s) or []
        hits = [o for o in orders if str(getattr(o, "replaces", "") or "") == old_id]
        return hits[0] if len(hits) == 1 else None

    def _book_order_fills(self, s: str, rec: dict, state: dict, o: Any, today: str) -> bool:
        """Book what `o` filled that is not booked yet (cold review L2: never re-protect over an unbooked fill).
        Full → the lot's exit (True). Part → _book_partial, the lot shrinks. A part whose price is unreadable → qty
        shrinks, partial_pnl_unknown, paged (never fabricated). Idempotent per order id across cycles."""
        oid = str(getattr(o, "id", "") or "")
        cum = getattr(o, "filled_qty", 0)
        cum_avg = getattr(o, "filled_avg_price", 0)
        new_fill, px = _net_fill(rec, oid, cum, cum_avg)            # incremental qty and price (nit)
        if not oid or new_fill <= 0:
            return False
        qty = int(rec.get("qty") or 0)
        _mark_booked(rec, oid, cum, cum_avg)
        # the lot's own stop order's id is cleared in the same save, so _reconcile can never re-book this fill (R3-2)
        clear = "stop_order_id" if str(rec.get("stop_order_id") or "") == oid else ""
        if new_fill >= qty > 0 and px > 0:
            if new_fill > qty:
                _page(f"CRITICAL [{s}] lot {rec.get('coid')}: order {oid} filled {new_fill} sh, more than the lot's "
                      f"{qty} — {new_fill - qty} never booked to the lot (R10-3)")
            self._book_exit(s, rec, state, self._exit_label(rec, oid), px, min(new_fill, qty))
            return True
        if px > 0:
            self._book_partial(s, rec, state, px, min(new_fill, qty), self._exit_label(rec, oid, partial=True),
                               clear_key=clear)
        else:
            rec.update(qty=max(qty - new_fill, 0), partial_pnl_unknown=True)
            # R5-2: a stop leg without its price is unknown — the lot is left out of the loss multiple, visibly
            rec.setdefault("stop_legs", {"qty": 0, "mult_qty": 0.0})["unknown"] = True
            if clear:
                rec[clear] = ""
            _save_state(state)
            _page(f"CRITICAL [{s}] lot {rec.get('coid')}: stop order {oid} filled {new_fill} sh at an unreadable price — "
                  f"P&L marked unknown and the stop leg excluded from the Kelly loss multiple; check fills")
        return False

    def _ratchet_reconcile(self, s: str, rec: dict, state: dict, rt: dict, params: dict, today: str) -> None:
        """Resolve a pending replace from broker links (rev 7 item 4). No new order of any kind while unproven."""
        from execution import broker
        old_id = str(rt.get("old_order_id") or "")
        old = broker.get_order(old_id) if old_id else None
        if old is None:
            self._rt_page(rec, "pending_unreadable", f"[{s}] swing ratchet: replace UNCONFIRMED — old stop {old_id} "
                                                     f"unreadable; no new order until proven", today)
            _save_state(state)
            return
        ost = _ord_status(old)
        new = None
        if ost == "replaced":
            chain = self._chain_ids(old_id)
            rt["chain_ids"] = chain
            new = broker.get_order(chain[-1]) if chain else self._successor(s, old_id, rt)
            if new is None:
                self._rt_page(rec, "successor_unknown", f"[{s}] swing ratchet: old stop {old_id} replaced but the new "
                                                        f"order is unreadable — no new order until proven", today)
                _save_state(state)
                return
        elif ost in _LIVE_STOP or ost == "pending_replace":
            new = self._successor(s, old_id, rt)
            if new is None:
                one = self._only_live_stop(s, rec, old_id)
                if ost == "pending_replace" or not one:
                    self._rt_page(rec, "pending_replace", f"[{s}] swing ratchet: stop {old_id} is {ost} "
                                                          f"({'orders unreadable' if one is None else 'not the only live stop' if not one else ''})"
                                                          f" — waiting for broker truth", today)
                    _save_state(state)
                    return
                self._rt_status(rt, "idle", "replace not applied; the old stop is the one live stop")
                _save_state(state)
                self._rt_log(s, rec, "idle", "replace not applied; the old stop is in force", price=rt.get("old_stop_px"),
                             old_status=ost)
                return
        elif _is_terminal(ost):
            # the old stop ended (filled, canceled, expired, rejected): book every fill FIRST, then re-protect the rest
            if self._book_order_fills(s, rec, state, old, today) or rec.get("status") == "closed":
                self._rt_status(rt, "done", f"old stop {ost} — lot closed")
                _save_state(state)
                return
            self._ratchet_reprotect_if_naked(s, rec, state, rt, params, today, f"old stop {ost}, no successor")
            return
        else:
            self._rt_page(rec, "old_not_final", f"[{s}] swing ratchet: old stop {old_id} is {ost} — waiting", today)
            _save_state(state)
            return
        nid, nst = str(getattr(new, "id", "") or ""), _ord_status(new)
        if not str(getattr(new, "client_order_id", "") or "").startswith("IN-"):
            # the order now in force is not this tier's (the owner moved it): never adopted as the ratchet's
            rt["foreign_stop_id"] = nid
            self._rt_status(rt, "idle", f"the order in force {nid} is not tagged IN- — left alone")
            _save_state(state)
            self._rt_page(rec, "foreign_successor", f"[{s}] swing ratchet: the stop now in force ({nid}, {nst}) was not "
                                                    f"placed by this tier — left alone; review", today)
            return
        if nst in _LIVE_STOP:
            one = self._only_live_stop(s, rec, nid)
            if not one:
                self._rt_page(rec, "new_not_only", f"[{s}] swing ratchet: new stop {nid} live but "
                                                   f"{'orders unreadable' if one is None else 'not the only live stop'}"
                                                   f" — waiting for broker truth", today)
                _save_state(state)
                return
            px = float(getattr(new, "stop_price", 0) or 0)
            ids = rt.setdefault("order_ids", [])
            if nid not in ids:
                ids.append(nid)
            rec.update(stop_order_id=nid, stop_px=round(px, 2) if px > 0 else rec.get("stop_px"), unprotected=False)
            rt.update(new_order_id=nid, ratchet_stop_px=round(px, 2))
            done = px >= float(rt.get("lock_px") or math.inf)
            self._rt_status(rt, "done" if done else "idle", "replaced")
            _save_state(state)
            self._rt_log(s, rec, "replaced", "broker confirms the raised stop", price=px, new_status=nst)
            logger.info("swing ratchet %s: stop %.2f -> %.2f (lock %.2f, %s) [change=%s]", s,
                        float(rt.get("old_stop_px") or 0), px, float(rt.get("lock_px") or 0),
                        "done" if done else "below lock", RATCHET_CHANGE)
            return
        if _is_terminal(nst):
            ids = rt.setdefault("order_ids", [])
            if nid not in ids:
                ids.append(nid)
            rt["new_order_id"] = nid
            if float(getattr(new, "stop_price", 0) or 0) > 0:
                rec["stop_px"] = round(float(getattr(new, "stop_price", 0) or 0), 2)   # the level that filled
            self._rt_log(s, rec, "replaced_ended", f"new stop {nid} {nst}", price=getattr(new, "filled_avg_price", 0),
                         new_status=nst, filled_qty=getattr(new, "filled_qty", 0))
            if self._book_order_fills(s, rec, state, new, today) or rec.get("status") == "closed":
                self._rt_status(rt, "done", f"the raised stop {nst}")
                _save_state(state)
                return
            rec["stop_px"] = rt.get("old_stop_px") or rec.get("stop_px")          # last CONFIRMED resting level
            _save_state(state)
            self._ratchet_reprotect_if_naked(s, rec, state, rt, params, today, f"new order {nid} {nst}")
            return
        self._rt_page(rec, "new_not_final", f"[{s}] swing ratchet: new stop {nid} is {nst} — waiting for broker truth",
                      today)
        _save_state(state)

    def _ratchet_reprotect_if_naked(self, s: str, rec: dict, state: dict, rt: dict, params: dict, today: str,
                                    why: str, may_submit: bool = True) -> None:
        """Shares still held and NO live sell stop → re-protect (rev 7 item 4, rev 8). Exactly one live sell stop for
        the held qty (this tier's re-protection, or one the owner placed) → adopted as the lot's protection and the lot
        returns to idle (an owner stop is recorded as foreign, never as the ratchet's). A stop in pending_cancel /
        pending_replace, several stops, or a quantity mismatch → stays pending, paged (rev 5)."""
        held = self._broker_qty(s)
        ls = self._lot_stops(s, rec)
        if held is None or ls is None:
            self._rt_page(rec, "lost_unreadable", f"[{s}] swing ratchet: {why}; position/orders unreadable — no new "
                                                  f"order until proven", today)
            _save_state(state)
            return
        if held == 0:
            rec["stop_order_id"] = ""              # an ended order: never re-read by _reconcile as a fresh fill (R3-2)
            self._rt_status(rt, "idle", f"{why}; no shares held (booked by reconcile)")
            _save_state(state)
            return
        trans = ls["own_trans"] + ls["owner_trans"]
        if trans:
            self._rt_page(rec, "stop_transitional", f"[{s}] swing ratchet: {why}; a sell stop is "
                                                    f"{_ord_status(trans[0])} — waiting for broker truth", today)
            _save_state(state)
            return
        want = self._tier_held(s, rec, held)
        own = ls["own"]
        if own:
            o = own[0]
            if len(own) == 1 and int(float(getattr(o, "qty", 0) or 0)) in (want, rt.get("reprotect_stop_qty")):
                oid = str(getattr(o, "id", ""))
                coid = str(getattr(o, "client_order_id", "") or "")
                if coid and coid == str(rt.get("reprotect_coid") or ""):
                    ids = rt.setdefault("order_ids", [])
                    if oid not in ids:
                        ids.append(oid)
                rec.update(stop_order_id=oid, stop_px=round(float(getattr(o, "stop_price", 0) or 0), 2),
                           qty=want, unprotected=False)
                self._rt_status(rt, "idle", f"{why}; this lot's one live stop {oid} adopted")
                _save_state(state)
                self._rt_log(s, rec, "adopted", f"{why}; one live own sell stop", price=rec.get("stop_px"),
                             adopted_coid=coid, position_qty=held)
                return
            self._rt_page(rec, "lost_ambiguous", f"[{s}] swing ratchet: {why}; {len(own)} own sell stop(s) not "
                                                 f"matching {want} sh — no new order until proven", today)
            _save_state(state)
            return
        covers, uncovered = self._owner_cover(s, ls["owner"], held, want, rec)
        if covers:
            # the owner's stop protects the shares: counted, never adopted or cancelled (R6-1)
            rt["foreign_stop_id"] = ",".join(sorted(str(getattr(o, "id", "")) for o in ls["owner"]))
            self._rt_status(rt, "idle", f"{why}; an owner stop protects the shares — left alone")
            self._owner_protects(s, rec, state, ls["owner"], held)
            self._rt_log(s, rec, "owner_protects", why, price=getattr(ls["owner"][0], "stop_price", 0),
                         position_qty=held)
            return
        lot_q = int(rec.get("qty") or 0)
        maybe = [o for o in ls["other"] if int(float(getattr(o, "qty", 0) or 0)) == lot_q]
        if maybe:
            # R6-4: an IN- stop for exactly the lot's qty may be this lot's own — no second stop
            self._rt_page(rec, "lost_maybe_own", f"[{s}] swing ratchet: {why}; an IN- stop for the lot's qty may be "
                                                 f"this lot's own — no new order; review", today)
            _save_state(state)
            return
        if ls["owner"]:
            rt["reprotect_qty_cap"] = uncovered      # R7-2: this lot's stop covers only what the owner's leaves
            rec["owner_partial_ids"] = sorted(str(getattr(o, "id", "")) for o in ls["owner"])   # R8-1a, R8-4
        else:
            rt.pop("reprotect_qty_cap", None)
        if not may_submit:
            self._rt_page(rec, "reprotect_unconfirmed", f"[{s}] swing ratchet: {why}; no live stop seen yet — "
                                                        f"re-checked next cycle", today)
            _save_state(state)
            return
        seq = rt.get("reprotect_seq")
        rt["reprotect_seq"] = 0 if seq is None else seq
        self._ratchet_reprotect_submit(s, rec, state, rt, params, today, held, why)

    def _ratchet_reprotect_submit(self, s: str, rec: dict, state: dict, rt: dict, params: dict, today: str,
                                  held: int, why: str) -> None:
        """POST a GTC stop under a deterministic client id (a retry after a timeout can never duplicate it). Level =
        min(last confirmed stop, bid − one tick) — owner exception (c) — rounded down to the cent, > 0."""
        from execution import broker
        seq = rt.get("reprotect_seq")
        cid = reprotect_coid(rec.get("coid"), seq)
        if cid is None:
            self._rt_page(rec, "bad_reprotect_id", f"[{s}] swing ratchet: re-protection id invalid (coid "
                                                   f"{rec.get('coid')!r}, seq {seq!r}) — NO stop placed; set one manually",
                          today)
            _save_state(state)
            return
        tick = params["tick_usd"]
        last = float(rt.get("old_stop_px") or rec.get("stop_px") or 0)
        bid, qf = self._fresh_bid(s)
        level = tick_floor(min(last, bid - tick) if bid is not None else last, tick)
        if not (_finite(level) and level > 0):
            self._rt_page(rec, "bad_reprotect_px", f"[{s}] swing ratchet: re-protection level ${level} not positive — "
                                                   f"NO stop placed; set one manually", today)
            _save_state(state)
            return
        qty = self._tier_held(s, rec, int(held))
        cap = rt.get("reprotect_qty_cap")
        stop_qty = min(qty, int(cap)) if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1 else qty
        rt.update(reprotect_coid=cid, reprotect_px=level, reprotect_qty=qty, reprotect_stop_qty=stop_qty)
        rt.setdefault("attempt_ts", datetime.now(ET).isoformat())   # the episode's age (R3-3)
        self._rt_status(rt, "reprotect_pending", why)
        rec.update(stop_order_id="", unprotected=True)
        if not _save_state(state):
            _page(f"[{s}] swing ratchet: state not saved — re-protection not sent; retried next cycle")
            return
        o = broker.submit_gtc_stop_order(s, stop_qty, "sell", level, tier=TIER, allow_cancel_blocking=False,
                                         client_order_id=cid)
        sentinel = o is not None and o in (getattr(broker, "PROTECTION_ALREADY_HELD", None),
                                           getattr(broker, "PROTECTION_UNKNOWN", None))
        self._rt_log(s, rec, "reprotect_sent", why, price=level, client_order_id=cid, submitted_qty=stop_qty,
                     position_qty=held, response_id=str(getattr(o, "id", "") or ""),
                     response_sentinel=repr(o) if sentinel else None, **qf)
        self._rt_page(rec, "reprotect", f"[{s}] swing ratchet: replace lost ({why}) — re-protecting {qty} sh at "
                                        f"${level:.2f} ({cid})", today)
        if sentinel:
            # held / unknown: something already holds the shares — re-check the book and adopt the live stop
            self._ratchet_reprotect_if_naked(s, rec, state, rt, params, today, "re-protection held by a live order",
                                             may_submit=False)
            return
        self._ratchet_reprotect_check(s, rec, state, rt, params, today, allow_submit=False)

    def _ratchet_reprotect_check(self, s: str, rec: dict, state: dict, rt: dict, params: dict, today: str,
                                 allow_submit: bool) -> None:
        """Read the re-protection by its client id. Live and the one live stop → idle (the ratchet may raise it again);
        filled / part-filled → booked first; rejected / canceled / expired → sequence + 1; not found → the book is
        re-checked (a live stop is adopted, else the SAME id is resubmitted — Alpaca refuses a duplicate);
        pending_cancel and other transitional statuses → stays pending (rev 5)."""
        from execution import broker
        cid = str(rt.get("reprotect_coid") or "")
        o = broker.get_order_by_client_order_id(cid) if cid else None
        if o is None:
            self._ratchet_reprotect_if_naked(s, rec, state, rt, params, today, "re-protection not found",
                                             may_submit=allow_submit)
            return
        oid, ost = str(getattr(o, "id", "") or ""), _ord_status(o)
        ids = rt.setdefault("order_ids", [])
        if oid and oid not in ids:
            ids.append(oid)
        if ost in _LIVE_STOP:
            one = self._only_live_stop(s, rec, oid)
            if not one:
                self._rt_page(rec, "reprotect_not_only", f"[{s}] swing ratchet: re-protection {cid} live but "
                                                         f"{'orders unreadable' if one is None else 'not the only live stop'}"
                                                         f" — waiting", today)
                _save_state(state)
                return
            rec.update(stop_order_id=oid, stop_px=float(rt.get("reprotect_px") or rec.get("stop_px") or 0),
                       qty=int(rt.get("reprotect_qty") or rec.get("qty") or 0), unprotected=False)
            self._rt_status(rt, "idle", f"re-protected ({ost})")
            _save_state(state)
            self._rt_log(s, rec, "reprotected", ost, price=rt.get("reprotect_px"), client_order_id=cid)
            return
        if _is_terminal(ost):
            if float(rt.get("reprotect_px") or 0) > 0:
                rec["stop_px"] = float(rt["reprotect_px"])                    # the level that filled
            if self._book_order_fills(s, rec, state, o, today) or rec.get("status") == "closed":
                self._rt_status(rt, "done", f"re-protection {ost} — lot closed")
                _save_state(state)
                return
            seq = rt.get("reprotect_seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                self._rt_page(rec, "bad_reprotect_id", f"[{s}] swing ratchet: re-protection sequence {seq!r} invalid — "
                                                       f"NO stop placed; set one manually", today)
                _save_state(state)
                return
            # the next id is recorded now, so the next lookup reads the next order, never this terminal one again
            rt.update(reprotect_seq=seq + 1, reprotect_coid=reprotect_coid(rec.get("coid"), seq + 1) or "")
            _save_state(state)
            self._rt_log(s, rec, "reprotect_retry", f"re-protection {cid} {ost}", price=rt.get("reprotect_px"))
            if allow_submit:
                self._ratchet_reprotect_if_naked(s, rec, state, rt, params, today, f"re-protection {ost}")
            return
        self._rt_page(rec, "reprotect_not_final", f"[{s}] swing ratchet: re-protection {cid} is {ost} — waiting",
                      today)
        _save_state(state)

    def status(self) -> dict:
        try:
            return _load_state()
        except Exception as e:
            return {"error": repr(e)}


# One manager per process (run_cycle is single-threaded; the manager holds no per-cycle state in memory).
MANAGER = SwingBreakoutManager()
