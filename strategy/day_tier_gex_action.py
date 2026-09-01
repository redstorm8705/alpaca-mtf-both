# ruff: noqa: E501
"""
strategy/day_tier_gex_action.py — Day-Tier Layer B: GEX whether-to-act (READ-ONLY, INERT).

Third increment of the day-tier engine rebuild (design record day_tier_v2_design_2026-08-29.md
§2 "WHETHER-TO-ACT" + §1.5 DTE/TOD conditioning + §1.6 single-name sign risk). Wired to NOTHING
(no live caller) — it computes and returns a decision; it sizes/enters/orders nothing. Committed
INERT so it survives (the lost-engine lesson). Composes the two increments already shipped:
Layer A structural side (strategy.day_tier_side.compute_side_bias) and the per-symbol GEX
regime + pin/wall levels (data.gex.get_gex_regime / get_gex_levels, #203/#221).

THE DECISION (design §2): dealer gamma sets the action MODE.
  +gamma (POSITIVE)  -> FADE  : dealers dampen; a failed sweep mean-reverts to the pin/centroid.
  -gamma (NEGATIVE)  -> RIDE  : dealers amplify; a break through a wall runs (momentum).
  NEAR-FLIP / STALE / UNKNOWN / no levels -> STAND_DOWN (ambiguous or untrustworthy → do nothing).
This module DECIDES THE MODE + supplies the target levels + a conviction score; the actual
price-action trigger (failed-sweep for a fade, wall-break for a ride) is Layer C's job (§7c).
Price action decides the entry; this only says which game is on (§7b.5 "price action is North Star").

SIGN RELIABILITY (§1.6, Sinclair): "+GEX = pinned" is dependable for INDEX gamma (SPY/QQQ) but a
single name's sign can invert (customer call-buying leaves dealers short gamma at a nominal +GEX
strike). So a single-name action requires the pin's own data-quality confidence to clear a floor;
an index is trusted at any resolved pin. A non-reliable read -> act_ok=False (STAND_DOWN-equivalent
for sizing) while still reporting the mode for the shadow log.

CONDITIONING (§1.5): pin pull is strongest near expiry (0-2 DTE / OpEx) and in the PM (pins tighten
after ~2pm ET); -gamma breaks are a morning open-drive phenomenon. The `strength` score folds
pin-confidence × a DTE-proximity factor × a time-of-day factor into [0,1] to inform Layer C sizing.
It NEVER changes the MODE — only the conviction. All conditioning constants are PROVISIONAL/tunable
on an INERT signal (PROV-tagged, no live risk); derived from outcomes once this feeds the engine.

FAIL-SAFE: any missing/stale/unresolved GEX read -> STAND_DOWN, act_ok=False, strength 0. Never
raises to a caller (a read-only signal must never break a future loop).

Data tier: reads cached T1-derived GEX snapshot via data.gex (no new fetch here).
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Index symbols whose gamma SIGN is dependable (§1.6) — trusted at any resolved pin.
_INDEX_SYMBOLS = frozenset(("SPY", "QQQ"))

# ── Conditioning constants — PROVISIONAL on an INERT signal (drives no live trade). Tagged per the
# no-static-regimes no-data-yet exception; derived from outcomes once Layer B feeds the engine
# (design §1.5/§1.9/§10 pre-registered validation). See DERIVATION PLAN below. ──
# DERIVATION PLAN (PROV:daytier-gex-action) — once compute_gex_action has logged enough scored reads
# WITH realized fade/ride outcomes, derive: the single-name confidence floor from the confidence-vs-
# hit-rate curve (the point where the gamma-classification call becomes reliable, §7 "first metric");
# the DTE decay + TOD factors from outcome-by-DTE / outcome-by-hour buckets (fade PM vs ride AM,
# §1.5); the strength floor from the strength-vs-expectancy curve. Until then these are starting values.
_SINGLE_NAME_CONF_FLOOR = 0.35   # PROV:daytier-gex-action — single-name pin confidence needed to trust the sign
_MIN_STRENGTH = 0.20             # PROV:daytier-gex-action — below this, act_ok False (too weak to act)
_DTE_FULL = 2                    # PROV:daytier-gex-action — <= this many DTE = full pin pull (OpEx/0DTE)
_DTE_ZERO = 9                    # PROV:daytier-gex-action — >= this many DTE = pin pull ~decayed to floor
_DTE_FLOOR_FACTOR = 0.4          # PROV:daytier-gex-action — mid-cycle pin-strength floor (never 0 — a weekly still pins some)
_TOD_PM_HOUR = 14                # PROV:daytier-gex-action — 2pm ET: pins tighten after this (fade PM boost / ride AM boost)
_TOD_BOOST = 1.0                 # PROV:daytier-gex-action — factor in the favored session
_TOD_OFF = 0.7                   # PROV:daytier-gex-action — factor in the unfavored session

_ACTIONS = {"POSITIVE": "FADE", "NEGATIVE": "RIDE"}   # regime label -> mode (design §2); else STAND_DOWN


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _dte_proximity(dte) -> float:
    """Pin-pull factor from days-to-expiry: 1.0 at <=_DTE_FULL (OpEx/0DTE), decaying linearly to
    _DTE_FLOOR_FACTOR at >=_DTE_ZERO, never below the floor (a weekly still pins somewhat). An
    unknown dte -> the floor (conservative)."""
    try:
        d = float(dte)
    except (TypeError, ValueError):
        return _DTE_FLOOR_FACTOR
    if d <= _DTE_FULL:
        return 1.0
    if d >= _DTE_ZERO:
        return _DTE_FLOOR_FACTOR
    frac = (d - _DTE_FULL) / (_DTE_ZERO - _DTE_FULL)   # 0..1 across the decay band
    return 1.0 - frac * (1.0 - _DTE_FLOOR_FACTOR)


def _tod_factor(mode: str, now_et: datetime) -> float:
    """Time-of-day conditioning (§1.5): a FADE is favored in the PM (pins tighten after ~2pm ET);
    a RIDE (-gamma break) is favored in the AM open-drive. Returns _TOD_BOOST in the favored
    session, _TOD_OFF otherwise."""
    is_pm = now_et.hour >= _TOD_PM_HOUR
    if mode == "FADE":
        return _TOD_BOOST if is_pm else _TOD_OFF
    if mode == "RIDE":
        return _TOD_BOOST if not is_pm else _TOD_OFF
    return _TOD_OFF


def compute_gex_action(symbol: str, side_bias: "dict | None" = None, now_et: "datetime | None" = None) -> dict:
    """Per-name GEX whether-to-act decision (design §2). READ-ONLY + INERT.

    Args:
      symbol   : the underlying (leveraged trackers resolve inside data.gex).
      side_bias: optional Layer-A result (strategy.day_tier_side.compute_side_bias) — carried into
                 the output for the meta-label combination Layer C will make; NOT a hard gate here.
      now_et   : injectable clock for the TOD factor (defaults to datetime.now(ET)).

    Returns a dict:
      {"symbol", "action": "FADE"|"RIDE"|"STAND_DOWN", "gex_label", "sign_reliable": bool,
       "pin_confidence", "dte", "pin_strength", "tod_factor", "strength", "targets": {..},
       "side": <side_bias side or None>, "act_ok": bool, "reason"}
    act_ok is True ONLY for a resolved, sign-reliable, strong-enough FADE/RIDE. Never raises.
    """
    if now_et is None:
        now_et = datetime.now(ET)
    _side = side_bias.get("side") if isinstance(side_bias, dict) else None
    result: dict = {
        "symbol": symbol, "action": "STAND_DOWN", "gex_label": "UNKNOWN",
        "sign_reliable": False, "pin_confidence": 0.0, "dte": None, "pin_strength": 0.0,
        "tod_factor": _TOD_OFF, "strength": 0.0, "targets": {}, "side": _side,
        "act_ok": False, "reason": "",
    }
    try:
        from data.gex import get_gex_levels, get_gex_regime
        levels = get_gex_levels(symbol)
        regime = get_gex_regime(symbol)
        label = levels.get("label", "UNKNOWN")
        result["gex_label"] = label
        result["pin_confidence"] = levels.get("confidence", 0.0) or 0.0
        result["dte"] = levels.get("dte")

        if not levels.get("levels_ok"):
            result["reason"] = f"no actionable pin levels (label={label})"
            return result

        mode = _ACTIONS.get(label)   # FADE / RIDE / None
        if mode is None:
            result["reason"] = f"regime {label} is not actionable (NEAR-FLIP/STALE/UNKNOWN) — stand down"
            return result

        # Sign reliability (§1.6): index trusted; single name needs pin confidence to clear the floor.
        conf = result["pin_confidence"]
        is_index = symbol.upper() in _INDEX_SYMBOLS
        sign_reliable = bool(is_index or conf >= _SINGLE_NAME_CONF_FLOOR)
        result["sign_reliable"] = sign_reliable

        # Conditioning (§1.5): pin_strength (DTE) × tod × confidence -> strength in [0,1].
        pin_strength = _dte_proximity(levels.get("dte"))
        tod = _tod_factor(mode, now_et)
        strength = _clamp01(pin_strength * tod * conf)
        result["pin_strength"] = round(pin_strength, 3)
        result["tod_factor"] = round(tod, 3)
        result["strength"] = round(strength, 3)

        # Targets: FADE -> the pin centroid (magnet); RIDE -> the walls (break levels). Layer C
        # picks the break direction from live price action (§7c); this supplies the candidates.
        if mode == "FADE":
            result["targets"] = {"pin": levels.get("centroid"), "wall": levels.get("wall")}
        else:  # RIDE
            result["targets"] = {"call_wall": levels.get("call_wall"), "put_wall": levels.get("put_wall")}

        result["action"] = mode
        result["act_ok"] = bool(sign_reliable and strength >= _MIN_STRENGTH)
        result["reason"] = (
            f"{mode} (gex={label}, raw_gex_m={regime.get('raw_gex_m')}, conf={conf:.2f}, "
            f"sign_reliable={sign_reliable}, strength={strength:.2f}, dte={result['dte']}, "
            f"pm={'Y' if now_et.hour >= _TOD_PM_HOUR else 'N'}, side={_side})"
            + ("" if result["act_ok"] else " — act_ok False (weak/unreliable; logged only)")
        )
        logger.info("[%s] day-tier GEX-ACTION (INERT): %s", symbol, result["reason"])
        return result
    except Exception as _e:  # a read-only signal must NEVER raise into a caller
        result["action"] = "STAND_DOWN"
        result["act_ok"] = False
        result["reason"] = f"unexpected error: {_e!r}"
        logger.warning("[%s] day-tier GEX-ACTION: unexpected error — STAND_DOWN: %s", symbol, _e)
        return result
