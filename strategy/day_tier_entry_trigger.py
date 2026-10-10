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

Data tier: T1 intraday bars via data.fetcher.fetch_bars_window(feed="iex") — the plan's real-time feed, completed
bars only (see fetch_bars_ref) — + the GEX walls from data.gex.get_gex_levels (cached snapshot).
All thresholds PROV-tagged.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta

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
# The whole 5-day IEX window (one session = 78 regular 5m bars): the regular-session volume baseline needs the PRIOR
# sessions' same-time-of-day bars at the open, which a 30-bar tail never contains (cold-2nd 2026-10-10). Same single
# fetch — fetch_bars_ref already reads _IEX_LOOKBACK_DAYS of bars and only trims to num_bars.
_TRIGGER_FRAME_BARS = 600
# FRESH CROSS (losers audit 2026-10-09; board Kyle+LdP, Gro, GAI aligned): a RIDE "close-through" must be a CROSS — the
# last close on the other side of the wall within this many completed regular-session bars (today, plus the prior
# session's last close so a gap through the wall counts only if the first bar CLOSES beyond it). The old level test
# rode 7 of 9 RIDE entries 6-75 bars after the break (SNXX 10/09: SNDK above its $1,600 wall since the prior close).
# PROV:daytier-ride-fresh-cross-2026-10-10 — a design-conformance value, NOT fitted to the 9 trades; bars_since_cross
# is logged on every RIDE decision so N can be derived from that distribution later.
_RIDE_FRESH_BARS = 3
_RTH_OPEN = (9, 30)
_RTH_CLOSE_HOUR = 16


def _recent_bars(symbol: str, bars):
    """The 5m bar frame to judge, from the injected `bars` (tests) or a live T1 fetch. None on failure."""
    if bars is not None:
        return bars
    try:
        return fetch_bars_ref(symbol, config.TF_5M, num_bars=_TRIGGER_FRAME_BARS)
    except Exception as _e:  # noqa: BLE001
        logger.debug("[%s] entry-trigger: 5m fetch failed: %s", symbol, _e)
        return None


# Indirection so the unit test can patch the fetch without importing alpaca (kept module-level for clarity).
# REAL-TIME + CLOSED BARS (2026-10-05 fix): the default bar feed (data.fetcher.fetch_bars) is consolidated SIP
# delayed ~15 minutes on this data plan, so the trigger was judging sweeps/breaks on 15-minute-old bars (live
# Track-A fills sat a mean 15bp, up to 112bp, from the signal price). Read the plan's REAL-TIME IEX feed instead
# and keep only COMPLETED 5m bars (data.fetcher.drop_forming_bars — signal code never reads a forming bar).
# IEX volume is IEX-only, so the RVOL confirmation compares IEX bars with IEX bars (same basis).
_IEX_LOOKBACK_DAYS = 5     # PROV:daytier-entry-trigger-iex — calendar days of IEX 5m history (>= 30 bars across a weekend/holiday)
_MAX_LAST_BAR_AGE = timedelta(minutes=10)  # PROV:daytier-entry-trigger-iex — newest closed 5m bar must have ended within this


def fetch_bars_ref(symbol, timeframe, num_bars):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from data.fetcher import drop_forming_bars, fetch_bars_window
    import pandas as pd
    now = datetime.now(ZoneInfo("America/New_York"))
    df = fetch_bars_window(symbol, timeframe, now - timedelta(days=_IEX_LOOKBACK_DAYS), now, feed="iex")
    closed = drop_forming_bars(df, timeframe, now).tail(num_bars)
    # FRESHNESS (cold-2nd + risk seat 2026-10-05): IEX prints a bar only when IEX trades, so on a thin name or at the
    # open the newest completed bar can be pre-market / prior-session. If it ENDED more than _MAX_LAST_BAR_AGE ago,
    # return nothing -> the trigger WAITs (never judge a wall or set entry_ref from a stale bar).
    if not closed.empty:
        last_end = closed.index[-1].to_pydatetime() + timedelta(minutes=5)
        if now - last_end > _MAX_LAST_BAR_AGE:
            return pd.DataFrame()
    return closed


def _et_times(df):
    """The frame's bar start times in ET, or None when the index carries no tz-aware timestamps (unit frames)."""
    try:
        idx = df.index
        if getattr(idx, "tz", None) is None:
            return None
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        return [t.astimezone(et) for t in idx]
    except Exception:  # noqa: BLE001
        return None


