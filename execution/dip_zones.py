# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/dip_zones.py — data-derived QHM dip-add zones (Claude 2026-10-15).

The QHM dip-add used fixed rungs (Rung A at cost x 0.98, Rung B at cost x 0.95) for every stock. Measured over the
past year the same 2% is half a typical day for SNDK and three for COST. The zones are now each stock's own past-year
distribution of 2-session returns (design: logs/design_records/qhm_dynamic_dip_zones_2026-10-15.md; board Thorp +
Taleb seats, Gro, GAI aligned; out-of-sample replay vs the old fixed rungs: QHM small adds 74% -> 61% of quarters,
forward return on the added dollars +4.91% -> +6.41%):

    Rung A = the deeper of: the stock's 10th-percentile 2-session return over the past year, and the pooled-universe
             10th percentile (same year) — a calm stock cannot buy shallower than the universe's own unusual dip
    Rung B = the deepest of: the stock's 2.5th percentile, the pooled-universe 2.5th percentile (all QHM / F6 /
             Swing names, same year), and Rung A minus the stock's median absolute daily move
    short history (the stock's bars start after the window does): each rung is also bounded by the pooled-universe
             5th percentile, and with no own history at all the pooled values are used alone

FREQUENCY: both rungs are floored by pooled-universe percentiles, so one calm stock cannot get a shallow zone of its
own (board 3-1 + Gro + GAI, 2026-10-15; replay: QHM small adds 62% -> 61% of quarters, forward return +5.93% ->
+6.41%). MEASURED, NOT GUARANTEED against the old fixed -2% / -5%: if the WHOLE universe calms, the pooled floors get
shallower too (Taleb seat's noted gap). Size caps are unchanged.
refresh() runs from cron (run_dip_zones.py) and writes data/state/dip_zones.json atomically. zones_for() is the only
reader QHM calls: missing / unreadable / unknown symbol -> None, and the caller makes NO dip-add (fail closed).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
_DEFAULT_STATE = Path(__file__).resolve().parent.parent / "data" / "state" / "dip_zones.json"
_STATE = _DEFAULT_STATE


def _test_guard() -> bool:
    """True in a test process using the PRODUCTION path: tests never read or write live state."""
    return os.environ.get("MTF_TEST_MODE") == "1" and _STATE == _DEFAULT_STATE


def zones_for(symbol: str) -> "tuple[float, float] | None":
    """(rung_a, rung_b) as returns vs cost (e.g. -0.039, -0.085), or None when no zone is available (no add)."""
    try:
        if _test_guard() or not _STATE.exists():
            return None
        d = json.loads(_STATE.read_text())
        z = (d.get("zones") or {}).get(str(symbol).upper()) if isinstance(d, dict) else None
        if not isinstance(z, dict):
            return None
        a, b = float(z["rung_a"]), float(z["rung_b"])
        if not (a < 0 and b <= a):           # NaN fails both comparisons
            return None
        return a, b
    except Exception as e:  # noqa: BLE001
        logger.warning("dip zones unreadable for %s (%s) — no dip-add", symbol, e)
        return None


def typical_move_for(symbol: str) -> "float | None":
    """The stock's median absolute 1-session return over the past year (e.g. 0.0154 for NVDA), or None when no
    positive finite value is available. The extended-hours dip-add uses it as its liquidity yardstick (fail closed)."""
    try:
        if _test_guard() or not _STATE.exists():
            return None
        d = json.loads(_STATE.read_text())
        z = (d.get("zones") or {}).get(str(symbol).upper()) if isinstance(d, dict) else None
        m = float(z["median_move"]) if isinstance(z, dict) and "median_move" in z else None
        return m if m is not None and m > 0 and m != float("inf") else None   # NaN fails `> 0`
    except Exception as e:  # noqa: BLE001
        logger.warning("dip zones: typical move unreadable for %s (%s)", symbol, e)
        return None


def compute_zones(closes, horizon: int) -> dict:
    """Pure: closes = DataFrame of split-adjusted daily closes (DatetimeIndex, one column per symbol, completed
    sessions only); horizon = the return horizon in sessions (QHM's minimum days between dip-adds).
    Returns {symbol: {"rung_a", "rung_b", "median_move", "short_history", "own_n"}} as of the last row
    (median_move = the stock's median absolute 1-session return over the window; the pooled value with no own data)."""
    import pandas as pd
    px = closes.astype(float).sort_index()
    if px.empty or horizon < 1:
        return {}
    # fill a missing bar INSIDE each name's own history so a move across the gap is kept, never before its first bar
    px = px.apply(lambda c: c.loc[c.first_valid_index():c.last_valid_index()].ffill().reindex(c.index)
                  if c.first_valid_index() is not None else c)
    window_start = px.index.max() - pd.DateOffset(years=1)
    two = px / px.shift(horizon) - 1
    one = (px / px.shift(1) - 1).abs()
    two_w = two[two.index > window_start]
    one_w = one[one.index > window_start]
    pooled = two_w.stack().dropna()
    if pooled.empty:
        return {}
    # Rung A is never shallower than the pooled 10th percentile; short history is bounded by the pooled 5th
    pool_a, pool_b, pool_floor = (float(pooled.quantile(0.10)), float(pooled.quantile(0.025)),
                                  float(pooled.quantile(0.05)))
    pool_move = float(one_w.stack().dropna().median())
    out = {}
    for s in px.columns:
        own = two_w[s].dropna()
        first = px[s].first_valid_index()
        short = first is None or first > window_start
        own_move = one_w[s].dropna()
        move = float(own_move.median()) if not own_move.empty else pool_move
        if own.empty:
            a = pool_floor
            b = min(pool_b, a - pool_move)
        else:
            a = min(float(own.quantile(0.10)), pool_a)
            b = min(float(own.quantile(0.025)), pool_b, a - move)
            if short:
                a = min(a, pool_floor)
                b = min(b, pool_floor)
        out[s] = {"rung_a": a, "rung_b": b, "median_move": move, "short_history": bool(short), "own_n": int(len(own))}
    return out


def universe() -> list:
    """QHM picks + held QHM symbols + the F6 universe + the Swing breakout pool."""
    import config
    from execution import swing_breakout_manager as sb
    root = Path(__file__).resolve().parent.parent / "data" / "state"
    names: list = []
    try:
        picks = json.loads((root / "quarterly_holds_config.json").read_text()).get("picks") or []
        names += [p if isinstance(p, str) else p.get("symbol") for p in picks]
    except Exception as e:  # noqa: BLE001
        logger.warning("dip zones: QHM picks unreadable (%s)", e)
    try:
        held = json.loads((root / "quarterly_holds.json").read_text())
        names += [s for s, p in held.items() if isinstance(p, dict) and "state" in p]
    except Exception as e:  # noqa: BLE001
        logger.warning("dip zones: QHM holds unreadable (%s)", e)
    names += list(getattr(config, "FOREVER6_UNIVERSE", []) or [])
    names += list(getattr(config, "SWING_BREAKOUT_POOL", sb._DEFAULT_POOL) or sb._DEFAULT_POOL)
    return list(dict.fromkeys(str(s).upper() for s in names if s))


def _alert(msg: str) -> None:
    """WARNING log + Slack (alerts.py refuses to send in a test process). Never raises."""
    logger.warning(msg)
    try:
        from alerts import send_slack
        send_slack(f":warning: {msg}")
    except Exception as e:  # noqa: BLE001
        logger.warning("dip zones alert not sent: %s", e)


def _write(d: dict) -> None:
    if _test_guard():
        raise RuntimeError("refusing to write the production dip-zone state from a test")
    os.makedirs(_STATE.parent, exist_ok=True)
    tmp = _STATE.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _STATE)


def refresh() -> "dict | None":
    """Fetch a year of completed daily bars for the universe (T1 Alpaca SIP, split-adjusted), compute the zones,
    write the state. None when nothing could be computed (the last good file stays). Never raises."""
    try:
        import pandas as pd
        import config
        from data.fetcher import fetch_bars_window
        today = f"{datetime.now(ET):%Y-%m-%d}"
        end = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        start = (pd.Timestamp(end) - pd.DateOffset(years=1) - pd.DateOffset(weeks=1)).to_pydatetime()
        cols, missing = {}, []
        for s in universe():
            df = fetch_bars_window(s, config.TF_DAILY, start, end, feed="sip", adjustment="split")
            if df is None or df.empty:
                missing.append(s)
                continue
            c = df["close"].astype(float)
            idx = pd.to_datetime(c.index)
            idx = idx.tz_localize(timezone.utc) if idx.tz is None else idx
            c.index = idx.tz_convert(ET).tz_localize(None).normalize()
            c = c[c.index < pd.Timestamp(today)]           # completed sessions only
            if not c.empty:
                cols[s] = c
        from execution.quarterly_hold_manager import _DIP_ADD_MIN_DAYS_BETWEEN
        zones = compute_zones(pd.DataFrame(cols), int(_DIP_ADD_MIN_DAYS_BETWEEN))
        if not zones:
            _alert("dip zones: nothing computed — the last good zones stay in force")
            return None
        if missing:   # a one-day data failure keeps that name's last good zone instead of deleting it
            try:
                prev = json.loads(_STATE.read_text()).get("zones") or {} if _STATE.exists() else {}
            except Exception as _pe:  # noqa: BLE001
                prev = {}
                logger.warning("dip zones: previous file unreadable (%s)", _pe)
            kept = [s for s in missing if isinstance(prev.get(s), dict)]
            for s in kept:
                zones[s] = {**prev[s], "carried_forward": True}
            _alert(f"dip zones: no daily bars for {missing}; kept the last good zone for {kept}, "
                   f"no zone (no dip-add) for {sorted(set(missing) - set(kept))}")
        state = {"session": today, "computed_at": datetime.now(PT).isoformat(), "zones": zones}
        _write(state)
        logger.info("dip zones refreshed for %d names", len(zones))
        return state
    except Exception as e:  # noqa: BLE001
        _alert(f"dip zones refresh failed — the last good zones stay in force: {e}")
        return None
