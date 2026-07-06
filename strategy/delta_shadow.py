"""
strategy/delta_shadow.py — SHADOW-ONLY delta-of-signal logger.

Cedar principle ("trade the delta, not the level"): the CHANGE in the confluence
score can carry more information than the level — a name that just jumped 6->11
("off the fence") vs one steady at 11. This module measures that change in
SHADOW mode ONLY.

Mirrors the proven VOLUME_CONFIRMATION shadow pattern (config-gated, log-only
until proven). While config.DELTA_SCORING_ENABLED is False (default), record()
logs the bar-over-bar change in the FEATURE-ISOLATED (non-momentum_12_1)
confluence score to logs/trade_events.jsonl and returns 0 — ZERO impact on
scoring, sizing, or entries. Nothing here can raise into the scan/entry path.

Board + Gro + GAI consensus 2026-07-06:
  - FEATURE-ISOLATION: exclude the momentum_12_1 factor (López de Prado — don't
    double-count momentum already scored elsewhere).
  - >=2-bar PERSISTENCE before a jump "counts" (Taleb — filter 5-min noise).
  - >=50 shadow samples + an independent-edge check before any LIVE weight, and
    that flip requires a FRESH board vote (Architecture Invariant #1: SPY 5-min
    bar-over-bar stays the sole entry gate; a proven delta only adjusts the
    quality bar).

State is in-memory (per process): the durable record is the trade_events.jsonl
log, not this cache. A restart simply resets the prev-score baseline (the first
post-restart scan of a symbol produces no delta), which is acceptable for a
shadow signal and avoids per-symbol file I/O in the hot scan loop.
"""
from __future__ import annotations

import logging
import threading

import config

logger = logging.getLogger(__name__)

# key "SYMBOL|direction" -> {"nm": non_momentum_score, "persist": consec_bars}
_state: dict = {}
_lock = threading.Lock()


def _non_momentum_score(score: int, conditions: dict) -> int:
    """Confluence score with the momentum_12_1 factor removed (feature-isolation)."""
    try:
        w = int(config.SCORE_WEIGHTS.get("momentum_12_1", 0))
        if conditions.get("momentum_12_1"):
            return int(score) - w
        return int(score)
    except Exception:
        return int(score)


def record(symbol: str, direction: str, score: int, conditions: dict) -> int:
    """
    SHADOW: log the bar-over-bar non-momentum score delta for (symbol, direction).

    Returns the LIVE scoring bonus — always 0 while config.DELTA_SCORING_ENABLED
    is False (shadow mode). NEVER raises: any error is swallowed so the scan /
    entry path can never be broken by this observational hook.
    """
    try:
        nm = _non_momentum_score(score, conditions or {})
        thr = int(getattr(config, "DELTA_OFF_FENCE_THRESHOLD", 3))
        need = int(getattr(config, "DELTA_PERSISTENCE_BARS", 2))
        key = f"{symbol}|{direction}"
        mom_now = bool((conditions or {}).get("momentum_12_1"))

        with _lock:
            prev = _state.get(key)
            prev_nm = prev["nm"] if prev else None
            prev_mom = prev.get("mom") if prev else None
            baseline = prev.get("baseline") if prev else None  # level before the jump
            held = int(prev.get("held", 0)) if prev else 0
            delta = (nm - prev_nm) if prev_nm is not None else 0

            if delta >= thr:
                # NEW off-the-fence jump: baseline = the pre-jump level, hold = 1
                baseline = prev_nm
                held = 1
                off_fence_now = True
            elif baseline is not None and nm >= baseline + thr:
                # elevation HELD (still >= baseline + threshold) — persistence grows
                held += 1
                off_fence_now = True
            else:
                # reverted below the elevated level (or no jump) — reset
                baseline = None
                held = 0
                off_fence_now = False

            persist = held
            _state[key] = {"nm": nm, "baseline": baseline, "held": held, "mom": mom_now}

        confirmed = persist >= need
        # Cold-agent flag: when the momentum_12_1 factor toggles between bars, the
        # non-momentum baseline shifts, so THIS bar's delta is not comparable to
        # the prior bar. Flag it so post-shadow analysis can filter these rows.
        momentum_flipped = prev_mom is not None and prev_mom != mom_now

        # Log on a real change (delta != 0) OR while an off-the-fence elevation
        # is holding (captures persistence duration) — keeps trade_events.jsonl
        # from flooding with steady-state no-op rows. First scan of a symbol
        # (prev_nm is None) has no delta and is skipped.
        if prev_nm is not None and (delta != 0 or off_fence_now):
            try:
                from trade_logger import log_event
                log_event(
                    "delta_shadow",
                    symbol=symbol,
                    score=int(score),
                    direction=direction,
                    non_momentum_score=nm,
                    prev_non_momentum_score=prev_nm,
                    score_delta=delta,
                    off_the_fence=bool(off_fence_now),
                    persistence_bars=persist,
                    persistence_confirmed=bool(confirmed),
                    momentum_flipped=bool(momentum_flipped),
                    shadow=True,
                )
            except Exception as e:
                logger.debug("delta_shadow log_event failed: %s", e)

        # LIVE weight path — only when explicitly enabled (future; post board vote).
        # Unreachable while DELTA_SCORING_ENABLED is False, by design.
        if getattr(config, "DELTA_SCORING_ENABLED", False) and confirmed:
            return 1
        return 0
    except Exception as e:  # shadow must never break the scan/entry path
        logger.debug("delta_shadow.record failed (ignored, shadow-safe): %s", e)
        return 0
