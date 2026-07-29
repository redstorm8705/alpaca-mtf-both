# ruff: noqa: E501  — prose docstring/explanatory comments run long (matches repo convention, e.g. options_scanner.py); logic lines are ≤88
"""
zdte_direction.py — SINGLE SOURCE OF TRUTH for the 0DTE SPY directional decision.

Position C (BGG-aligned + Rafael APPROVED 2026-07-29). Full rationale + citations:
logs/zdte_position_c_design_2026-07-29.md.

Why: scan_to_html.py and options_scanner.py previously each computed their own 0DTE SPY
direction and BOTH read/wrote logs/dte_prev.json with incompatible schemas — a dual-writer
race that produced a Frankenstein rec (label "call" rendered with a -0.380 PUT delta). This
one PURE function decides direction so both pages consume the identical answer; the caller
selects the strike/delta FOR that one direction, so delta can never disagree with the label.

Policy — gate 0DTE on the INTRADAY tape, not the multi-day swing score (that is a timeframe
category error). PRIMARY = intraday momentum (price vs 1-min VWAP) + dealer-gamma (GEX)
tradeability. CONFIRM = cross-asset regime. VETO/context = multi-day MTF lean (a STRONG
opposing lean vetoes; it never greenlights on its own). Greenlight only when momentum has a
direction AND gamma is not a positive-gamma pin AND regime does not oppose AND MTF does not
strongly oppose AND we are inside the tradeable time window; otherwise NO_REC ("skip") — never
a forced coin-flip. 0DTE is LONG-only (buy call/put; max loss = premium paid).

PURITY: no I/O, no fetching — callers pass pre-computed inputs. Never raises (fail-safe -> NO_REC).
"""

from __future__ import annotations

# Tradeable window (ET). No fresh 0DTE before the open settles or into terminal decay.
_ZDTE_OPEN_CUTOFF_MIN = 10 * 60   # 10:00 ET — skip the choppy first 30 min
_ZDTE_LATE_CUTOFF_MIN = 14 * 60   # 14:00 ET — no NEW long 0DTE into terminal theta

_LONG_LEANS = {"bull", "strong_bull"}
_SHORT_LEANS = {"bear", "strong_bear"}
_STRONG_LEANS = {"strong_bull", "strong_bear"}


def _dir_of_lean(lean: str) -> str | None:
    """MTF/regime lean string -> CALL/PUT/None."""
    ln = (lean or "").lower().strip()
    if ln in _LONG_LEANS:
        return "CALL"
    if ln in _SHORT_LEANS:
        return "PUT"
    return None


def _dir_of_regime(regime: str) -> str | None:
    """Cross-asset composite regime -> CALL/PUT/None."""
    rg = (regime or "").upper().strip()
    if rg == "BULL":
        return "CALL"
    if rg == "BEAR":
        return "PUT"
    return None


def _dir_of_vwap(vwap_state: str) -> str | None:
    """Intraday price vs 1-min VWAP -> CALL/PUT/None ('above'/'below'/'neutral')."""
    vs = (vwap_state or "").lower().strip()
    if vs == "above":
        return "CALL"
    if vs == "below":
        return "PUT"
    return None


