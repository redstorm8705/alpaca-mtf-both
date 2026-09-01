# ruff: noqa: E501
"""
strategy/day_tier_sizing.py — Day-Tier position SIZING (PURE, READ-ONLY, INERT).

Sixth increment of the day-tier engine rebuild. Turns a triggered day-tier decision into a
budget-bounded, conviction-scaled SHARE COUNT (design §2 "size per meta-label conviction, bounded
by tier allocation + gross-notional cap"; §3 risk; §7b.6 allocation split). It is a PURE function:
equity is passed IN (no broker call here), so it is deterministic and fully unit-testable, and it
RETURNS a number — it places NO order (the order + the live equity/book fetch + the account-level
gross-notional cap are Layer C's later RISK-PATH increments, gated with the full board + masked-loss
seat when the tier is wired to trade). Wired to NOTHING today; committed INERT (the lost-engine lesson).

THE SIZING (min()-ONLY — the conservative posture, never a max/upsize):
  tier_budget   = equity × DAYTIER_ALLOC_PCT                       (the whole day-tier slice, §7b.6: 15%)
  track_budget  = tier_budget × (TRACK_A_SHARE | TRACK_B_SHARE)    (A 65% / B 35%, §7b.6)
  target        = track_budget × conviction                       (conviction ∈ [0,1] scales within budget)
  notional      = min(target, track_budget)                       (never exceed the track's own budget)
  shares        = floor(notional / entry_ref)                     (RC-7: whole-share floor; 0 = can't afford → skip)
This NEVER increases size beyond a slice of equity, and conviction only ever SHRINKS it from that
slice. Track B is cash-only (§7b.6): its notional is already bounded by track_budget (a slice of
equity, not margin) and flagged cash_only for the order layer to enforce no-margin.

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

logger = logging.getLogger(__name__)

# Allocation policy (board-aligned §7b.6 — Thorp/Taleb/Dalio unanimous DIVIDE). PROV-tagged: these
# are the aligned STARTING allocation (start day-tier at 15% of equity, A 65% / B 35%), tunable as
# the tier validates (scale toward 25%); they are POLICY, not data-derived, and drive NO live trade
# here (inert). DERIVATION/REVIEW PLAN (PROV:daytier-sizing) — revisit the alloc % and the A/B split
# against realized per-track expectancy + the A/B P&L correlation watch-flag (§7b.6) before scaling.
_DAYTIER_ALLOC_PCT = 0.15   # PROV:daytier-sizing — whole day-tier slice of equity (§7b.6 start)
_TRACK_A_SHARE = 0.65       # PROV:daytier-sizing — Track A (GEX-core) share of the tier budget
_TRACK_B_SHARE = 0.35       # PROV:daytier-sizing — Track B (movers) share; cash-only


def compute_day_tier_size(symbol: str, decision: dict, entry_ref, equity, track: str = "A") -> dict:
    """Budget-bounded, conviction-scaled share count for a triggered day-tier decision. PURE + INERT.

    Args:
      symbol    : the underlying.
      decision  : a strategy.day_tier_decision result (needs would_consider + conviction).
      entry_ref : the entry reference price (e.g. entry_trigger["entry_ref"]).
      equity    : account equity (passed IN — no broker call here; the caller fetches it live).
      track     : "A" (GEX-core, 65%) or "B" (movers, 35%, cash-only).

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

        track_share = _TRACK_A_SHARE if _track == "A" else _TRACK_B_SHARE
        track_budget = eq * _DAYTIER_ALLOC_PCT * track_share
        result["budget"] = round(track_budget, 2)

        # min()-only: conviction scales DOWN from the track budget; the budget is the hard cap.
        target_notional = track_budget * conviction
        notional = min(target_notional, track_budget)
        # Whole-share floor (RC-7): int() of a sub-1.0 share count is 0 = cannot afford → skip.
        shares = int(math.floor(notional / px))
        result["shares"] = max(0, shares)
        result["notional"] = round(result["shares"] * px, 2)
        result["size_ok"] = result["shares"] >= 1
        if result["size_ok"]:
            result["reason"] = (
                f"track {_track}: budget ${track_budget:.2f} × conviction {conviction:.2f} "
                f"= ${target_notional:.2f} → {result['shares']} sh @ ${px:.2f} "
                f"(${result['notional']:.2f}{', cash-only' if result['cash_only'] else ''}) "
                f"— INERT, no order placed; account gross cap + cushion enforced at wire-time"
            )
        else:
            result["reason"] = (
                f"track {_track}: budget ${track_budget:.2f} × conviction {conviction:.2f} "
                f"= ${target_notional:.2f} < 1 share @ ${px:.2f} — size 0 (skip)"
            )
        logger.info("[%s] day-tier SIZE (INERT): %s", symbol, result["reason"])
        return result
    except Exception as _e:  # a pure sizing helper must NEVER raise into a caller
        result["shares"] = 0
        result["size_ok"] = False
        result["reason"] = f"unexpected error: {_e!r}"
        logger.warning("[%s] day-tier SIZE: unexpected error — size 0: %s", symbol, _e)
        return result
