# ruff: noqa: E501
"""
strategy/day_tier_momentum_trigger.py — Day-Tier TRACK B momentum ENTRY TRIGGER (pure signal).

Track B is the day-tier's DYNAMIC-MOVER momentum path (design records
logs/design_records/day_tier_v2_design_2026-08-29.md §7/§7b/§7c and
logs/design_records/day_tier_track_b_momentum_2026-09-21.md, BGGN-aligned): capture a big intraday
directional mover via CONFIRMED CONTINUATION, never the raw gap. Raw gap-continuation is NEGATIVE
base-rate after spread+slippage+halt (§7c); conditioning on "the move that BROKE and HELD a structural
level on volume" flips it positive. This is Track B's counterpart to strategy/day_tier_entry_trigger.py
(Track A's GEX-wall trigger); it follows that module's fail-safe + PROV-threshold conventions.

THE TRACK-B MECHANIC (§7c, unanimous BGGN — hardened twice under the Inc-1 cold-2nd + adversarial gate
2026-09-21; every tolerance is now INSTRUMENT-SCALED, not a bare fraction of the opening range):
  Structural level = the OPENING-RANGE extreme (first _OR_BARS 5m bars); the OR must itself be a
  tradeable structure (range >= _MIN_OR_RANGE_PX_FRAC x price) or -> WAIT (a 4-cent OR on a $100 name
  is noise, not a level). VWAP is a HARD trend filter (never long below VWAP / short above VWAP), and if
  VWAP cannot be computed -> WAIT (fail-CLOSED).
  CURRENT HELD EPISODE: anchor on the CURRENT contiguous run of closes holding beyond the level (walk
  back from the latest bar), NOT the first-ever break — so an early failed break that faded does not
  poison a later clean break-and-hold (the shakeout-then-go pattern this tier exists to catch).
  LONG (a gap-UP that HOLDS) requires ALL of:
    * BUFFERED BREAK inside the episode: a close cleared the level by buf = max(_BREAK_BUFFER_OR_FRAC x
      OR range, _BREAK_BUFFER_PX_FLOOR x price) — a tick-scale clear on a tiny OR is NOT a structural break.
    * CONFIRMED HOLD (>= 1 bar after the episode start): a single current-bar spike is not a hold.
    * NO round-trip (the episode is a contiguous run of holding closes by construction) and NO deep wick
      (deepest episode low(long)/high(short) beyond the level within wick_tol = min(_WICK_TOL_OR_FRAC x OR
      range, _WICK_TOL_PX_CAP x price) — capped so a huge OR cannot admit a full stop-run as a "hold").
    * NOT OVER-EXTENDED: the latest close is within min(_MAX_EXTENSION_OR_MULT x OR range,
      _MAX_EXTENSION_PX_CAP x price) of the level — price-capped so a huge OR cannot leave the gate loose
      (a late entry far beyond the level feeds a huge implied stop to Inc-2 sizing — reject it).
    * VOLUME: the latest bar's OWN volume is finite AND >= _VOL_CONFIRM x the MEDIAN of the last
      _VOL_BASELINE_BARS RECENT post-OR bars (a rolling recent baseline — not the whole-session median,
      which drifts loose late in a move).
  SHORT (a gap-DOWN that FAILS) is the exact mirror. Shorts run SMALLER/TIGHTER (§7c-d) — the SIZING
  (Increment 2) enforces that; this trigger only flags the side.
  MODE (§7c-b): PULLBACK if a bar after the episode start RETESTED near the level (a real higher-low/
  lower-high, within near = min(_RETEST_NEAR_OR_FRAC x OR range, _RETEST_NEAR_PX_CAP x price)); DRIVE if
  a recent break with no retest yet; STALE if an old break that never retested (no higher-low structure —
  Inc 2 declines STALE rather than trusting a retest that does not exist).

PURE SIGNAL: emits {trigger, direction, mode, entry_ref, structural_level, vwap, ...}; sizes NOTHING,
places NO order. Increment 2 wires it into run_day_tier.py with the halt-survivable / cash-only
ruin-guard sizing (§7b.3 / §7b.6) — a RISK-PATH increment gated separately, and it also owns the frame
integrity (from-open verification, missing-bar / clock-time cutoff, halt awareness).

INPUT CONTRACT: `bars` is REQUIRED — today's RTH 5m session frame FROM THE 09:30 ET OPEN (bar 0 == the
open). The Increment-2 runner builds + verifies that frame. FAIL-SAFE: insufficient/bad bars, a too-tight
OR, wrong side of / missing VWAP, no confirmed break-and-hold, a deep wick, over-extension, past the
bar-count cutoff, or ANY error -> WAIT (never a spurious ENTER); never raises into the caller.

Data tier: T1 intraday 5m bars via data.fetcher (supplied by the runner). The bar-level volume
confirmation lives here; the session RVOL >= ~3-5x MOVER SCREEN is the Increment-2 universe gate.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# DERIVATION PLAN (PROV:daytier-momentum-trigger) — every threshold is a PROVISIONAL v1 starting value
# on a not-yet-wired signal. Track B's design (§7, §7c, LdP selection-bias guard) requires deriving them
# from the pooled "gap that BROKE-AND-HELD a level" setup outcomes once live, and NEVER tuning them on
# live P&L. Until that data exists these are documented starting values. Each tolerance is scaled to BOTH
# the OR range AND the instrument price so it means the same structural thing across the OR-range tails.
_OR_BARS = 3                    # PROV:daytier-momentum-trigger — opening range = first 3x 5m bars (15 min; matches config.ORB_WINDOW_MINUTES=15)
_DRIVE_MAX_BARS = 3             # PROV:daytier-momentum-trigger — episode start within this many bars of the latest (no retest) = DRIVE
_SESSION_CUTOFF_BARS = 18       # PROV:daytier-momentum-trigger — coarse guard: no trigger past ~90 min of 5m session bars (the runner enforces the precise clock-time cutoff in Inc 2)
_MIN_BARS = 5                   # PROV:daytier-momentum-trigger — need the OR (3) + a break bar + >= 1 confirming hold bar
_VOL_CONFIRM = 1.2              # PROV:daytier-momentum-trigger — latest bar volume >= this x the MEDIAN of the recent post-OR bars (bar-level RVOL; the session 3-5x RVOL screen is Inc 2)
_VOL_BASELINE_BARS = 5          # PROV:daytier-momentum-trigger — rolling recent-window size for the volume baseline (excludes the OR bars + the latest)
_MIN_OR_RANGE_PX_FRAC = 0.0015  # PROV:daytier-momentum-trigger — the OR range must be >= 0.15% of price to be a tradeable structure (else WAIT)
_BREAK_BUFFER_OR_FRAC = 0.10    # PROV:daytier-momentum-trigger — a break must clear the level by this frac of the OR range ...
_BREAK_BUFFER_PX_FLOOR = 0.0010 # PROV:daytier-momentum-trigger — ... OR at least this frac of price (floor so a tiny OR cannot collapse the buffer below a tick)
_WICK_TOL_OR_FRAC = 0.50        # PROV:daytier-momentum-trigger — a post-break wick beyond the level up to this frac of the OR range is a hold ...
_WICK_TOL_PX_CAP = 0.0050       # PROV:daytier-momentum-trigger — ... capped at this frac of price (a huge OR cannot admit a full stop-run as a hold)
_RETEST_NEAR_OR_FRAC = 0.25     # PROV:daytier-momentum-trigger — a post-break low(long)/high(short) within this frac of the OR range of the level = a retest ...
_RETEST_NEAR_PX_CAP = 0.0025    # PROV:daytier-momentum-trigger — ... capped at this frac of price (so a huge OR does not label everything a retest)
_MAX_EXTENSION_OR_MULT = 2.0    # PROV:daytier-momentum-trigger — reject if the latest close is more than this x OR range beyond the level ...
_MAX_EXTENSION_PX_CAP = 0.05    # PROV:daytier-momentum-trigger — ... capped at this frac of price (so a HUGE OR cannot leave the extension gate loose — a 20%+ chase feeds a huge implied stop to Inc-2 sizing)


def _f(x):
    """float(x) or None — NaN / +-inf -> None. Never raises."""
    try:
        v = float(x)
        return v if (v == v and v not in (float("inf"), float("-inf"))) else None
    except (TypeError, ValueError):
        return None


def _vol_ok(df) -> bool:
    """The latest (trigger) bar's OWN volume is finite AND >= _VOL_CONFIRM x the MEDIAN of the last
    _VOL_BASELINE_BARS RECENT post-OR bars (excluding the huge opening-range bars and the latest bar).
    A recent rolling baseline (not the whole-session median) so the gate does not drift loose late in a
    move. A NaN/absent latest volume -> False (fail-closed: a break we cannot volume-confirm is not a
    trigger). Never raises."""
    try:
        if "volume" not in df.columns or len(df) < 2:
            return False
        latest = _f(df["volume"].iloc[-1])
        if latest is None:
            return False  # T1: the latest bar's own volume must be known
        prior = df["volume"].iloc[_OR_BARS:-1].dropna()   # recent post-OR bars, excluding the latest
        if len(prior) < 1:
            prior = df["volume"].iloc[:-1].dropna()        # fall back to all prior if too few post-OR bars
        if len(prior) < 1:
            return False
        baseline = float(prior.tail(_VOL_BASELINE_BARS).median())
        return baseline > 0 and latest >= _VOL_CONFIRM * baseline
    except Exception:  # noqa: BLE001
        return False


def _vwap_last(df) -> "float | None":
    """Session VWAP at the latest bar (indicators.vwap.add_vwap resets on the frame's calendar date; the
    Inc-2 runner supplies an RTH-from-open ET frame so the reset is the 09:30 ET open). None on failure."""
    try:
        from indicators.vwap import add_vwap
        v = add_vwap(df)
        return _f(v["vwap"].iloc[-1])
    except Exception as _e:  # noqa: BLE001
        logger.debug("momentum-trigger: vwap compute failed: %s", _e)
        return None


def compute_momentum_trigger(symbol: str, gap_direction: "str | None", bars) -> dict:
    """Track-B confirmed-continuation entry trigger (design §7c). READ-ONLY pure signal — orders nothing.

    Args:
      symbol        : the mover under evaluation.
      gap_direction : "up" / "down" from the pre-market mover screen (Increment 2); RESTRICTS the side
                      (never counter-trade the gap). None (tests / unscreened) = evaluate both sides.
      bars          : REQUIRED — today's RTH 5m session frame FROM THE 09:30 ET OPEN (bar 0 == the open;
                      the runner builds/verifies it — see the INPUT CONTRACT in the module docstring).

    Returns {"symbol","trigger","direction","mode","entry_ref","structural_level","vwap","vol_confirmed",
    "reason"}. ENTER only on a volume-confirmed, non-over-extended break-AND-HOLD of a tradeable
    opening-range level on the gap side, latest close on the correct side of VWAP. Never raises -> WAIT.
    """
    result: dict = {
        "symbol": symbol, "trigger": "WAIT", "direction": "none", "mode": "none",
        "entry_ref": None, "structural_level": None, "vwap": None,
        "vol_confirmed": False, "reason": "",
    }
    try:
        cols = getattr(bars, "columns", [])
        if (bars is None or getattr(bars, "empty", True) or len(bars) < _MIN_BARS
                or not all(c in cols for c in ("high", "low", "close"))):
            result["reason"] = "insufficient bars — wait"
            return result
        df = bars
        if len(df) > _SESSION_CUTOFF_BARS:
            result["reason"] = f"past bar-count cutoff ({len(df)} > {_SESSION_CUTOFF_BARS} session bars) — wait"
            return result

        close = _f(df["close"].iloc[-1])
        if close is None or close <= 0:
            result["reason"] = "no usable close — wait"
            return result
        vwap = _vwap_last(df)
        result["vwap"] = vwap
        if vwap is None:  # VWAP is a HARD filter — unverifiable -> fail-CLOSED (cold-2nd 2026-09-21)
            result["reason"] = "VWAP unavailable — hard filter cannot be verified — wait"
            return result
        vol_ok = _vol_ok(df)
        result["vol_confirmed"] = vol_ok

        or_win = df.iloc[:_OR_BARS]
        post = df.iloc[_OR_BARS:]
        or_high = _f(or_win["high"].max())
        or_low = _f(or_win["low"].min())
        if or_high is None or or_low is None or len(post) < 1:
            result["reason"] = "opening range not formed — wait"
            return result
        or_range = or_high - or_low
        if or_range < _MIN_OR_RANGE_PX_FRAC * close:  # not a tradeable structure (also rejects or_range<=0)
            result["reason"] = "opening range too tight to be a tradeable structure — wait"
            return result

        # Instrument-scaled tolerances (adversarial 2026-09-21): floor the break buffer, cap the
        # wick/retest tolerances, so each means the same structural thing across the OR-range tails.
        buf = max(_BREAK_BUFFER_OR_FRAC * or_range, _BREAK_BUFFER_PX_FLOOR * close)
        wick_tol = min(_WICK_TOL_OR_FRAC * or_range, _WICK_TOL_PX_CAP * close)
        near = min(_RETEST_NEAR_OR_FRAC * or_range, _RETEST_NEAR_PX_CAP * close)
        max_ext = min(_MAX_EXTENSION_OR_MULT * or_range, _MAX_EXTENSION_PX_CAP * close)

        pc = [_f(post["close"].iloc[i]) for i in range(len(post))]
        pl = [_f(post["low"].iloc[i]) for i in range(len(post))]
        ph = [_f(post["high"].iloc[i]) for i in range(len(post))]
        n = len(post)
        gd = str(gap_direction).strip().lower() if gap_direction is not None else None

        def _eval(direction: str) -> "dict | None":
            is_long = direction == "long"
            level = or_high if is_long else or_low
            # VWAP hard filter (vwap guaranteed non-None here).
            if is_long and close <= vwap:
                return None
            if not is_long and close >= vwap:
                return None
            # Latest bar must be holding beyond the level, else there is no current continuation.
            if (close <= level) if is_long else (close >= level):
                return None
            # CURRENT HELD EPISODE: walk back from the latest while the close holds beyond the level.
            ep_start = n
            for j in range(n - 1, -1, -1):
                if pc[j] is not None and ((pc[j] > level) if is_long else (pc[j] < level)):
                    ep_start = j
                else:
                    break
            if ep_start >= n - 1:
                return None  # confirmed hold needs >= 1 bar after the episode start (no lone spike)
            # A BUFFERED break must have occurred WITHIN the current held episode AND STRICTLY BEFORE the
            # latest bar (range stops at n-1), so the latest bar CONFIRMS the hold. A buffered break that
            # lands only on the latest bar — even if a prior bar held the raw level weakly (sub-buffer) —
            # is an unconfirmed current-bar spike, not a held break (cold-2nd + adversarial 2026-09-21).
            broke = any(pc[j] is not None and ((pc[j] > level + buf) if is_long else (pc[j] < level - buf))
                        for j in range(ep_start, n - 1))
            if not broke:
                return None  # holding, but no buffered break confirmed by a subsequent bar -> not a structural break
            if not vol_ok:
                return None
            # NO deep wick AFTER the break (a flush/stop-run on a HELD bar is not a hold). The break
            # bar itself (ep_start) is excluded — its low is naturally below the level (it broke up
            # through it); the flush concern is a subsequent held bar (incl. the entry bar) piercing far.
            if is_long:
                lows = [pl[j] for j in range(ep_start + 1, n) if pl[j] is not None]
                if lows and min(lows) < level - wick_tol:
                    return None
            else:
                highs = [ph[j] for j in range(ep_start + 1, n) if ph[j] is not None]
                if highs and max(highs) > level + wick_tol:
                    return None
            # NOT over-extended (a late entry far beyond the level feeds a huge implied stop to Inc 2).
            if abs(close - level) > max_ext:
                return None
            # MODE (§7c): a real retest after the episode start = PULLBACK; a recent break with no
            # retest yet = DRIVE. An OLD break that never retested is neither a §7c DRIVE (past the
            # recent-drive window) nor a PULLBACK (no higher-low structure) -> WAIT (do not emit an
            # ambiguous entry the Inc-2 sizer would have to special-case).
            if is_long:
                retested = any(pl[j] is not None and pl[j] <= level + near for j in range(ep_start + 1, n))
            else:
                retested = any(ph[j] is not None and ph[j] >= level - near for j in range(ep_start + 1, n))
            recent = (n - 1) - ep_start <= _DRIVE_MAX_BARS
            if retested:
                mode = "PULLBACK"
            elif recent:
                mode = "DRIVE"
            else:
                return None  # old break, no retest — not a §7c confirmed-continuation setup -> WAIT
            return {"direction": direction, "mode": mode, "level": level}

        sides = (["long"] if gd == "up" else ["short"] if gd == "down" else ["long", "short"])
        hit = next((h for h in (_eval(s) for s in sides) if h is not None), None)
        if hit is None:
            result["reason"] = "no volume-confirmed break-and-hold of the opening range on the gap side — wait"
            return result

        result["trigger"] = "ENTER"
        result["direction"] = hit["direction"]
        result["mode"] = hit["mode"]
        result["entry_ref"] = round(close, 4)  # PROV:feat-units-4dp
        result["structural_level"] = round(hit["level"], 4)  # PROV:feat-units-4dp
        result["reason"] = (
            f"{hit['mode']} {hit['direction']}: broke+held OR level {result['structural_level']} "
            f"(buffer {round(buf, 4)}) @ {result['entry_ref']} (vwap {round(vwap, 4)}, vol-confirmed) — "
            f"confirmed continuation, no order placed (Inc 1 pure signal)"
        )
        logger.info("[%s] day-tier MOMENTUM-TRIGGER: %s", symbol, result["reason"])
        return result
    except Exception as _e:  # a pure signal must NEVER raise into a caller
        result["trigger"] = "WAIT"
        result["reason"] = f"unexpected error: {_e!r}"
        logger.warning("[%s] day-tier MOMENTUM-TRIGGER: unexpected error — WAIT: %s", symbol, _e)
        return result
