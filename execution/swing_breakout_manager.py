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
  Entry     = a pending setup whose session open AND live ask are at or above its breakout level → a marketable
              DAY limit (first cycle >= 10:05 ET). An open or ask below the level waits (never entered below it).
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

LOGGING: every entry, skip and exit writes its decision stack to trade_events.jsonl (Rule D).
FAIL-SAFE: SWING_BREAKOUT_ENABLED False → no new entries; lots already held keep their stops and managed exits. Any error in one symbol aborts THAT
  symbol only and never raises into run_cycle.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime
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
            risk = (float(rec.get("entry_px") or 0) - float(rec.get("stop_px") or 0)) * float(rec.get("qty") or 0)
        risk = float(risk)
        if not (_finite(pnl, risk) and risk > 0):
            return None
        return float(pnl) / risk
    except (TypeError, ValueError):
        return None


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
                                          "entry_px_estimated")} | {"status": "closed"})


def kelly_risk(lots: list[dict], prior: Optional[dict]) -> dict:
    """Risk per lot r (fraction of equity) and the loss multiple, from this tier's closed lots once
    KELLY_MIN_SAMPLE_SIZE have a known R, else from the replay prior. r = SWING_BREAKOUT_KELLY_FRACTION × max(0,
    mean R − SE) / var R, never above SWING_BREAKOUT_MAX_RISK_PCT. Loss multiple = the mean loss, in R, of lots that
    exited on the protective stop (gap-throughs cost more than 1R), never below 1. No live sample and no prior → r 0
    (the caller's one-share floor still trades the setup)."""
    rs = [x for x in (lot_r(rec) for rec in lots) if x is not None and math.isfinite(x)]
    stop_rs = [x for rec in lots if rec.get("exit_reason") == "protective_stop"
               for x in [lot_r(rec)] if x is not None and math.isfinite(x)]
    out: dict[str, Any] = {"n_live": len(rs), "n_live_stopped": len(stop_rs)}
    try:
        import statistics
        if len(rs) >= int(config.KELLY_MIN_SAMPLE_SIZE):
            n, mean, sd, src = len(rs), statistics.fmean(rs), statistics.stdev(rs), "fills"
            lm = -statistics.fmean(stop_rs) if stop_rs else float((prior or {}).get("loss_mult_stopped") or 1.0)
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
    """A held lot's loss at its stop in $ (risk_usd recorded at entry, else (entry − stop) × qty); 0 when unknown."""
    try:
        v = rec.get("risk_usd")
        if v is None:
            v = (float(rec.get("entry_px") or 0) - float(rec.get("stop_px") or 0)) * float(rec.get("qty") or 0)
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
        trade_logger.log_event(event, symbol=symbol, data_source="alpaca_data", tier=TIER,
                               setup="c2_breakout_55d", family_id=FAMILY_ID,
                               hypothesis_version=HYPOTHESIS_VERSION, change=CHANGE, **kw)
    except Exception as e:
        logger.warning("[%s] swing_breakout trade_logger write failed: %s", symbol, e)


_TERMINAL = ("filled", "canceled", "cancelled", "rejected", "expired", "done_for_day")


