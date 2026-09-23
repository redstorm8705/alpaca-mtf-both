# ruff: noqa: E501
"""
strategy/day_tier_track_b.py — Day-Tier TRACK B universe + wiring adapters (PURE helpers; wired LIVE by run_day_tier.py — Inc 2 Part 2).

Track B is the day-tier's DYNAMIC-MOVER momentum path (design records
logs/design_records/day_tier_v2_design_2026-08-29.md §7/§7b/§7c,
logs/design_records/day_tier_track_b_momentum_2026-09-21.md, and the Increment-2 build record
logs/design_records/day_tier_track_b_inc2_2026-09-22.md). Increment 1 shipped the pure momentum TRIGGER
(strategy/day_tier_momentum_trigger.py). This module is the Increment-2 PART 1: the three pure, fail-safe,
unit-tested helpers the live runner (Increment-2 Part 2, run_day_tier.py) needs to feed that trigger and
route its output — WITHOUT any order primitive of its own. The helpers order nothing and mutate no state;
the live runner (run_day_tier.py, Inc 2 Part 2, behind config.DAYTRADE_TRACK_B_ENABLED) routes an ENTER to
execution.day_trade_manager.place_entry, which owns every order + risk guard.

THE THREE HELPERS:
  1. screen_mover()        — the PRE-REGISTERED mover SCREEN (§7.63). On a fixed liquid candidate list
                             (the pre-registered list IS the float / micro-cap / liquidity guard — no
                             market-wide gainers feed, no float call), decide whether a name is a REAL
                             intraday mover TODAY and on which side: |gap%| >= _MIN_GAP_PCT (gap from
                             prior close -> gap_direction), session RVOL >= _MIN_RVOL, price >= _MIN_PRICE.
                             A > _MAX_GAP_PCT move is treated as bad data / a corporate action (split) and
                             REJECTED (the NFLX-split lesson). Pure; any bad input / error -> not a mover.
  2. build_session_frame() — today's RTH 5m frame FROM the 09:30 ET open (the momentum trigger's INPUT
                             CONTRACT: bar 0 == the open, so indicators.vwap.add_vwap resets on today).
                             HALT AWARENESS: the 5m sequence must be CONTIGUOUS from the open; a missing
                             bar = a halt -> return None (never momentum-trade a halted name — that is the
                             reopen-gap tail §7b guards against). Fail-safe None on any error.
  3. momentum_to_entry()   — map an Increment-1 ENTER momentum result to the execution.day_trade_manager
                             .place_entry contract: a (decision, trigger) pair whose `wall_ref` = the
                             momentum structural_level (so _compute_stop_price places the stop at the OR
                             level -- the natural momentum invalidation), `mode` carried through
                             (DRIVE/PULLBACK), conviction from track_b_conviction (shorts smaller, §7c-d).

PURE / FAIL-SAFE: every function returns a value (never raises into a caller); a bad or missing
input degrades to "not a mover" / None / a non-ENTER trigger. Sizing, the order, the live book fetch and the
Track-B exposure cap live in run_day_tier.py + execution/day_trade_manager.py (Inc 2 Part 2). NOT yet built: the
per-track B sub-kill (DAYTRADE_TRACK_B_KILL_PCT is STAGED — a later increment) and any precise clock-time cutoff
(the only time limits are track_b_in_window's coarse gate and the trigger's bar-count cutoff).

Data tier: T1 intraday 5m bars via data.fetcher.fetch_bars_window(feed="iex") — the IEX feed, because this
account's data plan serves SIP only for SETTLED history (a SIP window ending within the last ~15 min returns
403 "subscription does not permit querying recent SIP data" — verified live 2026-09-23), while IEX is served
in real time. The runner supplies prior_close (settled SIP daily close) + avg_daily_volume (IEX daily volume,
the SAME basis as this frame's volume) from T1 daily fetches. All thresholds PROV-tagged.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# DERIVATION PLAN (PROV:daytier-track-b-screen) — every threshold is a PROVISIONAL v1 starting value on the
# live screen (wired 2026-09-23; Track-B size stays budget-capped). Track B's design (§7, §7c, LdP selection-bias guard) requires deriving them
# from the pooled "gap that BROKE-AND-HELD a level" setup outcomes once live, and NEVER tuning them on live
# P&L. Until that data exists these are documented starting values.
_MIN_GAP_PCT = 0.02          # PROV:daytier-track-b-screen — a real intraday mover has gapped/moved >= 2% off prior close
_MAX_GAP_PCT = 0.60          # PROV:daytier-track-b-screen — a > 60% move is bad data / a corporate action (split) -> reject (NFLX-split lesson)
_MIN_RVOL = 3.0              # PROV:daytier-track-b-screen — session relative volume >= 3x (today vol-so-far vs avg-daily x session-fraction)
_MIN_PRICE = 5.0             # PROV:daytier-track-b-screen — price floor (thin/penny names excluded; the pre-registered list is already liquid)
_MIN_FRAME_BARS = 5          # PROV:daytier-track-b-screen — need the OR (3) + a break bar + >= 1 confirming hold bar (matches momentum trigger _MIN_BARS)
_RTH_OPEN_MIN = 9 * 60 + 30  # 09:30 ET session open (minutes since midnight ET)
_RTH_CLOSE_MIN = 16 * 60     # 16:00 ET session close
_RTH_SESSION_MIN = _RTH_CLOSE_MIN - _RTH_OPEN_MIN  # 390 min full session (RVOL session-fraction denominator)
_BAR_SECONDS = 300           # 5-min bars — contiguity spacing (a larger gap = a halt)
_STALE_FRAME_S = _BAR_SECONDS + 120  # PROV:daytier-track-b-screen — newest COMPLETED bar ENDED more than one bar + 2 min
                                     # (publication lag) before now = a missing completed bar = a halt/feed-stall -> skip
                                     # (data seat R2: an end-based 2-bar bound let a name halted ~11 min through)

# Track B conviction (PROV:daytier-track-b-screen) — DRIVE vs PULLBACK base, shorts scaled SMALLER (§7c-d
# "shorts run smaller/tighter"). Conviction only ever SHRINKS the (already small, exposure-capped) Track-B budget
# in compute_day_tier_size (min-only) — it never up-sizes. Derived from realized per-mode/side expectancy once live.
_CONVICTION_DRIVE = 0.5      # PROV:daytier-track-b-screen — a fresh drive (no retest yet)
_CONVICTION_PULLBACK = 0.6  # PROV:daytier-track-b-screen — a retest that held (higher-quality continuation)
_SHORT_CONVICTION_SCALE = 0.7  # PROV:daytier-track-b-screen — shorts sized smaller (§7c-d)

# PRE-REGISTERED liquid mover candidate list (PROV:daytier-track-b-screen). This fixed list IS the float /
# micro-cap / liquidity guard — high-ADV, optionable names that genuinely gap and run intraday. Overlap with
# DAYTRADE_UNIVERSE (Track A) is fine: place_entry blocks same-symbol re-entry and the shared concurrency cap
# prevents double exposure. Promoted to config.DAYTRADE_TRACK_B_UNIVERSE in Part 2 (this default is the fallback).
_DEFAULT_TRACK_B_UNIVERSE = [
    "NVDA", "TSLA", "META", "AMD", "AMZN", "AAPL", "MSFT", "GOOGL",
    "AVGO", "NFLX", "MU", "COIN", "PLTR", "SMCI", "UBER",
]


def track_b_universe() -> list:
    """The pre-registered Track-B candidate list: config.DAYTRADE_TRACK_B_UNIVERSE if set (Part 2), else the
    module PROV default. Always a list of clean upper-case symbol strings; never raises."""
    try:
        raw = getattr(config, "DAYTRADE_TRACK_B_UNIVERSE", None)
        # A list/tuple/set only — a stray string config is truthy but would iterate into single-char
        # "symbols" (adversarial N5); anything not a real sequence falls back to the default.
        if not raw or not isinstance(raw, (list, tuple, set)):
            raw = _DEFAULT_TRACK_B_UNIVERSE
        out: list = []
        for s in raw:
            sym = str(s).strip().upper()
            if sym and sym not in out:
                out.append(sym)
        return out or list(_DEFAULT_TRACK_B_UNIVERSE)
    except Exception:  # noqa: BLE001 — a universe read must never raise into the runner
        return list(_DEFAULT_TRACK_B_UNIVERSE)


def track_b_in_window(now_et: "datetime | None" = None) -> bool:
    """True iff `now` is inside the only window where the momentum trigger CAN fire, so the runner skips the
    per-symbol frame/daily fetches (API load) when an ENTER is impossible. DERIVED from the trigger's own bar
    bounds (never a free-standing clock constant): the trigger needs >= _MIN_FRAME_BARS 5m bars from the open
    and WAITs past _SESSION_CUTOFF_BARS bars. Bounds are inclusive and one bar of slack wide on each side (a
    conservative margin; frames are completed-bars-only, so the first EVALUABLE tick is ~09:55 and the 09:50-09:54
    ticks only cost fetches):
      lower = (_MIN_FRAME_BARS - 1) bars after 09:30 ET   (09:50 ET with 5 bars),
      upper = (_SESSION_CUTOFF_BARS + 1) bars after 09:30 ET (11:05 ET with 18 bars).
    Pure efficiency gate — outside it every symbol would WAIT anyway. Never raises; an error -> False
    (skip Track B this tick = no new entry, the safe direction)."""
    try:
        from strategy.day_tier_momentum_trigger import _SESSION_CUTOFF_BARS
        n = _now_et(now_et)
        elapsed = (n.hour * 60 + n.minute) - _RTH_OPEN_MIN
        lower = (_MIN_FRAME_BARS - 1) * (_BAR_SECONDS // 60)
        upper = (int(_SESSION_CUTOFF_BARS) + 1) * (_BAR_SECONDS // 60)
        return lower <= elapsed <= upper
    except Exception as _e:  # noqa: BLE001 — a window check must never raise into the runner
        logger.warning("day-tier TRACK-B window check errored (skip Track B this tick): %s", _e)
        return False


def _f(x):
    """float(x) or None — NaN / +-inf -> None. Never raises."""
    try:
        v = float(x)
        return v if (v == v and v not in (float("inf"), float("-inf"))) else None
    except Exception:  # noqa: BLE001 — TypeError/ValueError AND OverflowError (float(10**400)); a numeric coerce never raises
        return None


def _now_et(now_et: "datetime | None" = None) -> datetime:
    """Normalize a caller-supplied `now` to an ET-aware datetime: naive -> assume ET; ANY other tz ->
    convert to ET. Both helpers derive the session-elapsed minutes and the 09:30 open FROM ET, so a
    non-ET `now` (e.g. datetime.now() on this Pacific host, or a UTC-aware clock) must be CONVERTED, not
    read raw — reading it raw collapses `elapsed` and inflates RVOL, WIDENING the admission gate
    (adversarial B3). Never raises (falls back to datetime.now(ET))."""
    try:
        n = now_et or datetime.now(ET)
        if getattr(n, "tzinfo", None) is None:
            n = n.replace(tzinfo=ET)
        return n.astimezone(ET)
    except Exception:  # noqa: BLE001
        return datetime.now(ET)


def track_b_conviction(mode: str, direction: str) -> float:
    """PROV conviction for a Track-B ENTER, in [0,1]. DRIVE/PULLBACK base, shorts scaled smaller (§7c-d).
    Only ever SHRINKS the Track-B budget (compute_day_tier_size is min-only). Never raises."""
    try:
        base = _CONVICTION_PULLBACK if str(mode).upper() == "PULLBACK" else _CONVICTION_DRIVE
        if str(direction).lower() == "short":
            base *= _SHORT_CONVICTION_SCALE
        return 0.0 if base < 0.0 else (1.0 if base > 1.0 else round(base, 3))
    except Exception:  # noqa: BLE001
        return 0.0


def screen_mover(symbol: str, intraday_5m, prior_close, avg_daily_volume,
                 now_et: "datetime | None" = None) -> dict:
    """The PRE-REGISTERED mover screen (§7.63). Decide whether `symbol` is a REAL intraday mover TODAY.

    Args:
      symbol            : the pre-registered candidate.
      intraday_5m       : today's RTH 5m frame FROM the open (build_session_frame output). Needs a 'close'
                          and 'volume' column; the LAST close is today's current price, sum of 'volume' is
                          today's RTH volume so far.
      prior_close       : yesterday's daily close (runner supplies from a T1 daily fetch). Gap basis.
                          CONTRACT: MUST be on TODAY's share basis — i.e. SPLIT-ADJUSTED. The frame is today's
                          RAW bars, which are already post-split on an ex-date; a RAW prior_close is PRE-split,
                          so a 2:1 split would read as a -50% "gap" (corrected 2026-09-23 by the data-integrity
                          seat — the earlier contract text had this backwards). Split adjustment rescales history
                          onto today's basis, so the runner fetches the daily context with adjustment="split"
                          (prices AND volumes). The _MAX_GAP_PCT cap remains a backstop for >=~2.5:1 splits.
      avg_daily_volume  : trailing average DAILY volume (e.g. 20-day; runner supplies). RVOL denominator basis.
                          CONTRACT: MUST be on the SAME FEED basis as intraday_5m's volume (build_session_frame
                          fetches IEX, whose volume is ~2-5% of consolidated) — a consolidated/SIP denominator
                          against an IEX numerator would read every name as ~0.03x RVOL and nothing would ever
                          qualify (Part 2: the runner fetches the ADV from IEX daily bars).
      now_et            : current ET time (session-fraction elapsed for RVOL); defaults to datetime.now(ET).

    Returns {"symbol","is_mover":bool,"gap_direction":"up"/"down"/"none","gap_pct":float,"rvol":float,
    "price":float,"reason"}. is_mover True ONLY when |gap%| in [_MIN_GAP_PCT, _MAX_GAP_PCT], RVOL >= _MIN_RVOL,
    and price >= _MIN_PRICE. Never raises -> is_mover False.
    """
    result: dict = {
        "symbol": symbol, "is_mover": False, "gap_direction": "none",
        "gap_pct": 0.0, "rvol": 0.0, "price": 0.0, "reason": "",
    }
    try:
        df = intraday_5m
        cols = getattr(df, "columns", [])
        if df is None or getattr(df, "empty", True) or len(df) < _MIN_FRAME_BARS \
                or "close" not in cols or "volume" not in cols:
            result["reason"] = "insufficient intraday frame — not a mover"
            return result
        price = _f(df["close"].iloc[-1])
        pc = _f(prior_close)
        if price is None or price <= 0 or pc is None or pc <= 0:
            result["reason"] = "no usable price/prior_close — not a mover"
            return result
        result["price"] = round(price, 4)
        if price < _MIN_PRICE:
            result["reason"] = f"price ${price:.2f} < ${_MIN_PRICE:.2f} floor — not a mover"
            return result

        gap = (price - pc) / pc
        result["gap_pct"] = round(gap, 4)
        result["gap_direction"] = "up" if gap > 0 else ("down" if gap < 0 else "none")
        agap = abs(gap)
        if agap > _MAX_GAP_PCT:  # bad data / a corporate action (split) — never trade it (NFLX-split lesson)
            result["gap_direction"] = "none"
            result["reason"] = f"move {gap:+.1%} exceeds +-{_MAX_GAP_PCT:.0%} sanity cap (bad data / split?) — not a mover"
            return result
        if agap < _MIN_GAP_PCT:
            result["reason"] = f"move {gap:+.1%} < +-{_MIN_GAP_PCT:.0%} — not a mover"
            return result

        # Session RVOL = today's RTH volume so far / (avg daily volume x fraction of the session elapsed).
        adv = _f(avg_daily_volume)
        if adv is None or adv <= 0:
            result["reason"] = "avg daily volume unavailable — not a mover (fail-closed)"
            return result
        try:
            today_vol = float(df["volume"].fillna(0).sum())
        except Exception:  # noqa: BLE001
            today_vol = 0.0
        if not (math.isfinite(today_vol) and today_vol >= 0):  # NaN / +-inf guard (inf slips an ==-based check)
            result["reason"] = "today volume unreadable — not a mover"
            return result
        n = _now_et(now_et)  # normalize to ET first — a non-ET now would collapse elapsed + inflate RVOL (B3)
        mins_now = n.hour * 60 + n.minute
        elapsed = mins_now - _RTH_OPEN_MIN
        # Clamp the elapsed session to a sane [one bar, full session] range: below one bar -> use one bar
        # (avoid a divide-by-tiny RVOL blow-up right at the open); after the close -> the full session.
        elapsed = max(_BAR_SECONDS // 60, min(elapsed, _RTH_SESSION_MIN))
        frac = elapsed / _RTH_SESSION_MIN
        expected_vol = adv * frac
        if expected_vol <= 0:
            result["reason"] = "non-positive expected volume — not a mover"
            return result
        rvol = today_vol / expected_vol
        result["rvol"] = round(rvol, 2)
        if rvol < _MIN_RVOL:
            result["reason"] = f"RVOL {rvol:.1f}x < {_MIN_RVOL:.0f}x — not a mover"
            return result

        result["is_mover"] = True
        result["reason"] = (f"MOVER {result['gap_direction']}: gap {gap:+.1%}, RVOL {rvol:.1f}x, "
                            f"price ${price:.2f} — screen PASS (pure screen; the runner routes any ENTER)")
        logger.info("[%s] day-tier TRACK-B SCREEN: %s", symbol, result["reason"])
        return result
    except Exception as _e:  # a pure screen must NEVER raise into a caller
        result["is_mover"] = False
        result["reason"] = f"unexpected error: {_e!r}"
        logger.warning("[%s] day-tier TRACK-B SCREEN: unexpected error — not a mover: %s", symbol, _e)
        return result


def build_session_frame(symbol: str, now_et: "datetime | None" = None):
    """Today's RTH 5m frame FROM the 09:30 ET open (the momentum trigger's INPUT CONTRACT). Fetches via
    data.fetcher.fetch_bars_window(feed="iex", RAW) over [today 09:30 ET, now] — IEX because it is the only
    feed this data plan serves in REAL TIME (a SIP window ending at `now` is rejected -> empty -> this would
    return None on every live tick; verified 2026-09-23). IEX 5m bars were contiguous for all 15 pre-
    registered names over the 2 probed sessions (a small sample — monitor live). Then VERIFIES:
      * bar 0's timestamp == today's 09:30 ET open (DST-aware) — else the OR window / VWAP reset misalign;
      * the 5m sequence is CONTIGUOUS (no missing bar) — a gap = a multi-bar HALT -> skip. LIMIT: a short
        LULD pause (< ~7 min) can leave contiguous partial bars and pass; the marketable limit + small size bound it;
      * >= _MIN_FRAME_BARS usable bars.
    Returns the RTH-only DataFrame (UTC-indexed, [open,high,low,close,volume]) or None on any failure/halt.
    A T1 read only + FAIL-SAFE: never raises. NOTE: the live runner (Inc 2 Part 2) calls it only inside
    track_b_in_window (a coarse, minute-granular gate) and only while the per-tick API/time budgets allow; there is
    NO precise clock-time cutoff beyond that gate and the trigger's bar-count cutoff.
    """
    try:
        import pandas as pd
        from data.fetcher import fetch_bars_window

        n = _now_et(now_et)  # ET-aware (naive->ET, any tz->ET) so the 09:30 anchor + freshness are correct
        # Anchor the window to today's 09:30 ET open (tz-aware; DST handled by ZoneInfo). end = now. Both
        # tz-aware -> fetch_bars_window accepts them (it rejects naive datetimes).
        open_et = n.replace(hour=9, minute=30, second=0, microsecond=0)
        if n <= open_et:  # pre-open / bad clock — no session frame yet
            return None
        df = fetch_bars_window(symbol, config.TF_5M, open_et, n, feed="iex")  # real-time feed (see docstring)
        if df is None or getattr(df, "empty", True) or len(df) < _MIN_FRAME_BARS:
            return None
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            return None
        # COMPLETED BARS ONLY (board data + risk seats 2026-09-23): drop any bar whose END (start + 5m) is after
        # `now`. A still-forming bar would let a half-built bar "confirm" a hold (and its partial volume fail the
        # volume check), and a historical --asof replay would otherwise see up to 5 min of the future. This also
        # makes the live FRAME match the Rule-C replay's bar convention (RVOL elapsed-time and fill timing still differ).
        try:
            _ends = idx + pd.Timedelta(seconds=_BAR_SECONDS)
            _now_ts = pd.Timestamp(n)
            if idx.tz is None:
                _now_ts = _now_ts.tz_convert("UTC").tz_localize(None)
            df = df[_ends <= _now_ts]
            idx = df.index
        except Exception as _ce:  # noqa: BLE001 — cannot prove bars are complete -> skip (fail-safe)
            logger.warning("[%s] day-tier TRACK-B frame: completed-bar filter errored (skip): %s", symbol, _ce)
            return None
        if len(df) < _MIN_FRAME_BARS:
            return None
        # RTH-only: drop any bar outside [09:30, 16:00) ET (SIP can include ext-hours if the window widens).
        try:
            idx_et = idx.tz_convert(ET) if idx.tz is not None else idx.tz_localize("UTC").tz_convert(ET)
        except Exception:  # noqa: BLE001
            return None
        mins = idx_et.hour * 60 + idx_et.minute
        rth_mask = (mins >= _RTH_OPEN_MIN) & (mins < _RTH_CLOSE_MIN)
        df = df[rth_mask]
        if len(df) < _MIN_FRAME_BARS:
            return None
        # Bar 0 MUST be the 09:30 open (else the OR / VWAP-reset assumptions break).
        first_ts = df.index[0]
        first_et = first_ts.tz_convert(ET) if df.index.tz is not None else first_ts.tz_localize("UTC").tz_convert(ET)
        if (first_et.hour * 60 + first_et.minute) != _RTH_OPEN_MIN:
            logger.info("[%s] day-tier TRACK-B frame: first bar %s is not the 09:30 ET open — skip", symbol, first_et)
            return None
        # HALT AWARENESS: the 5m sequence must be contiguous. expected = bars if no gap; fewer = a halt gap.
        span_s = (df.index[-1] - df.index[0]).total_seconds()
        expected = int(round(span_s / _BAR_SECONDS)) + 1
        if len(df) < expected:
            logger.info("[%s] day-tier TRACK-B frame: non-contiguous (%d bars, expected %d — a HALT) — skip",
                        symbol, len(df), expected)
            return None
        # END-OF-FRAME FRESHNESS (halt / feed-stall guard — adversarial B1): internal contiguity does NOT
        # catch a name that halted mid-session and is STILL halted now — it returns a contiguous but STALE
        # frame whose newest bar predates `now` by many minutes, which the momentum trigger would read as a
        # live break-and-hold and enter into a halted name (the reopen-gap tail §7b guards against). Require
        # the newest COMPLETED bar to be recent vs now, measured from the bar's END (Alpaca bar ts = the START;
        # frames are completed-bars-only, so a healthy frame's newest bar ended 0-5 min ago plus publication
        # lag; a multi-bar halt grows the age unbounded).
        try:
            _last = df.index[-1]
            if getattr(df.index, "tz", None) is None:  # naive-safe like the RTH-mask / first-bar handling above
                _last = _last.tz_localize("UTC")
            age_s = (n - _last.to_pydatetime()).total_seconds() - _BAR_SECONDS
        except Exception as _fe:  # noqa: BLE001 — LOG + skip THIS symbol; never let a silent raise shut Track B down
            logger.warning("[%s] day-tier TRACK-B frame: freshness check errored (skip): %s", symbol, _fe)
            return None
        if age_s > _STALE_FRAME_S:
            logger.info("[%s] day-tier TRACK-B frame: newest bar %s is stale (%.0fs > %ds — halt/stall) — skip",
                        symbol, df.index[-1], age_s, _STALE_FRAME_S)
            return None
        return df
    except Exception as _e:  # a frame build must NEVER raise into a caller
        logger.warning("[%s] day-tier TRACK-B frame build error (skip): %s", symbol, _e)
        return None


def momentum_to_entry(mom: dict, gap_direction: "str | None" = None) -> "tuple[dict, dict]":
    """Map an Increment-1 ENTER momentum result to the (decision, trigger) place_entry contract.

    The KEY mapping: `wall_ref` = the momentum `structural_level` (the OR level). place_entry's
    _compute_stop_price uses `wall_ref +- buffer` for any non-FADE mode with target=None, so the protective
    stop lands at the OR level — the natural momentum invalidation (a close back through it). `mode`
    (DRIVE/PULLBACK) is carried through honestly (it is NOT 'FADE', so it takes the wall-based stop branch).

    Returns (decision, trigger). On a non-ENTER / malformed momentum result, returns a WAIT trigger (which
    place_entry rejects) so a caller that skipped its ENTER gate still cannot enter. Never raises.
    """
    try:
        if not isinstance(mom, dict) or mom.get("trigger") != "ENTER":
            return ({"would_consider": False, "conviction": 0.0, "reason": "not an ENTER momentum result"},
                    {"trigger": "WAIT", "reason": "not an ENTER momentum result"})
        symbol = mom.get("symbol")
        direction = mom.get("direction")
        if direction not in ("long", "short"):
            return ({"would_consider": False, "conviction": 0.0, "reason": "momentum has no valid direction"},
                    {"symbol": symbol, "trigger": "WAIT", "reason": "no valid direction"})
        mode = mom.get("mode", "DRIVE")
        entry_ref = _f(mom.get("entry_ref"))
        structural_level = _f(mom.get("structural_level"))
        # An ENTER must carry a usable entry_ref AND structural_level (the stop reference). The Inc-1 trigger
        # guarantees both on ENTER; guard anyway so a malformed ENTER can NEVER emit a trigger with a
        # None/non-positive wall_ref that place_entry would have to catch downstream (adversarial N4).
        if entry_ref is None or entry_ref <= 0 or structural_level is None or structural_level <= 0:
            return ({"symbol": symbol, "would_consider": False, "conviction": 0.0,
                     "reason": "ENTER momentum missing a usable entry_ref/structural_level"},
                    {"symbol": symbol, "trigger": "WAIT",
                     "reason": "ENTER momentum missing a usable entry_ref/structural_level"})
        # GEOMETRY defense-in-depth (adversarial): a momentum continuation holds the structural level on the
        # STOP side of entry (long -> level BELOW entry; short -> level ABOVE). The Inc-1 trigger guarantees
        # this, but assert it here so a malformed ENTER can NEVER hand place_entry a wall_ref that yields an
        # inverted / at-entry stop. Wrong-side -> WAIT.
        if (direction == "long" and structural_level >= entry_ref) or \
           (direction == "short" and structural_level <= entry_ref):
            return ({"symbol": symbol, "would_consider": False, "conviction": 0.0,
                     "reason": f"ENTER momentum geometry invalid (level {structural_level} vs entry {entry_ref} for {direction})"},
                    {"symbol": symbol, "trigger": "WAIT",
                     "reason": "ENTER momentum geometry invalid (level on the wrong side of entry)"})
        conviction = track_b_conviction(mode, direction)
        side = "LONG" if direction == "long" else "SHORT"

        trigger = {
            "symbol": symbol,
            "trigger": "ENTER",
            "direction": direction,
            "mode": mode,                         # DRIVE / PULLBACK — NOT 'FADE' -> wall-based stop branch
            "entry_ref": entry_ref,
            "target": None,                       # RIDE-style: OCO take-profit = R-multiple of stop distance (place_entry)
            "wall_ref": structural_level,         # the OR level -> _compute_stop_price stop = level +- buffer
            "vol_confirmed": bool(mom.get("vol_confirmed")),
            "reason": f"track B {mode} {direction} — {mom.get('reason', '')}",
        }
        decision = {
            "symbol": symbol,
            "track": "B",                         # the per-track stamp Part 2's B sub-kill reads
            "would_consider": True,
            "conviction": conviction,
            "side": side,
            "mode": mode,
            "gap_direction": (str(gap_direction).lower() if gap_direction is not None else None),
            "structural_level": structural_level,
            "vwap": mom.get("vwap"),
            "reason": f"track B mover {side} ({mode}), conviction {conviction} — {mom.get('reason', '')}",
        }
        return decision, trigger
    except Exception as _e:  # a pure adapter must NEVER raise into a caller
        logger.warning("day-tier TRACK-B adapter error (fail to WAIT): %s", _e)
        return ({"would_consider": False, "conviction": 0.0, "reason": f"adapter error: {_e!r}"},
                {"trigger": "WAIT", "reason": f"adapter error: {_e!r}"})
