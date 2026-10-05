# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
strategy/day_tier_track_m.py — Day-tier Track M: QQQ Monday weekend-gap-down buy (Rafael-approved 2026-10-05).

THE RULE (design: logs/design_records/c2_day_tier_methodology_2026-10-04.md, TRACK M DESIGN; board Thorp/Harris/
Taleb + Gro + GAI aligned 5/5): on a Monday whose previous trading session is the Friday exactly 3 calendar days
earlier, on a full-length session, when QQQ's 09:30 opening print is BELOW Friday's split-adjusted SIP close, buy
QQQ once in the 09:45-10:15 ET window. Protective stop 1% below the fill; no profit target; the existing
calendar-driven EOD force-flat exits it. Size starts at half the standard day-tier per-trade risk and moves to
the full standard risk after 15 closed Track-M trades (never above it).

AUTO-OFF (pauses entries; eligibility is still logged): trailing 8 closed Track-M trades with mean realized P&L
<= 0, OR the last 4 closed Track-M trades all losses — measured from the day-tier exit journal, whose realized
P&L is booked from broker fills (day_trade_manager).

This module is PURE decision logic (no order). Order placement, the protective stop, the opposite-side guard,
the daily dollar budget, the tier kill and the EOD force-flat all live in execution/day_trade_manager.py.
FAIL-SAFE: any missing/stale/unreadable input -> not eligible, with the reason recorded. Never raises.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

SYMBOL = "QQQ"
_WINDOW_START_MIN = 9 * 60 + 45      # PROV:daytier-track-m-2026-10-05 — 09:45 ET entry window opens (design)
_WINDOW_END_MIN = 10 * 60 + 15       # PROV:daytier-track-m-2026-10-05 — 10:15 ET entry window closes (design)
_MIN_FULL_DAY_MINUTES_LEFT = 300     # at 09:45 a full session has 375 min left, a 13:00 half-day 195 — skip half-days
_ENTRY_BAR_MAX_AGE_S = 300           # the live 1m bar used as the entry reference must be <= 5 min old
_DAY_MARKER_KEY = "_track_m_day"     # day_tier_state.json key: {"date": YYYYMMDD} once the Monday shot is used


def _cfg(name: str, default):
    return getattr(config, name, default)


def in_window(now_et: datetime) -> bool:
    """True inside the 09:45-10:15 ET entry window."""
    m = now_et.hour * 60 + now_et.minute
    return _WINDOW_START_MIN <= m < _WINDOW_END_MIN


def used_today(state: dict, day: str) -> bool:
    """True if today's Track-M shot was already taken (persisted marker). Never raises."""
    try:
        m = (state or {}).get(_DAY_MARKER_KEY)
        return isinstance(m, dict) and m.get("date") == day
    except Exception:  # noqa: BLE001
        return True    # unreadable marker -> treat as used (fail closed: never a second entry)


def mark_used(dtm, day: str) -> bool:
    """Persist today's one-shot marker BEFORE any order (atomic state write). Returns the save result."""
    try:
        st = dtm._load_state()
        st[_DAY_MARKER_KEY] = {"date": day}
        return bool(dtm._save_state(st))
    except Exception as e:  # noqa: BLE001
        logger.warning("track-M one-shot marker write failed: %s", e)
        return False


def _closed_track_m_pnls(events: list) -> list:
    """Realized P&L per CLOSED Track-M trade, oldest first. A trade is Track M when its entry_fill carries
    track == "M"; it is closed when an exit_fill exists. Realized = exit_fill + every partial_exit_fill."""
    entry_qty: dict = {}
    order: list = []
    pnl: dict = {}
    partial_qty: dict = {}
    closed: set = set()
    for e in events or []:
        tid = str(e.get("trade_id") or "")
        if not tid:
            continue
        ev = e.get("event")
        if ev == "entry_fill" and str(e.get("track") or "") == "M":
            if tid not in entry_qty:
                entry_qty[tid] = abs(float(e.get("fill_qty") or 0.0))
                order.append(tid)
        elif ev in ("exit_fill", "partial_exit_fill"):
            pnl[tid] = pnl.get(tid, 0.0) + float(e.get("realized_pnl") or 0.0)
            if ev == "exit_fill":
                closed.add(tid)
            else:
                partial_qty[tid] = partial_qty.get(tid, 0.0) + abs(float(e.get("fill_qty") or 0.0))
    # Parity with day_tier_logger.open_trades_from_log: partial exits that consume the whole entry close the trade.
    for t in order:
        if entry_qty.get(t, 0.0) > 0 and partial_qty.get(t, 0.0) >= entry_qty[t]:
            closed.add(t)
    return [pnl.get(t, 0.0) for t in order if t in closed]


