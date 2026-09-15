# ruff: noqa: E501
"""
strategy/day_tier_sizing.py — Day-Tier position SIZING (PURE budget fn — no order; wired to the LIVE runner).

Sixth increment of the day-tier engine rebuild. Turns a triggered day-tier decision into a
budget-bounded, conviction-scaled SHARE COUNT (design §2 "size per meta-label conviction, bounded
by tier allocation + gross-notional cap"; §3 risk; §7b.6 allocation split). It is a PURE function:
equity is passed IN (no broker call here), so it is deterministic and fully unit-testable, and it
RETURNS a number — it places NO order (the order + the live equity/book fetch + the account-level
gross-notional cap live at WIRE-TIME in day_trade_manager._bounded_entry_qty). Wired to the LIVE runner
(run_day_tier.py) 2026-09-08 — still a PURE budget function here: no order, no broker call, deterministic
and fully unit-testable (equity + buying_power are passed IN).

THE SIZING (min()-ONLY — the conservative posture, never a max/upsize):
  Track A: track_budget = min(buying_power × DAYTRADE_TRACK_A_PER_TRADE_BP_PCT,
                              equity × DAYTRADE_TRACK_A_EQUITY_CEILING_PCT)
  Track B: track_budget = equity × DAYTIER_ALLOC_PCT × TRACK_B_SHARE        (CASH-only; unchanged)
  target        = track_budget × conviction                       (conviction ∈ [0,1] scales within budget)
  notional      = min(target, track_budget)                       (never exceed the track's own budget)
  shares        = floor(notional / entry_ref)                     (RC-7: whole-share floor; 0 = can't afford → skip)
  MIN-1-SHARE FLOOR (Rafael CEO directive 2026-09-15): if `shares` floors to 0 but the per-trade
    `track_budget` can afford a whole share (track_budget >= entry_ref), take 1 share — so a genuinely-
    triggered ENTER is never DROPPED purely because conviction-scaling rounded it below one share. It is
    bounded by track_budget (1×px <= budget by the guard) and re-clamped DOWN for margin/stop-risk/gross
    at WIRE-TIME (day_trade_manager._bounded_entry_qty). Gated by config.DAYTRADE_MIN_ONE_SHARE_FLOOR
    (default True; set False to disable — Rule D kill flag). A name too expensive for the per-trade
    budget (track_budget < px) still skips (size 0). The day-tier is EOD force-flat, so the BP returns
    before the close.
Conviction only ever SHRINKS the budget, EXCEPT the documented MIN-1-SHARE FLOOR above. Track A sizes from Alpaca's current BUYING POWER and is
flat-by-close; a missing/non-positive/non-finite BP → size 0 (fail-CLOSED — NEVER an equity fallback,
which would mask the failure and resurrect the old ~$243 equity cap). This function is the PER-TRADE
budget ONLY; the AGGREGATE Track-A gross cap, the main-bot BP reserve, and the maintenance cushion are
enforced at WIRE-TIME in day_trade_manager._bounded_entry_qty (board + Gro + GAI + masked-loss seat
2026-09-08; design logs/design_records/day_tier_bp_sizing_2026-09-08.md). Track B is cash-only.

WHAT IS NOT HERE (deferred to the wired Layer C, on purpose): the account-level GROSS-notional cap
across concurrent positions (needs the live book), the ~$650 maintenance-cushion guard, the per-tier
sub-kill accrual, and any real order. Those are the risk-path pieces that fire the masked-loss gate.

FAIL-SAFE: not would_consider, non-positive equity/entry_ref/conviction, or any error → 0 shares,
size_ok=False. Never over-sizes, never raises. All allocation/scaling constants are PROV-tagged
(board-aligned §7b.6 policy values, tunable as the tier validates) — and INERT (drive no live trade).
"""
from __future__ import annotations

import logging
import math

import config

logger = logging.getLogger(__name__)

