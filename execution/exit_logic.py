# ruff: noqa: E501, E701, E702, B023
"""
execution/exit_logic.py — Exit Logic Module
Phase 2 Decomposition: extracted verbatim from trade_engine.py

Contains all exit-related logic:
  - check_partial_exits()         — trailing stop / partial tranche logic
  - check_exits()                 — hard stop / reversal exit logic
  - _check_exits_extended_hours() — AH/PM exit monitoring
  - TQI helpers: _compute_tqi, _record_partial_tqi, _record_tqi

H1 DPE Integration (2026-05-11):
  - check_exits() overnight_atr_buffer_exit now calls get_be_buffer_mult(symbol, last_vix)
    instead of the static 3-tier VIX ternary — per-symbol realized vol scalar applied.
    Log tag changed from VIX_mult={:.2f} to Tier_adj={:.3f} to reflect dynamic value.

Extraction rules (board-approved lift-then-split):
  - All functions lifted verbatim from trade_engine.py — no internal restructuring.
  - trade_engine.py re-exports these names after extraction so callers are unchanged.
  - _submit_rth_day_stops() dropped — confirmed dead code (never called), user-approved.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import config
from alerts import alert_exit, alert_partial, alert_stop_breach, alert_gtc_failed
from data.alpaca_data import get_latest_trade
from data.fetcher import fetch_bars
from execution.broker import (
    cancel_order,
    cancel_open_orders_for_symbol,
    close_position,
    get_open_position,
    get_order,
    get_trading_client,
    partial_close_position,
    submit_day_stop_order,
    submit_gtc_stop_order,
    submit_limit_order,
)
from execution.fill_helpers import fetch_actual_fill_price as _fetch_actual_fill_price
from execution.lifecycle import (
    get_partial_fail_counts as _get_partial_fail_counts,
    set_shorts_banned as _set_shorts_banned,
)
from execution.orphan_manager import get_tod_phase as _get_tod_phase
from execution.param_engine import get_be_buffer_mult
from execution.quarterly_hold_manager import get_quarterly_hold_symbols as _get_qhm_syms
from execution.portfolio_tracker import PortfolioTracker
from execution.risk_manager import RiskManager
from strategy.scoring import get_live_score as _get_live_score
from strategy.signal_generator import get_exit_signal
from trade_logger import log_event as _log_trade_event

if TYPE_CHECKING:
    from events.macro_risk_index import MacroRiskIndex
    from execution.kelly import KellySizer
    from main import GateState

ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


# ─── TRADE QUALITY INDEX ─────────────────────────────────────────────────────

def _compute_tqi(trade: dict) -> int:
    """
    Trade Quality Index — 0-100 per-trade execution quality score.

    Components:
      Confluence at entry (0-25): config-anchored to CONVICTION_SKIP_BELOW (min enterable
                                   score) → 5pt floor, scaling linearly to 25 at the 12-pt
                                   max. Today (skip_below=8): 8→5, 9→10, 10→15, 11→20, 12→25.
      Exit reason quality (0-35): target→35, trail_stop→28, signal/opposite_signal→18,
                                   thesis_invalidation→15, pm_exit→12,
                                   overnight_atr_buffer_exit→10, hard_stop→0, other→8
      R-multiple achieved (0-40): R≥2→40, R≥1→30, R≥0.5→20, R≥0→10, R<0→0
    """
    # Component 1: confluence at entry (0-25). CONFIG-DERIVED baseline — the prior hardcoded
    # `9` went stale when the board lowered the entry floor (2026-06-30), scoring valid
    # score-8 (half) AND score-9 (FULL-conviction) entries as 0 and biasing the Kelly TQI
    # feedback downward. Anchor = CONVICTION_SKIP_BELOW (the min enterable score); min-entry
    # earns a 5pt floor (it cleared the gate), scaling to the 12-pt max = 25. Board 2-0
    # (Thorp/Kelly, LdP/feature-quality) + Gro + GAI; floor=5 (3-1 vs LdP's 2). TQI is
    # informational + Kelly rolling-quality feedback only — zero direct capital gate.
    entry_score = trade.get("score", 9)
    _min_entry  = config.CONVICTION_SKIP_BELOW          # effective min enterable score (8/12 today)
    _max_score  = sum(config.SCORE_WEIGHTS.values())    # 12-pt confluence max (canonical — matches validate_config)
    _span       = max(1, _max_score - _min_entry)       # guard div-by-zero if floor ever == max
    score_pts   = round(min(25.0, 5.0 + max(0.0, entry_score - _min_entry) / _span * 20.0))

    # Component 2: exit reason quality
    reason     = trade.get("exit_reason", "other")
    reason_pts = {
        "target":               35,
        "trail_stop":           28,
        "signal":               18,
        "opposite_signal":      18,
        "thesis_invalidation":  15,
        "pm_exit":              12,
        "overnight_atr_buffer_exit":  10,
        "hard_stop":             0,
    }.get(reason, 8)

    # Component 3: R-multiple (pnl / initial risk in dollars)
    entry     = trade.get("entry_price") or 0.0
    stop      = trade.get("stop")        or 0.0
    pnl       = trade.get("pnl")         or 0.0
    qty       = trade.get("qty", 1) or 1  # Finding 7: original qty for R-multiple base, not qty_remaining
    risk_base = abs(entry - stop) * qty
    r_mult    = (pnl / risk_base) if risk_base > 0 else 0.0
    if r_mult >= 2.0:
        r_pts = 40
    elif r_mult >= 1.0:
        r_pts = 30
    elif r_mult >= 0.5:
        r_pts = 20
    elif r_mult >= 0.0:
        r_pts = 10
    else:
        r_pts = 0

    return min(100, score_pts + reason_pts + r_pts)


def _record_partial_tqi(symbol: str, trade: dict, qty_closed: int,
                         exit_price: float, t_idx: int) -> None:
    """
    Build #14: Log a partial-exit quality score (0-65) after each tranche close.
    Simplified TQI — entry score belongs to the full-exit score at position close.

    Components:
      Tranche quality (0-25): T1→10, T2→18, T3→25 (later = more profit extracted)
      R-multiple on partial  (0-40): same scale as full TQI
    Logged only — not added to _tqi_history (full exit is the canonical record).
    """
    # Component 1: tranche quality
    tranche_pts = {0: 10, 1: 18, 2: 25}.get(t_idx, 10)

    # Component 2: R-multiple on the partial close
    entry  = trade.get("entry_price") or 0.0
    stop   = trade.get("stop")        or 0.0
    direction = trade.get("direction", "long")
    pnl    = (exit_price - entry) * qty_closed if direction == "long" \
             else (entry - exit_price) * qty_closed
    risk_base = abs(entry - stop) * qty_closed
    r_mult    = (pnl / risk_base) if risk_base > 0 else 0.0
    if r_mult >= 2.0:
        r_pts = 40
    elif r_mult >= 1.0:
        r_pts = 30
    elif r_mult >= 0.5:
        r_pts = 20
    elif r_mult >= 0.0:
        r_pts = 10
    else:
        r_pts = 0

    partial_tqi = tranche_pts + r_pts
    logger.info(
        f"[{symbol}] Partial TQI T{t_idx + 1}: {partial_tqi}/65 "
        f"(tranche={tranche_pts} R={r_mult:.2f}x → {r_pts}pts) | "
        f"P&L ${pnl:.2f}"
    )


def _record_tqi(trade: dict, kelly: "KellySizer") -> None:
    """Compute TQI for a just-closed trade, update KellySizer history, and log.

    Phase 0.5: _tqi_history moved to kelly._tqi_history.
    Disk persistence handled by kelly.append_tqi() — atomic write in KellySizer.

    UNVERIFIED-FILL GUARD (2026-07-16, board + Gro + GAI): when the close fill could not
    be recovered, record_exit stores a FABRICATED pnl (exit_price = entry_price fallback →
    pnl = 0.00). A TQI computed from that is not just wrong, it is wrong in a DANGEROUS
    direction: _compute_tqi's R-multiple tier gives `r_mult >= 0 → 10 pts` but a REAL LOSS
    → 0 pts, so a suppressed loss scores 10 points TOO HIGH. That inflates the rolling
    average consumed by AB-3 (entry_logic.py:1128-1150), which does `dollar_cap *=
    _tqi_kelly_adj` — so an inflated TQI DEMOTES LESS and sizes positions LARGER. (Real
    case: RIVN's true -$41 on 7/7 was recorded as $0.00 and scored 10 instead of 0.)
    So: never feed a fabricated score into a rolling average that gates sizing. The score
    is still computed + stored on the trade for the audit trail; it enters _tqi_history
    only once the fill is VERIFIED — appended by patch_exit_pnl when it repairs the P&L.
    Mirrors the established exclusion pattern (kelly.rebuild_from_trades:409,
    portfolio_tracker.get_stats). If the fill is never recovered the trade contributes NO
    TQI — a missing entry (honest) rather than a fabricated one (poison).
    """
    tqi = _compute_tqi(trade)
    trade["tqi_score"] = tqi   # H-1: persist to trade record → EOD file → weekly_review.py
    symbol  = trade.get("symbol", "?")
    reason  = trade.get("exit_reason", "?")
    if trade.get("_fill_unverified"):
        logger.warning(
            "[%s] TQI %d/100 (%s) computed from an UNVERIFIED fill (pnl is the "
            "entry_price fallback, not real) — stored on the trade for audit but NOT "
            "added to the rolling TQI history that gates AB-3 sizing. It will be "
            "appended with a TRUE score if fill reconciliation repairs the P&L.",
            symbol, tqi, reason,
        )
        return
    kelly.append_tqi(tqi)
    rolling_avg = (
        round(sum(kelly._tqi_history) / len(kelly._tqi_history), 1)
        if kelly._tqi_history else 0
    )
    r_count = len(kelly._tqi_history)
    logger.info(
        f"[{symbol}] TQI: {tqi}/100 ({reason}) | "
        f"Rolling {r_count}-trade avg: {rolling_avg:.1f}/100"
    )


# ─── PARTIAL EXITS ───────────────────────────────────────────────────────────

def check_partial_exits(tracker: "PortfolioTracker", kelly: "KellySizer", risk: "RiskManager" = None, mri: "MacroRiskIndex" = None, last_vix: float = 0.0):  # type: ignore[assignment]
    """
    4-tranche scaled profit taking (Item 3).

    Tranches T1-T3 are partial exits at 20/40/60% of full target distance.
    T4 (100% = full target) is handled by C-3 in check_exits — not here.
    Each tranche closes 25% of the original position qty.

    State field: profit_tranche_level (0–3) in trade dict.
    Backward compat: partial_exited=True without profit_tranche_level → level 1.

    Trail stop ratchets every scan once active; trail stop hit closes remainder.
    Trail stop does NOT block T2/T3 tranche checks — both run each cycle.
    """
    import main as _main  # lazy: avoids circular import at module load
    _close_pos = close_position  # alias — module-level import, no re-import needed
    # B1 fix: _partial_fail_counts now in execution/lifecycle.py — get live reference
    _partial_fail_counts = _get_partial_fail_counts()

    TRANCHE_FRACS = [0.40, 0.60, 1.00]   # T1, T2, T3 as fraction of full target dist
    TRANCHE_SHARE = 0.33                  # each tranche closes 33% of original qty
    TRAIL_PHASE_MULTS = {1: 0.75, 2: 0.50, 3: 0.25}  # trail dist multiplier per phase

    for symbol, trade in list(tracker.open_trades.items()):
        # QHM ownership guard (Option B, 2026-07-01): skip QHM-held symbols — the
        # main bot must not partial-close/trail a QHM position. The collision
        # alert is fired once by check_exits (same cycle); keep this a quiet skip.
        if symbol in _get_qhm_syms():
            logger.debug("[%s] QHM-held — skipped by main-bot partial-exit management.", symbol)
            continue
        direction   = trade["direction"]
        entry_price = trade["entry_price"]
        atr_value   = trade.get("atr_value") or 0
        qty_orig    = trade["qty"]
        qty_rem     = trade.get("qty_remaining", qty_orig)

        try:
            df = fetch_bars(symbol, config.TF_15M, num_bars=2)
            if df.empty:
                continue
            current_price = float(df["close"].iloc[-1])
        except Exception as _e:
            logger.error(
                "[%s] Partial exit 15M price fetch failed: %s: %s "
                "— skipping partial checks this cycle",
                symbol, type(_e).__name__, _e,
            )
            continue

        # ── Live price override — Alpaca Data REST API ───────────────────────
        # DATA-2c: replaced yfinance fast_info with get_latest_trade().
        # Alpaca 15m bars can lag 15 min on fast-moving names; last trade
        # gives millisecond-precision SIP price for accurate tranche triggers.
        try:
            _px_live = get_latest_trade(symbol)
            if _px_live and _px_live > 0:
                logger.debug(
                    f"[{symbol}] Partial exit live price ${_px_live:.2f} "
                    f"(bar was ${current_price:.2f})"
                )
                current_price = _px_live
        except Exception as _e:
            logger.warning(
                "[%s] Live price override failed (partial): %s: %s "
                "— using 15M bar close $%.2f",
                symbol, type(_e).__name__, _e, current_price,
            )

        # DS P0: symbol with active GTC cancel deferral may already be closed by
        # Alpaca's stop. Skip partial exits — check_exits handles cleanup.
        if trade.get("_gtc_sig_defer_count", 0) > 0:
            continue

        # ── Backward compat: treat legacy partial_exited as tranche level 1 ──
        if trade.get("partial_exited") and "profit_tranche_level" not in trade:
            trade["profit_tranche_level"] = 1

        tranche_lvl = trade.get("profit_tranche_level", 0)

        # ── Trail stop: ratchet every scan, close remainder if hit ───────────
        if trade.get("trail_stop") and atr_value > 0:
            _phase_mult = TRAIL_PHASE_MULTS.get(trade.get("trail_phase"), config.TRAIL_STOP_ATR_MULT)
            trail_dist  = atr_value * _phase_mult
            _stop_floor = trade.get("stop", 0.0)
            new_trail   = (
                max(round(current_price - trail_dist, 2), _stop_floor) if direction == "long"
                else min(round(current_price + trail_dist, 2), _stop_floor) if _stop_floor
                else round(current_price + trail_dist, 2)
            )
            # BVR-1 fix: evaluate trail_hit BEFORE ratcheting.
            # If ratchet runs first and stop is hit in the same cycle,
            # close_position() fires against shares still held_for_orders → 40310000.
            trail_stop = trade.get("trail_stop")
            trail_hit  = (
                (direction == "long"  and current_price <= trail_stop) or
                (direction == "short" and current_price >= trail_stop)
            )
            if trail_hit:
                _trail_phase = trade.get("trail_phase")
                if _trail_phase in (1, 2) and atr_value > 0:
                    _qty_to_adv = (
                        min(max(1, round(qty_orig * TRANCHE_SHARE)), qty_rem - 1)
                        if qty_rem > 1 else 0
                    )
                    if _qty_to_adv >= 1:
                        for _skey in ("rth_day_stop_order_id", "gtc_stop_order_id"):
                            _soid = trade.get(_skey)
                            if _soid:
                                cancel_order(_soid)
                                trade[_skey] = None
                        time.sleep(0.1)
                        _tph_ts = time.time()
                        _tph_ok = partial_close_position(symbol, _qty_to_adv)
                        if _tph_ok:
                            _tph_fill = _fetch_actual_fill_price(
                                symbol, trade, poll_secs=0.3, submitted_after=_tph_ts
                            )
                            _tph_pnl = (
                                (_tph_fill - entry_price) * _qty_to_adv if direction == "long"
                                else (entry_price - _tph_fill) * _qty_to_adv
                            )
                            trade["qty_remaining"] = qty_rem - _qty_to_adv
                            trade["partial_pnl"]   = round(trade.get("partial_pnl", 0.0) + _tph_pnl, 2)
                            trade["trail_phase"]   = _trail_phase + 1
                            _new_ph_mult  = TRAIL_PHASE_MULTS[_trail_phase + 1]
                            _new_ph_dist  = atr_value * _new_ph_mult
                            _new_ph_trail = (
                                max(round(current_price - _new_ph_dist, 2), entry_price)
                                if direction == "long"
                                else min(round(current_price + _new_ph_dist, 2), entry_price)
                            )
                            trade["trail_stop"] = _new_ph_trail
                            tracker._save_log()
                            logger.info(
                                f"[{symbol}] Trail phase {_trail_phase} hit @ ${current_price:.2f} "
                                f"— partial {_qty_to_adv}sh @ ${_tph_fill:.2f} P&L ${_tph_pnl:.2f}, "
                                f"phase → {_trail_phase + 1} trail={_new_ph_trail:.2f} "
                                f"({_new_ph_mult}×ATR)"
                            )
                            continue
                        else:
                            logger.warning(
                                f"[{symbol}] Trail phase {_trail_phase} partial close FAILED "
                                f"({_qty_to_adv}sh) — falling through to full close. "
                                f"GTC stop already cancelled. Verify Alpaca."
                            )
                logger.info(
                    f"[{symbol}] Trail stop hit @ ${current_price:.2f} "
                    f"(stop ${trail_stop:.2f}) — closing remainder"
                )
                # Cancel existing stops before close to release held_for_orders shares.
                for _skey in ("rth_day_stop_order_id", "gtc_stop_order_id"):
                    _soid = trade.get(_skey)
                    if _soid:
                        cancel_order(_soid)
                        trade[_skey] = None
                time.sleep(0.1)
                _ts_ts  = time.time()
                _close_pos(symbol)
                _ts_fill = _fetch_actual_fill_price(
                    symbol, trade, poll_secs=0.3, submitted_after=_ts_ts
                )
                pnl = tracker.record_exit(symbol, _ts_fill, reason="trail_stop",
                                          mri_level=mri.level() if mri else "NORMAL")
                if tracker.closed_trades:
                    _record_tqi(tracker.closed_trades[-1], kelly)
                if risk is not None:
                    risk.register_close(pnl or 0.0)
                if pnl is not None:
                    kelly.record_trade(
                        direction, trade["trade_mode"],
                        entry_price, _ts_fill,
                        trade["stop"], qty_rem,
                    )
                alert_exit(
                    symbol=symbol, direction=direction,
                    pnl=pnl or 0.0, reason="trail_stop",
                    tqi=tracker.closed_trades[-1].get("tqi_score", 0) if tracker.closed_trades else 0,
                )
                continue

            # Not hit — ratchet if trail price moved.
            _old_trail_px = trail_stop
            tracker.update_trail_stop(symbol, new_trail)
            _cur_trail_px = trade.get("trail_stop")

            if _cur_trail_px != _old_trail_px and _cur_trail_px is not None:
                # Robust cancel block (ported from partial exit CRITICAL-1 fix).
                _tr_cancel_ok = True
                for _skey in ("rth_day_stop_order_id", "gtc_stop_order_id"):
                    _soid = trade.get(_skey)
                    if _soid:
                        if not cancel_order(_soid):
                            try:
                                _ord_status = get_order(_soid)
                                _gone = _ord_status is None or getattr(
                                    _ord_status, "status", ""
                                ) in ("canceled", "filled", "expired", "done_for_day")
                            except Exception as _ve:
                                logger.error(
                                    f"[{symbol}] Cannot verify {_skey} {_soid} status "
                                    f"after cancel failure — {_ve}. Skipping trail resubmit."
                                )
                                _tr_cancel_ok = False
                                break
                            if _gone:
                                logger.warning(
                                    f"[{symbol}] cancel_order({_soid}) returned False "
                                    f"but order confirmed gone. Clearing ID."
                                )
                                trade[_skey] = None
                            else:
                                logger.error(
                                    f"[{symbol}] {_skey} {_soid} still live after "
                                    f"cancel failure — skipping trail ratchet resubmit."
                                )
                                _tr_cancel_ok = False
                                break
                        else:
                            trade[_skey] = None
                            logger.info(
                                f"[{symbol}] {_skey} {_soid} cancelled for trail ratchet."
                            )
                if not _tr_cancel_ok:
                    continue

                time.sleep(0.2)  # allow cancel propagation before resubmit

                _qty_left = trade.get("qty_remaining", trade.get("qty", 0))
                _stop_side = "sell" if direction == "long" else "buy"
                if _qty_left >= 1:
                    # Poll for held_for_orders to clear (ported from partial exit logic).
                    _tr_avail = 0
                    _tr_submit = True
                    _pos_tr = None
                    for _thp in range(5):  # max 2.0s wall-clock (4 × 0.5s sleeps)
                        _pos_tr = get_open_position(symbol)
                        if _pos_tr is None:
                            _tr_submit = False
                            break
                        _tr_qty_avail = getattr(_pos_tr, "qty_available", None)
                        if _tr_qty_avail is None:
                            _tr_submit = False
                            break
                        _tr_avail = int(float(_tr_qty_avail))
                        if _tr_avail >= _qty_left:
                            break
                        logger.debug(
                            "[%s] trail ratchet held_for_orders poll %d/5: avail=%d want=%d",
                            symbol, _thp + 1, _tr_avail, _qty_left,
                        )
                        if _thp < 4:
                            time.sleep(0.5)
                    else:
                        logger.warning(
                            "[%s] trail ratchet held_for_orders not released after 2.5s "
                            "(avail=%d want=%d) — submitting anyway",
                            symbol, _tr_avail, _qty_left,
                        )
                        try:
                            alert_gtc_failed(
                                symbol=symbol, side=_stop_side, stop_px=_cur_trail_px,
                                reason=(
                                    f"trail ratchet held_for_orders persisted >2.5s "
                                    f"(avail={_tr_avail} want={_qty_left})"
                                ),
                            )
                        except Exception as _tr_hf_e:
                            logger.error(
                                "[%s] trail ratchet held_for_orders alert failed: %s",
                                symbol, _tr_hf_e,
                            )

                    # Alpaca qty clamp (downward only — upward mismatch = stale bot state).
                    if _tr_submit and _pos_tr is not None:
                        _tr_alpaca_qty = int(float(getattr(_pos_tr, "qty", 0)))
                        if _tr_alpaca_qty < _qty_left:
                            logger.error(
                                "[%s] trail ratchet qty mismatch: bot=%d Alpaca=%d — clamping",
                                symbol, _qty_left, _tr_alpaca_qty,
                            )
                            _qty_left = _tr_alpaca_qty
                            trade["qty_remaining"] = _qty_left
                            tracker._save_log()
                        elif _tr_alpaca_qty > _qty_left:
                            logger.warning(
                                "[%s] trail ratchet qty mismatch: bot=%d Alpaca=%d (Alpaca higher)"
                                " — not clamping",
                                symbol, _qty_left, _tr_alpaca_qty,
                            )
                        if _qty_left < 1:
                            _tr_submit = False
                            logger.info(
                                "[%s] trail ratchet: Alpaca qty clamped to 0 "
                                "— skipping stop resubmit (position effectively closed)",
                                symbol,
                            )

                    # BVR-1 core fix: GTC/DAY mutual exclusion.
                    # overnight=True → GTC stop (persists through close).
                    # overnight=False → DAY stop (expires at close).
                    # datetime.now(ET).hour >= 16 is dead code during RTH (BoD ruling).
                    if _tr_submit:
                        if trade.get("overnight"):
                            logger.debug(
                                "[%s] trail ratchet overnight=True → GTC stop", symbol
                            )
                            _tr_gtc = submit_gtc_stop_order(
                                symbol=symbol, qty=_qty_left,
                                side=_stop_side, stop_price=_cur_trail_px,
                            )
                            if _tr_gtc:
                                tracker.set_gtc_stop_order_id(symbol, str(_tr_gtc.id))  # type: ignore[attr-defined]
                                _log_trade_event(
                                    "stop_promotion", symbol=symbol, price=_cur_trail_px,
                                    size=_qty_left, score=trade.get("score", 0),
                                    mri_level=mri.level() if mri else "NORMAL",
                                    data_source="alpaca_data",
                                    stop_type="trail_ratchet_gtc",
                                )
                                logger.info(
                                    f"[{symbol}] Trail ratchet → GTC stop updated: "
                                    f"{_qty_left}sh @ ${_cur_trail_px:.2f} | {_tr_gtc.id}"  # type: ignore[attr-defined]
                                )
                            else:
                                logger.critical(
                                    f"[{symbol}] Trail ratchet GTC resubmit FAILED — "
                                    f"overnight {_qty_left}sh has no exchange-level stop."
                                )
                                try:
                                    alert_gtc_failed(
                                        symbol=symbol, side=_stop_side,
                                        stop_px=_cur_trail_px,
                                        reason=(
                                            "trail ratchet GTC resubmit failed "
                                            "— overnight position unprotected"
                                        ),
                                    )
                                except Exception as _tga_e:
                                    logger.error(
                                        f"[{symbol}] Trail ratchet GTC orphan alert failed: {_tga_e}"
                                    )
                        else:
                            logger.debug(
                                "[%s] trail ratchet overnight=False → DAY stop", symbol
                            )
                            _tr_ord = submit_day_stop_order(
                                symbol=symbol, qty=_qty_left,
                                side=_stop_side, stop_price=_cur_trail_px,
                            )
                            if _tr_ord:
                                trade["rth_day_stop_order_id"] = str(_tr_ord.id)  # type: ignore[attr-defined]
                                tracker._save_log()  # persist DAY stop ID immediately (Data Integrity Item 5)
                                _log_trade_event(
                                    "stop_promotion", symbol=symbol, price=_cur_trail_px,
                                    size=_qty_left, score=trade.get("score", 0),
                                    mri_level=mri.level() if mri else "NORMAL",
                                    data_source="alpaca_data",
                                    stop_type="trail_ratchet_day",
                                )
                                logger.info(
                                    f"[{symbol}] Trail ratchet → DAY stop updated: "
                                    f"{_qty_left}sh @ ${_cur_trail_px:.2f} | {_tr_ord.id}"  # type: ignore[attr-defined]
                                )
                            else:
                                logger.warning(
                                    f"[{symbol}] Trail ratchet DAY stop re-submit FAILED "
                                    f"— {_qty_left}sh unprotected at exchange level."
                                )
            # Trail stop active but not hit — fall through to tranche check below

        # ── Skip if partial exits disabled or no target to compute levels ─────
        if not config.PARTIAL_EXIT_ENABLED:
            continue
        target = trade.get("target")
        _t3_pending = tranche_lvl == len(TRANCHE_FRACS) - 1
        if target is None or qty_rem < 1:
            continue
        if qty_rem < 2 and not _t3_pending:
            continue

        target_dist = (target - entry_price) if direction == "long" else (entry_price - target)
        if target_dist <= 0:
            continue

        # ── AB-6R: stressed-regime T2 threshold ──────────────────────────────
        # Sinclair/Thorp/Tudor Jones: in BROAD_* / EXTREME regimes or VIX > 30,
        # T2 drops to 0.85R (measured from entry in stop_distance units) rather
        # than 1.0R.  Captures profit before mean reversion completes.
        # Normal regime keeps T2 at 1.0R (TRANCHE_FRACS[1] = 0.40 of target_dist).
        _ab6r_stressed = (
            _main._spy_event_type in ("EXTREME", "BROAD_GEO_CONFLICT", "BROAD_GEO_ENERGY",
                                "BROAD_MACRO_MONETARY", "BROAD_MACRO_CREDIT", "BROAD_MACRO_FX",
                                "BROAD_MACRO_SYSTEMIC", "BROAD_GLOBAL_ASIA", "BROAD_GLOBAL_EU",
                                "BROAD_TECHNICAL")
            or last_vix > 30
        )
        # AB-6R: use original_stop (immutable at entry) not current stop,
        # which may have been moved to breakeven after T1.
        _original_stop = trade.get("original_stop") or trade.get("stop", entry_price)
        _stop_dist = abs(entry_price - _original_stop)

        # ── Scan tranches in order; execute those reached and allowed ─────────
        _cycle_start = time.monotonic()
        _MAX_CYCLE_BUDGET = 8.0
        for t_idx in range(tranche_lvl, len(TRANCHE_FRACS)):
            frac = TRANCHE_FRACS[t_idx]

            # AB-6R: override T2 price in stressed regimes
            if t_idx == 1 and _ab6r_stressed and _stop_dist > 0:
                _t2_r = 0.85
                t_price = (entry_price + _t2_r * _stop_dist if direction == "long"
                           else entry_price - _t2_r * _stop_dist)
                logger.debug(
                    f"[{symbol}] AB-6R T2 stressed threshold: 0.85R = ${t_price:.2f} "
                    f"(VIX={last_vix:.1f} SPY={_main._spy_event_type or 'CLEAR'})"
                )
            # Simons: T3 at 50% in stressed regimes (BROAD_*/VIX>30) — alpha
            # decays above 1.2x ATR in high-vol; mean reversion faster than 60%.
            # Normal regimes keep T3 at 60% (TRANCHE_FRACS[2]).
            elif t_idx == 2 and _ab6r_stressed:
                _t3_frac = 0.50
                t_price = (entry_price + _t3_frac * target_dist if direction == "long"
                           else entry_price - _t3_frac * target_dist)
                logger.debug(
                    f"[{symbol}] Simons T3 stressed threshold: 50% = ${t_price:.2f} "
                    f"(VIX={last_vix:.1f} SPY={_main._spy_event_type or 'CLEAR'})"
                )
            elif direction == "long":
                t_price = entry_price + frac * target_dist
            else:
                t_price = entry_price - frac * target_dist

            if direction == "long":
                t_hit = current_price >= t_price
            else:
                t_hit = current_price <= t_price

            if not t_hit:
                break   # price hasn't reached this level; higher levels won't be hit either

            # DS Finding 3: abort remaining tranches if cycle blocking budget exhausted
            _elapsed_budget = time.monotonic() - _cycle_start
            if _elapsed_budget >= _MAX_CYCLE_BUDGET:
                logger.warning(
                    f"[{symbol}] Partial exit cycle budget exhausted "
                    f"({_elapsed_budget:.1f}s >= {_MAX_CYCLE_BUDGET}s) "
                    f"— skipping remaining tranches to prevent watchdog SIGKILL."
                )
                break

            # ── Execute partial close ─────────────────────────────────────
            qty_each   = max(1, round(qty_orig * TRANCHE_SHARE))
            qty_to_cls = qty_rem if t_idx == len(TRANCHE_FRACS) - 1 else min(qty_each, qty_rem - 1)
            if qty_to_cls < 1:
                trade["profit_tranche_level"] = t_idx + 1
                # BUG-1 fix: qty too small for a real partial close (e.g. 1-share position).
                # Partial close silently skips every tranche so trail stop never activates.
                # At T1, always move hard stop to breakeven (H-15: fires regardless of atr_value).
                # Trail stop only activates if ATR data is available.
                if t_idx == 0 and not trade.get("trail_stop"):
                    trade["stop"] = entry_price   # move hard stop to breakeven — always
                    if atr_value > 0:
                        _ts_dist = atr_value * config.TRAIL_STOP_ATR_MULT
                        _ts_floor = trade.get("stop", 0.0)  # already = entry_price (set above)
                        _ts_val   = (max(round(current_price - _ts_dist, 2), _ts_floor)
                                     if direction == "long"
                                     else (min(round(current_price + _ts_dist, 2), _ts_floor)
                                           if _ts_floor else round(current_price + _ts_dist, 2)))
                        tracker.update_trail_stop(symbol, _ts_val)
                        logger.info(
                            f"[{symbol}] T1 reached (qty={qty_rem}, too small for partial) — "
                            f"trail stop activated @ ${_ts_val:.2f}, hard stop → breakeven ${entry_price:.2f}"
                        )
                    else:
                        logger.info(
                            f"[{symbol}] T1 reached (qty={qty_rem}, atr=0) — "
                            f"hard stop moved to breakeven ${entry_price:.2f} (no ATR for trail stop)"
                        )
                tracker._save_log()
                break

            # CRITICAL-1 fix: cancel any live DAY/GTC stop before partial close.
            # Alpaca holds shares for open stop orders (held_for_orders) and returns
            # 40310000 if a second sell order tries to move those same shares.
            # Cancel the stop first, close the partial, then re-protect the remainder.
            _gtc_cancel_ok = True
            for _skey in ("rth_day_stop_order_id", "gtc_stop_order_id"):
                _soid = trade.get(_skey)
                if _soid:
                    if not cancel_order(_soid):
                        try:
                            _ord_status = get_order(_soid)
                            _gone = _ord_status is None or getattr(
                                _ord_status, "status", ""
                            ) in ("canceled", "filled", "expired", "done_for_day")
                        except Exception as _ve:
                            logger.error(
                                f"[{symbol}] Cannot verify {_skey} {_soid} status "
                                f"after cancel failure — {_ve}. "
                                f"Failing closed to avoid naked position."
                            )
                            _gtc_cancel_ok = False
                            break
                        if _gone:
                            logger.warning(
                                f"[{symbol}] cancel_order({_soid}) returned False "
                                f"but order confirmed gone "
                                f"({getattr(_ord_status, 'status', 'None')}). "
                                f"Clearing ID."
                            )
                            trade[_skey] = None
                        else:
                            logger.error(
                                f"[{symbol}] {_skey} {_soid} still live after "
                                f"cancel failure — skipping tranche to avoid "
                                f"40310000 / naked position."
                            )
                            _gtc_cancel_ok = False
                            break
                    else:
                        trade[_skey] = None
                        logger.info(
                            f"[{symbol}] {_skey} {_soid} cancelled before "
                            f"partial close."
                        )
            if not _gtc_cancel_ok:
                break

            _pc_ts = time.time()
            success = partial_close_position(symbol, qty_to_cls)
            if success:
                # FIX: Fetch actual fill price for partial close to ensure P&L accuracy
                fill_price = _fetch_actual_fill_price(symbol, trade, poll_secs=0.3, submitted_after=_pc_ts)
                pnl = (
                    (fill_price - entry_price) * qty_to_cls if direction == "long"
                    else (entry_price - fill_price) * qty_to_cls
                )
                current_price = fill_price  # Update for the rest of the block

                # Update trade dict directly (multi-tranche compatible)
                trade["qty_remaining"]        = trade.get("qty_remaining", qty_orig) - qty_to_cls
                trade["profit_tranche_level"] = t_idx + 1
                trade["partial_exited"]       = True   # backward compat for downstream reads
                trade["partial_exit_price"]   = fill_price
                trade["partial_exit_time"]    = datetime.now(ET).isoformat()
                # Bug A fix: accumulate partial P&L across tranches so kill switch
                # and record_exit() see the correct running total.
                trade["partial_pnl"]          = round(trade.get("partial_pnl", 0.0) + pnl, 2)

                # T1: pin stop at entry_price (breakeven) and clear any prior trail
                # so line 2121 (_stop_px) uses entry_price, not a stale trail level.
                # T2/T3: activate ATR trail floored at the promoted stop.
                if t_idx == 0:
                    trade["stop"]        = entry_price
                    trade["trail_phase"] = 1
                    if atr_value > 0:
                        trail_dist  = atr_value * TRAIL_PHASE_MULTS[1]
                        _stop_floor = entry_price
                        trail_stop  = (
                            max(round(current_price - trail_dist, 2), _stop_floor) if direction == "long"
                            else min(round(current_price + trail_dist, 2), _stop_floor)
                        )
                        tracker.update_trail_stop(symbol, trail_stop)
                        logger.info(
                            f"[{symbol}] T1 partial: trail activated (phase 1) @ ${trail_stop:.2f} "
                            f"({TRAIL_PHASE_MULTS[1]}×ATR=${atr_value:.2f}) floor=${entry_price:.2f}"
                        )
                    else:
                        trade["trail_stop"] = None
                elif atr_value > 0:
                    trail_dist  = atr_value * config.TRAIL_STOP_ATR_MULT
                    _stop_floor = trade.get("stop", 0.0)
                    trail_stop  = (
                        max(round(current_price - trail_dist, 2), _stop_floor) if direction == "long"
                        else (min(round(current_price + trail_dist, 2), _stop_floor)
                              if _stop_floor else round(current_price + trail_dist, 2))
                    )
                    tracker.update_trail_stop(symbol, trail_stop)

                # Re-protect remaining shares after partial fill.
                # GAI Q9: DAY and GTC stops lock the same shares — submit only one.
                # If overnight → GTC only (persists through close).
                # If intraday → DAY only (expires at close).
                # Submitting both causes DAY to lock qty_available=0 so GTC always fails.
                # DS audit finding: never fall back to qty_orig — stale after restart.
                # If qty_remaining is missing, query Alpaca directly.
                _new_rem = trade.get("qty_remaining")
                if _new_rem is None:
                    _pos_fallback = get_open_position(symbol)
                    _new_rem = int(float(_pos_fallback.qty)) if _pos_fallback else 0
                    if _new_rem > 0:
                        trade["qty_remaining"] = _new_rem
                        tracker._save_log()
                        logger.error(
                            "[%s] qty_remaining missing after partial — recovered from Alpaca: %d",
                            symbol, _new_rem,
                        )
                    else:
                        logger.warning(
                            "[%s] qty_remaining missing and Alpaca confirms position closed"
                            " — skipping stop re-submission",
                            symbol,
                        )
                _stop_px = trade.get("trail_stop") or trade.get("stop")
                if _stop_px and _new_rem >= 1:
                    # Poll for held_for_orders to clear after cancel + partial fill.
                    # Cancel propagation and fill settlement leave shares held;
                    # immediate stop submission fails with error 40310000.
                    # _submit_stop=False only when position closed or qty_available missing.
                    _avail = 0
                    _submit_stop = True
                    for _hp in range(5):  # max 2.5s wall-clock
                        _pos_hf = get_open_position(symbol)
                        if _pos_hf is None:
                            _submit_stop = False  # position closed concurrently — skip stop
                            break
                        _qty_avail = getattr(_pos_hf, "qty_available", None)
                        if _qty_avail is None:
                            logger.warning(
                                "[%s] Position missing qty_available field — skipping stop",
                                symbol,
                            )
                            _submit_stop = False  # unknown state — skip to avoid 40310000
                            break
                        _avail = int(float(_qty_avail))
                        if _avail >= _new_rem:
                            break  # poll cleared — proceed with submit
                        logger.debug(
                            "[%s] held_for_orders poll %d/5: avail=%d want=%d",
                            symbol, _hp + 1, _avail, _new_rem,
                        )
                        if _hp < 4:
                            time.sleep(0.5)
                    else:
                        # Loop exhausted without clearing — submit anyway per board decision.
                        logger.warning(
                            "[%s] held_for_orders not released after 2.5s "
                            "(avail=%d want=%d) — submitting stop anyway",
                            symbol, _avail, _new_rem,
                        )
                        try:
                            alert_gtc_failed(
                                symbol=symbol,
                                side="sell" if direction == "long" else "buy",
                                stop_px=_stop_px,
                                reason=(
                                    f"held_for_orders persisted >2.5s "
                                    f"(avail={_avail} want={_new_rem})"
                                ),
                            )
                        except Exception as _hf_alert_e:
                            logger.error(
                                "[%s] held_for_orders alert failed: %s",
                                symbol, _hf_alert_e,
                            )
                        # _submit_stop remains True — submit despite timeout

                    # Clamp _new_rem to Alpaca actual qty — prevents requesting > existing_qty
                    # when trade state is stale after a restart (DS audit: root cause of PANW failure).
                    if _submit_stop and _pos_hf is not None:
                        _alpaca_qty = int(float(getattr(_pos_hf, "qty", 0)))
                        if _alpaca_qty < _new_rem:
                            # Alpaca has fewer shares than bot tracks — clamp downward.
                            # Root cause of PANW failure: stale qty_remaining after restart.
                            logger.error(
                                "[%s] qty mismatch: bot=%d Alpaca=%d — clamping to Alpaca value",
                                symbol, _new_rem, _alpaca_qty,
                            )
                            _new_rem = _alpaca_qty
                            trade["qty_remaining"] = _new_rem
                            tracker._save_log()
                        elif _alpaca_qty > _new_rem:
                            # Alpaca shows more shares than bot tracks — possible reconciliation
                            # error; do not clamp upward as bot state is authoritative for tranches.
                            logger.warning(
                                "[%s] qty mismatch: bot=%d Alpaca=%d (Alpaca higher) "
                                "— not clamping; possible reconciliation error",
                                symbol, _new_rem, _alpaca_qty,
                            )
                        if _new_rem < 1:
                            _submit_stop = False
                            logger.info(
                                "[%s] Alpaca confirms position fully closed — skipping stop re-submission",
                                symbol,
                            )

                    _stop_side = "sell" if direction == "long" else "buy"
                    if _submit_stop:
                        if trade.get("overnight"):
                            # Overnight: GTC stop persists through close.
                            _new_gtc = submit_gtc_stop_order(
                                symbol=symbol, qty=_new_rem,
                                side=_stop_side, stop_price=_stop_px,
                            )
                            if _new_gtc:
                                tracker.set_gtc_stop_order_id(symbol, str(_new_gtc.id))  # type: ignore[attr-defined]
                                _log_trade_event(
                                    "stop_promotion", symbol=symbol, price=_stop_px, size=_new_rem,
                                    score=trade.get("score", 0), mri_level=mri.level() if mri else "NORMAL",
                                    data_source="alpaca_data",
                                    stop_type="gtc_resubmit_after_partial",
                                )
                                logger.info(
                                    f"[{symbol}] GTC stop re-submitted after T{t_idx + 1} partial: "
                                    f"{_new_rem}sh @ ${_stop_px:.2f} | order {_new_gtc.id}"  # type: ignore[attr-defined]
                                )
                            else:
                                logger.critical(
                                    f"[{symbol}] GTC stop FAILED to re-submit after T{t_idx + 1} partial "
                                    f"— overnight position {_new_rem}sh has NO exchange-level stop. "
                                    f"Manual action required."
                                )
                                _log_trade_event(
                                    "gtc_stop_orphaned", symbol=symbol, price=0.0, size=_new_rem,
                                    score=trade.get("score", 0), mri_level=mri.level() if mri else "NORMAL",
                                    data_source="alpaca_data",
                                    stop_price=_stop_px,
                                )
                                try:
                                    alert_gtc_failed(
                                        symbol=symbol, side=_stop_side, stop_px=_stop_px,
                                        reason=f"re-submit failed after T{t_idx + 1} partial — overnight position unprotected",
                                    )
                                except Exception as _gtc_alert_e:
                                    logger.error(f"[{symbol}] GTC orphan alert failed: {_gtc_alert_e}")
                        else:
                            # Intraday: DAY stop expires at close.
                            _new_day_ord = submit_day_stop_order(
                                symbol=symbol, qty=_new_rem,
                                side=_stop_side, stop_price=_stop_px,
                            )
                            if _new_day_ord:
                                trade["rth_day_stop_order_id"] = str(_new_day_ord.id)  # type: ignore[attr-defined]
                                tracker._save_log()  # persist DAY stop ID immediately (Data Integrity Item 5)
                                _log_trade_event(
                                    "stop_promotion", symbol=symbol, price=_stop_px, size=_new_rem,
                                    score=trade.get("score", 0), mri_level=mri.level() if mri else "NORMAL",
                                    data_source="alpaca_data",
                                    stop_type="breakeven" if t_idx == 0 else "trail",
                                )
                                logger.info(
                                    f"[{symbol}] DAY stop re-submitted after T{t_idx + 1} partial: "
                                    f"{_new_rem} shares @ ${_stop_px:.2f} | order {_new_day_ord.id}"  # type: ignore[attr-defined]
                                )
                            else:
                                logger.error(
                                    f"[{symbol}] DAY stop re-submission FAILED after T{t_idx + 1} partial "
                                    f"— {_new_rem} shares unprotected. Set manual stop in Alpaca."
                                )
                    else:
                        logger.warning(
                            "[%s] Skipping stop re-submission — position closed or "
                            "qty_available unavailable (avail=%d want=%d).",
                            symbol, _avail, _new_rem,
                        )

                # C-13: Report partial P&L to kill switch so tranche losses
                # count toward daily loss limit — not just final close P&L.
                if risk is not None:
                    risk.register_close(pnl or 0.0)

                tracker._save_log()
                logger.info(
                    f"[{symbol}] T{t_idx + 1} PARTIAL EXIT: {qty_to_cls} shares "
                    f"@ ${current_price:.2f} | tranche P&L ${pnl:.2f} "
                    f"| cumulative partial_pnl ${trade['partial_pnl']:.2f} "
                    f"| Level {t_idx + 1}/3"
                )
                alert_partial(symbol=symbol, tranche=t_idx + 1, pnl=pnl,
                              qty=qty_to_cls, price=current_price)
                _log_trade_event(
                    "partial_exit", symbol=symbol, price=fill_price, size=qty_to_cls,
                    score=trade.get("score", 0), mri_level=mri.level() if mri else "NORMAL",
                    data_source="alpaca_data",
                    tranche=t_idx + 1, pnl=round(pnl, 2),
                    atr_value=trade.get("atr_value"),
                )
                _record_partial_tqi(symbol, trade, qty_to_cls, current_price, t_idx)

                try:
                    qty_rem = trade["qty_remaining"]
                except KeyError:
                    logger.error(
                        "[%s] CRITICAL: trade['qty_remaining'] missing after partial close. "
                        "qty_orig=%s qty_to_cls=%s t_idx=%s. "
                        "Using computed fallback to prevent infinite loop.",
                        symbol, qty_orig, qty_to_cls, t_idx,
                    )
                    qty_rem = qty_rem - qty_to_cls
                    trade["qty_remaining"] = qty_rem
                tranche_lvl = t_idx + 1
            else:
                # Partial close failed — track consecutive failures and alert
                _partial_fail_counts[symbol] = _partial_fail_counts.get(symbol, 0) + 1
                _fail_count = _partial_fail_counts[symbol]
                logger.error(
                    f"[{symbol}] Partial close FAILED (consecutive={_fail_count}) — "
                    f"position may be locked by a held GTC order. Check Alpaca."
                )
                if _fail_count == 1:
                    # Fix 4 / BUG-W1: auto-cancel all blocking open orders on first failure.
                    try:
                        _n_cancelled = cancel_open_orders_for_symbol(symbol)
                        logger.warning(
                            f"[{symbol}] Auto-cancelled {_n_cancelled} blocking order(s) "
                            f"(held_for_orders fix) — partial close will retry next cycle."
                        )
                    except Exception as _coe:
                        logger.error(f"[{symbol}] Auto-cancel blocking orders failed: {_coe}")
                elif _fail_count >= 2:
                    try:
                        from alerts import send_slack
                        send_slack(
                            f":rotating_light: *POSITION MANAGEMENT BLOCKED* :rotating_light:\n"
                            f"*{symbol}* partial close has failed *{_fail_count} consecutive times*.\n"
                            f"Position is likely locked by a held GTC order on Alpaca.\n"
                            f"*Manual action required* — check Alpaca orders for held_for_orders."
                        )
                    except Exception as _pfa:
                        logger.error(f"[{symbol}] Partial fail alert send failed: {_pfa}")
                break   # do not attempt higher tranches

            # Reset failure counter on any successful partial
            if symbol in _partial_fail_counts:
                del _partial_fail_counts[symbol]


# ─── EXIT CHECKS ─────────────────────────────────────────────────────────────

def _submit_gtc_limit_partial(symbol: str, qty: int, side: str, limit_price: float):
    """
    Submit a GTC limit order for tranche partial exits.
    broker.py's submit_limit_order only supports DAY TIF — this uses
    get_trading_client() directly for GTC+limit.
    side: "sell" for long positions, "buy" for short positions.
    Returns order object or None on failure.
    """
    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    if qty <= 0 or not (0 < limit_price < 99_999):
        logger.warning(f"[{symbol}] GTC limit partial skipped: qty={qty}, price={limit_price}")
        return None
    try:
        client     = get_trading_client()
        order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            limit_price=round(limit_price, 2),
            time_in_force=TimeInForce.GTC,
        )
        order = client.submit_order(order_data)
        logger.info(
            f"[{symbol}] GTC LIMIT PARTIAL submitted: {side.upper()} {qty} @ "
            f"${limit_price:.2f} | Order ID: {order.id}"  # type: ignore[union-attr]
        )
        return order
    except Exception as e:
        logger.error(f"[{symbol}] GTC limit partial failed: {e}")
        return None


# _submit_gtc_stop_close extracted to execution/lifecycle.py (Phase 2 Extraction 7).
# Imported at module level as: submit_gtc_stop_close as _submit_gtc_stop_close


def _cancel_open_gtc_orders(symbol, trade, tracker):
    """Shim — delegates to execution.gtc_manager (Phase 2 extraction)."""
    from execution.gtc_manager import cancel_open_gtc_orders
    return cancel_open_gtc_orders(symbol, trade, tracker)

# _submit_rth_day_stops DROPPED — confirmed dead code, never called. (user-approved 2026-05-11)


def check_exits(
    tracker: PortfolioTracker,
    risk: RiskManager,
    mri_level: str = "NORMAL",
    kelly: "KellySizer" = None,  # type: ignore[assignment]
    last_vix: float = 0.0,
    gate_state: "GateState" = None,  # type: ignore[assignment]
):
    """
    Check all open MTF positions for exit signals.

    Two exit paths per position:
      1. Hard stop    — immediate exit when price breaches trade["stop"] (or trail_stop).
      2. Reversal scan counter:
           Overnight  — exit when reversal_scan_count >= OVERNIGHT_REVERSAL_SCAN_MIN (10)
                        force-exit when count >= OVERNIGHT_REVERSAL_SCAN_MAX (15).
           RTH        — exit when reversal_scan_count >= RTH_REVERSAL_SCAN_MIN (3).
           Signal clear → reset count to 0 each scan.
    gate_state: Phase 0.5 — GateState instance (conviction_streak + entry_confirm_buffer).
    """
    import main as _main  # lazy: avoids circular import at module load
    if gate_state is None:
        raise RuntimeError(
            "check_exits() called without gate_state — Phase 0.5 refactor incomplete"
        )
    if kelly is None:
        raise RuntimeError(
            "check_exits() called without kelly — Phase 0.5 refactor incomplete"
        )
    closed = []

    # ── B2: Reset reversal counters on RTH open ──────────────────────────────
    _cur_tod = _get_tod_phase()
    for _sym, _tr in tracker.open_trades.items():
        _prev_tod = _tr.get("_exit_tod_last")
        if _prev_tod == "premarket" and _cur_tod == "normal":
            if _tr.get("reversal_scan_count", 0) > 0:
                logger.info(
                    f"[{_sym}] RTH open: reversal counter reset "
                    f"({_tr['reversal_scan_count']} premarket scan(s) cleared)"
                )
                _tr["reversal_scan_count"] = 0
        _tr["_exit_tod_last"] = _cur_tod

    for symbol, trade in list(tracker.open_trades.items()):
        # QHM ownership guard (Option B, 2026-07-01): the main bot must not exit a
        # QHM-held symbol (QHM owns its Alpaca position + its own GTC stop).
        # Entries are blocked in entry_logic, so a QHM symbol in the tracker is an
        # ownership COLLISION (held before QHM entered, or a race) — suppress
        # main-bot exit management and alert ONCE. Do NOT close here: at step 1 the
        # position is shared net on Alpaca, so closing would flatten QHM too.
        if symbol in _get_qhm_syms():
            if not trade.get("_qhm_collision_alerted"):
                trade["_qhm_collision_alerted"] = True
                tracker._save_log()
                logger.error(
                    "[%s] QHM COLLISION: main-bot position present for a QHM-held "
                    "symbol — main-bot exit management suppressed (Option B). "
                    "Reconcile ownership.", symbol,
                )
                try:
                    from alerts import send_slack as _qhm_slack
                    _qhm_slack(
                        f":warning: [{symbol}] QHM collision — main bot holds a "
                        f"position in a QHM-held symbol; main-bot exits suppressed "
                        f"(QHM owns it). Reconcile."
                    )
                except Exception as _qhm_ae:
                    logger.warning(
                        "[%s] QHM collision Slack alert failed: %s", symbol, _qhm_ae
                    )
            continue
        direction     = trade["direction"]
        trade_mode    = trade["trade_mode"]
        is_overnight  = trade.get("overnight", False)
        is_bucket_a   = symbol in config.LEVERAGED_TICKERS
        entry_price   = trade.get("entry_price", 0)
        current_price = None
        _cur_bar_ts   = None

        # ── Bucket A same-day close — HARD BLOCK ────────────────────────────
        if is_bucket_a and tracker.opened_today(symbol):
            try:
                df_price = fetch_bars(symbol, config.TF_15M, num_bars=2)
                if not df_price.empty:
                    current_price = float(df_price["close"].iloc[-1])
            except Exception as _bapfe:
                logger.warning(f"[{symbol}] Bucket A price fetch failed in check_exits — stop breach check skipped: {_bapfe}")
            if current_price is not None:
                active_stop = trade.get("trail_stop") or trade.get("stop")
                breach = active_stop is not None and (
                    (trade["direction"] == "long"  and current_price <= active_stop) or
                    (trade["direction"] == "short" and current_price >= active_stop)
                )
                if breach:
                    tracker.record_stop_breach_blocked(symbol, current_price)
            logger.info(f"[{symbol}] Bucket A same-day close blocked — hard rule")
            continue

        # ── Fetch current price (shared by both exit paths) ─────────────────
        if current_price is None:
            try:
                df_price = fetch_bars(symbol, config.TF_15M, num_bars=2)
                if not df_price.empty:
                    current_price = float(df_price["close"].iloc[-1])
                    _cur_bar_ts   = str(df_price.index[-1])
            except Exception as _e:
                logger.warning(f"[{symbol}] Price fetch failed in check_exits: {_e}")

        # ── Live price override (exit equivalent of Fix-C) ───────────────────
        if current_price is not None:
            try:
                _ex_live = get_latest_trade(symbol)
                if _ex_live and _ex_live > 0:
                    logger.debug(
                        f"[{symbol}] Exit live price ${_ex_live:.2f} "
                        f"(bar close ${current_price:.2f})"
                    )
                    current_price = _ex_live
            except Exception as _e:
                logger.warning(
                    "[%s] Live price override failed (exits): %s: %s "
                    "— using 15M bar close $%.2f for stop/reversal checks",
                    symbol, type(_e).__name__, _e, current_price,
                )

        # ── 0. Overnight breakeven exit (Item 1: multi-scan gate) ───────────
        _is_prior_session = (
            trade.get("entry_time", "")[:10] < datetime.now(ET).strftime("%Y-%m-%d")
        )
        if (current_price is not None
                and _is_prior_session
                and not tracker.opened_today(symbol)):
            # QHM positions use their own wide GTC stop — exempt from intraday buffer
            if symbol in _get_qhm_syms():
                logger.info('[%s] QHM-protected: overnight ATR buffer suppressed', symbol)
                continue
            _be_now   = datetime.now(ET)
            _be_mins  = _be_now.hour * 60 + _be_now.minute
            _be_grace = _be_mins < (10 * 60)
            if _be_grace:
                logger.info(
                    f"[{symbol}] OVERNIGHT BREAKEVEN: grace period active "
                    f"(before 10:00 AM ET, now {_be_now.strftime('%H:%M')} ET) "
                    f"— suppressing breakeven check this cycle."
                )
            else:
                _be_entry   = trade.get("entry_price", 0)
                _be_atr     = trade.get("atr_value")
                # ── H1 DPE: dynamic overnight ATR buffer multiplier ──────────
                # Replaces static 3-tier VIX ternary with per-symbol realized vol
                # scalar. get_be_buffer_mult() returns float in [0.25, 0.65].
                # Data source: T1 Alpaca daily bars via execution/param_engine.py.
                _be_mult    = get_be_buffer_mult(symbol, last_vix)  # H1 DPE
                _be_buf     = (_be_atr * _be_mult) if _be_atr else 0.0
                _be_rec_buf = (_be_atr * 0.1)      if _be_atr else 0.0
                # Trigger threshold: entry ± VIX-adjusted ATR buffer
                _be_thresh = (
                    (_be_entry - _be_buf) if direction == "long"
                    else (_be_entry + _be_buf)
                )
                # Recovery threshold: entry ± 0.1×ATR (tighter — price must recover meaningfully)
                _be_rec_thresh = (
                    (_be_entry - _be_rec_buf) if direction == "long"
                    else (_be_entry + _be_rec_buf)
                )
                _be_below = (
                    (direction == "long"  and current_price <= _be_thresh) or
                    (direction == "short" and current_price >= _be_thresh)
                )
                _be_recovered = (
                    (direction == "long"  and current_price > _be_rec_thresh) or
                    (direction == "short" and current_price < _be_rec_thresh)
                )
                _be_count = trade.get("breakeven_breach_count", 0)

                if _be_recovered and _be_count > 0:
                    trade["breakeven_breach_count"] = 0
                    tracker._save_log()
                    logger.info(
                        f"[{symbol}] OVERNIGHT BREAKEVEN: price ${current_price:.2f} "
                        f"recovered above ${_be_rec_thresh:.2f} — breach counter reset "
                        f"({_be_count} → 0)."
                    )
                elif _be_below:
                    _new_count = _be_count + 1
                    trade["breakeven_breach_count"] = _new_count
                    tracker._save_log()
                    logger.info(
                        f"[{symbol}] OVERNIGHT BREAKEVEN: price ${current_price:.2f} "
                        f"≤ threshold ${_be_thresh:.2f} "
                        f"— breach count {_be_count} → {_new_count}/9"
                    )
                    if _new_count >= 9:
                        _be_atr_tag = f"ATR={_be_atr:.3f}" if _be_atr else "ATR=unavailable"
                        _be_reason_detail = (
                            f"{_new_count}-scan breach | "
                            f"entry=${_be_entry:.2f} thresh=${_be_thresh:.2f} "
                            f"({_be_atr_tag} Tier_adj={_be_mult:.3f})"
                        )
                        logger.warning(
                            f"[{symbol}] OVERNIGHT BREAKEVEN EXIT: {_be_reason_detail} — closing."
                        )
                        _pre_cancel_stop_ids: dict = {
                            k: trade[k]
                            for k in ("gtc_stop_order_id", "rth_day_stop_order_id", "_gtc_stop_order_id")
                            if trade.get(k)
                        }
                        _be_ts = time.time()
                        trade["_forced_close_pending"] = True
                        tracker._save_log()
                        _be_gtc_ok = _cancel_open_gtc_orders(symbol, trade, tracker)
                        if not _be_gtc_ok:
                            logger.critical(
                                f"[{symbol}] OVERNIGHT BREAKEVEN: GTC cancel unconfirmed — "
                                f"proceeding immediately (hard-stop semantics, board Option D)."
                            )
                        success = close_position(symbol)
                        _entry_time = trade.get("entry_time", "")
                        _UTC = ZoneInfo("UTC")
                        try:
                            _entry_dt = datetime.fromisoformat(
                                _entry_time.replace("Z", "+00:00")
                            ).astimezone(_UTC) if _entry_time else None
                        except Exception as _parse_e:
                            logger.warning(
                                "[%s] Overnight ATR: entry_time parse failed (%r): %s "
                                "— skipping stale fill detection",
                                symbol, _entry_time, _parse_e,
                            )
                            _entry_dt = None
                        if success:
                            trade.pop("_forced_close_pending", None)
                            _be_fill: float | None = None
                            for _oid in _pre_cancel_stop_ids.values():
                                try:
                                    _ord = get_order(_oid)
                                    if _ord and float(getattr(_ord, "filled_avg_price", 0) or 0):
                                        _filled_at = str(getattr(_ord, "filled_at", "") or "")
                                        if _entry_dt and _filled_at:
                                            try:
                                                _filled_dt = datetime.fromisoformat(
                                                    _filled_at.replace("Z", "+00:00")
                                                ).astimezone(_UTC)
                                                if _filled_dt < _entry_dt:
                                                    logger.warning(
                                                        f"[{symbol}] ATR exit: stale stop {_oid} "
                                                        f"(filled before entry) — skipped."
                                                    )
                                                    continue
                                            except Exception as _stale_e:
                                                logger.warning(
                                                    f"[{symbol}] Stale fill datetime comparison "
                                                    f"failed for order {_oid}: {_stale_e} — "
                                                    f"skipping fill."
                                                )
                                                continue
                                        _be_fill = float(_ord.filled_avg_price)
                                        logger.info(
                                            f"[{symbol}] ATR exit: fill from GTC stop {_oid}: "
                                            f"${_be_fill:.2f}"
                                        )
                                        break
                                except Exception as _e:
                                    logger.warning(
                                        f"[{symbol}] ATR exit: stop order {_oid} fill lookup error: {_e}"
                                    )
                            if _be_fill is None:
                                _be_fill = _fetch_actual_fill_price(
                                    symbol, trade, poll_secs=0.3, submitted_after=_be_ts
                                )
                            pnl = tracker.record_exit(symbol, _be_fill,
                                                       reason=f"overnight_atr_buffer_exit | {_be_reason_detail}",
                                                       mri_level=mri_level)
                            if tracker.closed_trades:
                                _record_tqi(tracker.closed_trades[-1], kelly)
                            risk.register_close(pnl or 0.0)
                            closed.append(symbol)
                            alert_exit(symbol=symbol, direction=direction,
                                       pnl=pnl or 0.0, reason="overnight_atr_buffer_exit",
                                       tqi=tracker.closed_trades[-1].get("tqi_score", 0) if tracker.closed_trades else 0)
                        else:
                            _ap_pos = get_open_position(symbol)
                            if _ap_pos is None:
                                trade.pop("_forced_close_pending", None)
                                logger.warning(
                                    f"[{symbol}] OVERNIGHT BREAKEVEN: close_position rejected AND "
                                    f"position gone from Alpaca — recording exit from stop data."
                                )
                                _ep: float | None = None
                                for _oid in _pre_cancel_stop_ids.values():
                                    try:
                                        _ord = get_order(_oid)
                                        if _ord and float(getattr(_ord, "filled_avg_price", 0) or 0):
                                            _ep = float(_ord.filled_avg_price)
                                            break
                                    except Exception as _e:
                                        logger.warning(
                                            f"[{symbol}] ATR exit (else): stop {_oid} error: {_e}"
                                        )
                                if _ep is None:
                                    _after_ts = _be_ts
                                    if _entry_time:
                                        try:
                                            _after_ts = datetime.fromisoformat(
                                                _entry_time.replace("Z", "+00:00")
                                            ).timestamp()
                                        except Exception as _et_e:
                                            logger.warning(
                                                f"[{symbol}] Entry time parse failed "
                                                f"({_entry_time!r}): {_et_e} — "
                                                f"using close time as lower bound for fill query."
                                            )
                                    _ep = _fetch_actual_fill_price(
                                        symbol, trade, poll_secs=0.3, submitted_after=_after_ts
                                    )
                                if _ep is None:  # type: ignore[unreachable]
                                    trade["_fill_unverified"] = True  # type: ignore[unreachable]
                                    _ep = 0.0
                                    logger.critical(
                                        "[%s] ATR exit (else): fill lookup exhausted — "
                                        "recording 0.0 (fill unverified). Verify in Alpaca.",
                                        symbol,
                                    )
                                    try:
                                        from alerts import send_slack
                                        send_slack(f":rotating_light: [{symbol}] RC-4: ATR exit fill unverified — exit recorded at $0.00. Manual P&L review required.")
                                    except Exception as _slack_e:
                                        logger.error("[%s] RC-4 Slack alert failed: %s", symbol, _slack_e)
                                pnl = tracker.record_exit(
                                    symbol, _ep,
                                    reason=f"overnight_atr_buffer_exit | gtc_stop_executed | {_be_reason_detail}",
                                    mri_level=mri_level,
                                )
                                if tracker.closed_trades:
                                    _record_tqi(tracker.closed_trades[-1], kelly)
                                risk.register_close(pnl or 0.0)
                                closed.append(symbol)
                                try:
                                    from alerts import send_slack
                                    send_slack(
                                        f":warning: [{symbol}] Overnight ATR exit: GTC stop beat "
                                        f"bot close — exit recorded at ${_ep:.2f}. Verify P&L."
                                    )
                                except Exception as _slack_e:
                                    logger.warning(f"[{symbol}] ATR exit Slack alert failed: {_slack_e}")
                                alert_exit(symbol=symbol, direction=direction,
                                           pnl=pnl or 0.0,
                                           reason="overnight_atr_buffer_exit | gtc_stop_executed",
                                           tqi=tracker.closed_trades[-1].get("tqi_score", 0) if tracker.closed_trades else 0)
                            else:
                                logger.error(
                                    f"[{symbol}] OVERNIGHT BREAKEVEN: close_position failed and "
                                    f"position still on Alpaca — will retry next scan."
                                )
                        continue

        # ── 0B. Thesis invalidation exit ──────────────────────────────────────
        _ti_conflict = (
            _main._spy_risk_active
            and _main._spy_event_type not in ("", "SECTOR")
            and (
                (direction == "long"  and _main._spy_risk_direction == "down") or
                (direction == "short" and _main._spy_risk_direction == "up")
            )
        )
        if current_price is not None and _ti_conflict:
            logger.warning(
                f"[{symbol}] THESIS INVALIDATED: {_main._spy_event_type} "
                f"SPY {_main._spy_risk_magnitude:+.2f}% — closing {direction} immediately."
            )
            _ti_gtc_ok = _cancel_open_gtc_orders(symbol, trade, tracker)
            if not _ti_gtc_ok:
                logger.critical(
                    f"[{symbol}] GTC cancel unconfirmed before thesis invalidation close — "
                    f"proceeding to prevent exposure. Verify GTC manually."
                )
            _ti_ts  = time.time()
            success = close_position(symbol)
            if success:
                _ti_fill = _fetch_actual_fill_price(
                    symbol, trade, poll_secs=0.3, submitted_after=_ti_ts
                )
                pnl = tracker.record_exit(
                    symbol, _ti_fill, reason="thesis_invalidation",
                    mri_level=mri_level
                )
                if tracker.closed_trades:
                    _record_tqi(tracker.closed_trades[-1], kelly)
                risk.register_close(pnl or 0.0)
                closed.append(symbol)
                alert_exit(symbol=symbol, direction=direction,
                           pnl=pnl or 0.0, reason="thesis_invalidation",
                           tqi=tracker.closed_trades[-1].get("tqi_score", 0) if tracker.closed_trades else 0)
            continue

        # ── 0C. Break-even stop promotion ────────────────────────────────────
        if (
            current_price is not None
            and not trade.get("be_stop_promoted")
            and not trade.get("partial_exited")
        ):
            _be_entry  = trade.get("entry_price", 0)
            _be_stp    = trade.get("stop", 0)
            _be_risk   = abs(_be_entry - _be_stp) if _be_stp else 0
            _be_profit = (
                (current_price - _be_entry) if direction == "long"
                else (_be_entry - current_price)
            )
            _be_r = _be_profit / _be_risk if _be_risk > 0 else 0.0
            if _be_r >= 0.5 and _be_profit > 0:
                trade["stop"]             = _be_entry
                trade["be_stop_promoted"] = True
                logger.info(
                    f"[{symbol}] BE STOP PROMOTED: ${current_price:.2f} = "
                    f"{_be_r:.2f}R above entry ${_be_entry:.2f} — "
                    f"stop raised to entry ${_be_entry:.2f}."
                )
                _be_oid = trade.get("rth_day_stop_order_id")
                if _be_oid:
                    cancel_order(_be_oid)
                    trade["rth_day_stop_order_id"] = None
                _be_rem = int(trade.get("qty_remaining", trade.get("qty", 0)))
                if _be_rem >= 1:
                    _be_stop_px = _be_entry
                    _be_side = "sell" if direction == "long" else "buy"
                    _be_ord  = submit_day_stop_order(
                        symbol=symbol, qty=_be_rem,
                        side=_be_side, stop_price=_be_stop_px,
                    )
                    if _be_ord:
                        trade["rth_day_stop_order_id"] = str(_be_ord.id)  # type: ignore[attr-defined]
                        logger.info(
                            f"[{symbol}] BE DAY stop submitted: "
                            f"{_be_side.upper()} {_be_rem} @ ${_be_stop_px:.2f} | "
                            f"order {_be_ord.id}"  # type: ignore[attr-defined]
                        )
                    else:
                        logger.error(
                            f"[{symbol}] BE DAY stop submit FAILED — "
                            f"{_be_rem} shares unprotected above entry. "
                            f"Set manual stop in Alpaca."
                        )
                tracker._save_log()

        # ── 1. Hard stop enforcement (B4: 3-scan noise filter) ──────────────
        if current_price is not None:
            active_stop = trade.get("trail_stop") or trade.get("stop")
            hard_stop_hit = (
                active_stop is not None and (
                    (direction == "long"  and current_price <= active_stop) or
                    (direction == "short" and current_price >= active_stop)
                )
            )
            if hard_stop_hit:
                _last_breach_bar = trade.get("stop_breach_last_bar")
                if _cur_bar_ts and _cur_bar_ts == _last_breach_bar:
                    logger.info(
                        f"[{symbol}] Stop breach ${current_price:.2f} vs "
                        f"${active_stop:.2f} — same bar, not incrementing counter."
                    )
                    tracker._save_log()
                    continue
                if _cur_bar_ts:
                    trade["stop_breach_last_bar"] = _cur_bar_ts
                trade["stop_breach_count"] = trade.get("stop_breach_count", 0) + 1
                _breach = trade["stop_breach_count"]
                _STOP_CONFIRM = 1 if last_vix >= config.VIX_STOP_WIDEN_THRESHOLD_2 else 3
                if _breach < _STOP_CONFIRM:
                    logger.warning(
                        f"[{symbol}] Stop breach {_breach}/{_STOP_CONFIRM}: "
                        f"${current_price:.2f} vs stop ${active_stop:.2f} — monitoring."
                    )
                    tracker._save_log()
                    continue
                logger.warning(
                    f"[{symbol}] HARD STOP CONFIRMED ({_breach} scans): ${current_price:.2f} "
                    f"({'<=' if direction == 'long' else '>='} ${active_stop:.2f}) — closing."
                )
                _hs_gtc_ok = _cancel_open_gtc_orders(symbol, trade, tracker)
                if not _hs_gtc_ok:
                    logger.critical(
                        f"[{symbol}] GTC cancel unconfirmed before hard stop close — "
                        f"proceeding to prevent unlimited loss. Verify GTC manually."
                    )
                _hs_ts  = time.time()
                success = close_position(symbol)
                if success:
                    _hs_fill = _fetch_actual_fill_price(
                        symbol, trade, poll_secs=0.3, submitted_after=_hs_ts
                    )
                    pnl = tracker.record_exit(symbol, _hs_fill, reason="hard_stop",
                                              mri_level=mri_level)
                    if tracker.closed_trades:
                        _record_tqi(tracker.closed_trades[-1], kelly)
                    risk.register_close(pnl or 0.0)
                    closed.append(symbol)
                    alert_exit(symbol=symbol, direction=direction,
                               pnl=pnl or 0.0, reason="hard_stop",
                               tqi=tracker.closed_trades[-1].get("tqi_score", 0) if tracker.closed_trades else 0)
                    if direction == "short":
                        _eod_ts = datetime.now(
                            ZoneInfo("America/New_York")
                        ).replace(hour=23, minute=59, second=59, microsecond=0).timestamp()
                        _set_shorts_banned(
                            until_ts=_eod_ts,
                            trigger=f"hard_stop:{symbol}",
                        )
                else:
                    logger.critical(
                        f"[{symbol}] Hard stop close_position() FAILED — position may be naked. "
                        f"⚠️ MANUAL INTERVENTION REQUIRED."
                    )
                    alert_stop_breach(symbol=symbol, direction=direction,
                                      current_price=current_price, stop=active_stop)
            else:
                if trade.get("stop_breach_count", 0) > 0:
                    logger.info(
                        f"[{symbol}] Stop breach counter reset — price ${current_price:.2f} "
                        f"recovered inside stop ${active_stop:.2f}."
                    )
                    trade["stop_breach_count"] = 0
                    tracker._save_log()
            if hard_stop_hit:
                continue

        # ── C-3: Target-hit exit ─────────────────────────────────────────────
        target_price = trade.get("target")
        if current_price is not None and target_price is not None:
            _target_hit = (
                (direction == "long"  and current_price >= target_price) or
                (direction == "short" and current_price <= target_price) or
                trade.get("exit_pending_reason") == "target"
            )
            if _target_hit:
                _op = ">=" if direction == "long" else "<="
                logger.info(
                    f"[{symbol}] TARGET HIT: ${current_price:.2f} {_op} "
                    f"${target_price:.2f} — closing."
                )
                _tgt_gtc_ok = _cancel_open_gtc_orders(symbol, trade, tracker)
                if not _tgt_gtc_ok:
                    _tgt_defer = trade.get("_gtc_cancel_defer_count", 0) + 1
                    trade["_gtc_cancel_defer_count"] = _tgt_defer
                    trade["exit_pending_reason"] = "target"
                    tracker._save_log()
                    if _tgt_defer >= 2:
                        logger.critical(
                            f"[{symbol}] GTC cancel unconfirmed after {_tgt_defer} cycles — "
                            f"forcing target exit to prevent price escape. "
                            f"Verify GTC orders manually in Alpaca."
                        )
                    else:
                        logger.warning(
                            f"[{symbol}] GTC cancel not confirmed (defer {_tgt_defer}/2) — "
                            f"retrying next scan."
                        )
                        continue
                _tgt_ts = time.time()
                try:
                    success = close_position(symbol)
                except Exception as _te:
                    logger.warning(
                        f"[{symbol}] close_position raised during target exit: {_te}"
                    )
                    continue
                if success:
                    trade.pop("exit_pending_reason", None)
                    trade.pop("_gtc_cancel_defer_count", None)
                    _tgt_fill = _fetch_actual_fill_price(
                        symbol, trade, poll_secs=0.3, submitted_after=_tgt_ts
                    )
                    pnl = tracker.record_exit(symbol, _tgt_fill, reason="target",
                                              mri_level=mri_level)
                    if tracker.closed_trades:
                        _record_tqi(tracker.closed_trades[-1], kelly)
                    risk.register_close(pnl or 0.0)
                    closed.append(symbol)
                    alert_exit(symbol=symbol, direction=direction,
                               pnl=pnl or 0.0, reason="target",
                               tqi=tracker.closed_trades[-1].get("tqi_score", 0) if tracker.closed_trades else 0)
                else:
                    logger.warning(
                        f"[{symbol}] Target exit close_position() returned False — "
                        f"position may still be open. Will re-check next cycle."
                    )
                continue

        # ── 2. Reversal scan counter ─────────────────────────────────────────
        should_exit = get_exit_signal(symbol, direction, trade_mode)

        if should_exit:
            trade["reversal_scan_count"] = trade.get("reversal_scan_count", 0) + 1
            count = trade["reversal_scan_count"]

            if is_overnight:
                scan_min = config.OVERNIGHT_REVERSAL_SCAN_MIN
                scan_max = config.OVERNIGHT_REVERSAL_SCAN_MAX
                if count < scan_min:
                    logger.info(
                        f"[{symbol}] Overnight reversal signal: scan {count}/{scan_min} "
                        f"— waiting for confirmation."
                    )
                    tracker._save_log()
                    continue
                elif count < scan_max:
                    logger.info(
                        f"[{symbol}] Overnight reversal confirmed: {count}/{scan_min} scans "
                        f"— proceeding to close."
                    )
                else:
                    logger.warning(
                        f"[{symbol}] Overnight reversal FORCE EXIT: {count} scans "
                        f">= MAX ({scan_max}) — closing now."
                    )
            else:
                scan_min    = config.RTH_REVERSAL_SCAN_MIN
                entry_price = trade.get("entry_price")
                if entry_price and current_price is not None:
                    _entry_touched = (
                        (direction == "long"  and current_price <= entry_price) or
                        (direction == "short" and current_price >= entry_price)
                    )
                    if _entry_touched:
                        _atr_val     = trade.get("atr_value") or 0
                        _displacement = abs(current_price - entry_price)
                        _min_disp    = 0.5 * _atr_val if _atr_val > 0 else 0
                        _in_noise    = _min_disp > 0 and _displacement < _min_disp
                        if _in_noise:
                            logger.debug(
                                f"[{symbol}] Hard-out suppressed: disp ${_displacement:.2f} "
                                f"< 0.5×ATR ${_min_disp:.2f} — noise band, not counting."
                            )
                            if trade.get("hard_out_count", 0) > 0:
                                trade["hard_out_count"] = 0
                                tracker._save_log()
                            if count < scan_min:
                                logger.info(
                                    f"[{symbol}] RTH reversal (noise band): "
                                    f"scan {count}/{scan_min} — waiting for 30-min confirmation."
                                )
                                tracker._save_log()
                                continue
                        else:
                            _hoc = trade.get("hard_out_count", 0) + 1
                            trade["hard_out_count"] = _hoc
                            tracker._save_log()
                            if _hoc < 2:
                                logger.warning(
                                    f"[{symbol}] RTH REVERSAL HARD OUT warning: price "
                                    f"${current_price:.2f} at/past entry ${entry_price:.2f} "
                                    f"(disp ${_displacement:.2f} ≥ 0.5×ATR ${_min_disp:.2f}) "
                                    f"— scan {_hoc}/2, holding one more scan."
                                )
                                continue
                            else:
                                logger.warning(
                                    f"[{symbol}] RTH REVERSAL HARD OUT confirmed: price "
                                    f"${current_price:.2f} at/past entry ${entry_price:.2f} "
                                    f"(disp ${_displacement:.2f} ≥ 0.5×ATR ${_min_disp:.2f}) "
                                    f"for {_hoc} consecutive scans — exiting."
                                )
                    else:
                        if trade.get("hard_out_count", 0) > 0:
                            logger.info(
                                f"[{symbol}] Hard-out counter reset — price "
                                f"${current_price:.2f} recovered above entry "
                                f"${entry_price:.2f}."
                            )
                            trade["hard_out_count"] = 0
                            tracker._save_log()
                        if count < scan_min:
                            logger.info(
                                f"[{symbol}] RTH reversal signal: scan {count}/{scan_min} "
                                f"— waiting for 30-min confirmation."
                            )
                            tracker._save_log()
                            continue
                elif count < scan_min:
                    logger.info(
                        f"[{symbol}] RTH reversal signal: scan {count}/{scan_min} "
                        f"— waiting for 30-min confirmation."
                    )
                    tracker._save_log()
                    continue

            # ── Item 4: Score-drop gate ──────────────────────────────────────
            _live_score = _get_live_score(symbol, direction, trade_mode)
            if _live_score is not None:
                _score_threshold = 7
                if _live_score < _score_threshold:
                    _sdc = trade.get("score_drop_count", 0) + 1
                    trade["score_drop_count"] = _sdc
                    tracker._save_log()
                    if _sdc < 2:
                        logger.info(
                            f"[{symbol}] Score-drop gate: live score {_live_score}/12 "
                            f"< {_score_threshold} — scan {_sdc}/2, waiting for confirmation."
                        )
                        continue
                    else:
                        logger.info(
                            f"[{symbol}] Score-drop confirmed: {_live_score}/12 "
                            f"< {_score_threshold} for {_sdc} scans — exit proceeding."
                        )
                else:
                    if trade.get("score_drop_count", 0) > 0:
                        logger.info(
                            f"[{symbol}] Score-drop gate: live score recovered "
                            f"{_live_score}/12 ≥ {_score_threshold} — drop counter reset."
                        )
                        trade["score_drop_count"] = 0
                        tracker._save_log()

            if symbol in closed:
                logger.warning(
                    f"[{symbol}] Duplicate exit skipped — already closed this cycle."
                )
                continue

            logger.info(f"[{symbol}] Exit signal confirmed ({count} scans) — closing position.")
            _sig_ts  = time.time()
            _sig_gtc_ok = _cancel_open_gtc_orders(symbol, trade, tracker)
            if not _sig_gtc_ok:
                _sig_defer = trade.get("_gtc_sig_defer_count", 0) + 1
                trade["_gtc_sig_defer_count"] = _sig_defer
                tracker._save_log()
                if _sig_defer >= 6:
                    logger.critical(
                        f"[{symbol}] GTC cancel unconfirmed after {_sig_defer} cycles — "
                        f"forcing signal exit. Verify GTC orders manually in Alpaca."
                    )
                else:
                    _defer_pos: object | None = None
                    try:
                        _defer_pos = get_open_position(symbol)
                    except Exception as _dpe:
                        logger.warning(
                            f"[{symbol}] Defer GTC pos check failed: {_dpe}"
                        )
                    if _defer_pos is None and symbol in tracker.open_trades:
                        logger.warning(
                            f"[{symbol}] GTC stop executed during signal defer "
                            f"(cycle {_sig_defer}) — recording external close."
                        )
                        _ext_ep: float | None = None
                        for _skey in (
                            "rth_day_stop_order_id",
                            "gtc_stop_order_id",
                            "_gtc_stop_order_id",
                        ):
                            _oid = trade.get(_skey)
                            if _oid:
                                try:
                                    _ord = get_order(_oid)
                                    if _ord and getattr(_ord, "status", None) == "filled":
                                        _fap = getattr(_ord, "filled_avg_price", None)
                                        if _fap is not None and float(_fap) > 0:
                                            _ext_ep = float(_fap)
                                            break
                                except Exception as _de:
                                    logger.warning(
                                        f"[{symbol}] Defer GTC fill lookup {_skey}: {_de}"
                                    )
                        if _ext_ep is None:
                            _entry_ts: float = _sig_ts
                            _raw_et = trade.get("entry_time")
                            if _raw_et:
                                if isinstance(_raw_et, (int, float)):
                                    _entry_ts = float(_raw_et)
                                else:
                                    try:
                                        _entry_ts = datetime.fromisoformat(
                                            str(_raw_et).replace("Z", "+00:00")
                                        ).timestamp()
                                    except Exception as _ets_e:
                                        logger.debug(
                                            f"[{symbol}] Signal exit entry_ts parse failed "
                                            f"({_raw_et!r}): {_ets_e} — "
                                            f"using signal time as lower bound."
                                        )
                            _raw_fill = _fetch_actual_fill_price(
                                symbol, trade, poll_secs=0.3,
                                submitted_after=_entry_ts,
                            )
                            if _raw_fill > 0:
                                _ext_ep = _raw_fill
                        if _ext_ep is None:
                            trade["_fill_unverified"] = True
                            _ext_ep = 0.0
                            logger.critical(
                                "[%s] Defer GTC close: fill lookup exhausted — "
                                "recording 0.0 (fill unverified). Verify in Alpaca.",
                                symbol,
                            )
                            try:
                                from alerts import send_slack
                                send_slack(f":rotating_light: [{symbol}] RC-4: Defer GTC fill unverified — exit recorded at $0.00. Manual P&L review required.")
                            except Exception as _slack_e:
                                logger.error("[%s] RC-4 Slack alert failed: %s", symbol, _slack_e)
                        if symbol in tracker.open_trades:
                            pnl = tracker.record_exit(
                                symbol, _ext_ep,
                                reason="signal_exit|gtc_stop_executed_during_defer",
                                mri_level=mri_level,
                            )
                            if tracker.closed_trades:
                                _record_tqi(tracker.closed_trades[-1], kelly)
                            risk.register_close(pnl or 0.0)
                            trade.pop("_gtc_sig_defer_count", None)
                            closed.append(symbol)
                        continue
                    logger.warning(
                        f"[{symbol}] GTC cancel not confirmed for signal exit "
                        f"(defer {_sig_defer}/6) — retrying next scan."
                    )
                    continue
            success = close_position(symbol)
            if not success:
                try:
                    _ap_pos = get_open_position(symbol)
                    if _ap_pos is None:
                        logger.warning(
                            f"[{symbol}] close_position failed AND position gone from Alpaca "
                            f"— force-cleaning tracker (external close / stale state)."
                        )
                        _ep = None  # type: ignore[no-redef]
                        for _skey in ("rth_day_stop_order_id", "gtc_stop_order_id"):
                            _oid = trade.get(_skey)
                            if _oid:
                                try:
                                    _ord = get_order(_oid)
                                    if _ord and getattr(_ord, "status", None) == "filled":
                                        _fap = getattr(_ord, "filled_avg_price", None)
                                        if _fap is not None:
                                            _ep_cand = float(_fap)
                                            if _ep_cand > 0:
                                                _ep = _ep_cand
                                                logger.info(
                                                    f"[{symbol}] external_close: fill "
                                                    f"price from {_skey} = {_ep}"
                                                )
                                                break
                                except Exception as _oe:
                                    logger.warning(
                                        f"[{symbol}] external_close: "
                                        f"get_order({_oid}) failed — {_oe}"
                                    )
                        if not _ep:
                            _et = trade.get("entry_time")
                            if _et:
                                if isinstance(_et, (int, float)):
                                    _et_ts = float(_et)
                                else:
                                    try:
                                        _et_ts = datetime.fromisoformat(
                                            str(_et).replace("Z", "+00:00")
                                        ).timestamp()
                                    except Exception as _et_exc:
                                        logger.warning(
                                            "[%s] external_close: entry_time parse failed (%s) "
                                            "— fill query using t-1h as lower bound "
                                            "(epoch=0 has crosstalk risk; safe: handler fires only when Alpaca confirmed close this cycle).",
                                            symbol, _et_exc,
                                        )
                                        _et_ts = time.time() - 3600  # safe: only fires when close confirmed this cycle; RC-4 backstop if missed
                                _ep = _fetch_actual_fill_price(
                                    symbol, trade, poll_secs=0, submitted_after=_et_ts
                                )
                                if _ep:
                                    logger.info(
                                        f"[{symbol}] external_close: fill price "
                                        f"from fills API = {_ep}"
                                    )
                            else:
                                logger.warning(
                                    f"[{symbol}] external_close: entry_time missing "
                                    f"— skipping fills API (crosstalk risk). "
                                    f"Proceeding to price fallback."
                                )
                        if not _ep:
                            trade["_fill_unverified"] = True
                            _ep = 0.0
                            logger.critical(
                                "[%s] external_close: fill price unknown — "
                                "recording 0.0 (fill unverified). Manual P&L review required.",
                                symbol,
                            )
                            try:
                                from alerts import send_slack
                                send_slack(f":rotating_light: [{symbol}] RC-4: External close fill unverified — exit recorded at $0.00. Manual P&L review required.")
                            except Exception as _slack_e:
                                logger.error("[%s] RC-4 Slack alert failed: %s", symbol, _slack_e)
                        if symbol in tracker.open_trades:
                            pnl = tracker.record_exit(
                                symbol, _ep, reason="external_close", mri_level=mri_level,
                                # Verified: reached only under `if _ap_pos is None`
                                # (get_open_position confirmed absent) above.
                                alpaca_confirmed_absent=True,
                            )
                            risk.register_close(pnl or 0.0)
                            trade.pop("_gtc_sig_defer_count", None)
                            closed.append(symbol)
                    else:
                        # MTF FULL BOT AUDIT — JUNE 26 (Gro+GAI consensus,
                        # Medium-High): close failed AND position confirmed
                        # to still exist. Unlike the hard_stop path, this
                        # case previously had zero logging or alerting —
                        # silent retry next cycle. Mirrors the defer-counter
                        # escalation pattern already used elsewhere in this
                        # file (_gtc_sig_defer_count, _gtc_cancel_defer_count).
                        _sig_close_fails = trade.get("_signal_close_fail_count", 0) + 1
                        trade["_signal_close_fail_count"] = _sig_close_fails
                        tracker._save_log()
                        if _sig_close_fails >= 3:
                            logger.critical(
                                f"[{symbol}] Signal-exit close FAILED {_sig_close_fails} "
                                f"consecutive cycles — position remains open. "
                                f"Verify in Alpaca manually."
                            )
                            try:
                                from alerts import send_slack as _sce_slack
                                _sce_slack(
                                    f":warning: [{symbol}] Signal-exit close has failed "
                                    f"{_sig_close_fails} consecutive cycles — position "
                                    f"still open. Check Alpaca for a stuck order or API issue."
                                )
                            except Exception as _sce_e:
                                logger.warning(
                                    f"[{symbol}] Signal-exit close-fail alert failed: {_sce_e}"
                                )
                        else:
                            logger.warning(
                                f"[{symbol}] Signal-exit close_position() failed — "
                                f"position confirmed still open (cycle {_sig_close_fails}/3). "
                                f"Will retry next scan."
                            )
                except Exception as _gp_err:
                    logger.warning(
                        f"[{symbol}] Could not verify Alpaca position "
                        f"after close failure: {_gp_err}"
                    )
            if success:
                trade.pop("_gtc_sig_defer_count", None)
                trade.pop("_signal_close_fail_count", None)
                if not _sig_gtc_ok:
                    _cancel_open_gtc_orders(symbol, trade, tracker)
                _sig_fill = _fetch_actual_fill_price(
                    symbol, trade, poll_secs=0.3, submitted_after=_sig_ts
                )
                pnl = tracker.record_exit(symbol, _sig_fill, reason="signal",
                                          mri_level=mri_level)
                if tracker.closed_trades:
                    _record_tqi(tracker.closed_trades[-1], kelly)
                risk.register_close(pnl or 0.0)
                closed.append(symbol)
                alert_exit(symbol=symbol, direction=direction,
                           pnl=pnl or 0.0, reason="signal",
                           tqi=tracker.closed_trades[-1].get("tqi_score", 0) if tracker.closed_trades else 0)

        else:
            # H-2: Reversal counter decay — one neutral scan reduces count by 1
            prev_count = trade.get("reversal_scan_count", 0)
            if prev_count > 0:
                new_count = prev_count - 1
                logger.info(
                    f"[{symbol}] Reversal signal neutral — counter decayed "
                    f"{prev_count} → {new_count}."
                )
                trade["reversal_scan_count"] = new_count
                if new_count == 0 and trade.get("score_drop_count", 0) > 0:
                    trade["score_drop_count"] = 0
                    logger.info(f"[{symbol}] Score-drop counter cleared (reversal decayed to 0).")
                if new_count == 0 and trade.get("hard_out_count", 0) > 0:
                    trade["hard_out_count"] = 0
                    logger.info(f"[{symbol}] Hard-out counter cleared (reversal decayed to 0).")
                tracker._save_log()

    # Fix 2: Clear confirm buffer and conviction streak for all exited symbols.
    for _exited_sym in closed:
        gate_state.entry_confirm_buffer.pop(_exited_sym, None)
        gate_state.conviction_streak.pop(_exited_sym, None)
        logger.debug(f"[{_exited_sym}] Confirm buffer + conviction streak cleared on exit.")

    return closed



# ─── EXTENDED HOURS EXIT CHECKS ──────────────────────────────────────────────

def _check_exits_extended_hours(
    tracker: PortfolioTracker,
    risk: "RiskManager",
    kelly: "KellySizer",
):
    """
    24/7 exit monitoring: runs during overnight and pre-market sessions.

    Uses Alpaca Data T1 get_latest_trade() for live EH price (DATA-2f).

    Exit logic:
      - Pre-partial  : hard stop breach → limit order to close full position
      - Pre-partial  : partial target hit → limit order to close PARTIAL_EXIT_RATIO of position
      - Post-partial : trail stop OR breakeven stop (entry price) hit → limit order for remainder
      - Ratchets trail stop in favorable direction each cycle

    extended_hours=True only during active Alpaca AH/PM windows:
      - Pre-market : 4:00 – 9:30 AM ET
      - After-hours: 4:00 – 8:00 PM ET
    """
    now_et = datetime.now(ET)
    mins   = now_et.hour * 60 + now_et.minute

    is_premarket  = (4 * 60) <= mins < (9 * 60 + 30)
    is_afterhours = (16 * 60) <= mins < (20 * 60)
    use_extended  = is_premarket or is_afterhours

    for symbol, trade in list(tracker.open_trades.items()):
        # QHM ownership guard (Option B, 2026-07-01): the main bot must not manage
        # exits for a QHM-held symbol during extended hours — QHM owns its Alpaca
        # position + its own GTC stop, and the EH windows overlap QHM's active
        # window. Quiet skip (RTH collision alert fires once via check_exits).
        if symbol in _get_qhm_syms():
            logger.debug("[%s] QHM-held — skipped by extended-hours exit management.", symbol)
            continue
        if trade.get("status") != "open":
            continue

        direction   = trade["direction"]
        entry_price = trade.get("entry_price")
        atr_value   = trade.get("atr_value") or 0
        qty_rem     = trade.get("qty_remaining", trade.get("qty", 0))

        if entry_price is None or qty_rem <= 0:
            continue

        # ── Reconcile any pending PM exit order ───────────────────────────
        pm_order_id = trade.get("pm_exit_order_id")
        if pm_order_id:
            try:
                order = get_order(pm_order_id)
            except Exception as _goe:
                logger.error(
                    "[%s] EH: get_order(%s) failed: %s: %s "
                    "— skipping EH reconciliation this cycle",
                    symbol, pm_order_id, type(_goe).__name__, _goe,
                )
                continue

            if order is None or str(getattr(order, "status", "")).lower() in ("canceled", "expired", "replaced"):
                trade["pm_exit_order_id"] = None
                trade["pm_exit_type"]     = None
                tracker._save_log()

            elif str(getattr(order, "status", "")).lower() in ("filled", "partially_filled"):
                fill_price = float(getattr(order, "filled_avg_price", None) or 0.0)
                if fill_price <= 0:
                    _after_ts = time.time() - (config.SCAN_INTERVAL_INTRADAY * 60 + 60)
                    fill_price = _fetch_actual_fill_price(
                        symbol, trade, poll_secs=0.3, submitted_after=_after_ts
                    )
                filled_qty = int(float(getattr(order, "filled_qty", None) or 1))
                if trade.get("pm_exit_type") == "partial":
                    trail_dist = atr_value * config.TRAIL_STOP_ATR_MULT
                    trail_stop = (
                        round(fill_price - trail_dist, 2) if direction == "long"
                        else round(fill_price + trail_dist, 2)
                    )
                    pnl = tracker.record_partial_exit(symbol, fill_price, filled_qty, trail_stop)
                    # MTF FULL BOT AUDIT — JUNE 26 (Gro HIGH/GAI CRITICAL,
                    # consensus fix): every other partial-exit path in this
                    # file calls risk.register_close() immediately after the
                    # P&L is known. This EH reconciliation branch was the one
                    # exception — a loss taken on an extended-hours partial
                    # exit was invisible to the daily kill-switch total.
                    if risk is not None:
                        risk.register_close(pnl or 0.0)
                    kelly.record_trade(
                        direction, trade.get("trade_mode", "swing"),
                        entry_price, fill_price, trade.get("stop"), filled_qty,
                    )
                    logger.info(
                        f"[{symbol}] EH partial exit confirmed: fill=${fill_price:.2f} "
                        f"| P&L ${pnl:.2f} | Trail @ ${trail_stop:.2f} "
                        f"| Breakeven stop @ ${entry_price:.2f}"
                    )
                else:
                    pnl = tracker.record_exit(symbol, fill_price, reason="pm_exit",
                                              mri_level="UNKNOWN")
                    risk.register_close(pnl or 0.0)
                    logger.info(
                        f"[{symbol}] EH exit confirmed: fill=${fill_price:.2f} | P&L ${pnl:.2f}"
                    )
                trade["pm_exit_order_id"] = None
                trade["pm_exit_type"]     = None
                tracker._save_log()
                continue

            else:
                continue

        # ── Fetch current EH price via Alpaca Data REST API ─────────────
        try:
            current_price = get_latest_trade(symbol)
            if not current_price or current_price <= 0:
                continue
        except Exception as _e:
            logger.warning(f"[{symbol}] EH price fetch failed: {_e}")
            continue

        # ── Post-partial: trail stop + breakeven floor ────────────────────
        if trade.get("partial_exited"):
            trail_stop     = trade.get("trail_stop")
            breakeven_stop = trade.get("stop")

            trail_hit = trail_stop is not None and (
                (direction == "long"  and current_price <= trail_stop) or
                (direction == "short" and current_price >= trail_stop)
            )
            be_hit = breakeven_stop is not None and (
                (direction == "long"  and current_price <= breakeven_stop) or
                (direction == "short" and current_price >= breakeven_stop)
            )

            if trail_hit or be_hit:
                exit_side   = "sell" if direction == "long" else "buy"
                limit_price = round(
                    current_price * (0.999 if direction == "long" else 1.001), 2
                )
                order = submit_limit_order(
                    symbol, qty_rem, exit_side, limit_price, extended_hours=use_extended
                )
                if order:
                    trade["pm_exit_order_id"] = order.id  # type: ignore[attr-defined]
                    trade["pm_exit_type"]     = "full"
                    tracker._save_log()
                    reason = "trail_stop" if trail_hit else "breakeven_stop"
                    logger.info(
                        f"[{symbol}] EH {reason} @ ${current_price:.2f} → "
                        f"limit {exit_side.upper()} {qty_rem} @ ${limit_price:.2f} "
                        f"extended_hours={use_extended}"
                    )
                continue

            if trail_stop is not None and atr_value > 0:
                trail_dist = atr_value * config.TRAIL_STOP_ATR_MULT
                new_trail  = (
                    round(current_price - trail_dist, 2) if direction == "long"
                    else round(current_price + trail_dist, 2)
                )
                tracker.update_trail_stop(symbol, new_trail)
            continue

        # ── Pre-partial: hard stop check ──────────────────────────────────
        hard_stop = trade.get("stop")
        if hard_stop is not None:
            stop_hit = (
                (direction == "long"  and current_price <= hard_stop) or
                (direction == "short" and current_price >= hard_stop)
            )
            if stop_hit:
                exit_side   = "sell" if direction == "long" else "buy"
                limit_price = round(
                    current_price * (0.999 if direction == "long" else 1.001), 2
                )
                order = submit_limit_order(
                    symbol, qty_rem, exit_side, limit_price, extended_hours=use_extended
                )
                if order:
                    trade["pm_exit_order_id"] = order.id  # type: ignore[attr-defined]
                    trade["pm_exit_type"]     = "full"
                    tracker._save_log()
                    logger.info(
                        f"[{symbol}] EH HARD STOP hit @ ${current_price:.2f} → "
                        f"limit {exit_side.upper()} {qty_rem} @ ${limit_price:.2f} "
                        f"extended_hours={use_extended}"
                    )
                continue

        # ── Pre-partial: first partial exit check ─────────────────────────
        if not config.PARTIAL_EXIT_ENABLED or trade.get("partial_exited"):
            continue
        if atr_value <= 0 or qty_rem < 2:
            continue

        partial_target_dist = atr_value * config.PARTIAL_EXIT_ATR_MULT
        if direction == "long":
            partial_target = round(entry_price + partial_target_dist, 2)
            hit = current_price >= partial_target
        else:
            partial_target = round(entry_price - partial_target_dist, 2)
            hit = current_price <= partial_target

        if hit:
            qty_to_close = max(1, int(qty_rem * config.PARTIAL_EXIT_RATIO))
            exit_side    = "sell" if direction == "long" else "buy"
            order = submit_limit_order(
                symbol, qty_to_close, exit_side, partial_target, extended_hours=use_extended
            )
            if order:
                trade["pm_exit_order_id"] = order.id  # type: ignore[attr-defined]
                trade["pm_exit_type"]     = "partial"
                trade["partial_exited"]   = True
                tracker._save_log()
                logger.info(
                    f"[{symbol}] EH PARTIAL EXIT ORDER: {qty_to_close} @ ${partial_target:.2f} "
                    f"extended_hours={use_extended} | Order ID: {order.id}"  # type: ignore[attr-defined]
                )