def auto_off(events: list, readable: bool) -> "tuple[bool, str]":
    """(off, reason). Off when the exit journal is unreadable (fail closed), when the trailing 8 closed Track-M
    trades average <= 0, or when the last 4 closed Track-M trades are all losses."""
    if not readable:
        return True, "day-tier event journal unreadable (fail closed)"
    try:
        p = _closed_track_m_pnls(events)
    except (TypeError, ValueError) as e:
        return True, f"Track-M P&L unreadable (fail closed): {e!r}"
    n_mean = int(_cfg("DAYTRADE_TRACK_M_OFF_TRAILING_N", 8))
    n_loss = int(_cfg("DAYTRADE_TRACK_M_OFF_CONSEC_LOSSES", 4))
    if len(p) >= n_mean and sum(p[-n_mean:]) / n_mean <= 0:
        return True, f"trailing {n_mean} Track-M trades mean ${sum(p[-n_mean:]) / n_mean:.2f} <= 0"
    if len(p) >= n_loss and all(x < 0 for x in p[-n_loss:]):
        return True, f"last {n_loss} Track-M trades all losses"
    return False, f"{len(p)} closed Track-M trade(s); auto-off not triggered"


def risk_mult(events: list) -> float:
    """0.5 of the standard per-trade risk until DAYTRADE_TRACK_M_FULL_SIZE_AFTER closed trades, then 1.0."""
    try:
        n = len(_closed_track_m_pnls(events))
    except (TypeError, ValueError):
        n = 0
    full_after = int(_cfg("DAYTRADE_TRACK_M_FULL_SIZE_AFTER", 15))
    return 1.0 if n >= full_after else float(_cfg("DAYTRADE_TRACK_M_START_RISK_MULT", 0.5))


