# ruff: noqa: E501
"""
strategy/day_tier_entry_trigger.py — Day-Tier Track-A ENTRY TRIGGER (READ-ONLY, INERT).

Fifth increment of the day-tier engine rebuild. Turns a fade/ride CANDIDATE (from the meta-label
composition, strategy.day_tier_decision.compute_day_tier_decision) into an actual "ENTER now, this
direction" signal by reading LIVE PRICE ACTION against the GEX walls — the design's North Star
(§7b.5 "price action decides"; §2 Track-A GEX-core mechanic; §7c entry principles).

THE TRACK-A MECHANIC (§2):
  FADE (+gamma / POSITIVE regime — dealers dampen): a FAILED wall SWEEP mean-reverts to the pin.
    * price pokes ABOVE call_wall then closes back BELOW it  -> failed upside sweep -> FADE SHORT to the pin.
    * price pokes BELOW put_wall then closes back ABOVE it   -> failed downside sweep -> FADE LONG to the pin.
  RIDE (-gamma / NEGATIVE regime — dealers amplify): a wall CLOSE-THROUGH runs.
    * latest close ABOVE call_wall (closed through)          -> RIDE LONG  with the break.
    * latest close BELOW put_wall (closed through)           -> RIDE SHORT with the break.
  Otherwise -> WAIT (the setup has not triggered yet).

A trigger additionally requires a VOLUME confirmation (the triggering bar's volume vs the recent
average) — a wall sweep/break on thin tape is noise (§7c/§1.5 RVOL). The RETEST tell (a higher-low /
lower-high that HOLDS the broken level, §7c) is a Phase-B refinement noted for the next increment;
v1 detects the sweep-fail / close-through core.

READ-ONLY + INERT: wired to NOTHING (no live caller). Emits a signal only — sizes NOTHING, places
NO order (sizing + the order are Layer C's later, RISK-PATH increments). Committed INERT (lesson).
FAIL-SAFE: a candidate that is not would_consider, or missing/insufficient bars, or missing walls,
or any error -> WAIT (never a spurious ENTER); never raises.

Data tier: T1 intraday bars via data.fetcher.fetch_bars (the only approved bar source) + the GEX
walls from data.gex.get_gex_levels (cached snapshot). All thresholds PROV-tagged (inert signal).
"""
from __future__ import annotations

import logging

import config

logger = logging.getLogger(__name__)

# DERIVATION PLAN (PROV:daytier-entry-trigger) — the sweep/break margins + volume floor + lookback
# are PROVISIONAL on an INERT signal (no live trade). Once this shadow-logs enough triggers WITH
# realized outcomes, derive: the sweep/break margin from the false-trigger-vs-fill-quality curve;
# the volume floor from the RVOL-vs-continuation curve (§7c "on volume"); the sweep lookback from
# the sweep-duration distribution. Until then these are starting values.
_SWEEP_MARGIN = 0.0015     # PROV:daytier-entry-trigger — a poke > this frac past a wall counts as a sweep/break (~0.15%)
_VOL_CONFIRM = 1.2         # PROV:daytier-entry-trigger — triggering-bar volume >= this × recent avg (RVOL floor)
_SWEEP_LOOKBACK = 3        # PROV:daytier-entry-trigger — bars back to look for the sweep extreme (failed-sweep window)
_MIN_BARS = 6              # PROV:daytier-entry-trigger — need >= this many 5m bars to judge tape + a recent-vol avg
_INTRADAY_BARS = 30        # how many 5m bars to fetch (covers the session's recent action)


def _recent_bars(symbol: str, bars):
    """The 5m bar frame to judge, from the injected `bars` (tests) or a live T1 fetch. None on failure."""
    if bars is not None:
        return bars
    try:
        return fetch_bars_ref(symbol, config.TF_5M, num_bars=_INTRADAY_BARS)
    except Exception as _e:  # noqa: BLE001
        logger.debug("[%s] entry-trigger: 5m fetch failed: %s", symbol, _e)
        return None


# Indirection so the unit test can patch the fetch without importing alpaca (kept module-level for clarity).
def fetch_bars_ref(symbol, timeframe, num_bars):
    from data.fetcher import fetch_bars
    return fetch_bars(symbol, timeframe, num_bars=num_bars)


def _vol_ok(df) -> bool:
    """Triggering (latest) bar volume >= _VOL_CONFIRM × the average of the prior bars. Missing
    volume -> False (fail-safe: a break we can't volume-confirm is not a trigger)."""
    try:
        if "volume" not in df.columns or len(df) < 2:
            return False
        vols = df["volume"].dropna()
        if len(vols) < 2:
            return False
        last = float(vols.iloc[-1])
        avg = float(vols.iloc[:-1].mean())
        return avg > 0 and last >= _VOL_CONFIRM * avg
    except Exception:  # noqa: BLE001
        return False


def _f(x):
    try:
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else None  # NaN/inf -> None
    except (TypeError, ValueError):
        return None