def _is_terminal(status: Any) -> bool:
    """True for a final order status. 'partially_filled' is NOT terminal (it also ends in 'filled')."""
    st = str(status or "").lower().split(".")[-1]
    return st in _TERMINAL


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
                      ask=ask, qty=int(qty))
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
                   opened_ts=time.time(), risk_usd=round(int(filled) * (px - stop_px), 2))
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
        stop_px = float(rec.get("stop_px") or 0.0)
        o: Any = None
        if stop_px > 0:
            try:
                o = broker.submit_gtc_stop_order(symbol, qty, "sell", stop_px, tier=TIER, allow_cancel_blocking=False)
            except Exception as e:
                logger.warning("[%s] breakout stop submit raised: %s", symbol, e)
                o = None
        held_sentinel = getattr(broker, "PROTECTION_ALREADY_HELD", None)
        if o is not None and held_sentinel is not None and o is held_sentinel:
            # a stable live reducing order already holds these shares (typically this lot's own stop that a read
            # could not see) — protected; nothing cancelled. The next cycle re-checks.
            rec.update(status="open", qty=qty, unprotected=False, stop_order_id=self._find_resting_stop(symbol, qty))
            _save_state(state)
            logger.info("[%s] breakout stop not placed: shares already held by a live reducing order (%s)",
                        symbol, rec["stop_order_id"] or "id not found")
            return True
        if o is not None and getattr(o, "id", None):
            rec.update(status="open", qty=qty, unprotected=False, stop_order_id=str(getattr(o, "id", "")))
            _save_state(state)
            logger.info("[%s] breakout lot protected: %d sh, GTC stop %.2f", symbol, qty, stop_px)
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

    @staticmethod
    def _last_trade(symbol: str) -> float:
        """The last trade price (T1 Alpaca Data), or NaN when unavailable or older than SWING_LAST_TRADE_MAX_AGE_S.
        Price-movement decisions (stop breached, trend break) use the last trade, never the IEX bid/ask, which swings
        wildly (CEO 2026-10-09; 10/07-08 Day-tier logs: MU's IEX spread went $3.50 -> $37.86 two minutes apart). A
        stale print (board Harris+Taleb: a thin name's last IEX print can be hours old) counts as unavailable, so the
        caller retries next cycle instead of deciding — and latching the day — on an old price."""
        try:
            from datetime import timezone
            from data.alpaca_data import get_latest_trade_with_time
            r = get_latest_trade_with_time(symbol)
            if r is None:
                return float("nan")
            px, ts = float(r[0]), r[1]
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            max_age = float(_cfg("SWING_LAST_TRADE_MAX_AGE_S", 180))  # PROV:swing-last-trade-2026-10-09
            if not (-5.0 <= age <= max_age):
                logger.warning("[%s] breakout last trade %.2f is %.0fs old (> %.0fs) — treated as unavailable",
                               symbol, px, age, max_age)
                return float("nan")
            return px if math.isfinite(px) and px > 0 else float("nan")
        except Exception as e:
            logger.warning("[%s] breakout last-trade read failed: %s", symbol, e)
            return float("nan")

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
        if not self._near_close(now):
            return []
        today = now.strftime("%Y-%m-%d")
        closed: list[str] = []
        for s, rec in list(state["positions"].items()):
            if rec.get("status") != "open" or rec.get("exit_checked") == today:
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
            final = self._final_fill(stop_id)
            if final is None:
                # the stop is confirmed cancelled but its fill is unreadable: never sell blind (a fired stop plus
                # a close would short the name). _reconcile resolves it next cycle.
                rec["status"] = "exit_unverified"
                _page_throttled(rec, f"[{symbol}] breakout exit ({reason}) HALTED — stop cancelled but its fill is "
                                     f"unreadable; the lot may be UNPROTECTED — resolving each cycle")
                _save_state(state)
                return False
            filled, px = final
            qty = int(rec.get("qty") or 0)
            if filled >= qty > 0 and px > 0:   # the stop fired during the cancel
                self._book_exit(symbol, rec, state, "protective_stop", px, filled)
                return True
            if 0 < filled < qty:
                self._book_partial(symbol, rec, state, px, filled, "protective_stop_partial", clear_key="stop_order_id")
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
                self._book_exit(symbol, rec, state, reason, px, filled, **ctx)
                return True
            if 0 < filled < qty and px > 0:
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
        entry = float(rec.get("entry_px") or 0.0)
        # an ESTIMATED entry price never produces a P&L number (never fabricate — booked unknown instead)
        pnl = (round((px - entry) * filled, 2) if entry > 0 and px > 0 and not rec.get("entry_px_estimated")
               else None)
        if pnl is not None:
            rec["partial_pnl"] = round(float(rec.get("partial_pnl") or 0.0) + pnl, 2)
        else:
            rec["partial_pnl_unknown"] = True
        rec["qty"] = max(int(rec.get("qty") or 0) - int(filled), 0)
        if clear_key:
            rec[clear_key] = ""
        _save_state(state)
        _log("partial_exit", symbol, price=px, size=int(filled), exit_reason=reason, realized_pnl=pnl,
             trade_id=rec.get("coid"), entry_px_estimated=bool(rec.get("entry_px_estimated")))

    def _book_exit(self, symbol: str, rec: dict, state: dict, reason: str, px: float, qty: int, **ctx) -> None:
        """Final exit. realized_pnl = this fill's P&L + every partial booked earlier (None if any part is unknown)."""
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
            final = self._final_fill(stop_id)
            qty = int(rec.get("qty") or 0)
            if final is not None and final[0] >= qty > 0 and final[1] > 0:
                self._book_exit(symbol, rec, state, "protective_stop", final[1], final[0])
                return
        close_id = str(rec.get("close_order_id") or "")
        if close_id:
            # a restart between our close submit and its booking: book the REAL close fill, not "unknown"
            # (board execution seat 2026-10-02)
            r = self._settle(close_id)
            if r is not None and r[2] and r[0] > 0 and r[1] > 0:
                reason = str(rec.get("exit_reason_pending") or "close")
                if r[0] >= int(rec.get("qty") or 0):
                    self._book_exit(symbol, rec, state, reason, r[1], r[0])
                    return
                self._book_partial(symbol, rec, state, r[1], r[0], f"{reason}_partial", clear_key="close_order_id")
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
                qty = int(rec.get("qty") or 0)
                stop_id = str(rec.get("stop_order_id") or "")
                if not stop_id or rec.get("unprotected"):
                    self._reprotect(s, rec, state, qty)
                    continue
                r = self._read_order(stop_id)
                if r is None:
                    continue                       # unreadable this cycle: no action (stop presumed resting)
                filled, px, terminal = r
                if filled >= qty > 0 and px > 0:
                    self._book_exit(s, rec, state, "protective_stop", px, filled)
                    continue
                if terminal:
                    # the stop ended without covering the lot (cancelled by another path, expired, rejected)
                    if 0 < filled < qty and px > 0:
                        self._book_partial(s, rec, state, px, filled, "protective_stop_partial", clear_key="stop_order_id")
                    rec["stop_order_id"] = ""
                    _page_throttled(rec, f"[{s}] breakout stop {stop_id} ENDED with {filled}/{qty} filled — re-protecting")
                    self._reprotect(s, rec, state, int(rec.get("qty") or 0))
                    continue
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
            r = self._settle(oid)
            if r is None or not r[2]:
                return                                   # still unreadable / working: retry next cycle
            filled, px, _ = r
            qty = int(rec.get("qty") or 0)
            if filled >= qty > 0 and px > 0:
                self._book_exit(symbol, rec, state, label, px, filled)
                return
            if 0 < filled < qty and px > 0:
                self._book_partial(symbol, rec, state, px, filled, f"{label}_partial", clear_key=key)
            rec[key] = ""
            _save_state(state)
        if self._reprotect(symbol, rec, state, int(rec.get("qty") or 0), depth=1):
            _page(f"[{symbol}] breakout lot re-protected ({rec.get('qty')} sh, GTC stop {rec.get('stop_px')}) after an unverified exit")

    def status(self) -> dict:
        try:
            return _load_state()
        except Exception as e:
            return {"error": repr(e)}


# One manager per process (run_cycle is single-threaded; the manager holds no per-cycle state in memory).
MANAGER = SwingBreakoutManager()