def evaluate(now_et: datetime, prev_session: "str | None", mins_to_close: "float | None") -> dict:
    """Eligibility + entry reference for today. Returns
    {"eligible": bool, "retry": bool, "reason": str, "friday_close", "open_930", "gap_pct", "entry_ref", "stop_ref"}.
    retry=True marks a transient data gap (re-evaluate next tick); False is a definitive answer for today.
    Every data read is T1 (data.fetcher). Never raises."""
    out: dict = {"eligible": False, "retry": False, "reason": "", "symbol": SYMBOL, "friday_close": None,
                 "open_930": None, "gap_pct": None, "entry_ref": None, "stop_ref": None}
    try:
        if now_et.weekday() != 0:
            out["reason"] = "not a Monday"
            return out
        if not prev_session:
            out["reason"] = "previous session unknown (calendar unreadable) — skip"
            out["retry"] = True   # transient calendar outage: re-evaluate next tick (never burn the Monday on it)
            return out
        if prev_session != (now_et.date() - timedelta(days=3)).isoformat():
            out["reason"] = f"previous session {prev_session} is not the Friday 3 days earlier (holiday) — skip"
            return out
        if mins_to_close is None or mins_to_close < _MIN_FULL_DAY_MINUTES_LEFT:
            out["reason"] = f"not a full-length session (minutes to close {mins_to_close}) — skip"
            return out
        from data.fetcher import fetch_bars, fetch_bars_window
        day0 = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        # Friday close: settled, split-adjusted SIP daily bar for the previous session (T1).
        d = fetch_bars_window(SYMBOL, config.TF_DAILY, day0 - timedelta(days=10), day0, feed="sip", adjustment="split")
        if d is None or getattr(d, "empty", True) or "close" not in d.columns:
            out["reason"] = "Friday daily bar unavailable — skip"
            out["retry"] = True   # transient data gap: re-evaluate on the next tick inside the window
            return out
        last = d.index[-1]
        last = last.tz_convert(ET) if last.tzinfo is not None else last.tz_localize("UTC").tz_convert(ET)
        if last.date().isoformat() != prev_session:
            out["reason"] = f"last daily bar {last.date()} != previous session {prev_session} — skip"
            out["retry"] = True   # transient data gap: re-evaluate on the next tick inside the window
            return out
        fri = float(d["close"].iloc[-1])
        # 09:30 opening print: the SIP 1-minute bar opening at 09:30 ET (>= 15 min old by 09:45, so SIP-eligible).
        o = fetch_bars_window(SYMBOL, config.TF_1M, day0.replace(hour=9, minute=30), day0.replace(hour=9, minute=31),
                              feed="sip", adjustment="split")
        if o is None or getattr(o, "empty", True) or "open" not in o.columns:
            out["reason"] = "09:30 SIP opening bar unavailable — skip"
            out["retry"] = True   # transient data gap: re-evaluate on the next tick inside the window
            return out
        first = o.index[0]
        first = first.tz_convert(ET) if first.tzinfo is not None else first.tz_localize("UTC").tz_convert(ET)
        if (first.hour, first.minute) != (9, 30):
            out["reason"] = f"first SIP bar is {first:%H:%M}, not 09:30 — skip"
            out["retry"] = True   # transient data gap: re-evaluate on the next tick inside the window
            return out
        opn = float(o["open"].iloc[0])
        if not (math.isfinite(fri) and fri > 0 and math.isfinite(opn) and opn > 0):
            out["reason"] = "non-finite Friday close / opening print — skip"
            return out
        out.update(friday_close=round(fri, 4), open_930=round(opn, 4), gap_pct=round((opn / fri - 1) * 100, 3))
        if opn >= fri:
            out["reason"] = f"no weekend gap-down (open {opn:.2f} >= Friday close {fri:.2f})"
            return out
        # Live entry reference: the latest 1-minute bar close (T1), which must be fresh.
        live = fetch_bars(SYMBOL, config.TF_1M, num_bars=5)
        if live is None or getattr(live, "empty", True) or "close" not in live.columns:
            out["reason"] = "live 1m bar unavailable — skip"
            out["retry"] = True   # transient data gap: re-evaluate on the next tick inside the window
            return out
        lt = live.index[-1]
        lt = lt.tz_convert(ET) if lt.tzinfo is not None else lt.tz_localize("UTC").tz_convert(ET)
        age = (now_et - lt).total_seconds()
        if not (0 <= age <= _ENTRY_BAR_MAX_AGE_S + 60):     # bar is stamped at its OPEN: allow its 60s span
            out["reason"] = f"live 1m bar is {age:.0f}s old (stale) — skip"
            out["retry"] = True   # transient data gap: re-evaluate on the next tick inside the window
            return out
        ref = float(live["close"].iloc[-1])
        stop_pct = float(_cfg("DAYTRADE_TRACK_M_STOP_PCT", 0.01))  # PROV:daytier-track-m-2026-10-05 — default mirrors config
        if not (math.isfinite(ref) and ref > 0 and 0 < stop_pct < 0.05):
            out["reason"] = "invalid entry reference / stop percent — skip"
            return out
        out.update(eligible=True, entry_ref=round(ref, 4), stop_ref=round(ref * (1 - stop_pct), 2),
                   reason=f"Monday weekend gap-down {out['gap_pct']:+.2f}% (open {opn:.2f} < Friday {fri:.2f})")
        return out
    except Exception as e:  # noqa: BLE001 — a data/decision failure skips the trade; never raises
        logger.warning("track-M evaluate failed (skip): %s", e)
        out["reason"] = f"evaluation error (skip): {e!r}"
        out["retry"] = True   # transient data gap: re-evaluate on the next tick inside the window
        return out


def build_order_dicts(ev: dict, rmult: float) -> "tuple[dict, dict]":
    """(decision, trigger) for day_trade_manager.place_entry from an eligible evaluate() result."""
    decision = {"track": "M", "symbol": SYMBOL, "would_consider": True, "conviction": 1.0,
                "side": "LONG", "reason": ev.get("reason"), "friday_close": ev.get("friday_close"),
                "open_930": ev.get("open_930"), "gap_pct": ev.get("gap_pct"), "risk_mult": rmult}
    trigger = {"trigger": "ENTER", "direction": "long", "mode": "TRACK_M", "entry_ref": ev.get("entry_ref"),
               "stop_ref": ev.get("stop_ref"), "no_target": True, "target": None}
    return decision, trigger
