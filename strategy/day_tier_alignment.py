# ruff: noqa: E501
"""
strategy/day_tier_alignment.py — Day-tier SHORT-TERM ALIGNMENT gate + COUNTER-TREND TREND-FAILURE test
(Confluence 2.0 integration).

Design record: logs/design_records/day_tier_short_term_alignment_2026-10-04.md (owner direction + BGG alignment).

Owner rules (Rafael 2026-10-04):
  * Every day-tier entry needs its 2-minute AND 5-minute indicators to agree with the trade direction
    (blocking). The checks are EMA13 vs EMA30, close vs session VWAP and the MACD-fast histogram sign; which of
    them block is config.DAYTRADE_ALIGN_BLOCKING_CHECKS, the rest (and the whole 15-minute read) are logged as
    information only.
  * A counter-trend FADE (a trade against the Layer-A daily side) also needs proof that today's intraday
    trend has FAILED, not merely pulled back into a flag: the 15-minute leads (trend_failure B2 + B3) and
    a 30-minute bar confirms (B4). See trend_failure() for the exact test (board seat Brandt/Harris +
    Gro + GAI, 2026-10-04).
  * Counter-trend fades auto-disable on live evidence (counter_trend_fades_ok).

Data (T1 Alpaca): ONE IEX 1-minute fetch per symbol (data.fetcher.fetch_bars_window, feed="iex" — the plan's
real-time feed; SIP is not entitled for the most recent ~15 min), regular-session bars only (09:30-16:00 ET),
resampled to 2m/5m/15m/30m buckets anchored at 09:30 ET. A bucket is used only when every minute it spans has
ended AND the data reaches its end (no forming bars). EMAs/MACD warm up across prior sessions; VWAP and the
trend-failure structure use TODAY's session bars only.

FAIL-SAFE: any fetch failure, too little history, or a missing/non-finite value -> not aligned / not failed
(no trade). Never raises.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import config

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_FETCH_CALENDAR_DAYS = 8      # covers >= 40 closed 30m RTH bars after a 3-day weekend
_MIN_WARMUP_BARS = 40         # per timeframe, across sessions (EMA30 warm-up; board seat Brandt)
_MAX_DATA_AGE = timedelta(minutes=15)   # data-quality contract #1: newest 1m bar must close within 15 min of now
# The bars the with-trend gate reads must be TODAY's and recent (a 09:31 read must never use the prior session's
# close — cold-2nd 2026-10-04): the last closed bucket must have ENDED within this window of now.
_MAX_BAR_END_AGE = {2: timedelta(minutes=6), 5: timedelta(minutes=10)}

# Trend-failure structure (board 2026-10-04; PROV:daytier-trend-failure — reversal criterion in the design record)
_TF_WINDOW_15M = 16           # session 15m bars searched for today's trend extreme
_TF_MIN_SESSION_15M = 6       # no fade before 6 closed session 15m bars (11:00 ET)
_TF_MIN_BARS_AFTER_EXTREME = 2
_TF_HL_LOOKBACK = 4           # bars before the extreme that hold the last higher-low / lower-high
_TF_BREAK_ATR = 0.10          # close must clear the higher-low by 0.10 x ATR15 (a body, not a wick)
_TF_EXTREME_ATR = 0.10        # the extreme must be within 0.10 x ATR15 of today's session extreme
_TF_BOUNCE_ATR = 0.25         # the bounce after the extreme must stay 0.25 x ATR15 short of it
_TF_MIN_RETRACE = 0.50        # the move must give back >= 50% of the leg (a flag retraces less)
_ATR_PERIOD = 14

# Reversal criterion (board 2026-10-04): counter-trend fades auto-disable on live evidence.
_CT_MAX_CUM_LOSS_USD = 25.0   # PROV:daytier-st-alignment — ~1% of equity
_CT_MAX_CONSEC_LOSSES = 3     # PROV:daytier-st-alignment

_frames_cache: dict = {}      # (symbol, ET minute) -> frames; one fetch per symbol per runner tick


def _fnum(x) -> "float | None":
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _now_et() -> datetime:
    return datetime.now(_ET)


def _fetch_1m(symbol: str, now: datetime) -> pd.DataFrame:
    from data.fetcher import fetch_bars_window
    return fetch_bars_window(symbol, config.TF_1M, now - timedelta(days=_FETCH_CALENDAR_DAYS), now, feed="iex")


def _rth_1m(raw: pd.DataFrame) -> pd.DataFrame:
    """Regular-session 1m bars, ET-indexed."""
    if raw is None or raw.empty or not isinstance(raw.index, pd.DatetimeIndex) or raw.index.tz is None:
        return pd.DataFrame()
    df = raw[["open", "high", "low", "close", "volume"]].copy()
    df.index = df.index.tz_convert(_ET)
    t = df.index.time
    return df[(t >= _RTH_OPEN) & (t < _RTH_CLOSE)].sort_index()


def _resample_closed(m1: pd.DataFrame, minutes: int, now: datetime) -> pd.DataFrame:
    """`minutes`-wide buckets anchored at 09:30 ET (09:30 is a multiple of 2/5/15/30 minutes from midnight).
    A bucket is kept only when it has ended by `now` AND the 1m data reaches its last minute."""
    if m1.empty:
        return pd.DataFrame()
    last_data_end = m1.index[-1] + timedelta(minutes=1)
    cutoff = min(now, last_data_end)
    out = m1.resample(f"{minutes}min", label="left", closed="left", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    ends = out.index + timedelta(minutes=minutes)
    in_session = [(e.time() <= _RTH_CLOSE) and (e.date() == s.date()) for s, e in zip(out.index, ends, strict=True)]
    keep = [(e <= cutoff) and ok for e, ok in zip(ends, in_session, strict=True)]
    return out[keep]


def _with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    from indicators.macd import add_both_macds
    from indicators.moving_averages import add_all_mas
    from indicators.vwap import add_vwap
    out = add_vwap(add_both_macds(add_all_mas(df.copy())))   # ET index -> VWAP resets per ET session date
    prev_close = out["close"].shift(1)
    tr = pd.concat([out["high"] - out["low"], (out["high"] - prev_close).abs(),
                    (out["low"] - prev_close).abs()], axis=1).max(axis=1)
    out["atr"] = tr.rolling(_ATR_PERIOD).mean()
    return out


def load_frames(symbol: str, now: "datetime | None" = None) -> "dict | None":
    """{2: df, 5: df, 15: df, 30: df} of closed RTH bars with indicators, or None (fail closed). Cached per
    (symbol, ET minute) so the alignment and trend-failure reads of one tick share one fetch."""
    now = now or _now_et()
    key = (symbol, now.strftime("%Y%m%d%H%M"))
    if key in _frames_cache:
        return _frames_cache[key]
    m1 = _rth_1m(_fetch_1m(symbol, now))
    frames: "dict | None" = {}
    if m1.empty or m1.index[-1] + timedelta(minutes=1) < now - _MAX_DATA_AGE:
        frames = None   # no data, or the newest regular-session bar is stale -> fail closed
    for m in (2, 5, 15, 30):
        if frames is None:
            break
        f = _resample_closed(m1, m, now)
        if len(f) < _MIN_WARMUP_BARS:
            frames = None
            break
        frames[m] = _with_indicators(f)  # type: ignore[index]
    _frames_cache.clear()
    _frames_cache[key] = frames
    return frames


def _tf_checks(row, want_up: bool) -> dict:
    ema_f, ema_s = f"ema_{config.EMA_FAST}", f"ema_{config.EMA_SLOW}"
    hist = f"{config.MACD_FAST['label']}_histogram"
    pairs = {"ema": (_fnum(row.get(ema_f)), _fnum(row.get(ema_s))),
             "vwap": (_fnum(row.get("close")), _fnum(row.get("vwap")))}
    out: dict = {}
    for name, (a, b) in pairs.items():
        out[name] = None if a is None or b is None else ((a > b) if want_up else (a < b))
    h = _fnum(row.get(hist))
    out["macd_fast"] = None if h is None else ((h > 0) if want_up else (h < 0))
    return out


def short_term_alignment(symbol: str, direction: str, frames: "dict | None" = None,
                         now: "datetime | None" = None) -> dict:
    """{"aligned": bool, "checks": {...}, "info": {...}, "reason": str} for `direction` ('long'/'short').
    aligned is True only when every BLOCKING check (config.DAYTRADE_ALIGN_BLOCKING_CHECKS) agrees with the
    direction on BOTH the 2m and 5m; the non-blocking checks and the 15m read are reported in "info" and never
    block. The 2m and 5m bars read must be from TODAY's session and must have ended within 6 / 10 minutes of now
    (otherwise not aligned). An empty blocking set fails closed. Never raises."""
    out: dict = {"aligned": False, "checks": {}, "info": {}, "reason": ""}
    d = str(direction or "").lower()
    if d not in ("long", "short"):
        out["reason"] = f"invalid direction {direction!r}"
        return out
    want_up = d == "long"
    try:
        now = (now or _now_et()).astimezone(_ET)
        fr = frames if frames is not None else load_frames(symbol, now)
        if not fr:
            out["reason"] = "insufficient closed 2m/5m/15m/30m bars"
            return out
        for m, max_age in _MAX_BAR_END_AGE.items():
            last_start = fr[m].index[-1]
            last_end = last_start + timedelta(minutes=m)
            if last_start.date() != now.date() or last_end < now - max_age:
                out["reason"] = f"no fresh closed {m}m bar from today's session (last {last_start})"
                return out
        blocking = set(getattr(config, "DAYTRADE_ALIGN_BLOCKING_CHECKS", ("ema", "vwap", "macd_fast")))
        if not blocking:
            out["reason"] = "no blocking alignment checks configured (fail closed)"
            return out
        checks: dict = {}
        info: dict = {}
        for m in (2, 5):
            for k, v in _tf_checks(fr[m].iloc[-1], want_up).items():
                (checks if k in blocking else info)[f"{k}{m}"] = v
        info.update({f"{k}15": v for k, v in _tf_checks(fr[15].iloc[-1], want_up).items()})
        out["checks"] = checks
        out["info"] = info
        failed = [k for k, v in checks.items() if v is not True]
        out["aligned"] = not failed
        out["reason"] = "2m+5m indicators agree" if not failed else f"not aligned: {', '.join(failed)}"
        return out
    except Exception as e:  # noqa: BLE001 — a read-only gate must never raise; unknown = not aligned
        out["reason"] = f"alignment error: {e!r}"
        logger.warning("[%s] day-tier short-term alignment failed (not aligned): %s", symbol, e)
        return out


def trend_failure(symbol: str, fade_direction: str, frames: "dict | None" = None,
                  now: "datetime | None" = None) -> dict:
    """Has today's intraday trend FAILED in favour of a fade in `fade_direction`? Written for a SHORT fade of
    an up-trend; a LONG fade of a down-trend is the exact mirror (highs<->lows, comparisons flipped).
      B2 (15m leads): last closed 15m EMA13 < EMA30, close < VWAP, MACD-fast histogram < 0.
      B3 (15m structure, session bars only; window W = last 16 session 15m bars, >= 6 required -> 11:00 ET):
         H = highest high in W at index h, with >= 2 closed bars after it, and H within 0.10 x ATR15 of the
         session high. S = lowest low in W up to h. HL = lowest low of the 4 bars before h.
         (i)  last close < HL - 0.10 x ATR15          (a body break of the last higher low)
         (ii) (H - last close) / (H - S) >= 0.50       (deeper than a flag)
         (iii) highest high after h < H - 0.25 x ATR15, and no bar after h+1 closed above its EMA13
               (the bounce failed — a flag re-tests its high, a failed trend cannot)
      B4 (30m ladder): the last closed 30m bar (today's session) closes below its EMA13 AND below the prior
         session 30m bar's low.
    Returns {"failed": bool, "checks": {...}, "levels": {...}, "reason": str}. Never raises."""
    out: dict = {"failed": False, "checks": {}, "levels": {}, "reason": ""}
    d = str(fade_direction or "").lower()
    if d not in ("long", "short"):
        out["reason"] = f"invalid direction {fade_direction!r}"
        return out
    short = d == "short"
    try:
        fr = frames if frames is not None else load_frames(symbol, now)
        if not fr:
            out["reason"] = "insufficient closed 15m/30m bars"
            return out
        f15, f30 = fr[15], fr[30]
        today = f15.index[-1].date()
        if (now or _now_et()).astimezone(_ET).date() != today:
            out["reason"] = "no closed 15m bar from today's session"
            return out
        checks: dict = {}
        # B2 — the 15m leads the turn
        b2 = _tf_checks(f15.iloc[-1], want_up=not short)
        checks.update({f"b2_{k}15": v for k, v in b2.items()})
        # B3 — 15m structure break (session bars only)
        sess = f15[f15.index.date == today]
        atr = _fnum(f15["atr"].iloc[-1])
        if len(sess) < _TF_MIN_SESSION_15M or atr is None or atr <= 0:
            out["checks"] = checks
            out["reason"] = (f"too early: {len(sess)} closed session 15m bars (< {_TF_MIN_SESSION_15M})"
                             if len(sess) < _TF_MIN_SESSION_15M else "15m ATR unavailable")
            return out
        w = sess.tail(_TF_WINDOW_15M)
        hi, lo, cl = w["high"].tolist(), w["low"].tolist(), w["close"].tolist()
        ema_f = w[f"ema_{config.EMA_FAST}"].tolist()
        n = len(w)
        ext_series = hi if short else lo
        ext_val = max(ext_series) if short else min(ext_series)
        h = max(i for i, v in enumerate(ext_series) if v == ext_val)   # latest bar at the extreme
        sess_ext = float(sess["high"].max() if short else sess["low"].min())
        checks["b3_bars_after_extreme"] = h <= n - 1 - _TF_MIN_BARS_AFTER_EXTREME
        checks["b3_extreme_is_today"] = (ext_val >= sess_ext - _TF_EXTREME_ATR * atr) if short else (
            ext_val <= sess_ext + _TF_EXTREME_ATR * atr)
        hl_rng = range(max(0, h - _TF_HL_LOOKBACK), h)
        last = cl[-1]
        if not hl_rng:
            checks["b3_body_break"] = checks["b3_retrace"] = checks["b3_failed_bounce"] = False
        else:
            start = min(lo[: h + 1]) if short else max(hi[: h + 1])          # S
            hl = min(lo[i] for i in hl_rng) if short else max(hi[i] for i in hl_rng)
            leg = (ext_val - start) if short else (start - ext_val)
            checks["b3_body_break"] = (last < hl - _TF_BREAK_ATR * atr) if short else (last > hl + _TF_BREAK_ATR * atr)
            retrace = ((ext_val - last) / leg if short else (last - ext_val) / leg) if leg > 0 else None
            checks["b3_retrace"] = retrace is not None and retrace >= _TF_MIN_RETRACE
            after = range(h + 1, n)
            if after:
                bounce = max(hi[i] for i in after) if short else min(lo[i] for i in after)
                bounce_ok = (bounce < ext_val - _TF_BOUNCE_ATR * atr) if short else (bounce > ext_val + _TF_BOUNCE_ATR * atr)
            else:
                bounce, bounce_ok = None, False
            reclaim = any(
                (_fnum(ema_f[i]) is None) or ((cl[i] > ema_f[i]) if short else (cl[i] < ema_f[i]))
                for i in range(h + 2, n))
            checks["b3_failed_bounce"] = bool(bounce_ok and not reclaim)
            out["levels"] = {"extreme": ext_val, "leg_start": start, "higher_low" if short else "lower_high": hl,
                             "bounce": bounce, "retrace": None if retrace is None else round(retrace, 3),
                             "atr15": round(atr, 4)}
        # B4 — 30m ladder confirmation (both bars from today's session)
        s30 = f30[f30.index.date == today]
        if len(s30) >= 2:
            r30, p30 = s30.iloc[-1], s30.iloc[-2]
            c30, e30 = _fnum(r30.get("close")), _fnum(r30.get(f"ema_{config.EMA_FAST}"))
            pl = _fnum(p30.get("low" if short else "high"))
            checks["b4_close_vs_ema13_30"] = None if c30 is None or e30 is None else ((c30 < e30) if short else (c30 > e30))
            checks["b4_beyond_prior_30"] = None if c30 is None or pl is None else ((c30 < pl) if short else (c30 > pl))
        else:
            checks["b4_close_vs_ema13_30"] = checks["b4_beyond_prior_30"] = None
        out["checks"] = checks
        failed = [k for k, v in checks.items() if v is not True]
        out["failed"] = not failed
        out["reason"] = ("intraday trend failed (15m lead + structure break, 30m confirms)" if not failed
                         else f"trend not confirmed failed: {', '.join(failed)}")
        return out
    except Exception as e:  # noqa: BLE001 — unknown = not failed (no fade)
        out["reason"] = f"trend-failure error: {e!r}"
        logger.warning("[%s] day-tier trend-failure read failed (no fade): %s", symbol, e)
        return out


def counter_trend_fades_ok(events: "list[dict] | None" = None) -> "tuple[bool, str]":
    """(allowed, reason). Counter-trend fades are allowed unless the kill flag is off, or their live record hits the
    reversal criterion: cumulative realized P&L <= -$25 or 3 consecutive losses. A counter-trend entry is identified
    by its decision record (trigger.counter_trend is True), joined through its entry_fill (decision_id -> trade_id) to its
    exit_fill / partial_exit_fill rows.
    This is a "stop adding new fades" trigger checked at entry time, not a loss cap on open positions.
    An unreadable log -> NOT allowed (fail closed). Never raises."""
    try:
        if not bool(getattr(config, "DAYTRADE_COUNTER_TREND_FADES_ENABLED", True)):
            return False, "DAYTRADE_COUNTER_TREND_FADES_ENABLED is False"
        if events is None:
            from strategy import day_tier_logger
            events, readable = day_tier_logger.read_events_checked()
            if not readable:
                return False, "day-tier event log unreadable (fail closed)"
        # Join: a counter-trend decision record (decision_id) -> its entry_fill (entry_fill.decision_id; the
        # entry_fill's trade_id is the BROKER-tagged coid, which differs from the decision record's trade_id in
        # live logs) -> that trade_id's exit_fill / partial_exit_fill rows. The decision's own trade_id is kept
        # too (harmless when it never matches).
        ct_decisions: set = set()
        ct_ids: set = set()
        for e in events:
            if e.get("event") == "decision" and (e.get("trigger") or {}).get("counter_trend"):
                for k in ("decision_id", "trade_id"):
                    if e.get(k):
                        ct_decisions.add(e[k])
                if e.get("trade_id"):
                    ct_ids.add(e["trade_id"])
        for e in events:
            if e.get("event") == "entry_fill" and e.get("decision_id") in ct_decisions and e.get("trade_id"):
                ct_ids.add(e["trade_id"])
        # A trade's realized P&L is its partial_exit_fill rows PLUS its terminal exit_fill (the terminal row prices
        # only the residual qty). Partial losses of a still-open fade count toward the cumulative criterion; a
        # trade enters the loss streak only once its terminal exit_fill exists.
        pnl_by: dict = {}
        ts_by: dict = {}
        closed_ids: set = set()
        for e in events:
            ev = e.get("event")
            if ev in ("exit_fill", "partial_exit_fill") and e.get("trade_id") in ct_ids:
                tid = e["trade_id"]
                p = _fnum(e.get("realized_pnl"))
                pnl_by[tid] = pnl_by.get(tid, 0.0) + (p or 0.0)
                ts_by[tid] = max(ts_by.get(tid, ""), str(e.get("ts") or ""))
                if ev == "exit_fill":
                    closed_ids.add(tid)
        closed = sorted(closed_ids, key=lambda t: ts_by[t])
        cum = sum(pnl_by.values())
        streak = 0
        for t in reversed(closed):
            if pnl_by[t] < 0:
                streak += 1
            else:
                break
        if cum <= -_CT_MAX_CUM_LOSS_USD:
            return False, f"counter-trend fades disabled: cumulative ${cum:.2f} <= -${_CT_MAX_CUM_LOSS_USD:.0f}"
        if streak >= _CT_MAX_CONSEC_LOSSES:
            return False, f"counter-trend fades disabled: {streak} consecutive losses"
        return True, f"counter-trend fades allowed ({len(closed)} closed, cumulative ${cum:.2f}, streak {streak})"
    except Exception as e:  # noqa: BLE001
        logger.warning("counter-trend fade record check failed (fail closed): %s", e)
        return False, f"counter-trend record check error: {e!r}"