def _is_rth(t) -> bool:
    return (t.hour, t.minute) >= _RTH_OPEN and t.hour < _RTH_CLOSE_HOUR


def _vol_ok(df) -> bool:
    """Triggering (latest) bar volume >= _VOL_CONFIRM × a REGULAR-SESSION baseline (board/Gro/GAI 2026-10-10: the old
    29-bar average included thin premarket and prior-session bars, so an opening bar passed almost by default).
    Baseline = today's earlier regular-session bars (>= 3 of them); before that, the prior sessions' bars at the SAME
    time of day; neither -> False (WAIT). A frame without timestamps keeps the plain prior-bar average. Missing
    volume -> False (fail-safe: a break we can't volume-confirm is not a trigger)."""
    try:
        if "volume" not in df.columns or len(df) < 2:
            return False
        vols = [float(v) for v in df["volume"].tolist()]
        last = vols[-1]
        if not math.isfinite(last):
            return False
        times = _et_times(df)
        if times is None:
            prior = [v for v in vols[:-1] if math.isfinite(v)]
        else:
            t_last = times[-1]
            today = [vols[i] for i in range(len(vols) - 1)
                     if times[i].date() == t_last.date() and _is_rth(times[i])]
            if len(today) >= 3:
                prior = today
            else:
                prior = [vols[i] for i in range(len(vols) - 1)
                         if times[i].date() != t_last.date() and _is_rth(times[i])
                         and (times[i].hour, times[i].minute) == (t_last.hour, t_last.minute)]
            prior = [v for v in prior if math.isfinite(v)]
        if not prior:
            return False
        avg = sum(prior) / len(prior)
        return avg > 0 and last >= _VOL_CONFIRM * avg
    except Exception:  # noqa: BLE001
        return False


def _bars_since_cross(df, wall: float, direction: str) -> "int | None":
    """Completed bars from the last close on the OTHER side of `wall` to the latest bar (1 = the latest bar is the
    break). Regular-session bars of the latest bar's day, preceded by the prior session's last regular-session close
    (a gap through the wall counts only when the first bar closes beyond it). None = no other-side close in that
    window (the break is older than today's session). A frame without timestamps uses all its bars. Never raises."""
    try:
        closes = [float(c) for c in df["close"].tolist()]
        times = _et_times(df)
        if times is not None:
            day = times[-1].date()
            today = [i for i, t in enumerate(times) if t.date() == day and _is_rth(t)]
            before = [i for i, t in enumerate(times) if t.date() < day and _is_rth(t)]
            seq = ([before[-1]] if before else []) + today
            if not seq or seq[-1] != len(closes) - 1:
                return None                      # the latest bar is not a regular-session bar of its day
            closes = [closes[i] for i in seq]
        for k in range(len(closes) - 1, -1, -1):
            c = closes[k]
            if math.isfinite(c) and ((c <= wall) if direction == "long" else (c >= wall)):
                return len(closes) - 1 - k
        return None
    except Exception:  # noqa: BLE001
        return None


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
            _n = _bars_since_cross(df, float(result["wall_ref"]), str(result["direction"]))
            result["bars_since_cross"] = _n
            if _n is None or not (1 <= _n <= _RIDE_FRESH_BARS):
                result.update(direction="none", entry_ref=None, wall_ref=None)
                _nd = f"{_n} > {_RIDE_FRESH_BARS}" if _n is not None else "none since the prior session"
                result["reason"] = (f"RIDE: {_cand} is not a fresh cross (bars since the other-side close: {_nd}) — wait")
                logger.info("[%s] day-tier ENTRY-TRIGGER: %s", symbol, result["reason"])
                return result

        # PROFIT-SIDE TARGET (2026-10-06 replay: 10 of 25 Track-A setups on 10/05-06 died at place_entry because the
        # pin sat on the LOSS side of the entry, e.g. NVDA short 241.25 vs pin 241.74). A fade whose pin is already
        # behind price keeps the fade but drops the pin target: target None -> place_entry stops just beyond the
        # swept wall (the setup's invalidation) and takes profit at an R-multiple of that stop.
        if result["mode"] == "FADE" and result["target"] is not None:
            _t = _f(result["target"])
            if _t is None or (result["direction"] == "long" and _t <= close) or \
                    (result["direction"] == "short" and _t >= close):
                result["target"] = None
                result["pin_ref"] = _t
                _cand += f" (pin {_t} behind price -> wall stop + R-multiple target)"
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