# Allocation policy (board-aligned §7b.6 — Thorp/Taleb/Dalio unanimous DIVIDE). PROV-tagged: these
# are the aligned STARTING allocation (start day-tier at 15% of equity, A 65% / B 35%), tunable as
# the tier validates (scale toward 25%); they are POLICY, not data-derived, and drive NO live trade
# here (inert). DERIVATION/REVIEW PLAN (PROV:daytier-sizing) — revisit the alloc % and the A/B split
# against realized per-track expectancy + the A/B P&L correlation watch-flag (§7b.6) before scaling.
_DAYTIER_ALLOC_PCT = 0.15   # PROV:daytier-sizing — whole day-tier slice of equity (§7b.6 start)
_TRACK_A_SHARE = 0.65       # PROV:daytier-sizing — Track A (GEX-core) share of the tier budget
_TRACK_B_SHARE = 0.35       # PROV:daytier-sizing — Track B (movers) share; cash-only


def compute_day_tier_size(symbol: str, decision: dict, entry_ref, equity, buying_power=None, track: str = "A") -> dict:
    """Budget-bounded, conviction-scaled share count for a triggered day-tier decision. PURE (no order).

    Args:
      symbol       : the underlying.
      decision     : a strategy.day_tier_decision result (needs would_consider + conviction).
      entry_ref    : the entry reference price (e.g. entry_trigger["entry_ref"]).
      equity       : account equity (passed IN — no broker call here; the caller fetches it live).
      buying_power : account buying power (Track A budget basis; passed IN). None/≤0/non-finite on
                     Track A → size 0, size_ok False (fail-CLOSED, no equity fallback). Ignored on Track B.
      track        : "A" (GEX-core, sizes off buying power) or "B" (movers, cash-only).

    Returns:
      {"symbol", "shares": int, "notional", "budget", "track", "cash_only": bool,
       "conviction", "size_ok": bool, "reason"}
    shares is 0 (size_ok False) whenever the decision is not a would_consider candidate, any input is
    non-positive, or the budget cannot afford a whole share. Never over-sizes; never raises.
    """
    result: dict = {
        "symbol": symbol, "shares": 0, "notional": 0.0, "budget": 0.0, "track": "A",
        "cash_only": False, "conviction": 0.0, "size_ok": False, "reason": "",
    }
    try:
        # Inside the try so even a pathological `track` (a raising __str__) fails safe, not raises.
        _track = "B" if str(track).upper() == "B" else "A"
        result["track"] = _track
        result["cash_only"] = (_track == "B")
        if not isinstance(decision, dict) or not decision.get("would_consider"):
            result["reason"] = "not a would_consider candidate — size 0"
            return result
        try:
            conviction = float(decision.get("conviction", 0.0) or 0.0)
            eq = float(equity)
            px = float(entry_ref)
        except (TypeError, ValueError):
            result["reason"] = "non-numeric conviction/equity/entry_ref — size 0"
            return result
        # NaN/inf slip the numeric cast; guard them explicitly (a clean skip, not the catch-all).
        if not (math.isfinite(conviction) and math.isfinite(eq) and math.isfinite(px)):
            result["reason"] = "non-finite conviction/equity/entry_ref — size 0"
            return result
        # Clamp conviction to [0,1] (a signal never up-sizes past the budget slice).
        conviction = 0.0 if conviction < 0.0 else (1.0 if conviction > 1.0 else conviction)
        result["conviction"] = round(conviction, 3)
        if eq <= 0 or px <= 0 or conviction <= 0:
            result["reason"] = f"non-positive equity({eq})/entry_ref({px})/conviction({conviction}) — size 0"
            return result

        if _track == "A":
            # Track A sizes off BUYING POWER (Rafael 2026-09-08; config Track-A BP block). Missing/
            # non-positive/non-finite BP → size 0, size_ok False (fail-CLOSED; NEVER an equity fallback —
            # that would mask the failure and resurrect the old ~$243 equity cap).
            try:
                bp = float(buying_power) if buying_power is not None else 0.0
            except (TypeError, ValueError):
                bp = 0.0
            if not (math.isfinite(bp) and bp > 0):
                result["reason"] = f"track A: buying_power unavailable ({buying_power}) — size 0 (fail-closed)"
                logger.info("[%s] day-tier SIZE: %s", symbol, result["reason"])
                return result
            per_trade_pct = float(getattr(config, "DAYTRADE_TRACK_A_PER_TRADE_BP_PCT", 0.20))  # PROV:daytier-bp-2026-09-08
            equity_ceiling_pct = float(getattr(config, "DAYTRADE_TRACK_A_EQUITY_CEILING_PCT", 0.60))  # PROV:daytier-bp-2026-09-08
            track_budget = min(bp * per_trade_pct, eq * equity_ceiling_pct)
        else:
            # Track B stays CASH-ONLY (settled-cash proxy = equity slice) — unchanged; B is OFF day-1.
            track_budget = eq * _DAYTIER_ALLOC_PCT * _TRACK_B_SHARE
        result["budget"] = round(track_budget, 2)

        # min()-only: conviction scales DOWN from the track budget; the budget is the hard cap.
        target_notional = track_budget * conviction
        notional = min(target_notional, track_budget)
        # Whole-share floor (RC-7): int() of a sub-1.0 share count is 0 = cannot afford → skip.
        shares = int(math.floor(notional / px))
        # MIN-1-SHARE FLOOR (Rafael CEO directive 2026-09-15): a genuinely-triggered ENTER whose
        # conviction-scaled notional floors below one whole share still takes ONE share — PROVIDED the
        # per-trade track_budget can afford a whole share (track_budget >= px). This never exceeds the
        # per-trade budget cap (1×px <= track_budget by the guard), and the wire-time _bounded_entry_qty
        # (execution/day_trade_manager.place_entry) still re-clamps DOWN for the main-bot BP reserve, the
        # maintenance cushion, the day/global gross caps, and the <=2%-equity stop-risk cap — re-zeroing
        # it whenever live margin/risk cannot afford it (min()-only, fail-closed). The day-tier is EOD
        # force-flat at T-DAYTRADE_FORCE_FLAT_MINUTES, so this buying power is returned before the close.
        # Sizing runs only after trigger==ENTER (run_day_tier.py entry loop). Kill flag:
        # config.DAYTRADE_MIN_ONE_SHARE_FLOOR (default True; set False in config.py to disable — Rule D).
        floor_enabled = bool(getattr(config, "DAYTRADE_MIN_ONE_SHARE_FLOOR", True))
        floored = False
        if shares < 1 and track_budget >= px and floor_enabled:
            shares = 1
            floored = True
        result["shares"] = max(0, shares)
        result["notional"] = round(result["shares"] * px, 2)
        result["size_ok"] = result["shares"] >= 1
        if result["size_ok"]:
            _floor_note = (
                " [MIN-1-SHARE FLOOR: conviction-scaled notional < 1 share; per-trade budget affords 1 "
                "— wire-time re-clamps for margin/stop-risk/gross]" if floored else ""
            )
            result["reason"] = (
                f"track {_track}: budget ${track_budget:.2f} × conviction {conviction:.2f} "
                f"= ${target_notional:.2f} → {result['shares']} sh @ ${px:.2f} "
                f"(${result['notional']:.2f}{', cash-only' if result['cash_only'] else ''}){_floor_note} "
                f"— per-trade budget; aggregate gross cap + main-bot reserve + cushion enforced at wire-time"
            )
        elif track_budget >= px and not floor_enabled:
            # Budget COULD afford a whole share; the min-1-share floor is switched OFF. Report the REAL
            # cause honestly (do NOT say 'budget < 1 share' when the floor being disabled is why).
            result["reason"] = (
                f"track {_track}: budget ${track_budget:.2f} affords a whole share @ ${px:.2f} but the "
                f"MIN-1-SHARE FLOOR is disabled (DAYTRADE_MIN_ONE_SHARE_FLOOR=False) — size 0 (skip)"
            )
        else:
            result["reason"] = (
                f"track {_track}: budget ${track_budget:.2f} × conviction {conviction:.2f} "
                f"= ${target_notional:.2f} < 1 share @ ${px:.2f} — size 0 (skip; track_budget < 1 share)"
            )
        logger.info("[%s] day-tier SIZE: %s", symbol, result["reason"])
        return result
    except Exception as _e:  # a pure sizing helper must NEVER raise into a caller
        result["shares"] = 0
        result["size_ok"] = False
        result["reason"] = f"unexpected error: {_e!r}"
        logger.warning("[%s] day-tier SIZE: unexpected error — size 0: %s", symbol, _e)
        return result