def compute_entry_trigger(symbol: str, decision: dict, bars=None, levels: "dict | None" = None) -> dict:
    """Track-A entry trigger from live price action vs the GEX walls (design §2). READ-ONLY + INERT.

    Args:
      symbol   : the underlying.
      decision : a strategy.day_tier_decision.compute_day_tier_decision result (mode + would_consider).
      bars     : optional injected 5m bar frame (tests); default = live T1 fetch.
      levels   : optional injected get_gex_levels result (tests); default = live read.

    Returns:
      {"symbol", "trigger": "ENTER"|"WAIT", "direction": "long"|"short"|"none", "mode",
       "entry_ref", "target", "wall_ref", "vol_confirmed": bool, "reason"}
    trigger is ENTER only on a volume-confirmed failed-sweep (fade) or close-through (ride) for a
    would_consider candidate. Never raises.
    """
    result: dict = {
        "symbol": symbol, "trigger": "WAIT", "direction": "none",
        "mode": (decision or {}).get("gex_action", "STAND_DOWN") if isinstance(decision, dict) else "STAND_DOWN",
        "entry_ref": None, "target": None, "wall_ref": None, "vol_confirmed": False, "reason": "",
    }
    try:
        if not isinstance(decision, dict) or not decision.get("would_consider"):
            result["reason"] = "not a would_consider candidate — wait"
            return result
        mode = decision.get("gex_action")
        if mode not in ("FADE", "RIDE"):
            result["reason"] = f"mode {mode} not tradeable — wait"
            return result

        if levels is None:
            from data.gex import get_gex_levels
            levels = get_gex_levels(symbol)
        if not isinstance(levels, dict) or not levels.get("levels_ok"):
            result["reason"] = "no actionable GEX levels — wait"
            return result
        call_wall = _f(levels.get("call_wall"))
        put_wall = _f(levels.get("put_wall"))
        centroid = _f(levels.get("centroid"))

        df = _recent_bars(symbol, bars)
        if df is None or getattr(df, "empty", True) or len(df) < _MIN_BARS or "close" not in getattr(df, "columns", []):
            result["reason"] = "insufficient bars — wait"
            return result
        close = _f(df["close"].iloc[-1])
        if close is None:
            result["reason"] = "no usable close — wait"
            return result
        window = df.tail(_SWEEP_LOOKBACK)
        hi = _f(window["high"].max()) if "high" in df.columns else None
        lo = _f(window["low"].min()) if "low" in df.columns else None
        vol_ok = _vol_ok(df)
        result["vol_confirmed"] = vol_ok

        if mode == "FADE":
            # Failed sweep of a wall → fade back toward the pin/centroid.
            if call_wall is not None and hi is not None and hi > call_wall * (1.0 + _SWEEP_MARGIN) and close < call_wall:
                result.update(direction="short", wall_ref=call_wall, target=centroid,
                              entry_ref=close, mode="FADE")
                _cand = "failed UPSIDE sweep of call_wall"
            elif put_wall is not None and lo is not None and lo < put_wall * (1.0 - _SWEEP_MARGIN) and close > put_wall:
                result.update(direction="long", wall_ref=put_wall, target=centroid,
                              entry_ref=close, mode="FADE")
                _cand = "failed DOWNSIDE sweep of put_wall"
            else:
                result["reason"] = "FADE: no failed wall-sweep yet — wait"
                return result
        else:  # RIDE
            # Close-through of a wall → ride with the break.
            if call_wall is not None and close > call_wall * (1.0 + _SWEEP_MARGIN):
                result.update(direction="long", wall_ref=call_wall, target=None,
                              entry_ref=close, mode="RIDE")
                _cand = "close-through ABOVE call_wall"
            elif put_wall is not None and close < put_wall * (1.0 - _SWEEP_MARGIN):
                result.update(direction="short", wall_ref=put_wall, target=None,
                              entry_ref=close, mode="RIDE")
                _cand = "close-through BELOW put_wall"
            else:
                result["reason"] = "RIDE: no wall close-through yet — wait"
                return result

        # A candidate pattern is present — require the volume confirmation to ENTER.
        if not vol_ok:
            result["reason"] = f"{_cand} but volume not confirmed (RVOL < {_VOL_CONFIRM}) — wait"
            return result
        result["trigger"] = "ENTER"
        result["reason"] = f"{_cand} @ {close} (wall {result['wall_ref']}, vol-confirmed) -> {result['direction']} — INERT, no order placed"
        logger.info("[%s] day-tier ENTRY-TRIGGER (INERT): %s", symbol, result["reason"])
        return result
    except Exception as _e:  # a read-only signal must NEVER raise into a caller
        result["trigger"] = "WAIT"
        result["reason"] = f"unexpected error: {_e!r}"
        logger.warning("[%s] day-tier ENTRY-TRIGGER: unexpected error — WAIT: %s", symbol, _e)
        return result
