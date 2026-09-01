# ruff: noqa: E501
"""
strategy/day_tier_decision.py — Day-Tier meta-label COMPOSITION (READ-ONLY, INERT).

Fourth increment of the day-tier engine rebuild. Composes the two shipped read-only decision
layers into ONE structured decision record for a symbol:
  Layer A — structural SIDE (strategy.day_tier_side.compute_side_bias): LONG/SHORT/TWO_SIDED.
  Layer B — GEX whether-to-act (strategy.day_tier_gex_action.compute_gex_action): FADE/RIDE/
            STAND_DOWN + target levels + act_ok + a strength score.
This is the meta-label the design calls for (§2 "SIDE = primary model; B+C = whether-to-act + size;
size per meta-label CONVICTION"). It DECIDES NOTHING new about direction — the price-action trigger
(failed-sweep for a fade, wall-break for a ride) and the actual sizing + order are Layer C's job
(§7c, "price action is North Star" §7b.5). It only ASSEMBLES the decision-stack + a provisional
conviction so Layer C has one object to size + execute, and so this can be shadow-LOGGED now to
accumulate the front-loaded-validation data the design requires before Layer C goes live (§B, §4
Decision-Explainability).

READ-ONLY + INERT: wired to NOTHING (no live caller); computes and returns a record; sizes/enters/
orders nothing. Committed INERT (the lost-engine lesson). FAIL-SAFE: any sub-layer that errors or
returns UNKNOWN degrades the composition to would_consider=False / conviction 0 — never raises.

The conviction blend + would_consider gate are PROVISIONAL on an INERT signal (PROV-tagged, no live
risk); derived from the shadow-logged outcomes once this feeds Layer C (design's "prune-if-no-edge"
/ pre-registered validation). Every threshold here drives NO live trade today.
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# DERIVATION PLAN (PROV:daytier-decision) — the conviction weights + the would_consider floor are
# PROVISIONAL starting values on an INERT composition (no live trade). Once day_tier_decision has
# shadow-logged enough scored reads WITH realized outcomes, derive: the SIDE-vs-ACTION weight split
# from each component's marginal contribution to forward-return separation (§7 "first metric"); the
# conviction floor from the conviction-vs-expectancy curve. Until then these are starting values.
_SIDE_WEIGHT = 0.4        # PROV:daytier-decision — structural-side share of conviction
_ACTION_WEIGHT = 0.6      # PROV:daytier-decision — gex-action-strength share (the tradeable edge leads)
_MIN_CONVICTION = 0.15    # PROV:daytier-decision — below this, would_consider is False (too weak)

# A structural side that carries a directional lean (UNKNOWN/none carry none). TWO_SIDED is a valid
# read (play either way) so it does NOT block consideration — it just contributes no directional score.
_DIRECTIONAL_SIDES = frozenset(("LONG", "SHORT"))
_VALID_SIDES = frozenset(("LONG", "SHORT", "TWO_SIDED"))


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def compute_day_tier_decision(symbol: str, now_et: "datetime | None" = None) -> dict:
    """Compose Layer A (side) + Layer B (whether-to-act) into one day-tier decision record.

    READ-ONLY + INERT. Returns a dict (the shadow-log decision-stack):
      {"symbol", "side", "side_score", "gex_action", "gex_label", "act_ok", "strength",
       "targets", "sign_reliable", "conviction", "would_consider": bool, "reason"}
    would_consider is True ONLY when Layer B says act_ok, the structural side is a valid read
    (LONG/SHORT/TWO_SIDED, not UNKNOWN), and the blended conviction clears _MIN_CONVICTION. It is a
    CANDIDATE flag for Layer C to then confirm with live price action — NOT a trade. Never raises.
    """
    result: dict = {
        "symbol": symbol, "side": "UNKNOWN", "side_score": None, "gex_action": "STAND_DOWN",
        "gex_label": "UNKNOWN", "act_ok": False, "strength": 0.0, "targets": {},
        "sign_reliable": False, "conviction": 0.0, "would_consider": False, "reason": "",
    }
    try:
        from strategy.day_tier_gex_action import compute_gex_action
        from strategy.day_tier_side import compute_side_bias

        side = compute_side_bias(symbol)
        action = compute_gex_action(symbol, side_bias=side, now_et=now_et)

        _side = side.get("side", "UNKNOWN")
        _side_score = side.get("score")
        result["side"] = _side
        result["side_score"] = _side_score
        result["gex_action"] = action.get("action", "STAND_DOWN")
        result["gex_label"] = action.get("gex_label", "UNKNOWN")
        result["act_ok"] = bool(action.get("act_ok"))
        result["strength"] = action.get("strength", 0.0) or 0.0
        result["targets"] = action.get("targets", {}) or {}
        result["sign_reliable"] = bool(action.get("sign_reliable"))

        # Conviction: blend the structural-side magnitude (|score|, directional sides only) with the
        # gex-action strength. A TWO_SIDED side contributes NO directional score but does not block.
        # A non-act_ok action collapses conviction to 0 (nothing to confide in). PROVISIONAL blend.
        _side_mag = abs(_side_score) if (_side in _DIRECTIONAL_SIDES and isinstance(_side_score, (int, float))) else 0.0
        _strength = result["strength"]
        if result["act_ok"]:
            # Round ONCE and gate on the same value that is stored/logged, so the logged conviction
            # can never read as meeting the floor while would_consider is False (cold-2nd note).
            conviction = round(_clamp01(_SIDE_WEIGHT * _clamp01(_side_mag) + _ACTION_WEIGHT * _clamp01(_strength)), 3)
        else:
            conviction = 0.0
        result["conviction"] = conviction

        result["would_consider"] = bool(
            result["act_ok"] and _side in _VALID_SIDES and conviction >= _MIN_CONVICTION
        )
        result["reason"] = (
            f"side={_side}({_side_score}) gex={result['gex_action']}/{result['gex_label']} "
            f"act_ok={result['act_ok']} strength={_strength:.2f} conviction={conviction:.2f} "
            f"-> would_consider={result['would_consider']} "
            f"(Layer C confirms with live price action; INERT — trades nothing)"
        )
        logger.info("[%s] day-tier DECISION (INERT): %s", symbol, result["reason"])
        return result
    except Exception as _e:  # read-only composition must NEVER raise into a caller
        result["would_consider"] = False
        result["conviction"] = 0.0
        result["reason"] = f"unexpected error: {_e!r}"
        logger.warning("[%s] day-tier DECISION: unexpected error — would_consider=False: %s", symbol, _e)
        return result
