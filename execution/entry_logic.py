# ruff: noqa: E501, E701, E702, B023, F401  — pre-existing: inherited from trade_engine.py extraction
"""
execution/entry_logic.py — Phase 2 Extraction 11

Entry execution logic extracted from execution/trade_engine.py.
Owns:
  - execute_entries()          — signal → order submission (RTH)
  - _overnight_entry_check()  — overnight swing dry-run scanner
  - FVG confluence helpers    — _find_recent_fvgs, _score_fvg_for_signal, _compute_fvg_mult
  - _write_confirm_gate_json  — confirm gate persistence shim

All main.py module-level globals are accessed via lazy ``import main as _main``
inside the function bodies that need them. Phase 3 will complete the decoupling.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
import numpy as _np
from datetime import datetime, timedelta, date  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import config
import data.bar_cache as _bar_cache
from alerts import (
    alert_entry, alert_gtc_failed, alert_stale_bar,
    alert_systemic_stale_feed, send_slack,
)
import events.catalyst_engine as _catalyst
from data.alpaca_data import get_latest_trade, get_latest_quote  # noqa: F401
from data.fetcher import fetch_bars
from data.premarket import calculate_atr
from data.sectors import SECTOR_MAP as _SECTOR_MAP
from events.handlers import get_halt_entries as _get_halt_entries
from execution.broker import (
    get_account,
    get_open_orders,
    get_open_position,
    get_order,
    get_portfolio_value,
    get_short_blocked_symbols,
    is_market_open,
    submit_gtc_stop_order,
    submit_limit_order,
    submit_market_order,
)
from execution.lifecycle import (
    get_shorting_enabled as _get_shorting_enabled,
    set_shorting_enabled as _set_shorting_enabled,
)
from execution.orphan_manager import get_tod_phase as _get_tod_phase
from execution.risk_manager import RiskManager
from execution.portfolio_tracker import PortfolioTracker
from execution import param_engine as _param_engine
from indicators.momentum import get_momentum_summary
from strategy.scoring import (
    rc8_clear_buffers as _rc8_clear_buffers_fn,
)
from strategy.signal_generator import run_scan
from execution.exit_logic import _cancel_open_gtc_orders  # noqa: F401
from execution.quarterly_hold_manager import get_quarterly_hold_symbols
from execution import reentry_cooldown as _reentry_cooldown
from execution import counter_trend as _counter_trend

if TYPE_CHECKING:
    from events.calendar import EventCalendar
    from events.macro_risk_index import MacroRiskIndex
    from execution.kelly import KellySizer
    from main import GateState

ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


def _find_recent_fvgs(df_ohlc, max_look_back: int = 30, max_zone_width_pct: float = 0.03) -> list:
    """
    Scan the last max_look_back bars for FVG patterns.
    Returns list of dicts: {type, zone_low, zone_high, filled, bar_idx}

    Bullish FVG: bar[n-1].high < bar[n+1].low  — gap up, potential support
    Bearish FVG: bar[n-1].low  > bar[n+1].high — gap down, potential resistance

    Unfilled: no subsequent bar has traded into the gap zone.
    Handles lowercase column names from fetch_bars (Alpaca Data T1).
    """
    # Normalize column names (fetch_bars returns lowercase; MultiIndex guard removed —
    # Alpaca Data never returns MultiIndex, and yfinance is no longer used for bars)
    col_lower = {c.lower(): c for c in df_ohlc.columns}
    high_col = col_lower.get("high", "High")
    low_col  = col_lower.get("low",  "Low")

    fvgs     = []
    n_bars   = len(df_ohlc)
    start    = max(0, n_bars - max_look_back)

    for i in range(start, n_bars - 2):
        try:
            a_high = float(df_ohlc[high_col].iloc[i])
            a_low  = float(df_ohlc[low_col].iloc[i])
            c_high = float(df_ohlc[high_col].iloc[i + 2])
            c_low  = float(df_ohlc[low_col].iloc[i + 2])
        except Exception as _fvg_e:
            logger.debug("FVG parse error at bar index %s: %s", i, _fvg_e)
            continue

        # Bullish FVG: gap between A.high and C.low
        if a_high < c_low:
            z_lo, z_hi = a_high, c_low
            if z_lo > 0 and (z_hi - z_lo) / z_lo <= max_zone_width_pct:
                filled = any(
                    float(df_ohlc[low_col].iloc[j]) <= z_hi
                    for j in range(i + 3, n_bars)
                )
                fvgs.append({"type": "bullish", "zone_low": z_lo, "zone_high": z_hi,
                              "filled": filled, "bar_idx": i})

        # Bearish FVG: gap between C.high and A.low
        if c_high < a_low:
            z_lo, z_hi = c_high, a_low
            if z_lo > 0 and (z_hi - z_lo) / z_lo <= max_zone_width_pct:
                filled = any(
                    float(df_ohlc[high_col].iloc[j]) >= z_lo
                    for j in range(i + 3, n_bars)
                )
                fvgs.append({"type": "bearish", "zone_low": z_lo, "zone_high": z_hi,
                             "filled": filled, "bar_idx": i})

    return fvgs


def _score_fvg_for_signal(fvgs: list, direction: str, price: float,
                           approach_pct: float = 0.0075) -> int:
    """
    Given a list of FVGs, return:
      +1  most recent unfilled FVG is same-direction AND price is in/approaching zone
      -1  most recent unfilled FVG is counter-direction AND price is in/approaching zone
       0  no relevant FVG near current price
    """
    unfilled = [f for f in fvgs if not f["filled"]]
    if not unfilled:
        return 0

    fvg = max(unfilled, key=lambda f: f["bar_idx"])  # most recent
    z_lo, z_hi = fvg["zone_low"], fvg["zone_high"]

    in_zone     = z_lo <= price <= z_hi
    approaching = (abs(price - z_hi) / price <= approach_pct or
                   abs(price - z_lo) / price <= approach_pct)

    if not (in_zone or approaching):
        return 0

    same_dir = (fvg["type"] == "bullish" and direction == "long") or \
               (fvg["type"] == "bearish" and direction == "short")
    return 1 if same_dir else -1


def _compute_fvg_mult(symbol: str, direction: str, price: float,
                      daily_df=None) -> tuple:
    """
    Compute FVG size multiplier for a given symbol/direction/price.
    Returns (multiplier: float, reason: str | None).
    Fail-safe: returns (1.0, None) on any error.
    """
    _MAX_WIDTH  = 0.03
    _APPROACH   = 0.0075
    _LOOK_DAILY = 30
    _LOOK_1H    = 20

    daily_hit  = 0
    hourly_hit = 0

    # ── Daily FVG (reuses already-fetched daily_df) ───────────────────────────
    try:
        if daily_df is not None and len(daily_df) >= 3:
            fvgs = _find_recent_fvgs(daily_df, max_look_back=_LOOK_DAILY,
                                     max_zone_width_pct=_MAX_WIDTH)
            daily_hit = _score_fvg_for_signal(fvgs, direction, price, _APPROACH)
            if daily_hit != 0:
                logger.debug(f"[{symbol}] Daily FVG hit: {'+1' if daily_hit > 0 else '-1'}")
    except Exception as _de:
        logger.debug(f"[{symbol}] Daily FVG error: {_de}")

    # ── 1h FVG (T1 Alpaca Data API — 5 trading days of hourly bars) ─────────
    # DATA-2a: replaced yfinance download with fetch_bars(TF_1H).
    # Alpaca returns clean lowercase columns — no MultiIndex, no socket timeout wrapper.
    try:
        _h_df = fetch_bars(symbol, config.TF_1H, num_bars=35)
        if _h_df is not None and len(_h_df) >= 3:
            fvgs = _find_recent_fvgs(_h_df, max_look_back=_LOOK_1H,
                                     max_zone_width_pct=_MAX_WIDTH)
            hourly_hit = _score_fvg_for_signal(fvgs, direction, price, _APPROACH)
            if hourly_hit != 0:
                logger.debug(f"[{symbol}] 1h FVG hit: {'+1' if hourly_hit > 0 else '-1'}")
    except Exception as _he:
        logger.debug(f"[{symbol}] 1h FVG error: {_he}")

    # ── Multiplier ────────────────────────────────────────────────────────────
    if daily_hit == -1 or hourly_hit == -1:
        return 0.85, "counter-direction FVG"
    elif daily_hit == 1 and hourly_hit == 1:
        return 1.15, "dual-TF same-direction FVG"
    elif daily_hit == 1:
        return 1.08, "daily same-direction FVG"
    elif hourly_hit == 1:
        return 1.08, "1h same-direction FVG"
    return 1.0, None


def _write_confirm_gate_json(ecb: dict, cs: dict) -> None:
    """Persist confirm gate state to disk. Called at end of each entry cycle."""
    try:
        from state.persistence import write_confirm_gate
        write_confirm_gate(ecb, cs)
    except Exception as _e_cg:
        logger.warning(f"[confirm_gate] Failed to persist gate state: {_e_cg}")


def _check_portfolio_correlation(
    symbol: str,
    open_syms: list,
    threshold: float = 0.70,
    lookback: int = 20,
) -> bool:
    """Block if candidate rho(daily,20d) > threshold with any open position. Fail-closed."""
    if not open_syms:
        return False
    try:
        bars_new = fetch_bars(symbol, config.TF_DAILY, num_bars=lookback + 3)
    except Exception as _corr_e_new:
        logger.warning("[%s] correlation gate: fetch failed for candidate — BLOCK: %s", symbol, _corr_e_new)
        return True
    if bars_new is None or bars_new.empty or len(bars_new) < (lookback + 1):
        logger.warning("[%s] correlation gate: insufficient data (%d bars) — BLOCK", symbol, len(bars_new) if bars_new is not None else 0)
        return True
    try:
        ret_new = bars_new["close"].pct_change().dropna().iloc[-lookback:]
    except Exception as _corr_e_ret:
        logger.warning("[%s] correlation gate: returns failed for candidate — BLOCK: %s", symbol, _corr_e_ret)
        return True
    for open_sym in open_syms:
        try:
            bars_open = fetch_bars(open_sym, config.TF_DAILY, num_bars=lookback + 3)
        except Exception as _corr_e_open:
            logger.warning("[%s] correlation gate: fetch failed for %s — BLOCK: %s", symbol, open_sym, _corr_e_open)
            return True
        if bars_open is None or bars_open.empty or len(bars_open) < (lookback + 1):
            logger.warning("[%s] correlation gate: insufficient data for %s — BLOCK", symbol, open_sym)
            return True
        try:
            ret_open = bars_open["close"].pct_change().dropna().iloc[-lookback:]
        except Exception as _corr_e_reto:
            logger.warning("[%s] correlation gate: returns failed for %s — BLOCK: %s", symbol, open_sym, _corr_e_reto)
            return True
        aligned_new, aligned_open = ret_new.align(ret_open, join="inner")
        if len(aligned_new) < max(lookback // 2, 10):
            logger.warning("[%s] correlation gate: too few aligned bars with %s (%d) — BLOCK", symbol, open_sym, len(aligned_new))
            return True
        rho = float(_np.corrcoef(aligned_new.values, aligned_open.values)[0, 1])
        if _np.isnan(rho):
            logger.warning("[%s] correlation gate: NaN rho with %s — BLOCK", symbol, open_sym)
            return True
        if rho > threshold:
            logger.warning("[%s] CORRELATION GATE BLOCK: rho(daily,20d)=%.2f > %.2f with %s — skipping entry.", symbol, rho, threshold, open_sym)
            return True
        logger.debug("[%s] correlation gate: rho=%.2f with %s (threshold=%.2f)", symbol, rho, open_sym, threshold)
    return False


def execute_entries(
    signals: list,
    risk: RiskManager,
    tracker: PortfolioTracker,
    kelly: "KellySizer",
    mri: "MacroRiskIndex",
    size_multiplier: float = 1.0,
    news_size_mult: float = 1.0,
    vix: float = 0.0,
    spy_risk_direction: str = "",
    gate_state: Optional["GateState"] = None,
):
    """
    Walk the ranked signal list and place orders until position limits are hit.
    size_multiplier: combined multiplier from event risk + regime + TOD + SPY risk.
    news_size_mult: SPY risk multiplier (0.75 or 0.5) — used to tighten stops.
    spy_risk_direction: "down" | "up" | "extreme" | "" — drives direction gates.
      down    → SPY dropped >1% in last 5-min bar → block new longs
      up      → SPY surged >1% in last 5-min bar  → block new shorts
      extreme → blocked at run_cycle level — should not reach here
    Uses Kelly for dynamic per-signal-type risk sizing.
    gate_state: Phase 0.5 — GateState instance (conviction_streak + entry_confirm_buffer).
    """
    import main as _main  # lazy: avoids circular import at module load
    if gate_state is None:
        raise RuntimeError(
            "execute_entries() called without gate_state — Phase 0.5 refactor incomplete"
        )
    entered: list[str] = []

    # H-6: if _safe_close_all was called this session (NEWS HALT), block all entries
    if _get_halt_entries():
        logger.warning("execute_entries: HALTED for session — no new entries permitted.")
        return entered

    # MN-AH-GATE: block entries after market close — fail-closed (DS+GAI audit 2026-04-29)
    try:
        _clock_open = is_market_open()
    except Exception as _e_clock:
        logger.warning(f"execute_entries: is_market_open() failed — assuming market closed (fail-closed): {_e_clock}")
        _clock_open = False
    if not _clock_open:
        logger.warning("execute_entries: market closed (after-hours gate) — skipping all entries.")
        return entered

    # ── Bug 3 fix: live shorting pre-flight ─────────────────────────────────
    # _SHORTING_ENABLED is set at startup and updated each cycle by run_cycle's
    # per-cycle recheck — but Alpaca's execution-time check can differ (buying
    # power constraints, paper account RTH state). Do ONE live get_account()
    # the first time a short signal is encountered per cycle and cache the result.
    # Feeds back into _SHORTING_ENABLED so the next cycle's main filter is accurate.
    _short_live_verified = None   # None=unchecked, True/False=result for this cycle

    def _verify_shorting_live() -> bool:
        nonlocal _short_live_verified
        # B1 fix: _SHORTING_ENABLED now in execution/lifecycle.py — use setter
        if _short_live_verified is not None:
            return _short_live_verified  # type: ignore[unreachable]
        try:
            _sv_acct  = get_account()
            _sv_bp    = float(getattr(_sv_acct, "buying_power", 0))
            _sv_flag  = bool(getattr(_sv_acct, "shorting_enabled", False))
            _short_live_verified = _sv_flag and _sv_bp >= 2000
            if not _short_live_verified:
                _set_shorting_enabled(False)    # B1: lifecycle.py module state
                logger.warning(
                    f"Shorting pre-flight FAILED — buying_power=${_sv_bp:.2f}, "
                    f"shorting_enabled={_sv_flag}. Blocking shorts this cycle."
                )
            else:
                if not _get_shorting_enabled():
                    _set_shorting_enabled(True)  # B1: lifecycle.py module state
                    logger.info(
                        f"Shorting pre-flight OK — buying_power=${_sv_bp:.2f}. "
                        f"_SHORTING_ENABLED re-enabled."
                    )
        except Exception as _sv_err:
            _short_live_verified = False
            logger.warning(
                f"Shorting pre-flight check error: {_sv_err} — blocking shorts this cycle."
            )
        return _short_live_verified
    # ────────────────────────────────────────────────────────────────────────

    # ── Rule 1: Premarket Red Lockout ────────────────────────────────────────────
    # If ≥75% of tracked premarket movers (those that moved ≥2%) are RED,
    # block all long entries for the session. A sea of red premarket = broad
    # macro selloff — going long into that is fighting the tape with no edge.
    # Shorts and leveraged inverse ETFs (SQQQ, etc.) remain permitted.
    #
    # On April 2: 23/24 premarket movers were red. Bot entered XOM + CVX long.
    # This rule would have blocked both.
    _longs_blocked_rule1 = False
    _pm_detail_r1: dict  = {}
    try:
        from data.premarket import get_premarket_detail as _get_pm_detail
        _pm_detail_r1 = _get_pm_detail()
        if _pm_detail_r1:
            _pm_signif  = {k: v for k, v in _pm_detail_r1.items() if abs(v) >= 2.0}
            _pm_red     = [v for v in _pm_signif.values() if v <= -2.0]
            if _pm_signif and len(_pm_signif) >= 5:
                _red_ratio = len(_pm_red) / len(_pm_signif)
                if _red_ratio >= 0.75:
                    _longs_blocked_rule1 = True
                    logger.critical(
                        f"🔴 RULE 1 — PREMARKET RED LOCKOUT: "
                        f"{len(_pm_red)}/{len(_pm_signif)} tracked movers red ≥2% "
                        f"({_red_ratio:.0%}) — ALL LONGS BLOCKED this session. "
                        f"Shorts + inverse ETFs only. (April 2: 23/24 were red.)"
                    )
    except Exception as _r1_err:
        logger.debug(f"Rule 1 premarket red check failed: {_r1_err}")

    _cycle_bar_ages: list = []  # P2-2: collect bar ages this cycle for systemic latency check

    # ── RC-8: helper to clear stale conviction buffers on external gate blocks ──
    # Defined OUTSIDE the for loop (GAI anti-pattern fix: was re-compiled every iteration).
    # Called whenever a symbol is blocked by a constraint EXTERNAL to signal quality
    # (sector gate, portfolio cap, earnings calendar, analyst data, etc.)
    # Includes immediate atomic JSON persistence (DS Condition 1): prevents stale disk state
    # from being restored after a watchdog restart, which would allow a blocked symbol to
    # re-enter on the next cycle before its buffers have been legitimately rebuilt.
    # NOT called on: BoD-1 confirm gate
    # (intentional accumulation), or score minimum gate (auto-resets at lines 813-816).
    # _rc8_clear_buffers extracted to strategy/scoring.py (Phase 2 B2 fix).
    # Calls below use _rc8_clear_buffers_fn(sym, reason, gate_state) — same semantics.
    def _rc8_clear_buffers(sym: str, reason: str) -> None:
        """Shim — delegates to strategy.scoring.rc8_clear_buffers (Phase 2)."""
        _rc8_clear_buffers_fn(sym, reason, gate_state)

    # P0-CYCLE-SYNC-GUARD → ROBUST ALPACA-AUTHORITATIVE RECONCILE (2026-08-01, Rafael).
    # PRIMARY: reconcile risk.open_positions to Alpaca's authoritative live count of NON-QHM
    # positions — heals BOTH an over-count (a close that removed the tracker entry WITHOUT
    # register_close(); the old tracker-only guard REFUSED to decrement, silently blocking a
    # real entry slot until restart) and an under-count. Alpaca /v2/positions is ground truth.
    # FALLBACK (Alpaca read failed): the prior tracker-based sync-UP-only guard — never blindly
    # decrement on a tracker-only signal (a stale tracker at 0 must not wipe a correct high count).
    if risk is not None:
        _qhm_syms = get_quarterly_hold_symbols()
        if not risk.reconcile_open_positions_from_alpaca(_qhm_syms):
            _tracker_open_count = sum(
                1 for sym, t in (tracker.open_trades or {}).items()
                if t.get("status") != "closed" and sym not in _qhm_syms
            )
            if _tracker_open_count > risk.open_positions:
                logger.warning(
                    f"[CYCLE-SYNC] Alpaca reconcile unavailable; Tracker ({_tracker_open_count}) "
                    f"> RiskManager ({risk.open_positions}) — syncing UP to tracker count."
                )
                risk.open_positions = _tracker_open_count
            elif _tracker_open_count < risk.open_positions:
                logger.critical(
                    f"[CYCLE-SYNC] Alpaca reconcile unavailable; STATE DESYNC: Tracker "
                    f"({_tracker_open_count}) < RiskManager ({risk.open_positions}). Ignoring "
                    f"tracker — decreases must happen via register_close() or an Alpaca reconcile. "
                    f"Manual reconciliation required."
                )
                # DO NOT update risk.open_positions downward on a tracker-only signal

    # Pre-compute power-hour expansion gate once per cycle — prevents inconsistent application
    # across symbols in scans that straddle the 3:30 PM ET boundary (Data Integrity board fix)
    _now_ph_cyc   = datetime.now(ET)
    _mins_ph_cyc  = _now_ph_cyc.hour * 60 + _now_ph_cyc.minute
    _is_ph_cyc    = _mins_ph_cyc >= config.TOD_EXPANSION_WINDOW_START

    # ── Catalyst entry-gate cache (1b) — loaded ONCE per cycle (BGG: Gro+GAI+cold seat). ──
    # DARK until _catalyst.CATALYST_GATE_ENABLED. On a cache fault (missing/malformed/stale) we
    # block NOTHING (never-mask: a data fault is not a trade decision) and fire ONE CRITICAL alert
    # this cycle. Per-symbol blocking (below) reads this pre-loaded map — no hot-path network.
    _cat_on     = _catalyst.CATALYST_GATE_ENABLED
    _cat_state: dict = _catalyst.read_cached_state() if _cat_on else {"ok": True, "active": {}}
    _cat_active: dict[str, dict] = _cat_state.get("active", {})
    if _cat_on and not _cat_state.get("ok"):
        try:
            send_slack(f":rotating_light: CATALYST CACHE FAULT — {_cat_state.get('reason')} — "
                       f"catalyst entry-gate INACTIVE this cycle (blocking nothing).")
        except Exception as _cat_alert_e:
            logger.critical("CATALYST CACHE FAULT (alert failed): %s | %s",
                            _cat_state.get("reason"), _cat_alert_e)

    for sig in signals:
        symbol    = sig["symbol"]
        direction = sig["direction"]
        trade_mode = sig["trade_mode"]
        score      = sig["score"]
        # Mean-reversion dispatch flag (item 2 diff 3d). MR RESPECTS the market/safety gates that
        # run before this is read (QHM, catalyst, Rule1/2, SPY-direction — the SPY-down gate IS the
        # "suppress MR longs in a selloff" guard) and BYPASSES the trend-structure gates below
        # (ORB, 12-pt conviction/confirm, counter_trend, the 0-12 linear size map).
        _is_mr = sig.get("strategy") == "mean_reversion"

        # QHM exclusion: skip intraday entries for buy-and-hold held symbols
        if symbol in get_quarterly_hold_symbols():
            _rc8_clear_buffers(symbol, "qhm-hold")
            continue

        # ── Catalyst gate (1b, DARK until CATALYST_GATE_ENABLED) ──────────
        # Block a NEW entry into a name with an active high-severity NEGATIVE stock-specific
        # catalyst (offering/dilution/guidance-cut/solvency/probe). Reads the once-per-cycle
        # cache loaded above; a cache fault already alerted + blocks nothing (never-mask). This
        # is ENTRY-ONLY — it never runs in check_exits/safe_close_all/QHM, so it cannot force a
        # held position (incl. a never-sell tier) to close. Blocking = bounded missed entry;
        # the alternative is the RIVN case (bought into a dilutive offering). (Rafael 2026-07-14)
        if _cat_on and _cat_state.get("ok"):
            _cat_hit = _catalyst.blocking_catalyst_for(symbol, _cat_active)
            if _cat_hit is not None:
                logger.warning(
                    "[%s] CATALYST BLOCK [%s]: %s — refusing new entry.",
                    symbol, _cat_hit.get("type"), (_cat_hit.get("headline") or "")[:80],
                )
                _rc8_clear_buffers(symbol, "catalyst-negative")
                continue

        # ── Rule 1 enforcement ───────────────────────────────────────────
        if _longs_blocked_rule1 and direction == "long":
            _is_inverse_etf = symbol in getattr(config, "LEVERAGED_TICKERS", set())
            if not _is_inverse_etf:
                logger.info(
                    f"[{symbol}] Rule 1 BLOCKED: no longs in premarket red session "
                    f"(≥75% of movers red). Short or inverse ETF signals only."
                )
                _rc8_clear_buffers(symbol, "rule1-pm-red")
                continue

        # ── Rule 2: Gap-Down Settling Window ─────────────────────────────
        # If this symbol gapped down ≥3% premarket, block LONG entries for
        # the first 30 minutes of RTH (9:30–10:00 ET).
        # Gap-down bounces at RTH open are a momentum reversal trap: institutions
        # exiting into strength, not new buyers entering. Expected return: negative.
        # On April 2: XOM was -4.75% premarket, scored 12/12 at RTH open → entered long → lost.
        if direction == "long" and _pm_detail_r1:
            _pm_pct = _pm_detail_r1.get(symbol, 0.0)
            if _pm_pct <= -3.0:
                _td = timedelta  # module-level import
                _now_et     = datetime.now(ET)
                _rth_open   = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                _settle_end = _rth_open + _td(minutes=30)
                if _now_et < _settle_end:
                    logger.warning(
                        f"[{symbol}] Rule 2 BLOCKED: gap-down {_pm_pct:+.1f}% premarket — "
                        f"30-min RTH settling window active until "
                        f"{_settle_end.strftime('%I:%M %p ET')}. "
                        f"Bounce entry in gap-down structure has negative expected return."
                    )
                    _rc8_clear_buffers(symbol, "rule2-gap-down-settle")
                    continue

        # ── SPY Direction Gate (Market Reaction Engine) ───────────────────
        # Driven by SPY 5-min bar movement computed in run_cycle().
        # This is the market-reaction-first entry filter — no keyword logic.
        #
        #   spy_risk_direction == "down": SPY dropped >1% last 5-min bar
        #     → block all new longs (tape just moved against long bias)
        #     → inverse ETFs (SQQQ etc.) and short signals pass through
        #
        #   spy_risk_direction == "up": SPY surged >1% last 5-min bar
        #     → block all new shorts (tape just moved against short bias)
        #     → long signals and leveraged ETFs pass through
        _is_lev_etf = symbol in getattr(config, "LEVERAGED_TICKERS", set())

        if spy_risk_direction == "down" and direction == "long" and not _is_lev_etf:
            logger.info(
                f"[{symbol}] SPY DIRECTION GATE [DOWN]: SPY moved "
                f"{_main._spy_risk_magnitude:+.2f}% — no new longs while tape is selling off."
            )
            _rc8_clear_buffers(symbol, "spy-direction-down")
            continue

        if spy_risk_direction == "up" and direction == "short":
            logger.info(
                f"[{symbol}] SPY DIRECTION GATE [UP]: SPY moved "
                f"{_main._spy_risk_magnitude:+.2f}% — no new shorts while tape is surging."
            )
            _rc8_clear_buffers(symbol, "spy-direction-up")
            continue

        # ── ORB Gate: SPY must have confirmed a breakout above/below opening range ──
        # Board vote 2026-05-17, 26/26 unanimous. Fail-closed per config.ORB_BLOCK_ON_FEED_FAILURE.
        # DS/GAI P0: single datetime.now(ET) call to prevent TOCTOU minute-boundary straddle.
        # MR bypass: ORB is a trend-BREAKOUT confirmation, antithetical to a mean-reversion entry.
        if config.ORB_ENABLED and not _is_mr:
            _now_orb_et   = datetime.now(ET)
            _now_mins_orb = _now_orb_et.hour * 60 + _now_orb_et.minute
            _past_compute = _now_mins_orb >= config.ORB_COMPUTE_AFTER_MIN
            _orb_computed = bool(_main._orb_computed_date)
            if _main._orb_feed_failed and config.ORB_BLOCK_ON_FEED_FAILURE:
                logger.warning(
                    f"[{symbol}] ORB gate BLOCK: SPY feed failure — BLOCK_ALL entries for session (fail-closed)"
                )
                _rc8_clear_buffers(symbol, "orb-feed-failure")
                continue
            if not _orb_computed:
                # Before 9:55 AM: opening window already blocks entries; this is defence-in-depth.
                # After 9:55 AM: cycle gap — compute should have fired but didn't. Fail-closed.
                logger.warning(
                    f"[{symbol}] ORB gate BLOCK: ORB not yet computed — fail-closed"
                )
                _rc8_clear_buffers(symbol, "orb-not-computed")
                continue
            if _orb_computed and not _main._orb_feed_failed:
                _spy_cls = _main._spy_last_close
                if direction == "long" and _spy_cls <= _main._orb_high:
                    logger.info(
                        f"[{symbol}] ORB gate BLOCK (long): SPY {_spy_cls:.2f} ≤ ORB_H {_main._orb_high:.2f}"
                        f" — no confirmed breakout above range"
                    )
                    _rc8_clear_buffers(symbol, "orb-long-no-breakout")
                    continue
                if direction == "short" and _spy_cls >= _main._orb_low:
                    logger.info(
                        f"[{symbol}] ORB gate BLOCK (short): SPY {_spy_cls:.2f} ≥ ORB_L {_main._orb_low:.2f}"
                        f" — no confirmed breakdown below range"
                    )
                    _rc8_clear_buffers(symbol, "orb-short-no-breakdown")
                    continue

        # ── BoD-2: Block 3x leveraged ETF longs during EXTREME / BROAD_GEO_CONFLICT ──
        # Leveraged ETFs amplify 3x drawdowns in panic/geo regimes.
        # Shorts on 3x are allowed — they hedge the very regime that triggered the block.
        if (_main._spy_event_type in ("EXTREME", "BROAD_GEO_CONFLICT", "BROAD_GEO_ENERGY", "BROAD_MACRO_SYSTEMIC")
                and direction == "long"
                and symbol in getattr(config, "LEVERAGED_3X_TICKERS", set())):
            logger.warning(
                f"[{symbol}] 3x ETF LONG BLOCKED [{_main._spy_event_type}]: "
                f"SPY {_main._spy_risk_magnitude:+.2f}% — refusing leveraged long in panic regime."
            )
            _rc8_clear_buffers(symbol, "bod2-3x-etf-extreme-regime")
            continue

        # ── Leveraged-ETF flag (Bucket A/B collapsed 2026-07-15) ─────────
        is_leveraged = symbol in config.LEVERAGED_TICKERS

        # ── BoD-1: Entry confirmation buffer ─────────────────────────────
        # Simons: single qualifying scans can be noise; require 2 consecutive
        # qualifying scans (≥CONVICTION_SKIP_BELOW) before committing a day
        # trade.  Counter resets on any sub-threshold scan — streak unbroken.
        # MR bypass: the entry-confirm buffer is keyed to the 0-12 conviction score; MR carries a
        # 0-3 score and has its own confirmed-reversal trigger, so it neither feeds nor reads this.
        if not _is_mr:
            if score >= config.CONVICTION_SKIP_BELOW:
                gate_state.entry_confirm_buffer[symbol] = gate_state.entry_confirm_buffer.get(symbol, 0) + 1
            else:
                gate_state.entry_confirm_buffer[symbol] = 0
            logger.debug(
                f"[{symbol}] entry_confirm={gate_state.entry_confirm_buffer[symbol]}/2 "
                f"(score={score}/{config.CONVICTION_SKIP_BELOW})"
            )
        # Leveraged ETFs are LONG ONLY — not shortable on Alpaca (inverse ETF = the "short")
        if is_leveraged and direction == "short":
            logger.info(f"[{symbol}] Leveraged ETF long-only — skipping short signal")
            _rc8_clear_buffers(symbol, "leveraged-short-skip")
            continue

        # ── Live shorting pre-flight (first short per cycle only) ────────
        if direction == "short" and not _verify_shorting_live():
            logger.info(
                f"[{symbol}] Short skipped — live shorting pre-flight failed this cycle."
            )
            _rc8_clear_buffers(symbol, "shorting-preflight-fail")
            continue

        # ── Conviction gate ──────────────────────────────────────────────
        # MR bypass: the 0-12 conviction MIN would always skip an MR signal (0-3 score); MR
        # eligibility IS its own conviction (confirmed reversal + regime + score >= MR_MIN_SCORE).
        if not _is_mr and score < config.CONVICTION_SKIP_BELOW:
            logger.info(
                f"[{symbol}] Score {score}/12 below minimum "
                f"(need ≥{config.CONVICTION_SKIP_BELOW}) — skipping"
            )
            continue
        # BoD-1: 2-scan confirmation gate — ALL buckets (unchanged). MR bypass: keyed to the
        # 12-pt confirm buffer above, which MR does not populate.
        _confirm = gate_state.entry_confirm_buffer.get(symbol, 0)
        _min_confirm = _param_engine.h4_entry_confirm_scans(vix, is_leveraged)
        if not _is_mr and _confirm < _min_confirm:
            logger.info(
                f"[{symbol}] BoD-1 CONFIRM GATE: scan {_confirm}/{_min_confirm} qualifying "
                f"(score={score}/12 ≥{config.CONVICTION_SKIP_BELOW}) — "
                f"need {_min_confirm - _confirm} more scan(s) before entry."
            )
            continue

        # ── Position limit check ─────────────────────────────────────────
        # Standard RTH limit: BUCKET_B_MAX_POSITIONS (3)
        # Power-hour expansion (≥TOD_EXPANSION_WINDOW_START=3:30 PM ET): BUCKET_B_MAX_POSITIONS_POWER (5)
        #   Power-hour: score ≥ CONVICTION_FULL_MIN (11) required for slots 4-5
        #   Kill-switch active: expansion unconditionally blocked
        _is_ph         = _is_ph_cyc                               # pre-computed once per cycle (DI board fix)
        _open_count    = risk.open_positions                       # FIX BUG-PH-3: authoritative counter
        _std_limit     = config.BUCKET_B_MAX_POSITIONS            # 3
        _ph_limit      = config.BUCKET_B_MAX_POSITIONS_POWER      # 5

        if not risk.can_open_position():
            # FIX BUG-PH-1: kill-switch is unconditional — must check BEFORE expansion logic.
            # can_open_position() returns False for BOTH kill-switch AND position-limit;
            # expansion is only valid for position-limit case.
            if risk.check_kill_switch():
                logger.warning(
                    f"[{symbol}] Kill switch active — stopping entries (expansion blocked)."
                )
                break
            # Standard position-limit (not kill-switch).
            # Power-hour expansion: allow up to _ph_limit slots with score gate.
            if _is_ph and _open_count < _ph_limit:
                _ph_score_req = config.CONVICTION_FULL_MIN
                if score >= _ph_score_req:
                    logger.info(
                        f"[{symbol}] Power-hour slot expansion: "
                        f"{_open_count}/{_std_limit} → {_ph_limit}, "
                        f"score {score}/{sig['max_score']} ≥ {_ph_score_req} — allowing slot"
                    )
                    # FIX BUG-PH-4: re-validate before fall-through (count may shift mid-cycle)
                    if risk.open_positions >= _ph_limit:
                        logger.warning(
                            f"[{symbol}] Power-hour limit reached on re-check "
                            f"({risk.open_positions}/{_ph_limit}) — stopping entries."
                        )
                        break
                    # Fall through to entry logic — intentional power-hour bypass
                else:
                    logger.info(
                        f"[{symbol}] Power-hour expansion denied: "
                        f"score {score} < {_ph_score_req}. Position limit reached."
                    )
                    break
            else:
                logger.warning(
                    f"[{symbol}] Position limit reached "
                    f"({_open_count}/{_std_limit}) — stopping entries."
                )
                break

        # ── #12c: Opposite-direction signal during RTH → immediate exit ─────────
        # If we hold this symbol in the OPPOSITE direction at high conviction,
        # the new signal represents a thesis reversal — exit the existing position
        # now rather than waiting for the stop to be hit.
        # Gate: RTH only (not AH); signal must be ≥ CONVICTION_FULL_MIN (11/12).
        if tracker.is_in_trade(symbol):
            _open_trade = tracker.open_trades.get(symbol, {})
            _open_dir   = _open_trade.get("direction")
            _in_rth     = _get_tod_phase() in ("normal", "power_hour")
            _is_opposite = _open_dir and _open_dir != direction
            if _is_opposite and _in_rth and score >= config.CONVICTION_FULL_MIN:
                logger.warning(
                    f"[{symbol}] #12c OPPOSITE SIGNAL: holding {_open_dir}, "
                    f"new {direction} signal score {score}/12 ≥ {config.CONVICTION_FULL_MIN} — "
                    f"exiting existing position immediately."
                )
                try:
                    _exit_side  = "sell" if _open_dir == "long" else "buy"
                    _exit_qty   = _open_trade.get("qty_remaining") or _open_trade.get("qty", 1)
                    _cancel_open_gtc_orders(symbol, _open_trade, tracker)
                    _exit_order = submit_market_order(symbol=symbol, qty=_exit_qty, side=_exit_side)
                    if _exit_order:
                        # Fetch fill price — 3× retry at 1s intervals (mirrors entry fill pattern L1293-1327).
                        # RC-4: never use entry_price as exit fill — retry until confirmed or exhaust retries.
                        _exit_price = _open_trade.get("entry_price", 0)  # pre-fill fallback only
                        try:
                            for _12c_attempt in range(3):
                                time.sleep(1.0)
                                _filled_12c = get_order(str(_exit_order.id))  # type: ignore[attr-defined]
                                if _filled_12c and getattr(_filled_12c, "filled_avg_price", None):
                                    _exit_price = float(_filled_12c.filled_avg_price)
                                    break
                            else:
                                logger.warning(
                                    f"[{symbol}] #12c fill price unavailable after 3 polls — "
                                    f"using entry_price=${_exit_price:.2f} as fallback. "
                                    f"P&L estimate may be wrong; review fill manually."
                                )
                        except Exception as _e12c_fill:
                            logger.error(
                                f"[{symbol}] #12c fill price fetch failed — using entry_price={_exit_price} "
                                f"as fallback (P&L estimate may be wrong): {_e12c_fill}"
                            )
                        if _exit_price == 0:
                            logger.error(
                                f"[{symbol}] #12c exit_price is 0 — P&L recorded as $0, "
                                f"kill switch may not reflect actual loss. Manual review required."
                            )
                        pnl_12c = tracker.record_exit(symbol, _exit_price, reason="opposite_signal",
                                                      mri_level=mri.level() if mri else "NORMAL")
                        risk.register_close(pnl_12c or 0.0)
                        logger.warning(
                            f"[{symbol}] #12c exit complete — "
                            f"fill ${_exit_price:.2f} | PnL ${pnl_12c:.2f}"
                        )
                    else:
                        logger.error(f"[{symbol}] #12c exit order submission failed — skipping entry.")
                except Exception as _e12c:
                    logger.error(f"[{symbol}] #12c exit error: {_e12c}")
                # Either way — do not enter the new position this cycle.
                # The scanner will regenerate the signal on the next cycle with fresh data.
                _rc8_clear_buffers(symbol, "12c-exit")  # RC-8: clear buffers on all exit paths
                continue
            else:
                # Same direction or low-conviction opposite — normal skip
                continue

        # Verify no existing Alpaca position
        if get_open_position(symbol):
            logger.info(f"[{symbol}] Already has Alpaca position — skipping.")
            continue

        # ── Re-entry cooldown (2026-08-01, BGG) ──────────────────────────────
        # NEW ENTRIES ONLY. Placed AFTER the is_in_trade/#12c block AND the Alpaca-position
        # check, so it can NEVER block a #12c defensive exit of an existing OPPOSITE position
        # (board catch 2026-08-01: the earlier placement would have swallowed a short signal
        # meant to defensively exit a held long). At this point the symbol is confirmed NOT
        # held (tracker + Alpaca), so this purely gates a genuine RE-ENTRY. If this
        # (symbol, direction) was STOPPED OUT earlier in TODAY's session, do not reload the
        # same losing side (SMCI: re-shorted a rising stock on every scan). Opposite-direction
        # reversal still allowed; other symbols unaffected. State inferred from the
        # already-persisted tracker.closed_trades (survives restart for free); FAIL-OPEN.
        # See execution/reentry_cooldown.py.
        _cd_blocked, _cd_trade = _reentry_cooldown.is_in_cooldown(
            symbol, direction, tracker.closed_trades
        )
        if _cd_blocked:
            _cd_reason = (_cd_trade or {}).get("exit_reason", "stop")
            logger.info(
                f"[{symbol}] RE-ENTRY COOLDOWN: {direction} was stopped out today "
                f"({_cd_reason}) — not reloading the same losing side this session "
                f"(opposite direction still allowed)."
            )
            _rc8_clear_buffers(symbol, "reentry-cooldown")
            continue

        # Get current price + daily ATR for stop sizing
        try:
            df = fetch_bars(symbol, config.TF_15M, num_bars=2)
            if df.empty:
                continue
            entry_price = float(df["close"].iloc[-1])   # bar-close fallback

            # ── Fix-C: Real-time entry price via Alpaca Data REST API ────────
            # DATA-2b: replaced yfinance fast_info with get_latest_trade().
            # Alpaca /v2/stocks/{symbol}/trades/latest returns the actual SIP
            # last trade with millisecond precision — strictly better than the
            # ~1 min yfinance delay. Falls back to bar close on any failure.
            # All signal computation (MACD, RSI, EMA) continues to use Alpaca bars.
            try:
                _live = get_latest_trade(symbol)
                if _live and _live > 0:
                    _gap = abs(_live - entry_price) / max(entry_price, 0.01) * 100
                    logger.debug(
                        f"[{symbol}] Live price ${_live:.2f} "
                        f"(bar close ${entry_price:.2f}, gap={_gap:.2f}%)"
                    )
                    entry_price = _live
            except Exception as _fxc_e:
                logger.debug(f"[{symbol}] Live price fetch skipped ({_fxc_e}) — using bar close.")

            # ── C-3: Price sanity check (CLAUDE.md Guardrail 5) ──────────────
            # A data glitch returning $0.01 or $99,999 would submit a full
            # market order at that price. Reject before any downstream sizing.
            # Absolute bounds: $0.50–$99,999 (covers all WATCHLIST symbols).
            if entry_price < 0.50 or entry_price > 99_999:
                logger.critical(
                    f"[{symbol}] PRICE SANITY FAIL: entry_price=${entry_price:.2f} "
                    f"out of bounds [$0.50, $99,999] — skipping entry."
                )
                _rc8_clear_buffers(symbol, "price-sanity-fail")
                continue
            # ── TB-2: Bar staleness guard ─────────────────────────────────────
            # If the most recent 15-min bar is >25 min old (from close time) during
            # RTH, the feed is lagging or the symbol has gone dark. Entering on
            # genuinely stale data risks fills far from the quoted price — skip.
            _bar_ts = df.index[-1]
            if hasattr(_bar_ts, "tzinfo") and _bar_ts.tzinfo is None:
                _bar_ts = _bar_ts.tz_localize("UTC")
            _bar_ts_et = _bar_ts.astimezone(ET)
            _now_et    = datetime.now(ET)
            # ── Bar age: measure from BAR CLOSE, not bar open ─────────────────
            # Alpaca indexes 15m bars by their OPEN time. Measuring age from open
            # adds a systematic +15 min phantom error — a bar that closed 5 min ago
            # reads as 20 min old. Add the 15m bar duration to convert open→close.
            _stale_td = timedelta  # module-level import
            _bar_close_ts_et = _bar_ts_et + _stale_td(minutes=15)
            _bar_age_min = (_now_et - _bar_close_ts_et).total_seconds() / 60
            _cycle_bar_ages.append(_bar_age_min)  # P2-2: always collect, even if not stale
            _rth_open   = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            _rth_close  = _now_et.replace(hour=16, minute=0, second=0, microsecond=0)
            _grace_end  = _now_et.replace(hour=9, minute=45, second=0, microsecond=0)
            _in_rth     = _rth_open <= _now_et <= _rth_close
            # Stale threshold — age measured from bar CLOSE (not open).
            # 25 min from close = bar closed 25+ min ago = >2 bar periods behind.
            # 35 min from close during 9:30–9:45 open grace (first bar takes longer).
            _in_open_grace   = _rth_open <= _now_et < _grace_end
            _stale_threshold = 35 if _in_open_grace else 25
            if _in_rth and _bar_age_min > _stale_threshold:
                logger.warning(
                    f"[{symbol}] STALE BAR: last 15m bar is {_bar_age_min:.1f} min old "
                    f"(>{_stale_threshold} min threshold"
                    f"{' — open grace window' if _in_open_grace else ''}) — skipping entry."
                )
                alert_stale_bar(symbol, _bar_age_min)
                _rc8_clear_buffers(symbol, "stale-bar")
                continue

        except Exception as e:
            logger.warning(f"[{symbol}] Could not get entry price: {e}")
            continue

        # Fetch daily ATR for dynamic stop sizing (Finding 7: per-symbol cache, 4h TTL)
        # Cache stores daily_df so FVG scoring, HTF earnings check, and ATR-compression
        # bypass all work correctly on hits — not just atr_value/rvol_20d.
        atr_value = None
        rvol_20d  = 0.0
        daily_df  = None   # Build #15: init here so FVG compute can reference it safely
        _cached = _bar_cache.get_atr(symbol)
        if _cached:
            # Cache hit — restore all daily values
            daily_df  = _cached["daily_df"]
            rvol_20d  = _cached["rvol_20d"]
            atr_value = _cached["atr_pct"] * entry_price
            logger.debug(
                f"[{symbol}] ATR cache hit "
                f"(age={(time.time() - _cached['ts']) / 3600:.1f}h)"
            )
        else:
            try:
                # num_bars covers BOTH the ATR window (ATR uses the last ATR_PERIOD bars) AND the
                # counter-trend gate's 1-month (MOMENTUM_SHORT_LOOKBACK=21) return, which needs
                # ≥ N+2 bars. Extra bars never change the ATR (it reads the tail) — 2026-08-02.
                daily_df = fetch_bars(
                    symbol, config.TF_DAILY,
                    num_bars=max(config.ATR_PERIOD + 5,
                                 getattr(config, "MOMENTUM_SHORT_LOOKBACK", 21) + 3),
                )
                if not daily_df.empty:
                    _atr_pct  = calculate_atr(daily_df, config.ATR_PERIOD) / 100
                    atr_value = _atr_pct * entry_price
                    rvol_20d  = get_momentum_summary(daily_df).get("realized_vol_20d", 0) or 0
                    _bar_cache.set_atr(symbol, atr_pct=_atr_pct, rvol_20d=rvol_20d, daily_df=daily_df)
            except Exception as _atr_e:
                # U-4: Log ATR fetch failure — previously silent, stop/target was less
                # precise and no record existed in the log to diagnose it.
                logger.warning(
                    f"[{symbol}] ATR fetch failed — falling back to fixed % stops. "
                    f"Stop and target will be less precise. Error: {_atr_e}"
                )

        # S49 board + meta-audit (Quant Logic unanimous): if ATR unavailable after fetch,
        # skip entry entirely. Fallback to fixed % stops + 1-share floor is worse than
        # no entry — it produces undefined position sizing with no volatility context.
        # RC-8: clear confirm buffer so symbol can re-qualify on next scan.
        if atr_value is None:
            logger.warning(
                "[%s] ATR unavailable — skipping entry. "
                "Cannot size position without volatility data.",
                symbol,
            )
            _rc8_clear_buffers(symbol, "atr-unavailable")
            continue

        # C-3 deviation check: >20% from prior daily close is a spike, not a move.
        # Placed here (after daily_df fetch) so prior-close is guaranteed available.
        if daily_df is not None and not daily_df.empty and len(daily_df) > 1:
            _prev_close = float(daily_df["close"].iloc[-2])
            if _prev_close > 0 and abs(entry_price / _prev_close - 1) > 0.20:
                logger.critical(
                    f"[{symbol}] PRICE SANITY FAIL: entry_price=${entry_price:.2f} "
                    f"deviates >20% from prior close=${_prev_close:.2f} — skipping entry."
                )
                _rc8_clear_buffers(symbol, "c3-deviation")
                continue

        # ── Counter-trend bounce / falling-knife gate (2026-08-02, BGG) ──────────
        # The 12-pt confluence gives ~5/12 points from LONG-TERM structure (below/above 150/200
        # SMA + 12-1 momentum), so a crashed name scores 10-12 SHORT permanently and the bot
        # shorts the RIP (SMCI +15% one-month bounce, stopped 3x). Block a SHORT that is
        # structurally bearish AND up over the last month (bouncing), and the mirror LONG (a
        # structurally-bullish name down over the last month = falling knife). Parameter-free
        # (reuses MOMENTUM_SHORT_LOOKBACK — the reversal window 12-1 momentum skips); scoped to
        # an elevated structural score so a FRESH breakdown is not gated; FAIL-OPEN. Runs AFTER
        # the is_in_trade/#12c + Alpaca-position checks (new entries only — never blocks a
        # defensive exit). LIVE with daily decision-impact logging (grep COUNTER-TREND GATE).
        # See execution/counter_trend.py. Kill switch: config.COUNTER_TREND_GATE_ENABLED
        # (default True) — flip to False + restart to disable instantly, matching the codebase's
        # GEX_ENABLED / ORB_ENABLED / DELTA_SCORING_ENABLED feature-gate convention.
        # MR bypass: counter_trend BLOCKS long-into-falling-knife / short-into-bounce — which is
        # exactly the MR setup. The MR path is the complement that GENERATES that reversal trade.
        _ct_blocked, _ct_ctx = (
            _counter_trend.counter_trend_block(direction, sig, daily_df)
            if (getattr(config, "COUNTER_TREND_GATE_ENABLED", True) and not _is_mr)
            else (False, {})
        )
        if _ct_blocked:
            logger.warning(
                "[%s] COUNTER-TREND GATE [%s]: 1-month return %+.2f%% opposes the trade on a "
                "structurally-%s name (score=%s) — skipping (%s). ctx=%s",
                symbol, direction, _ct_ctx.get("mom_1m_pct", 0.0),
                "bearish" if direction == "short" else "bullish",
                _ct_ctx.get("score"),
                "don't short a bounce" if direction == "short" else "don't long a falling knife",
                _ct_ctx,
            )
            _rc8_clear_buffers(symbol, "counter-trend-bounce")
            continue

        # Calculate ATR-based stop and target
        _h2_scalar = _param_engine.h2_stop_atr_mult(symbol, vix)  # None when VIX unavailable
        # ATH proximity for the 0.90x scalar in get_stop_and_target()
        _spy_ath_pct = (
            (_main._spy_52w_high - _main._spy_last_close) / _main._spy_52w_high * 100
            if _main._spy_52w_high > 0 and _main._spy_last_close > 0
            else 99.0
        )
        stop, target = risk.get_stop_and_target(
            entry_price, direction, trade_mode,
            atr_value=atr_value, symbol=symbol,
            rvol_20d=rvol_20d, vix=vix,
            atr_mult_override=_h2_scalar,
            spy_ath_dist_pct=_spy_ath_pct,
        )

        # Tighten stop during HIGH_RISK news (reduces max loss on new trades)
        stop = risk.get_news_adjusted_stop(stop, entry_price, direction, news_size_mult)

        # ── Earnings HTF directional check ────────────────────────────────
        # FMP earnings calendar check at entry time (T2 — see DATA-4 below).
        # No hard block — instead: evaluate weekly+daily HTF technicals.
        #   HTF strongly aligns with signal direction → 0.5x size, enter.
        #   HTF neutral or conflicts with signal      → skip.
        # Earnings are binary; we only take the trade if the multi-day trend
        # already leans the same direction as the signal.
        _earnings_mult = 1.0
        try:
            # DATA-4: FMP API earnings calendar (T2) — replaces yfinance .calendar.
            # Use batch cache (preloaded at pre-market) to avoid per-entry API calls.
            from data.fmp_client import get_cached_earnings_dates
            _today_earn = datetime.now(ET).date()
            _earn_dates = get_cached_earnings_dates(symbol)   # list[datetime.date]

            _near_earnings = False
            for _ed in _earn_dates[:2]:
                try:
                    if abs((_ed - _today_earn).days) <= 3:
                        _near_earnings = True
                        break
                except Exception as _earn_e:
                    logger.debug("[%s] Earnings date check error: %s", symbol, _earn_e)

            if _near_earnings:
                # HTF evaluation — reuse already-fetched daily_df (20d window)
                _htf_direction = "neutral"
                if daily_df is not None and not daily_df.empty and len(daily_df) >= 20:
                    _cl = daily_df["close"].values
                    _sma10 = float(_cl[-10:].mean())
                    _sma20 = float(_cl[-20:].mean())
                    _last  = float(_cl[-1])
                    # RSI-14 (simplified)
                    _chg     = [_cl[i] - _cl[i - 1] for i in range(1, len(_cl))]
                    _avg_g   = sum(max(c, 0) for c in _chg[-14:]) / 14
                    _avg_l   = sum(max(-c, 0) for c in _chg[-14:]) / 14
                    _rsi14   = 100 - (100 / (1 + _avg_g / max(_avg_l, 0.0001)))
                    _htf_bull = (_last > _sma10 > _sma20) and (_rsi14 > 52)
                    _htf_bear = (_last < _sma10 < _sma20) and (_rsi14 < 48)
                    if _htf_bull and not _htf_bear:
                        _htf_direction = "long"
                    elif _htf_bear and not _htf_bull:
                        _htf_direction = "short"

                if _htf_direction == "neutral":
                    logger.info(
                        f"[{symbol}] EARNINGS SKIP: earnings within 3 days, "
                        f"HTF daily is neutral — binary event, no clear edge. Skipping."
                    )
                    _rc8_clear_buffers(symbol, "earnings-htf-neutral")
                    continue
                elif _htf_direction != direction:
                    logger.warning(
                        f"[{symbol}] EARNINGS CONFLICT: signal={direction.upper()} but "
                        f"HTF daily trend={_htf_direction.upper()} into earnings. "
                        f"Skipping — HTF opposes signal direction."
                    )
                    _rc8_clear_buffers(symbol, "earnings-htf-conflict")
                    continue
                else:
                    _earnings_mult = 0.5
                    logger.info(
                        f"[{symbol}] EARNINGS ALIGNED: {direction.upper()} signal matches "
                        f"HTF daily (SMA10={_sma10:.2f} SMA20={_sma20:.2f} RSI={_rsi14:.0f}) "
                        f"— entering at 0.5x size."
                    )
        except Exception as _earn_e:
            logger.debug(f"[{symbol}] Earnings HTF check skipped: {_earn_e}")

        # ── Analyst sentiment gate (T2 FMP grades proxy) ──────────────────────
        # BEARISH analyst sentiment = net downgrade pressure → skip entry.
        # Uses pre-loaded _main._analyst_sentiment cache; no live API call here.
        # Only blocks on BEARISH. BULLISH is informational only (logged).
        _analyst_data = _main._analyst_sentiment.get(symbol, {})
        if not isinstance(_analyst_data, dict):
            logger.warning("[%s] _analyst_sentiment value is not a dict (%s) — treating as NEUTRAL", symbol, type(_analyst_data).__name__)
            _analyst_data = {}
        _analyst_sig = _analyst_data.get("signal", "NEUTRAL")
        if _analyst_sig == "BEARISH":
            _a_net = _analyst_data.get("net_score", 0)
            logger.info(
                f"[{symbol}] ANALYST BEARISH (net={_a_net:+.2f}) — "
                f"analyst downgrade pressure, skipping entry."
            )
            _rc8_clear_buffers(symbol, "analyst-bearish")
            continue
        elif _analyst_sig == "BULLISH":
            logger.debug(
                f"[{symbol}] analyst BULLISH (net={_analyst_data.get('net_score', 0):+.2f})"
            )

        # ── AB-2: R:R minimum gate (upgraded from Bug 5 — 2.0→2.5 Tudor Jones) ──
        # Block entry if reward-to-risk ratio is below 2.5x (5:1 framework).
        # VIX stop-widening can push stops out without expanding targets,
        # collapsing R:R to 1.3 or lower — mathematically insufficient for
        # positive expectancy at any reasonable win rate.
        # ATR-compression bypass (Dalio): if ATR is below the 20-day median,
        # the tight stop is a product of low-vol compression not poor setup
        # geometry — relax the threshold to 2.0x in that case.
        # Leveraged ETFs exempt: their target/stop multipliers already account
        # for the asymmetry and the gate would over-filter them.
        _is_leveraged_any = symbol in getattr(config, "LEVERAGED_TICKERS", set()) or \
                            symbol in getattr(config, "LEVERAGED_3X_TICKERS", set())
        if not _is_leveraged_any and stop and target:
            _risk_dist   = abs(entry_price - stop)
            _reward_dist = abs(target - entry_price)
            _rr          = round(_reward_dist / _risk_dist, 2) if _risk_dist > 0 else 0
            # ATR-compression bypass: atr_value < 50th-pct of recent ATR means
            # the stop is mechanically tight due to low vol, not bad setup geometry.
            # Use a looser 2.0x floor rather than the standard 2.5x in that case.
            _atr_compressed = (
                atr_value is not None
                and daily_df is not None
                and not daily_df.empty
                and len(daily_df) >= 20
                and atr_value < float(daily_df["close"].pct_change().abs().rolling(20).median().iloc[-1] * entry_price)
            )
            _rr_min = 2.0  # board vote Apr 6 2026: 11–3 — gate aligned with paper profile R:R (stop=1.25×ATR, target=2.5×ATR → R:R=2.0)
            if _rr < _rr_min:
                _compress_tag = " [ATR-compressed, relaxed to 2.0x]" if _atr_compressed else ""
                logger.warning(
                    f"[{symbol}] AB-2 R:R BLOCKED: {_rr:.2f} < {_rr_min:.1f} minimum{_compress_tag} "
                    f"(entry=${entry_price:.2f} stop=${stop:.2f} target=${target:.2f}) "
                    f"— skipping entry."
                )
                _rc8_clear_buffers(symbol, "rr-below-minimum")
                continue

        # ── Bug 8: Sector correlation gate ──────────────────────────────
        # Block entry if an open position already exists in the same sector.
        # Prevents concentrated same-sector exposure (e.g. XOM + CVX long
        # into an energy selloff). Symbols not in _SECTOR_MAP or mapped to
        # None (ETFs, leveraged) are always allowed through.
        # Bug 3 fix: log a debug warning when a symbol bypasses as "unknown"
        # so the gap shows up in logs and the map can be expanded iteratively.
        _this_sector = _SECTOR_MAP.get(symbol)
        if symbol not in _SECTOR_MAP:
            logger.debug(
                f"[{symbol}] Sector unknown — not in _SECTOR_MAP. "
                f"Bypassing sector gate as uncorrelated. Add to _SECTOR_MAP to enforce."
            )
        if _this_sector is not None:
            _sector_conflict = None
            for _open_sym, _open_trade in tracker.open_trades.items():
                if _open_sym == symbol:
                    continue
                if _open_trade.get("status") == "closed":
                    continue
                if _SECTOR_MAP.get(_open_sym) == _this_sector:
                    _sector_conflict = _open_sym
                    break
            if _sector_conflict:
                logger.warning(
                    f"[{symbol}] Sector correlation BLOCKED: already holding "
                    f"{_sector_conflict} in sector '{_this_sector}' — "
                    f"skipping to avoid concentrated exposure."
                )
                _rc8_clear_buffers(symbol, "sector-correlation")
                continue

        # ── Portfolio pairwise correlation gate ─────────────────────────
        # threshold: 0.70 (VIX < 25, normal) | 0.55 (VIX >= 25, stress)
        # Fail-closed: block on data errors or NaN. Source: T1 Alpaca daily bars.
        _open_active_syms = [
            sym for sym, trd in tracker.open_trades.items()
            if trd.get("status") != "closed" and sym != symbol
        ]
        _corr_threshold = max(0.40, 0.55 - (vix - 25.0) * 0.010) if vix >= 25.0 else 0.70
        if _open_active_syms and _check_portfolio_correlation(
            symbol, _open_active_syms, threshold=_corr_threshold
        ):
            _rc8_clear_buffers(symbol, "portfolio-correlation")
            continue

        # ── Unified position sizing (Bucket A/B collapsed 2026-07-15, board+Gro+GAI) ──
        # ALL symbols — including leveraged ETFs — size via conviction-linear + Kelly + TQI,
        # then the shared TSMOM/earnings/VOTE-3/FVG multiplier chain below. Leveraged names
        # then get a hard notional clamp AFTER the multipliers (LEVERAGED_NOTIONAL_MAX_PCT) —
        # the ring-fence replacing the deleted Bucket-A 15%/leverage cap (board FORK-5). The
        # `if True:` keeps the body in place (no risky mass de-indent) while running it for
        # every symbol; the old `if is_bucket_a: … else:` routing is removed.
        # ── MR sizing (item 2 diff 3d) — base allocation × MR-Kelly-scale × MR_SIZE_MULT(0.5) ──
        # Routes through the SAME downstream tail (TSMOM/FVG/min-lot/share-conv/VOTE-5/Kelly-share-
        # clamp/BP/gross) as trend — NO duplicated sizing routine (masked-loss seat). The per-trade
        # Kelly SHARE clamp bounds MR risk at KELLY_MAX_RISK_PCT. get_risk_pct gets score=12 (a
        # 0-12-scale value) NOT the 0-3 MR score, and strategy="mean_reversion" for the dedicated key.
        # NOTE (masked-loss seat): MR_SIZE_MULT is a NOTIONAL 0.5x. For MR's characteristic WIDE stops
        # (crashed names, 5-10%+ ATR), the shared Kelly SHARE clamp binds and per-trade RISK sits at
        # the FULL KELLY_MAX_RISK_PCT cap — do not assume 0.5×cap per trade in aggregate reasoning.
        if _is_mr:
            dollar_cap = risk.portfolio_value * config.INTRA_ALLOCATION_PCT
            _portfolio_val = get_portfolio_value()
            try:
                _mrk = kelly.get_risk_pct(direction, trade_mode, _portfolio_val, score=12, strategy="mean_reversion")
                dollar_cap *= (_mrk / max(config.MAX_PORTFOLIO_RISK_PCT, 0.001)) * float(getattr(config, "MR_SIZE_MULT", 0.5))
                logger.info(
                    f"[{symbol}] MR sizing: base {config.INTRA_ALLOCATION_PCT:.0%} × MR-Kelly({_mrk:.2%}) "
                    f"× {getattr(config, 'MR_SIZE_MULT', 0.5)}x = ${dollar_cap:,.2f} (mr_score={sig.get('mr_score')}/3)"
                )
            except Exception as _mke:
                dollar_cap *= float(getattr(config, "MR_SIZE_MULT", 0.5))
                logger.warning(f"[{symbol}] MR Kelly sizing failed — base × MR_mult: {_mke}")
            if dollar_cap <= 0:
                logger.info(f"[{symbol}] MR: no allocation (dollar_cap<=0). Skipping.")
                continue

        if not _is_mr:
            # Rank 2: conviction sizing — linear
            #   score 10  → ½ size  (42.5%)
            #   score 11+ → full    (85%)
            _pct_min  = config.INTRA_ALLOCATION_PCT * 0.5   # 42.5%
            _pct_max  = config.INTRA_ALLOCATION_PCT          # 85%
            if score < _main._LINEAR_SCORE_MIN:
                dollar_cap = 0.0
            elif score >= _main._LINEAR_SCORE_MAX:
                dollar_cap = risk.portfolio_value * _pct_max
            else:
                _fraction  = (score - _main._LINEAR_SCORE_MIN) / (_main._LINEAR_SCORE_MAX - _main._LINEAR_SCORE_MIN)
                _pct       = _pct_min + _fraction * (_pct_max - _pct_min)
                dollar_cap = risk.portfolio_value * _pct
                logger.info(
                    f"[{symbol}] Linear sizing: score {score}/12 → "
                    f"{_pct:.1%} of ${risk.portfolio_value:,.2f} = ${dollar_cap:,.2f}"
                )

            if dollar_cap == 0:
                logger.info(f"[{symbol}] Score {score}/12 — no allocation. Skipping.")
                continue

            # H-8: Kelly scaling on top of linear allocation
            try:
                _portfolio_val = get_portfolio_value()
                kelly_risk_pct = kelly.get_risk_pct(direction, trade_mode, _portfolio_val, score=score)
                kelly_scale    = kelly_risk_pct / max(config.MAX_PORTFOLIO_RISK_PCT, 0.001)
                dollar_cap     = dollar_cap * kelly_scale
                logger.debug(
                    f"[{symbol}] Kelly scale: {kelly_scale:.3f} "
                    f"({kelly_risk_pct:.2%} vs {config.MAX_PORTFOLIO_RISK_PCT:.2%} baseline)"
                )
            except Exception as _ke:
                logger.warning(f"[{symbol}] Kelly scaling failed — using base position size: {_ke}")

            # ── AB-3: TQI → Kelly confidence demotion ────────────────────────
            # López de Prado: rolling TQI < 40/100 (10-trade avg) indicates model
            # is in a degraded regime — step Kelly confidence down 20% per σ below 50.
            # Thorp's feedback-loop guard: require ≥10 trades before demoting
            # (stdev is unreliable on n<10) and floor at 0.5x.
            if len(kelly._tqi_history) >= 10:
                import statistics as _stats_ab3
                _tqi_avg = sum(kelly._tqi_history) / len(kelly._tqi_history)
                if _tqi_avg < 50:
                    try:
                        _tqi_std  = _stats_ab3.stdev(kelly._tqi_history) or 1.0
                        _tqi_sigmas_below = (50 - _tqi_avg) / _tqi_std
                        _tqi_kelly_adj = max(0.5, 1.0 - 0.20 * _tqi_sigmas_below)
                    except Exception as _tqi_std_err:
                        logger.warning("[%s] TQI stdev calc failed — using fallback divisor=15: %s",
                                       symbol, _tqi_std_err)
                        _tqi_kelly_adj = max(0.5, 1.0 - 0.20 * ((50 - _tqi_avg) / 15))
                    dollar_cap *= _tqi_kelly_adj
                    logger.info(
                        f"[{symbol}] AB-3 TQI demotion: rolling {len(kelly._tqi_history)}-trade avg "
                        f"{_tqi_avg:.1f}/100 < 50 → Kelly adj {_tqi_kelly_adj:.2f}x | "
                        f"dollar_cap → ${dollar_cap:,.2f}"
                    )

        # TSMOM vol-scaling (board vote 2026-04-22, 17-0 — sizing + logging, no scoring)
        # Scales dollar_cap by target_vol/ewma_vol_60d, capped [0.5×, 1.5×].
        _tsmom_mult = float(sig.get("tsmom_vol_mult") or 1.0)
        if _tsmom_mult != 1.0:
            dollar_cap *= _tsmom_mult
            logger.info(
                f"[{symbol}] TSMOM vol-scale: {_tsmom_mult:.2f}× "
                f"(ewma_vol={sig.get('ewma_vol_60d') or 0:.1%}, "
                f"target={config.TSMOM_TARGET_VOL:.0%}) | "
                f"dollar_cap → ${dollar_cap:,.2f}"
            )

        # Apply earnings 0.5x multiplier (1.0 = no earnings nearby)
        if _earnings_mult < 1.0:
            dollar_cap = dollar_cap * _earnings_mult
            logger.info(
                f"[{symbol}] Earnings 0.5x size applied: "
                f"dollar_cap → ${dollar_cap:,.2f}"
            )

        # ── VOTE-3: Volume cap on Kelly (board-approved 2026-04-20) ──────────
        # vol_ratio = current bar volume / 20d avg volume.
        # Cap only: never amplifies Kelly, only suppresses when vol < 1.5x avg.
        # Thorp: volume as cap prevents oversizing into low-liquidity signals.
        _sig_vol_ratio = sig.get("vol_ratio")
        if _sig_vol_ratio is not None and _sig_vol_ratio < 1.5:
            _vol_cap = min(1.0, _sig_vol_ratio / 1.5)
            dollar_cap *= _vol_cap
            logger.info(
                f"[{symbol}] VOTE-3 vol cap {_vol_cap:.2f}x "
                f"(vol_ratio={_sig_vol_ratio:.2f}x avg) | dollar_cap → ${dollar_cap:,.2f}"
            )

        # ── Build #15: FVG confluence multiplier ─────────────────────────────
        # Per-symbol size adjustment based on proximity to unfilled Fair Value Gaps.
        # Reuses daily_df (already fetched above); fetches 1h separately via
        # fetch_bars() (Alpaca Data T1) inside _compute_fvg_mult().
        _fvg_mult, _fvg_reason = _compute_fvg_mult(symbol, direction, entry_price, daily_df)
        if _fvg_mult != 1.0:
            dollar_cap *= _fvg_mult
            _fvg_arrow  = "↑" if _fvg_mult > 1.0 else "↓"
            logger.info(
                f"[{symbol}] FVG {_fvg_arrow} {_fvg_mult:.2f}x ({_fvg_reason}) | "
                f"dollar_cap → ${dollar_cap:,.2f}"
            )

        # ── Leveraged-ETF notional ring-fence (Bucket A/B collapse 2026-07-15) ──────
        # Replaces the deleted Bucket-A 15%/leverage cap. Now that leveraged names size via
        # the unified conviction+Kelly path, a 3x ETF could otherwise take a huge notional on
        # a small account (board FORK-5: ~$70k on $2.8k = ruin). Hard-cap leveraged names to
        # LEVERAGED_NOTIONAL_MAX_PCT of equity AFTER all multipliers, BEFORE the share sizing.
        if is_leveraged:
            _lev_cap = risk.portfolio_value * config.LEVERAGED_NOTIONAL_MAX_PCT
            if dollar_cap > _lev_cap:
                logger.info(
                    f"[{symbol}] LEVERAGED notional ring-fence: ${dollar_cap:,.2f} → ${_lev_cap:,.2f} "
                    f"({config.LEVERAGED_NOTIONAL_MAX_PCT:.0%} of equity)"
                )
                dollar_cap = _lev_cap

        # ── BoD-5R: Minimum-lot guard ────────────────────────────────────────
        # After all multipliers are applied, sub-$50 notional orders have
        # negative expectancy after commissions — Peterffy: commission drag
        # exceeds the edge at that size.  Skip cleanly rather than submitting
        # a noise trade below minimum lot.
        if dollar_cap < 50.0:
            logger.info(
                f"[{symbol}] BoD-5R MINIMUM-LOT: dollar_cap ${dollar_cap:.2f} < $50 "
                f"after all multipliers — skipping entry (commission drag > edge)."
            )
            _rc8_clear_buffers(symbol, "minimum-lot-guard")
            continue

        # Inverse ATR sizing: dollar_cap / stop_distance = shares
        stop_distance = abs(entry_price - stop)
        if stop_distance == 0:
            continue
        raw_shares = int(dollar_cap / stop_distance)

        # Apply size multipliers (regime, event risk, TOD, news)
        # RC-7: floor only when dollar_cap can cover ≥1 share; otherwise size→0 and skip.
        # int(raw × mult) truncates to 0 when the product < 1.0 — don't blindly floor to 1
        # when dollar_cap < entry_price (that would exceed the Kelly notional budget).
        _sized = int(raw_shares * size_multiplier)
        _can_afford_one = (entry_price > 0 and int(dollar_cap / entry_price) >= 1)

        if size_multiplier == 0.0:
            shares = 0
            logger.info(
                f"[{symbol}] size_multiplier=0.0 (explicit blockade: HALT/regime/event) "
                f"— entry suppressed. raw_shares={raw_shares}"
            )
        elif _can_afford_one and _sized > 0:
            shares = max(1, _sized)
        elif _can_afford_one and _sized == 0:
            shares = 1  # RC-7 floor: non-zero multiplier truncated by int()
            logger.debug(
                f"[{symbol}] sizing truncated: {raw_shares:.1f}sh × "
                f"{size_multiplier:.3f} = {raw_shares * size_multiplier:.3f} → int()=0 "
                f"— floored to 1 (affordable, multiplier nonzero)"
            )
        else:
            shares = 0
            logger.info(
                f"[{symbol}] Size→0: dollar_cap=${dollar_cap:.2f} < 1sh@${entry_price:.2f} "
                f"| mult={size_multiplier:.4f} (stacked: event×regime×tod×spy×pnl×overnight) — skipping."
            )

        # Hard cap: position value cannot exceed dollar_cap
        max_shares_by_value = int(dollar_cap / entry_price) if entry_price > 0 else 0
        shares = min(shares, max_shares_by_value)

        # ── VOTE-5: Volatility targeting cap (board-approved 2026-04-20) ─────
        # final_size = min(kelly_size, (σ* / σ_20d_ann) × equity / price)
        # σ* = 15% annualized target. Cap only — never increases size above Kelly.
        # Thorp: prevents oversizing in low-vol regimes where Kelly grows unchecked.
        # Fallback: no cap if σ_20d unavailable or < 0.001 (avoids divide-by-zero).
        _sigma_20d = sig.get("sigma_20d")
        if _sigma_20d and _sigma_20d > 0.001 and entry_price > 0:
            try:
                _pv5 = risk.portfolio_value if risk.portfolio_value > 0 else (
                    _portfolio_val if "_portfolio_val" in dir() and _portfolio_val else 0
                )
                if _pv5 > 0:
                    _vol_target_shares = int((0.15 / _sigma_20d) * _pv5 / entry_price)
                    if _vol_target_shares < shares:
                        logger.info(
                            f"[{symbol}] VOTE-5 vol-target cap: {shares}sh → {_vol_target_shares}sh "
                            f"(σ_20d={_sigma_20d:.3f} ann | σ*=15%)"
                        )
                        shares = _vol_target_shares  # M-16: no floor — let L1489 skip if 0
                        if shares == 0:
                            logger.warning(
                                f"[{symbol}] VOTE-5 vol-target cap produced 0 shares "
                                f"(σ_20d={_sigma_20d:.3f} ann | equity=${_pv5:.0f} | price=${entry_price:.2f}) — skipping entry."
                            )
            except Exception as _ve5:
                logger.debug(f"[{symbol}] VOTE-5 vol-target cap error: {_ve5}")

        # ── Per-trade Kelly risk re-clamp (2026-08-02, BGG) ──────────────────
        # TSMOM (L1225) and FVG (L1260) multiply dollar_cap AFTER the Kelly cap
        # (L1189 — kelly_risk_pct is clamped to KELLY_MAX_RISK_PCT inside kelly.py)
        # with NO re-clamp, so a low-vol name at the cap can breach per-trade risk
        # by the up-multiplier (25% at [.,1.25]; 50% at [.,1.50]). Bound the FINAL
        # share count to the true risk-of-ruin quantity: shares × stop_distance ≤
        # KELLY_MAX_RISK_PCT × equity. Clamp SHARES, not dollar_cap (overloaded
        # risk/notional — rescaling it over-constrains via the notional leg).
        # min() only: never RAISES size; preserves TSMOM's down-leg + legitimate
        # sub-cap up-sizing. Universal — bounds ANY future up-multiplier, not just
        # TSMOM. Reads the same KELLY_MAX_RISK_PCT kelly.py enforces (consistent).
        if risk.portfolio_value > 0 and stop_distance > 0:
            _max_sh_kelly = int(config.KELLY_MAX_RISK_PCT * risk.portfolio_value / stop_distance)
            if _max_sh_kelly < shares:
                logger.info(
                    f"[{symbol}] Kelly per-trade risk clamp: {shares}sh → {_max_sh_kelly}sh "
                    f"(risk ${shares * stop_distance:,.2f} → cap {config.KELLY_MAX_RISK_PCT:.1%} × "
                    f"${risk.portfolio_value:,.2f} = ${config.KELLY_MAX_RISK_PCT * risk.portfolio_value:,.2f})"
                )
                shares = _max_sh_kelly

        if shares < 1:
            logger.info(f"[{symbol}] Position size < 1 share at ${entry_price:.2f}. Skipping.")
            continue

        # Per-symbol short block gate — skip symbols Alpaca has already rejected
        # with 40310000 this session. broker.py populates the cache on first rejection;
        # no point hammering Alpaca on every scan cycle for the same symbol.
        if direction == "short" and symbol in get_short_blocked_symbols():
            logger.info(
                f"[{symbol}] Short skipped — symbol is in session short-block cache "
                f"(Alpaca 40310000 from earlier this session)."
            )
            _rc8_clear_buffers(symbol, "short-block-cache")
            continue

        # ── Overnight exposure cap (Patch 2, Apr 15 2026) ───────────────────
        # Cap total overnight notional to avoid dangerous concentration.
        # Dynamic cap:
        #   60% base  |  80% if zone_tier=0 (Green, no distribution)
        #   40% if macro_regime_tier=2 (Contraction / High-risk)

        # ── Buying-power pre-flight (fail-closed) — 2026-07-14 board+Gro+GAI ──────────
        # Closes the latent over-commit/desync bug: the bot used to fire the order and let
        # Alpaca reject it when short of funds, drifting tracker vs risk. Verify live BP
        # covers the notional (+10% cushion) BEFORE submitting; skip cleanly if not.
        if not risk.check_buying_power_for_order(shares, entry_price):
            _rc8_clear_buffers(symbol, "bp-insufficient")
            continue
        # ── Aggregate gross-exposure gate (MAX_GROSS_EXPOSURE_RATIO × equity) ─────────
        # The PRIMARY governor of position count now that MAX_OPEN_POSITIONS is a
        # circuit-breaker. Blocks the entry if it would push total open notional past cap.
        if not risk.check_gross_exposure_for_order(tracker, entry_price, shares):
            _rc8_clear_buffers(symbol, "gross-exposure-cap")
            continue

        # ── MR-only guards (item 2 diff 3d) — idempotency + aggregate correlation sub-cap ──────
        if _is_mr:
            # (a) Pending-order idempotency: the trend path only checks FILLED positions
            # (get_open_position); an MR signal can regenerate next cycle before a fill. FAIL-CLOSED:
            # get_open_orders returns None on API error (broker.py: "None = unknown state; callers
            # must not treat as empty"), so None OR a non-empty list → skip (never risk a double-submit).
            try:
                _oo = get_open_orders(symbol)
                if _oo is None or _oo:
                    logger.info(f"[{symbol}] MR: open/pending order present or unknown — skipping (idempotency, fail-closed).")
                    continue
            except Exception as _oe:
                logger.warning(f"[{symbol}] MR open-orders check error ({_oe}) — skipping MR entry (fail-closed).")
                continue
            # (b) Aggregate MR correlation sub-cap — the account-ender guard the gross cap (which is
            # correlation-BLIND) misses: in a broad selloff many crashed names bounce together, so the
            # summed open-MR per-trade risk can trip the 7% daily kill (4 × 0.5×4.5% = 9%). Bound the
            # TOTAL open MR risk (Σ qty×|entry-stop|) + this trade to MR_AGG_RISK_CAP_PCT (3.5%) × equity.
            # FAIL-CLOSED: any compute error BLOCKS the MR entry (a risk cap never over-exposes on error).
            try:
                _mr_open_risk = 0.0
                for _t in (getattr(tracker, "open_trades", {}) or {}).values():
                    if (_t or {}).get("strategy") == "mean_reversion":
                        _q = abs(float(_t.get("qty") or 0))
                        _e = float(_t.get("entry_price") or 0)
                        # original (immutable) stop preferred — a TRAILED stop would shrink |entry-stop|
                        # and loosen the basket budget as winners age (masked-loss seat). NOTE the cap
                        # bounds ORDERLY stop-out basket loss, not gap-THROUGH-stop (the true correlated tail).
                        _s = float(_t.get("original_stop") or _t.get("stop") or 0)
                        if _q > 0 and _e > 0 and _s > 0:
                            _mr_open_risk += _q * abs(_e - _s)
                _this_risk = shares * stop_distance
                _mr_cap = float(getattr(config, "MR_AGG_RISK_CAP_PCT", 0.035)) * risk.portfolio_value
                if _mr_open_risk + _this_risk > _mr_cap:
                    logger.warning(
                        f"[{symbol}] MR AGG SUB-CAP: open MR risk ${_mr_open_risk:,.2f} + this ${_this_risk:,.2f} "
                        f"> {getattr(config, 'MR_AGG_RISK_CAP_PCT', 0.035):.1%} × ${risk.portfolio_value:,.2f} "
                        f"= ${_mr_cap:,.2f} — blocking (correlated-basket guard)."
                    )
                    _rc8_clear_buffers(symbol, "mr-agg-risk-cap")
                    continue
            except Exception as _ae:
                logger.warning(f"[{symbol}] MR agg sub-cap error ({_ae}) — BLOCKING MR entry (fail-closed).")
                continue

        # Submit plain market order — no brackets, no stops attached
        # All stop/target levels stored in tracker only, bot manages execution
        side = "buy" if direction == "long" else "sell"
        order = submit_market_order(symbol=symbol, qty=shares, side=side)

        if order:
            # P5-H2: Fetch actual fill price — retry up to 3x at 1s intervals.
            # Single 1.5s poll silently missed unfilled orders; now warns explicitly on fallback.
            fill_price = entry_price    # pre-fill estimate (fallback)
            try:
                for _fp_attempt in range(3):
                    time.sleep(1.0)
                    _filled = get_order(str(order.id))  # type: ignore[attr-defined]
                    if _filled and getattr(_filled, "filled_avg_price", None):
                        fill_price = float(_filled.filled_avg_price)
                        gap_pct = abs(fill_price - entry_price) / max(entry_price, 0.01) * 100
                        logger.info(
                            f"[{symbol}] Fill price: ${fill_price:.2f} "
                            f"(estimate ${entry_price:.2f}, gap={gap_pct:.2f}%, "
                            f"attempt {_fp_attempt + 1}/3)"
                        )
                        # Recalculate stop/target from actual fill if gap is material (>0.05%)
                        if gap_pct > 0.05:
                            stop, target = risk.get_stop_and_target(
                                fill_price, direction, trade_mode,
                                atr_value=atr_value, symbol=symbol,
                                rvol_20d=rvol_20d, vix=vix,
                                spy_ath_dist_pct=_spy_ath_pct,
                            )
                            stop = risk.get_news_adjusted_stop(stop, fill_price, direction, news_size_mult)
                            logger.info(
                                f"[{symbol}] Stop/target recalculated from fill: "
                                f"stop=${stop:.2f} target=${target:.2f}"
                            )
                        break  # got a valid fill — stop polling
                else:
                    logger.warning(
                        f"[{symbol}] Fill price unavailable after 3 polls — "
                        f"using pre-order estimate ${fill_price:.2f}. Review fill manually."
                    )
            except Exception as _fe:
                logger.warning(f"[{symbol}] Fill price fetch failed — using estimate: {_fe}")

            # ── MR decision-stack logging (item 2 diff 3e, Rule D keystone) ──────────────
            # record_entry(**extra_log) forwards straight to the trade_events.jsonl entry event
            # (verified at source: portfolio_tracker.py record_entry — extra_log is splatted into
            # _log_event unfiltered). Additive only: _mr_log_extra is {} for a trend entry, so a
            # non-MR entry's logged event is byte-identical to today. Closes the gap where an MR
            # entry's "strategy" was never written to the JSONL event (only tagged post-hoc onto the
            # in-memory trade dict below, which record_entry does not read back) — without it, an MR
            # entry's 0-3 score is indistinguishable from a low trend score at query time. regime/RSI/
            # confirmation are already covered by the existing conditions=... kwarg below (sig
            # ["conditions"] IS mean_reversion_confluence's own conditions dict for an MR signal —
            # structural/trigger/mean_reverting/rsi_extreme/stretched/rsi/stretch_pct). mr_size_mult
            # and mr_replaced_trend (which opposite trend signal, if any, Option B discarded for this
            # symbol — see signal_generator.py) were not logged anywhere before this diff.
            _mr_log_extra = {}
            if _is_mr:
                try:
                    _mr_log_extra = {
                        "strategy":          "mean_reversion",
                        "mr_score":          sig.get("mr_score"),
                        "mr_max_score":      sig.get("mr_max_score"),
                        "mr_size_mult":      float(getattr(config, "MR_SIZE_MULT", 0.5)),
                        "mr_replaced_trend": sig.get("mr_replaced_trend"),
                    }
                except Exception as _mrlog_e:
                    # Reliability seat catch (2026-08-04 board): kept narrow and OUTSIDE the
                    # PHANTOM ENTRY try below — a logging-field failure must never be conflated
                    # with (or suppress) that handler's post-fill reconciliation duty, and must
                    # never abort the rest of this cycle's signals loop. Falls back to the one
                    # field that cannot fail (a string literal) rather than losing the MR tag
                    # entirely — mirrors the D1 numpy.bool_ postmortem (trade_logger.py).
                    logger.warning(f"[{symbol}] MR log-extra build failed ({_mrlog_e}) — "
                                   f"logging strategy tag only.")
                    _mr_log_extra = {"strategy": "mean_reversion"}
            try:
                tracker.record_entry(
                    symbol=symbol,
                    direction=direction,
                    qty=shares,
                    entry_price=fill_price,   # C-5/H-5: use actual fill, not estimate
                    stop=stop,
                    target=target,
                    trade_mode=trade_mode,
                    score=sig["score"],
                    score_16pt=sig.get("score_16pt"),
                    atr_value=atr_value or 0.0,
                    # Guardrail 7: pass full context for trade_events.jsonl
                    mri_level=mri.level(),
                    data_source="alpaca_data",
                    # TSMOM fields — logged for 90-day gate tracking (Gate 3)
                    tsmom_12m=sig.get("tsmom_12m"),
                    tsmom_6m=sig.get("tsmom_6m"),
                    tsmom_ewma_vol=sig.get("ewma_vol_60d"),
                    tsmom_vol_mult=sig.get("tsmom_vol_mult"),
                    tsmom_direction=sig.get("tsmom_direction"),
                    # IC Phase 1b: per-factor pass/fail breakdown (the 12 confluence components)
                    # for the traded direction — forwarded via record_entry(**extra_log) → the
                    # trade_events.jsonl entry event so research/ic_engine.py can rank each factor's
                    # IC/ICIR individually (not just the total score). sig IS long_r/short_r, which
                    # carries "conditions"; this was the only missing link (per-factor never logged).
                    conditions=sig.get("conditions"),
                    **_mr_log_extra,
                )
                # Tag MR strategy on the persisted trade dict (item 2 diff 3d) — record_entry does
                # NOT persist strategy (it only forwards it to the JSONL event), so without this the
                # Kelly rebuild (kelly.py reads t.get("strategy")) would mis-book the MR trade to the
                # trend key. Mirrors the overnight-tag pattern below.
                if _is_mr and symbol in tracker.open_trades:
                    tracker.open_trades[symbol]["strategy"] = "mean_reversion"
                    tracker._save_log()
                    logger.info(f"[{symbol}] Tagged strategy=mean_reversion (MR entry).")
                # Tag overnight: AH entries (after 3:30 PM ET) are overnight holds
                entry_time = datetime.now(ET)
                entry_mins = entry_time.hour * 60 + entry_time.minute
                is_overnight_entry = entry_mins >= (15 * 60 + 30)
                if is_overnight_entry and symbol in tracker.open_trades:
                    tracker.open_trades[symbol]["overnight"] = True
                    tracker.open_trades[symbol]["overnight_since"] = entry_time.isoformat()
                    tracker.open_trades[symbol]["overnight_reason"] = "ah_entry"
                    logger.info(f"[{symbol}] Tagged overnight=True (entry after 3:30 PM ET)")
                    tracker._save_log()
                    gtc_side  = "sell" if direction == "long" else "buy"
                    gtc_order = submit_gtc_stop_order(
                        symbol=symbol, qty=shares,
                        side=gtc_side, stop_price=stop,
                    )
                    if gtc_order:
                        tracker.set_gtc_stop_order_id(symbol, str(gtc_order.id))  # type: ignore[attr-defined]
                        logger.info(
                            f"[{symbol}] GTC stop submitted (overnight): "
                            f"{shares}sh {gtc_side} @ ${stop:.2f} | order {gtc_order.id}"  # type: ignore[attr-defined]
                        )
                    else:
                        logger.warning(
                            f"[{symbol}] GTC stop order failed — overnight position "
                            f"has NO exchange-level stop. Monitor manually."
                        )
                        alert_gtc_failed(
                            symbol=symbol,
                            side=gtc_side,
                            stop_px=stop,
                            reason="submit_gtc_stop_order() returned None at entry",
                        )
                risk.register_open()
                logger.info(
                    f"[{symbol}] {direction.upper()} {shares} shares | "
                    f"Score: {sig['score']}/{sig['max_score']} | "
                    f"Stop: ${stop:.2f} (internal) | Target: ${target:.2f} (internal)"
                )
                _ae_ath = (_main._spy_52w_high - _main._spy_last_close) / _main._spy_52w_high * 100 \
                    if _main._spy_52w_high > 0 and _main._spy_last_close > 0 else None
                alert_entry(
                    symbol=symbol,
                    direction=direction,
                    shares=shares,
                    price=fill_price,
                    score=sig["score"],
                    size_mult=size_multiplier,
                    overnight=is_overnight_entry,
                    spy_ath_dist_pct=_ae_ath or 0.0,
                    mri_level=mri.level() if mri else "NORMAL",
                )
                entered.append(symbol)   # LAST — only appended after all tracking steps succeed
            except Exception as _phantom_err:
                import sys as _sys
                _incident_id = str(uuid.uuid4())
                try:
                    _post_fill = get_order(str(order.id))  # type: ignore[attr-defined]
                    _order_state = {
                        "id": str(getattr(_post_fill, "id", "?")),
                        "status": str(getattr(_post_fill, "status", "?")),
                        "filled_qty": getattr(_post_fill, "filled_qty", 0),
                        "filled_avg_price": getattr(_post_fill, "filled_avg_price", None),
                    }
                except Exception as _os_err:
                    _order_state = {"error": f"could not fetch broker state: {_os_err}"}
                logger.critical(
                    "[%s] PHANTOM ENTRY incident=%s — order %s is FILLED in Alpaca but tracking "
                    "failed. Position is LIVE and UNTRACKED. Forcing bot restart to trigger "
                    "adoption on next startup. broker_state_best_effort=%s | error=%s "
                    "(NOTE: broker_state captured post-exception; Alpaca state may differ — "
                    "use _reconcile_positions() holdings query at restart as authoritative source)",
                    symbol, _incident_id, getattr(order, "id", "?"),  # type: ignore[attr-defined]
                    _order_state, _phantom_err,
                    exc_info=True,
                )
                try:
                    from alerts import alert_crash as _alert_crash
                    _alert_crash(
                        reason=(
                            f"PHANTOM ENTRY incident={_incident_id}: {symbol} "
                            f"order={getattr(order, 'id', '?')} FILLED but untracked — "  # type: ignore[attr-defined]
                            f"forcing restart for auto-adoption. error={_phantom_err}"
                        ),
                        open_positions=[symbol],
                    )
                except Exception as _alert_err:
                    logger.critical(
                        "[%s] PHANTOM ENTRY alert FAILED — operator may not be notified. "
                        "incident=%s error=%s", symbol, _incident_id, _alert_err, exc_info=True
                    )
                    print(
                        f"PHANTOM_ENTRY_ALERT_FAILED symbol={symbol} incident={_incident_id} "
                        f"error={_alert_err}",
                        file=_sys.stderr,
                        flush=True,
                    )
                _sys.exit(1)   # triggers systemd restart → _reconcile_positions() adopts orphan

    # ── confirm_gate.json: end-of-cycle persist (GAI I/O fix) ───────────────
    # Moved from inside the per-symbol for loop (was firing 30x/cycle, once per signal).
    # Persist once at cycle end — buffer state is consistent at this point and any
    # mid-cycle RC-8 clears already wrote their own immediate persist inside _rc8_clear_buffers.
    # Atomic write (TB-Minsky): tmp → os.replace() prevents truncated JSON on mid-write restart.
    try:
        _write_confirm_gate_json(
            gate_state.entry_confirm_buffer, gate_state.conviction_streak
        )
    except Exception as _e_cg:
        logger.warning(f"[confirm_gate] Failed to persist confirm_gate.json — gate state not durable: {_e_cg}")

    # ── P2-2: Systemic feed latency monitor ──────────────────────────────────
    # Track median bar age across all scanned symbols per cycle.
    # If median exceeds 20 min for 3 consecutive cycles, fire a single systemic
    # alert rather than per-ticker noise. Resets when a healthy cycle returns.
    # B1 fix: _feed_age_history/_systemic_stale_alerted now in execution/lifecycle.py
    # Accessed via module attribute to allow mutation of module-level state.
    import execution.lifecycle as _lc
    if _cycle_bar_ages and len(_cycle_bar_ages) >= 3:
        _sorted_ages  = sorted(_cycle_bar_ages)
        _cycle_median = _sorted_ages[len(_sorted_ages) // 2]
        if _lc._feed_age_history is None:
            from collections import deque as _feed_deque
            _lc._feed_age_history = _feed_deque(maxlen=3)
        _lc._feed_age_history.append(_cycle_median)  # type: ignore[attr-defined]
        # Finding 5: unified with log msg "> 20 min"
        if (len(_lc._feed_age_history) == 3  # type: ignore[arg-type]
                and all(a > 20 for a in _lc._feed_age_history)):  # type: ignore[attr-defined]
            if not _lc._systemic_stale_alerted:
                _lc._systemic_stale_alerted = True
                logger.warning(
                    f"[SYSTEMIC FEED LATENCY] Median bar age > 20 min for 3 consecutive "
                    f"cycles ({[round(a, 1) for a in _lc._feed_age_history]} min) — "  # type: ignore[attr-defined]
                    f"Alpaca Data T1 feed degraded."
                )
                alert_systemic_stale_feed(median_age=_cycle_median, cycles=3)
        elif _lc._systemic_stale_alerted:
            logger.info(
                f"[FEED RECOVERED] Median bar age {_cycle_median:.1f} min — "
                f"feed healthy again."
            )
            _lc._systemic_stale_alerted = False
    # ─────────────────────────────────────────────────────────────────────────

    return entered


def _overnight_entry_check(
    tracker: PortfolioTracker,
    risk: "RiskManager",
    kelly: "KellySizer",
    calendar: "EventCalendar",
):
    """
    Phase 1 (DRY-RUN): Scan for overnight swing entries after market close.
    Window: 8 PM – midnight ET. Requires ≥11/12 score + strong_bull/strong_bear bias.
    When _main.OVERNIGHT_ENTRIES_ENABLED=False: logs [DRY-RUN OVERNIGHT] but submits no orders.
    When True: submits DAY limit order and records pending_overnight in tracker.
    """
    import main as _main  # lazy: avoids circular import at module load
    from events.calendar import EventRisk

    _now  = datetime.now(ET)
    _mins = _now.hour * 60 + _now.minute
    _log  = "[DRY-RUN OVERNIGHT]" if not _main.OVERNIGHT_ENTRIES_ENABLED else "[OVERNIGHT]"

    # Gate 1: Only between 8 PM and midnight ET
    if not (_main._OVERNIGHT_ENTRY_START <= _mins < 24 * 60):
        return

    # Gate 2: Kill switch
    if risk.check_kill_switch():
        return

    # Gate 3: Max 1 overnight entry — already have one pending or active?
    pending = tracker.get_pending_overnight_entries()
    if pending:
        logger.debug(f"{_log} Pending order already exists for {[s for s, _ in pending]} — skip")
        return
    active_count = sum(1 for t in tracker.open_trades.values() if t.get("status") == "open")
    if active_count >= 1:   # Marcus Thorne: max 1 overnight position
        logger.info(f"{_log} {active_count} active position(s) — overnight max is 1, skip")
        return

    # Gate 4: Tomorrow's macro calendar
    tomorrow = _now.date() + timedelta(days=1)
    while tomorrow.weekday() >= 5:          # skip to next trading day
        tomorrow += timedelta(days=1)
    tom_risk = calendar.get_day_risk(check_date=tomorrow)
    if tom_risk["risk"] in (EventRisk.BLACKOUT, EventRisk.HIGH_RISK):
        logger.info(f"{_log} Blocked — tomorrow's macro risk: {tom_risk['note']}")
        return

    # Run signal scan
    logger.info(f"{_log} Running overnight signal scan...")
    try:
        signals = run_scan(trade_mode=config.TradeMode.INTRADAY)
    except Exception as e:
        logger.warning(f"{_log} Scan failed: {e}")
        return

    # Filter: ≥11/12, strong bias only, not already in trade
    candidates = [
        s for s in signals
        if s.get("score", 0) >= config.CONVICTION_FULL_MIN
        and s.get("bias") in ("strong_bull", "strong_bear")
        and not tracker.is_in_trade(s["symbol"])
    ]
    if not candidates:
        logger.info(f"{_log} No qualifying candidates (need ≥11/12 + strong bias)")
        return

    sig       = candidates[0]         # top-ranked signal
    symbol    = sig["symbol"]
    direction = sig["direction"]
    score     = sig["score"]
    bias      = sig["bias"]
    side      = "buy" if direction == "long" else "sell"

    # Gate 5: Tomorrow's earnings / symbol-level blackout
    tom_sym_risk = calendar.get_day_risk(check_date=tomorrow, symbol=symbol)
    if tom_sym_risk["risk"] == EventRisk.BLACKOUT:
        logger.info(f"{_log} {symbol} blocked — event/earnings tomorrow: {tom_sym_risk['note']}")
        return

    # Fetch AH price and daily ATR
    # DATA-2h: replaced yfinance fast_info with get_latest_trade() (T1 Alpaca Data).
    try:
        ah_price = get_latest_trade(symbol) or 0.0
        if ah_price <= 0:
            logger.warning(f"{_log} {symbol}: invalid AH price — skip")
            return
    except Exception as e:
        logger.warning(f"{_log} {symbol}: AH price fetch failed: {e}")
        return

    try:
        daily_df    = fetch_bars(symbol, config.TF_DAILY, num_bars=config.ATR_PERIOD + 5)
        if daily_df.empty:
            logger.warning(f"{_log} {symbol}: no daily bars — skip")
            return
        close_price = float(daily_df["close"].iloc[-1])
        atr_value   = (calculate_atr(daily_df, config.ATR_PERIOD) / 100) * close_price
    except Exception as e:
        logger.warning(f"{_log} {symbol}: daily ATR fetch failed: {e}")
        return

    # Gate 6: AH drift > 3% (Dr. Chen: don't chase AH moves)
    ah_drift = abs(ah_price - close_price) / close_price if close_price > 0 else 1.0
    if ah_drift > _main._OVERNIGHT_MAX_AH_DRIFT:
        logger.info(
            f"{_log} {symbol}: AH drift {ah_drift:.1%} > {_main._OVERNIGHT_MAX_AH_DRIFT:.0%} — skip"
        )
        return

    # Gate 7: AH volume check (Marcus Thorne: thin tape = unreliable price)
    try:
        _ah_df = fetch_bars(symbol, config.TF_1M, num_bars=30)  # T1 — Alpaca Data API
        if not _ah_df.empty:
            _ah_vol = int(_ah_df["volume"].iloc[-30:].sum())
            if _ah_vol < _main._OVERNIGHT_MIN_AH_VOLUME:
                logger.info(f"{_log} {symbol}: AH volume {_ah_vol:,} < {_main._OVERNIGHT_MIN_AH_VOLUME:,} — thin tape, skip")
                return
    except Exception as _ahv_e:
        logger.warning("[%s] AH volume check failed: %s — proceeding without volume confirmation", symbol, _ahv_e)

    # Limit price: ask for buys, bid for sells (Sarah Okonkwo: improve fill probability)
    try:
        _quote = get_latest_quote(symbol)  # T1 — Alpaca Data REST API
        _bid   = float((_quote or {}).get("bid", 0) or 0)
        _ask   = float((_quote or {}).get("ask", 0) or 0)
        limit_price = (_ask if side == "buy" else _bid) if (_bid > 0 and _ask > 0) else ah_price
    except Exception as _quote_err:
        logger.warning("[%s] AH quote fetch failed — skipping overnight entry "
                       "for safety (cannot verify spread): %s", symbol, _quote_err)
        return
    limit_price = round(limit_price, 2)

    # Spread gate: wide spread = stale/thin AH market — skip overnight entry (Finding 5)
    if _bid > 0 and _ask > 0:
        _spread_pct = (_ask - _bid) / _ask
        if _spread_pct > 0.02:
            logger.info(
                f"{_log} {symbol}: AH spread {_spread_pct:.1%} > 2% "
                f"(bid=${_bid:.2f} ask=${_ask:.2f}) — skip overnight entry"
            )
            return

    # Stop + target: SWING multipliers on daily ATR (Dr. Priya Desai)
    if atr_value and atr_value > 0:
        stop_dist   = atr_value * config.SWING_STOP_ATR_MULT
        target_dist = atr_value * config.SWING_TARGET_ATR_MULT
    else:
        stop_dist   = limit_price * config.SWING_STOP_PCT
        target_dist = limit_price * config.SWING_TARGET_PCT

    if direction == "long":
        stop   = round(limit_price - stop_dist, 2)
        target = round(limit_price + target_dist, 2)
    else:
        stop   = round(limit_price + stop_dist, 2)
        target = round(limit_price - target_dist, 2)

    # Size: 50% of full Bucket B allocation (Marcus Thorne: overnight gap discount)
    portfolio_value      = get_portfolio_value()
    overnight_dollar_cap = portfolio_value * config.INTRA_ALLOCATION_PCT * 0.95 * 0.50
    stop_distance        = abs(limit_price - stop)
    if stop_distance == 0:
        logger.warning(f"{_log} {symbol}: zero stop distance — skip")
        return
    shares = max(1, int(overnight_dollar_cap / stop_distance))
    shares = min(shares, int(overnight_dollar_cap / limit_price) if limit_price > 0 else 0)
    if shares < 1:
        logger.info(f"{_log} {symbol}: size < 1 share at ${limit_price:.2f} — skip")
        return

    logger.info(
        f"{_log} {symbol} {direction.upper()} {shares}sh | score={score}/12 bias={bias} | "
        f"limit=${limit_price:.2f} stop=${stop:.2f} target=${target:.2f} | "
        f"ATR=${atr_value:.2f} AH=${ah_price:.2f} drift={ah_drift:.1%}"
    )

    if not _main.OVERNIGHT_ENTRIES_ENABLED:
        return  # Phase 1: dry-run — log only, no order submission

    # After 8 PM ET the AH session is closed — submit plain DAY limit (queues for pre-mkt)
    _use_extended = _mins < _main._OVERNIGHT_ENTRY_START   # True only if before 8 PM (edge case)
    order = submit_limit_order(
        symbol=symbol, qty=shares, side=side,
        limit_price=limit_price, extended_hours=_use_extended,
    )
    if order:
        tracker.record_pending_entry(
            symbol=symbol, order_id=str(order.id),  # type: ignore[attr-defined]
            direction=direction, qty=shares,
            limit_price=limit_price, stop=stop, target=target,
            score=score, atr_value=atr_value,
        )
    else:
        logger.warning(f"{_log} {symbol}: limit order submission failed")