def decide_spy_0dte(
    *,
    mtf_lean: str = "unknown",
    mtf_score: int = 0,
    vwap_state: str = "neutral",
    gex_regime: str = "UNKNOWN",
    cross_regime: str = "NEUTRAL",
    now_et_minutes: int | None = None,
    prev_direction: str | None = None,
) -> dict:
    """Decide the single canonical 0DTE SPY direction (Position C). PURE + fail-safe.

    Inputs (pre-computed by the caller):
      mtf_lean       : bull/strong_bull/bear/strong_bear/neutral/unknown (VETO/context only)
      mtf_score      : multi-day confluence x/12 (context only — never gates)
      vwap_state     : 'above'/'below'/'neutral' (SPY vs 1-min VWAP, >0.10% separation)
      gex_regime     : NEGATIVE/POSITIVE/UNKNOWN/NEAR_FLIP (dealer gamma; fail-safe UNKNOWN)
      cross_regime   : BULL/BEAR/NEUTRAL
      now_et_minutes : minutes since ET midnight (hour*60+min); None skips the time gate
      prev_direction : CALL/PUT/None — prior canonical direction (stickiness anti-whipsaw)

    Returns: status ('REC'|'NO_REC'), direction ('CALL'|'PUT'|None), conviction_n_of_3 (0..3),
      components {momentum, gamma, regime}, mtf_veto (bool), reasons (list[str]).
    """
    reasons: list[str] = []
    gex = (gex_regime or "UNKNOWN").upper().strip()

    def _no_rec(msg: str) -> dict:
        reasons.append(msg)
        return {
            "status": "NO_REC", "direction": None, "conviction_n_of_3": 0,
            "components": {"momentum": None, "gamma": gex, "regime": None},
            "mtf_veto": False, "reasons": reasons,
        }

    # Time-of-day gate (theta discipline)
    if now_et_minutes is not None:
        if now_et_minutes < _ZDTE_OPEN_CUTOFF_MIN:
            return _no_rec("before 10:00 ET — opening chop, no 0DTE read")
        if now_et_minutes >= _ZDTE_LATE_CUTOFF_MIN:
            return _no_rec("after 14:00 ET — terminal theta, no fresh 0DTE")

    # PRIMARY 1: intraday momentum (VWAP). Direction comes from momentum; if flat, fall to
    # cross-asset regime; if both are flat there is no clean intraday read.
    mom_dir = _dir_of_vwap(vwap_state)
    regime_dir = _dir_of_regime(cross_regime)
    direction = mom_dir or regime_dir
    if direction is None:
        return _no_rec("no intraday momentum (VWAP flat) and neutral regime — skip")

    # PRIMARY 2: dealer-gamma tradeability. POSITIVE gamma pins the tape -> a directional
    # long bleeds to theta -> skip. NEGATIVE trends -> convexity favors the long. UNKNOWN/
    # NEAR_FLIP -> neutral (fail-safe: allowed, but not counted toward confluence).
    if gex == "POSITIVE":
        return _no_rec("POSITIVE dealer gamma (pin regime) — directional 0DTE long disfavored")

    # A regime-only direction (flat momentum) is not a clean intraday read.
    if mom_dir is None:
        return _no_rec("momentum flat; regime-only lean is insufficient for a 0DTE long")

    # VETO: a STRONG opposing multi-day lean blocks (MTF is veto/context only).
    mtf_dir = _dir_of_lean(mtf_lean)
    if mtf_dir is not None and mtf_dir != direction and \
            (mtf_lean or "").lower().strip() in _STRONG_LEANS:
        reasons.append(
            f"strong opposing multi-day lean ({mtf_lean}) vetoes {direction} 0DTE"
        )
        return {
            "status": "NO_REC", "direction": None, "conviction_n_of_3": 0,
            "components": {"momentum": True, "gamma": gex, "regime": regime_dir == direction},
            "mtf_veto": True, "reasons": reasons,
        }

    # Confluence N/3 = momentum + gamma-tradeable(NEGATIVE) + cross-regime supporting direction.
    mom_ok = (mom_dir == direction)
    gamma_ok = (gex == "NEGATIVE")
    regime_ok = (regime_dir == direction)
    n = int(mom_ok) + int(gamma_ok) + int(regime_ok)

    # Stickiness anti-whipsaw: a flip vs the prior direction needs full 3/3 confluence.
    if prev_direction and prev_direction != direction and n < 3:
        reasons.append(
            f"flip vs prior {prev_direction} needs 3/3 (have {n}/3) — hold, no whipsaw"
        )
        return {
            "status": "NO_REC", "direction": None, "conviction_n_of_3": n,
            "components": {"momentum": mom_ok, "gamma": gex, "regime": regime_ok},
            "mtf_veto": False, "reasons": reasons,
        }

    reasons.append(
        f"{direction}: VWAP {vwap_state}, gamma {gex}, regime {cross_regime}, MTF {mtf_score}/12"
    )
    return {
        "status": "REC", "direction": direction, "conviction_n_of_3": n,
        "components": {"momentum": mom_ok, "gamma": gex, "regime": regime_ok},
        "mtf_veto": False, "reasons": reasons,
    }
