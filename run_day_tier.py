#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
run_day_tier.py — Day-Tier 2-3 min EXECUTION runner (LIVE, behind config.DAYTRADE_ENABLED).

The fast execution loop the design calls for (§7 CADENCE: a 2-3 min EXECUTION loop for fill / re-peg
/ flat-by-close; the SIGNAL stays on the 15/30-min bar-close read the pipeline computes). Invoked by
its OWN cron every ~2 min during RTH — a SEPARATE process from the 5-min main bot (so it never
starves check_exits) and a fresh process EACH tick (so restart-safety = the normal loop mechanic).

Graduates run_day_tier_shadow.py from logging-only to order-placing. Per tick (in order):
  0. flock singleton — exit immediately if a prior tick still holds the lock (no overlap / double-fire).
  1. gate on config.DAYTRADE_ENABLED; gate on market-open (broker.get_clock — half-day aware).
  2. RECONCILE (day_trade_manager.reconcile_open_state) — the go-live gate: never leave a day-tier
     position naked across the cron's process boundary (flatten any position with no live DT stop).
  3. FORCE-FLAT window: within DAYTRADE_FORCE_FLAT_MINUTES of the real close, flatten the tier and
     place NO new entries (the board's EOD go-live gate; runs BEFORE the pre-close sweep window so a
     day-tier lot never inherits an intraday-tagged sweep stop — config validates FORCE_FLAT > SWEEP).
  4. TIER-KILL (day_trade_manager.tier_kill_check) — force-flat + halt for the day at
     −DAYTRADE_TIER_KILL_EQUITY_PCT of SOD equity (both tracks' lots count toward it).
  5. 30-min PRICE SAMPLING — one price_sample per open trade per 30-min bucket (Rafael's price PATH),
     derived restart-safely from the durable log.
  6. ENTRY LOOP — Track A (decision → trigger → size → place_entry), then Track B inside its window
     (IEX session frame → settled daily context → mover screen → momentum trigger → size → place_entry),
     per symbol, idempotent per bar_id.
  7. heartbeat.

ALL order mechanics + every safety guard (B1-B6, the naked-guards, the co-hold guards, the durable +
price-path logging) live in execution/day_trade_manager.py + strategy/day_tier_logger.py — this file
only ORCHESTRATES them. It is a pure runner: no order primitive is defined here.

FAIL-SAFE: DAYTRADE_ENABLED False or market closed → no-op. Any per-symbol error aborts THAT symbol
only. A fatal tick error exits non-zero (cron logs it) but never leaves a traceback mid-order (the
order module's own try/except owns that). Nothing here runs on the 5-min main-scan thread.
"""
from __future__ import annotations

import fcntl
import json
import logging
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Self-load .env so this runs with T1/exec auth under cron (matches run_day_tier_shadow / the audit
# crons). GUARDED — dotenv is absent on some hosts (the unit-test env); there the caller supplies creds.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parent
_LOCK = "/tmp/day_tier_runner.lock"                       # POSIX singleton lock (/tmp allowed for locks)
_HEARTBEAT = _ROOT / "logs" / "day_tier.heartbeat"
_SAMPLE_MARK = _ROOT / "logs" / "day_tier_last_sample.txt"  # last 30-min bucket sampled (dedupe)

logger = logging.getLogger("day_tier_runner")


def _touch_heartbeat(status: str = "ok") -> None:
    """Write a STATUS heartbeat (timestamp + phase) so a freshness monitor can tell an alive-but-
    degraded runner (e.g. repeated 'equity_fail') from a dead one — not just read a bare timestamp
    (reliability seat secondary)."""
    try:
        _HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        _HEARTBEAT.write_text(json.dumps({"ts": datetime.now(PT).isoformat(), "status": status}))
    except Exception as e:  # noqa: BLE001
        logger.warning("heartbeat write failed: %s", e)


def _clock_state() -> "tuple[str, float | None]":
    """Return (OPEN/CLOSED/UNKNOWN, minutes_to_real_close) from Alpaca's half-day-aware clock.

    UNKNOWN never permits an entry.  It differs from CLOSED because an open day-tier position still
    needs reconciliation and a scoped forced exit when the clock endpoint is unavailable; treating
    the two states as identical could leave that intraday-only position unmanaged through the close.
    """
    from execution import broker
    try:
        clk = broker.get_clock()
        if not clk.get("is_open"):
            return "closed", None
        nc = clk.get("next_close")
        if nc is None:
            return "open", None
        now = datetime.now(getattr(nc, "tzinfo", None) or ET)
        return "open", (nc - now).total_seconds() / 60.0
    except Exception as e:  # noqa: BLE001
        logger.warning("clock read failed (entries blocked; owned day-tier positions will flatten): %s", e)
        return "unknown", None


def _maybe_sample_prices() -> None:
    """(2) Price PATH: at each 30-min ET bucket, log ONE price_sample per open day-tier trade. The
    */2 cron fires this many times per bucket, so a last-bucket marker file dedupes it to one write
    per bucket. Open set + last_seq come from the durable log (restart-safe)."""
    from execution import broker
    from strategy import day_tier_logger
    now_et = datetime.now(ET)
    bucket = f"{now_et:%Y%m%d-%H}{(now_et.minute // 30) * 30:02d}"
    try:
        if _SAMPLE_MARK.exists() and _SAMPLE_MARK.read_text().strip() == bucket:
            return  # already sampled this 30-min bucket
    except Exception:
        pass
    try:
        opens = day_tier_logger.open_trades_from_log()
        if opens:
            positions = broker.get_open_positions()
            pos_by = {getattr(p, "symbol", None): p for p in (positions or [])}
            samples = []
            for tid, t in opens.items():
                sym = t.get("symbol")
                pos = pos_by.get(sym)
                if pos is None:
                    continue
                try:
                    cur = abs(float(getattr(pos, "current_price", 0.0) or 0.0))
                except Exception:
                    cur = 0.0
                ent = abs(float(t.get("entry_price") or 0.0))
                qty = abs(float(t.get("fill_qty") or 0.0))
                side = t.get("side", "long")
                upl = ((cur - ent) * qty if side == "long" else (ent - cur) * qty) if (cur > 0 and ent > 0) else 0.0
                samples.append({"trade_id": tid, "symbol": sym, "seq": int(t.get("last_seq", -1)) + 1,
                                "market_price": cur, "unrealized_pnl": upl})
            if samples:
                day_tier_logger.log_price_samples(samples)
        _SAMPLE_MARK.parent.mkdir(parents=True, exist_ok=True)
        _SAMPLE_MARK.write_text(bucket)
    except Exception as e:  # noqa: BLE001
        logger.warning("price sampling failed (non-fatal): %s", e)


_daily_ctx_cache: dict = {}  # (symbol, YYYYMMDD) -> (prior_close, avg_daily_volume); in-process layer
# Cross-process layer: the runner is a FRESH process every 2-min tick, so without a file the settled daily
# context would be re-fetched (2 calls/symbol) every tick. The context is immutable for the trading day
# (strictly-prior settled bars), so it is cached ONCE per day. data/cache/ is the approved market-data cache
# location (CLAUDE.md §3); single writer = the flock'd runner. Any cache read/write failure just re-fetches.
_DAILY_CTX_FILE = _ROOT / "data" / "cache" / "day_tier_track_b_daily_ctx.json"
_DAILY_CTX_BASIS = "split-sip-close/iex-adv20/prev-session-v1"  # a cache written on any other basis is a miss
_TRACK_B_ADV_DAYS = 20      # PROV:daytier-track-b-screen — trailing prior-day window for the RVOL denominator
_TRACK_B_ADV_MIN_DAYS = 15  # PROV:daytier-track-b-screen — refuse an ADV built from fewer prior days (a partial
                            # fetch / one half-day would halve the ADV and double RVOL — data-integrity seat F3)
_prev_session_cache: dict = {}  # YYYYMMDD (today) -> YYYY-MM-DD of the previous trading session
# Track-B wall-clock budget per tick (execution-reliability seat F1): stop evaluating further Track-B symbols
# once the tick has run this long, so a slow/erroring data API (5x backoff per fetch) cannot hold the runner's
# flock across the next */2 ticks — which would skip reconcile + the tier-kill check. Checked between symbols.
_TRACK_B_TICK_BUDGET_S = 75.0  # PROV:daytier-track-b-screen — < the 120 s cadence with room for one slow symbol
_TICK_CADENCE_S = 120.0        # the */2 cron cadence
_PLACE_ENTRY_RESERVE_S = 35.0  # PROV:daytier-track-b-screen — a place_entry (fill polls + stop/OCO) needs this
                               # much tick time left, else the ENTER is deferred (exec seat R2)
_SIGNAL_DAY_KEY = "_track_b_signal_day"  # state key: {"date": YYYYMMDD, "symbols": [...]} — per-day signal marker


def _mark_track_b_signal(dtm, sym: str, day: str) -> None:
    """Persist `sym` in today's Track-B signal marker (state file, atomic via day_trade_manager._save_state).
    Best-effort: on a failed write the in-process set still blocks the symbol for this tick; logged."""
    try:
        st = dtm._load_state()
        m = st.get(_SIGNAL_DAY_KEY)
        syms = list(m["symbols"]) if (isinstance(m, dict) and m.get("date") == day
                                      and isinstance(m.get("symbols"), list)) else []
        if sym not in syms:
            syms.append(sym)
        st[_SIGNAL_DAY_KEY] = {"date": day, "symbols": syms}
        if not dtm._save_state(st):
            logger.warning("[%s] track-B signal marker write failed (in-process block only this tick)", sym)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] track-B signal marker write errored (in-process block only this tick): %s", sym, e)


def _side_for(sym: str) -> str:
    """Layer-A structural trend side for `sym` (LONG / SHORT / TWO_SIDED / UNKNOWN). Any error returns
    UNKNOWN, which the direction rule treats as no trade (fail closed)."""
    try:
        from strategy.day_tier_side import compute_side_bias
        return str((compute_side_bias(sym) or {}).get("side") or "UNKNOWN")
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] Layer-A side read failed — UNKNOWN (no trade): %s", sym, e)
        return "UNKNOWN"


def _log_direction_skip(sym: str, decision: dict, trigger: dict, reason: str, bar_id: str) -> None:
    """Decision-explainability (Rule D) for a trend-direction skip: the full decision + trigger stack plus
    the skip reason go to day_tier_events.jsonl. Best-effort: a logging failure never blocks the skip."""
    logger.info("[%s] day-tier ENTER skipped — %s", sym, reason)
    try:
        from strategy import day_tier_logger
        day_tier_logger.log_decision(f"SKIP-{sym}-{bar_id}", sym, decision=decision,
                                     trigger={**(trigger or {}), "skip_reason": reason})
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] direction-skip decision log failed: %s", sym, e)


def _alignment_for(sym: str, direction: object) -> dict:
    """Short-term (2m + 5m blocking, 15m informational) indicator alignment for the trade direction
    (strategy.day_tier_alignment). Any error -> not aligned (fail closed)."""
    try:
        from strategy.day_tier_alignment import short_term_alignment
        return short_term_alignment(sym, str(direction or ""))
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] short-term alignment unavailable (not aligned): %s", sym, e)
        return {"aligned": False, "checks": {}, "reason": f"alignment unavailable: {e!r}"}


def _trend_failure_for(sym: str, direction: str) -> dict:
    """Has today's intraday trend failed (15m lead + structure break, 30m confirmation) in favour of a fade in
    `direction`? (strategy.day_tier_alignment.trend_failure). Any error -> not failed (no fade)."""
    try:
        from strategy.day_tier_alignment import trend_failure
        return trend_failure(sym, direction)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] trend-failure read unavailable (no fade): %s", sym, e)
        return {"failed": False, "checks": {}, "levels": {}, "reason": f"trend-failure unavailable: {e!r}"}


def _record_counter_trend_intent(sym: str, decision: dict, trigger: dict, bar_id: str) -> str:
    """Durably log a counter-trend fade's decision record (trigger.counter_trend=True) BEFORE its order, under a
    fresh decision_id that place_entry then stamps on the entry_fill. Returns the decision_id, or "" when the
    write did not persist — the caller must then NOT place the fade (its loss could escape the reversal
    criterion). Never raises."""
    import uuid
    did = f"CT-{sym}-{bar_id}-{uuid.uuid4().hex[:8]}"
    try:
        from strategy import day_tier_logger
        if day_tier_logger.log_decision(did, sym, decision=decision, trigger=trigger):
            return did
        logger.warning("[%s] counter-trend fade NOT placed — its decision record did not persist", sym)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] counter-trend fade NOT placed — decision record write failed: %s", sym, e)
    return ""


def _counter_trend_fades_ok() -> "tuple[bool, str]":
    """Live reversal criterion for counter-trend fades (strategy.day_tier_alignment). Any error -> not allowed."""
    try:
        from strategy.day_tier_alignment import counter_trend_fades_ok
        return counter_trend_fades_ok()
    except Exception as e:  # noqa: BLE001
        logger.warning("counter-trend fade check unavailable (not allowed): %s", e)
        return False, f"counter-trend check unavailable: {e!r}"


def _entry_direction_gate(sym: str, side: object, direction: object, mode: object,
                          allow_counter_fade: bool) -> dict:
    """Owner rules (Rafael 2026-10-03/04): no clear daily trend -> no trade; every entry needs its 2m and 5m
    indicators to agree with it (15m informational); a trade against the daily trend is allowed ONLY as a FADE
    (Track A) whose intraday trend has FAILED — the 15m leads with a structure break and a 30m bar confirms (not a
    flag pullback) — and only while counter-trend fades pass their live reversal criterion. Returns
    {"ok": bool, "reason": str, "counter_trend": bool, "alignment": dict, "trend_failure": dict}. Never raises."""
    out: dict = {"ok": False, "reason": "", "counter_trend": False, "alignment": {}, "trend_failure": {}}
    s = str(side or "").upper()
    d = str(direction or "").lower()
    if s not in ("LONG", "SHORT"):
        out["reason"] = f"no clear trend direction (side={s or 'UNKNOWN'})"
        return out
    if d not in ("long", "short"):
        out["reason"] = f"invalid trade direction {direction!r}"
        return out
    counter = (s == "LONG") != (d == "long")
    if counter and not (allow_counter_fade and str(mode or "").upper() == "FADE"):
        out["reason"] = f"{d} trade against the {s} trend side (counter-trend allowed only for aligned fades)"
        return out
    al = _alignment_for(sym, d)
    out["alignment"] = al
    if not al.get("aligned"):
        out["reason"] = f"short-term indicators not aligned with {d} ({al.get('reason', '')})"
        return out
    if counter:
        tf = _trend_failure_for(sym, d)
        out["trend_failure"] = tf
        if not tf.get("failed"):
            out["reason"] = f"counter-trend fade blocked — intraday trend not confirmed failed ({tf.get('reason', '')})"
            return out
        ok, why = _counter_trend_fades_ok()
        if not ok:
            out["reason"] = why
            return out
        out["counter_trend"] = True
    out["ok"] = True
    out["reason"] = ("counter-trend fade: trend failed, short-term aligned" if counter
                     else "with trend, short-term aligned")
    return out


def _gate_blocks() -> bool:
    """CEO order 2026-10-06: the day tier must trade — the trend/alignment gate RECORDS its verdict but does not
    block entries. config.DAYTRADE_ALIGN_GATE_BLOCKS=True restores blocking (kill flag, Rule D). Only an explicit
    True blocks."""
    try:
        import config
        return getattr(config, "DAYTRADE_ALIGN_GATE_BLOCKS", False) is True
    except Exception:  # noqa: BLE001
        return False


def _is_counter(side: object, direction: object) -> bool:
    """True when the trade direction opposes a clear LONG/SHORT daily side (attribution tag only)."""
    s = str(side or "").upper()
    d = str(direction or "").lower()
    return s in ("LONG", "SHORT") and d in ("long", "short") and (s == "LONG") != (d == "long")


def _held_by_other_tiers() -> "set | None":
    """Symbols with a live broker position NOT owned by the day tier (main bot, swing, QHM, F6), or None when the
    book cannot be read. Interim no-co-hold rule (2026-10-07): the day tier never shares a symbol with another tier
    (their whole-symbol exits would sell its shares and book a foreign fill); longs switch to a free 2x ETF.
    place_entry re-checks the live book right before submit. Never raises."""
    try:
        from execution import broker
        from strategy import day_tier_logger as _dtl
        owned = {str(t.get("symbol") or "") for t in _dtl.open_trades_from_log().values()}
        return {str(getattr(p, "symbol", "")) for p in (broker.get_open_positions() or [])} - owned
    except Exception as e:  # noqa: BLE001
        logger.warning("day-tier: position read for the co-hold check failed (place_entry re-checks): %s", e)
        return None


def _prior_close(sym: str) -> "float | None":
    """The last COMPLETED session's daily close (today's forming bar dropped), or None. Feeds the exact daily-reset
    ETF stop and the ETF tracking check; None -> first-order stop / tracking check fails safe. Never raises."""
    try:
        import config
        from data.fetcher import fetch_bars
        df = fetch_bars(sym, config.TF_DAILY, num_bars=6)
        done = _prior_days(df, datetime.now(ET))
        v = float(done["close"].iloc[-1]) if done is not None else None
        return v if (v is not None and math.isfinite(v) and v > 0) else None
    except Exception as e:  # noqa: BLE001
        logger.debug("[%s] prior close unavailable: %s", sym, e)
        return None


def _today_iex_volume(sym: str) -> "float | None":
    """Shares traded on IEX today so far (5m IEX bars since 09:30 ET — the real-time feed this plan serves), or None.
    Never raises."""
    try:
        import config
        from data.fetcher import fetch_bars_window
        now = datetime.now(ET)
        df = fetch_bars_window(sym, config.TF_5M, now.replace(hour=9, minute=30, second=0, microsecond=0), now,
                               feed="iex")
        if df is None or getattr(df, "empty", True) or "volume" not in df.columns:
            return None
        v = float(df["volume"].fillna(0).sum())
        return v if math.isfinite(v) else None
    except Exception as e:  # noqa: BLE001
        logger.debug("[%s] today's IEX volume unavailable: %s", sym, e)
        return None


def _day_tier_exposure() -> "dict | None":
    """{underlying: +1/-1} for the day tier's open lots (stock, bull ETF = +1, inverse ETF = -1 on its underlying),
    or None when the durable log cannot be read. Never raises."""
    try:
        from strategy import day_tier_leverage as lev
        from strategy import day_tier_logger as _dtl
        out: dict = {}
        for t in _dtl.open_trades_from_log().values():
            und, sgn = lev.exposure_sign(str(t.get("symbol") or ""), str(t.get("side") or "long"))
            out[und] = sgn
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("day-tier: open-exposure read failed (one-direction check skipped this tick): %s", e)
        return None


def _direction_conflict(exposure: "dict | None", order_sym: str, direction: object) -> "str | None":
    """One direction per underlying (board Taleb 2026-10-07): refuse an entry whose exposure on its underlying is
    opposite an open day-tier lot (NVDL vs NVD, NVDA vs NVD, ...). An unreadable log ALLOWS the entry (logged): an
    opposite pair only wastes a spread — it cannot hide a loss — and the CEO order is that the day tier trades.
    Returns a skip reason or None."""
    if exposure is None:
        logger.warning("[%s] day-tier open-exposure unreadable — one-direction check skipped this entry", order_sym)
        return None
    try:
        from strategy import day_tier_leverage as lev
        und, sgn = lev.exposure_sign(order_sym, "long" if str(direction).lower() == "long" else "short")
        cur = exposure.get(und)
        if cur is not None and cur != sgn:
            return f"day tier already holds the opposite direction on {und} — one direction per stock"
        return None
    except Exception as e:  # noqa: BLE001
        return f"direction check error ({e!r}) — entry refused"


def _inverse_pivot(sym: str, decision: dict, trigger: dict, held: "set | None", equity: float,
                   buying_power: float, track: str, prior_close: "float | None" = None) -> "tuple[tuple | None, str]":
    """Route a SHORT signal on `sym` to a BUY of its inverse ETF (strategy/day_tier_leverage.inverse_entry), then
    gate it live: tracking vs the stock, size, liquidity. Returns ((decision, trigger, size, etf), reason) or
    (None, reason). Never raises."""
    try:
        from data.alpaca_data import get_latest_quote
        from data.live_price import DELAYED_FEED_AGE_S, live_price
        from strategy import day_tier_leverage as lev
        from strategy.day_tier_sizing import compute_day_tier_size
        route = lev.inverse_etf_for_order(sym, held)
        if route is None:
            return None, "no free liquid inverse ETF for this stock"
        etf, k = route
        up, ep = live_price(sym), live_price(etf, max_age_s=DELAYED_FEED_AGE_S)
        c = prior_close if prior_close else _prior_close(sym)
        piv = lev.inverse_entry(decision, trigger, etf, k, up.price if up else None, ep.price if ep else None,
                                prior_close=c)
        if piv is None:
            return None, f"inverse route to {etf}: price/geometry not usable"
        d2, t2 = piv
        ok, why = lev.etf_tracking_ok(t2.get("entry_ref"), _prior_close(etf), t2.get("underlying_entry_ref"), c, k,
                                      inverse=True)
        if not ok:
            return None, f"inverse route to {etf}: {why}"
        size = compute_day_tier_size(etf, d2, t2.get("entry_ref"), equity, buying_power=buying_power, track=track)
        if not size.get("size_ok"):
            return None, f"inverse route to {etf}: size not ok ({size.get('reason')})"
        _n = datetime.now(ET)
        _frac = ((_n.hour * 60 + _n.minute) - (9 * 60 + 30)) / 390.0   # session fraction elapsed (expected-by-now)
        ok, liq = lev.etf_liquidity_ok(get_latest_quote(etf), _today_iex_volume(etf), size.get("shares"),
                                       t2.get("entry_ref"), t2.get("wall_ref"), session_frac=_frac)
        if not ok:
            return None, f"inverse route to {etf}: {liq}"
        return (d2, {**t2, "tracking": why, "liquidity": liq}, size, etf), f"inverse route -> {etf}"
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] inverse route error (no route): %s", sym, e)
        return None, f"inverse route error: {e!r}"


def _watch_day_ok(sym: str, side: object, direction: object) -> "tuple[bool, str]":
    """CEO watch-day rule (Rafael 2026-10-06; board Asness/LdP + GAI; design record day_tier_not_trading_and_price_
    confirm_2026-10-06.md): a SHORT against a LONG daily side is taken only after a WATCH DAY — the previous session
    closed red AND below the prior session's low (settled daily bars; today's partial bar is dropped). Evidence (2y,
    21 names, open->close): unconfirmed counter-trend shorts -3.6 bps (n=3,095), after a watch day +6.8 bps (n=709) —
    an unproven hypothesis with a pre-registered reversal test (after ~100 live watch-day shorts, mean P&L after costs
    <= 0 -> skip all counter-trend shorts). Every other trade returns True (no watch day). Unreadable data -> False
    (only the losing pattern is affected). Never raises."""
    if not (str(side or "").upper() == "LONG" and str(direction or "").lower() == "short"):
        return True, "not a short against a LONG daily side — no watch day needed"
    try:
        import config
        from data.fetcher import fetch_bars
        df = fetch_bars(sym, config.TF_DAILY, num_bars=6)
        if df is None or getattr(df, "empty", True):
            return False, "watch day: daily bars unavailable — counter-trend short skipped"
        idx = df.index
        idx_et = idx.tz_convert(ET) if getattr(idx, "tz", None) is not None else idx.tz_localize("UTC").tz_convert(ET)
        done = df[idx_et.date < datetime.now(ET).date()]          # completed sessions only
        if len(done) < 2:
            return False, "watch day: fewer than 2 completed sessions — counter-trend short skipped"
        y, p = done.iloc[-1], done.iloc[-2]
        red_below = float(y["close"]) < float(y["open"]) and float(y["close"]) < float(p["low"])
        return (red_below, f"watch day {'CONFIRMED' if red_below else 'not confirmed'}: prior session close "
                           f"{float(y['close']):.2f} vs open {float(y['open']):.2f}, prior-prior low {float(p['low']):.2f}")
    except Exception as e:  # noqa: BLE001
        return False, f"watch day check error ({e!r}) — counter-trend short skipped"


def _run_track_m(dtm, equity: float, mins_to_close: "float | None", bar_id_day: str) -> "tuple[int, str]":
    """Track M (QQQ Monday weekend-gap-down buy; strategy.day_tier_track_m). Inside 09:45-10:15 ET only, at most
    ONE shot per day: the one-shot marker is persisted BEFORE any order (a restart can never double-enter). A
    definitive skip (not a gap-down Monday, holiday, half-day, auto-off) is journaled once and uses the day's
    shot; a transient data gap retries on the next tick. Returns (entries placed 0/1, note). Never raises."""
    try:
        import config
        from strategy import day_tier_logger, day_tier_track_m as tm
        now_et = datetime.now(ET)
        if not tm.in_window(now_et) or now_et.weekday() != 0:
            return 0, "outside_window"
        day = now_et.strftime("%Y%m%d")
        if tm.used_today(dtm._load_state(), day):
            return 0, "used_today"
        ev = tm.evaluate(now_et, _prev_session_date(now_et), mins_to_close)
        if not ev.get("eligible") and ev.get("retry"):
            logger.info("[QQQ] track-M: %s (retry next tick)", ev.get("reason"))
            return 0, "retry"
        events, readable = day_tier_logger.read_events_checked()
        off, off_why = tm.auto_off(events, readable)
        rmult = tm.risk_mult(events) if readable else float(getattr(config, "DAYTRADE_TRACK_M_START_RISK_MULT", 0.5))
        route = None
        if ev.get("eligible") and not off:
            from strategy.day_tier_family_router import route_families
            routed = route_families([tm.route_candidate(now_et)], now=now_et)
            route = routed[0] if len(routed) == 1 else None
            try:
                route_share = float(route.allocation) if route is not None else 0.0
            except (TypeError, ValueError):
                route_share = 0.0
            if (route is None or not route.admitted or not math.isfinite(route_share)
                    or not 0 < route_share <= 1.0):
                why = route.reason if route is not None else "router returned no decision"
                logger.error("[QQQ] track-M admission router denied entry: %s (retry next tick)", why)
                return 0, "router_denied"
            # Router allocation is a share of the admitted-family sleeve, never
            # an account allocation. It may only shrink Track M's existing risk.
            rmult *= route_share
        decision, trigger = tm.build_order_dicts(ev, rmult)
        did = f"TM-{day}"
        if not tm.mark_used(dtm, day):
            return 0, "marker_write_failed"     # fail closed: no persisted one-shot marker -> no order
        if not ev.get("eligible") or off:
            reason = ev.get("reason") if not ev.get("eligible") else f"auto-off: {off_why}"
            day_tier_logger.log_decision(f"SKIP-{did}", tm.SYMBOL, decision={**decision, "would_consider": False},
                                         trigger={**trigger, "trigger": "WAIT", "skip_reason": reason})
            logger.info("[QQQ] track-M skip: %s", reason)
            return 0, "skip"
        if not tm.in_window(datetime.now(ET)):
            return 0, "window_closed"           # slow data reads ran past 10:15 — never enter late
        px = float(ev["entry_ref"])
        shares = max(1, math.floor(equity * float(getattr(config, "DAYTRADE_MAX_SINGLE_NAME_NOTIONAL_PCT", 0.65)) / px))
        size = {"symbol": tm.SYMBOL, "size_ok": True, "shares": shares, "track": "M", "risk_mult": rmult,
                "notional": round(shares * px, 2), "budget": 0.0,
                "reason": f"track M: risk x{rmult:g} of the standard per-trade basis; wire-time caps size it"}
        ok = dtm.place_entry(tm.SYMBOL, decision, trigger, size, bar_id=f"{bar_id_day}-TM", equity=equity,
                             decision_id=did)
        return (1 if ok else 0), ("entered" if ok else "not_entered")
    except Exception as e:  # noqa: BLE001 — Track M must never abort the tick
        logger.warning("track-M error (non-fatal): %s", e)
        return 0, "error"


def _track_b_symbols_today(state: dict, day: str) -> set:
    """Symbols already USED by Track B today: any Track-B ENTER signal (the persisted per-day marker — counted
    at the signal, before sizing/min-stop/caps, exactly like the Rule-C replay; exec seat R2) OR any Track-B
    entry record. One Track-B shot per symbol per ET day (a re-entry after a stop-out is otherwise allowed by
    place_entry). Never raises."""
    out: set = set()
    try:
        marker = (state or {}).get(_SIGNAL_DAY_KEY)
        if isinstance(marker, dict) and marker.get("date") == day and isinstance(marker.get("symbols"), list):
            out |= {str(x) for x in marker["symbols"]}
        for k, v in (state or {}).items():
            if (str(k).startswith("entry::") and isinstance(v, dict) and str(v.get("track") or "") == "B"
                    and str(v.get("bar_id") or "").split("-", 1)[0] == day and v.get("symbol")):
                out.add(str(v["symbol"]))
    except Exception as e:  # noqa: BLE001
        logger.warning("track-B per-day entry scan failed (treating as none): %s", e)
    return out


def _read_daily_ctx_file(day: str) -> dict:
    """Return {symbol: [prior_close, adv]} cached for `day` (YYYYMMDD), else {}. Never raises."""
    try:
        if _DAILY_CTX_FILE.exists():
            d = json.loads(_DAILY_CTX_FILE.read_text(encoding="utf-8"))
            if (isinstance(d, dict) and d.get("date") == day and d.get("basis") == _DAILY_CTX_BASIS
                    and isinstance(d.get("ctx"), dict)):
                return d["ctx"]
    except Exception as e:  # noqa: BLE001 — a cache miss is a re-fetch, never an error path
        logger.debug("track-B daily-ctx cache read failed (re-fetch): %s", e)
    return {}


def _write_daily_ctx_file(day: str, ctx: dict) -> None:
    """Atomic tmp -> replace (RC-5). Best-effort: a failed write only costs a re-fetch next tick."""
    try:
        import os
        _DAILY_CTX_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DAILY_CTX_FILE.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps({"date": day, "basis": _DAILY_CTX_BASIS, "ctx": ctx}), encoding="utf-8")
        os.replace(tmp, _DAILY_CTX_FILE)
    except Exception as e:  # noqa: BLE001
        logger.warning("track-B daily-ctx cache write failed (will re-fetch): %s", e)


def _prior_days(df, now_et: datetime):
    """The strictly-before-today rows of a daily bar frame (drops any forming today bar), or None."""
    import pandas as pd
    if df is None or getattr(df, "empty", True) or "close" not in getattr(df, "columns", []):
        return None
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        return None
    idx_et = idx.tz_convert(ET) if getattr(idx, "tz", None) is not None else idx.tz_localize("UTC").tz_convert(ET)
    prior = df[idx_et.date < now_et.date()]
    return prior if len(prior) >= 1 else None


def _last_bar_date(df) -> "str | None":
    """ET calendar date (YYYY-MM-DD) of a daily frame's last row, or None."""
    try:
        ts = df.index[-1]
        ts = ts.tz_convert(ET) if ts.tzinfo is not None else ts.tz_localize("UTC").tz_convert(ET)
        return ts.strftime("%Y-%m-%d")
    except Exception as e:  # noqa: BLE001
        logger.debug("track-B daily frame last-date read failed: %s", e)
        return None


def _prev_session_date(now_et: datetime) -> "str | None":
    """The previous trading session (YYYY-MM-DD) before `now_et`'s date, from the Alpaca trading calendar
    (reporting.pnl_ledger REST plumbing — the same calendar source execution/orphan_manager uses). None if the
    calendar is unreadable -> the caller FAILS CLOSED (skips Track B for the symbol; no entry). Cached per day
    in-process. Never raises."""
    from datetime import timedelta
    day = now_et.strftime("%Y%m%d")
    if day in _prev_session_cache:
        return _prev_session_cache[day] or None  # "" = a failure already seen in THIS process (one tick)
    try:
        from reporting import pnl_ledger as _pl
        lo = (now_et - timedelta(days=12)).strftime("%Y-%m-%d")
        hi = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
        cal = _pl._get_json(f"{_pl._PAPER_BASE}/v2/calendar?start={lo}&end={hi}", tries=2)
        dates = sorted(str(d.get("date")) for d in (cal or []) if isinstance(d, dict) and d.get("date"))
        prev = dates[-1] if dates else None
    except Exception as e:  # noqa: BLE001 — unreadable calendar -> None -> fail closed
        logger.warning("track-B previous-session calendar read failed (Track B skips — fail closed): %s", e)
        prev = None
    # Cache the result INCLUDING a failure ("") for the life of this process (the runner = one tick), so a
    # down calendar costs one call per tick, not one per symbol (exec seat R2).
    _prev_session_cache[day] = prev or ""
    return prev


def _track_b_daily_context(symbol: str, now_et: datetime, prev_session: "str | None" = None,
                           write_cache: bool = True) -> "tuple[float | None, float | None]":
    """Track B daily context: (prior_close, avg_daily_volume), both from SETTLED bars STRICTLY BEFORE today
    (the window ends at today's 00:00 ET, and any forming today bar is dropped), both SPLIT-ADJUSTED so they
    sit on TODAY's share basis (the frame is today's RAW bars = post-split on an ex-date; a RAW prior close
    or RAW pre-split volumes would fake a gap / inflate RVOL for ~20 sessions — data-integrity seat F1):
      * prior_close — the SIP (consolidated, official) daily close. Settled history, so the plan's recent-SIP
        restriction does not apply.
      * avg_daily_volume — the mean of the last _TRACK_B_ADV_DAYS prior IEX daily volumes: the SAME feed
        basis as the IEX session frame's volume (screen_mover's RVOL numerator). A SIP denominator against an
        IEX numerator would read every name as ~0.03x RVOL (IEX is ~2-5% of consolidated volume — measured
        2026-09-23) and Track B would never trade. Requires >= _TRACK_B_ADV_MIN_DAYS prior days.
    BOTH series must END on the previous trading session per the Alpaca calendar (else a missing D-1 bar
    would silently measure a two-day "gap" — data-integrity seat F2); an unreadable calendar fails closed.
    `prev_session` (YYYY-MM-DD) lets the runner resolve the calendar ONCE per tick; None -> resolved here.
    `write_cache=False` (the read-only preflight) never writes the runner's cache file.
    Cached once per trading day (in-process + data/cache file) only after passing every check. Returns
    (None, None) on any failure / insufficient data (the caller then SKIPS the symbol — no entry, the safe
    direction). Never raises."""
    try:
        from datetime import timedelta

        import config
        from data.fetcher import fetch_bars_window
        day = now_et.strftime("%Y%m%d")
        key = (symbol, day)
        if key in _daily_ctx_cache:
            return _daily_ctx_cache[key]
        live_today = day == datetime.now(ET).strftime("%Y%m%d")  # an --asof replay never writes the live cache
        if live_today:
            cached = _read_daily_ctx_file(day).get(symbol)
            if isinstance(cached, list) and len(cached) == 2:
                try:
                    pc_c, adv_c = float(cached[0]), float(cached[1])
                    if math.isfinite(pc_c) and pc_c > 0 and math.isfinite(adv_c) and adv_c > 0:
                        _daily_ctx_cache[key] = (pc_c, adv_c)
                        return pc_c, adv_c
                except (TypeError, ValueError) as ce:
                    logger.debug("[%s] track-B daily-ctx cache row malformed (re-fetch): %s", symbol, ce)
        prev = prev_session or _prev_session_date(now_et)
        if not prev:
            return None, None
        end = now_et.replace(hour=0, minute=0, second=0, microsecond=0)  # today 00:00 ET: settled days only
        start = end - timedelta(days=45)  # ~30 trading days covers the 20-day ADV + holidays
        sip = _prior_days(fetch_bars_window(symbol, config.TF_DAILY, start, end, feed="sip",
                                            adjustment="split"), now_et)
        iex = _prior_days(fetch_bars_window(symbol, config.TF_DAILY, start, end, feed="iex",
                                            adjustment="split"), now_et)
        if sip is None or iex is None or "volume" not in iex.columns:
            return None, None
        if _last_bar_date(sip) != prev or _last_bar_date(iex) != prev:
            logger.info("[%s] track-B daily context: last bars (sip %s, iex %s) != previous session %s — skip",
                        symbol, _last_bar_date(sip), _last_bar_date(iex), prev)
            return None, None
        prior_close = float(sip["close"].iloc[-1])
        vols = iex["volume"].dropna().tail(_TRACK_B_ADV_DAYS)
        if len(vols) < _TRACK_B_ADV_MIN_DAYS:
            logger.info("[%s] track-B daily context: only %d prior IEX volume days (< %d) — skip",
                        symbol, len(vols), _TRACK_B_ADV_MIN_DAYS)
            return None, None
        avg_vol = float(vols.mean())
        if not (math.isfinite(prior_close) and prior_close > 0 and math.isfinite(avg_vol) and avg_vol > 0):
            return None, None
        out = (prior_close, avg_vol)
        _daily_ctx_cache[key] = out
        if live_today and write_cache:
            ctx = _read_daily_ctx_file(day)
            ctx[symbol] = [prior_close, avg_vol]
            _write_daily_ctx_file(day, ctx)
        return out
    except Exception as e:  # noqa: BLE001 — a data-context failure SKIPS the symbol; never aborts the tick
        logger.warning("[%s] track-B daily context failed (skip symbol): %s", symbol, e)
        return None, None


def run_tick() -> dict:
    """One execution tick. Returns a summary dict. Never raises for a per-symbol error."""
    _tick_t0 = time.monotonic()
    import config
    # Gate on DAYTRADE_ENABLED FIRST, before the heavy execution/strategy imports (cold-2nd NOTE 3):
    # while the tier is disabled, a strategy-import failure must be a clean no-op, not a non-zero exit.
    if not getattr(config, "DAYTRADE_ENABLED", False):
        _touch_heartbeat("disabled")
        return {"skipped": "disabled"}
    from execution import broker, day_trade_manager as dtm
    from strategy.day_tier_decision import compute_day_tier_decision
    from strategy.day_tier_entry_trigger import compute_entry_trigger
    from strategy.day_tier_sizing import compute_day_tier_size

    clock_state, mins_to_close = _clock_state()
    try:
        _acct = broker.get_account()
        equity = float(getattr(_acct, "equity", 0.0) or 0.0)
        day_start_equity = float(getattr(_acct, "last_equity", 0.0) or 0.0)
        buying_power = float(getattr(_acct, "buying_power", 0.0) or 0.0)
    except Exception as e:  # noqa: BLE001
        logger.error("account/equity fetch failed — position management continues; entries fail closed: %s", e)
        _acct = None
        equity = day_start_equity = buying_power = float("nan")
    # Reconcile FIRST — never leave a naked day-tier position across the cron's process boundary.
    recon = dtm.reconcile_open_state()

    # A failed clock read must never be mistaken for a known market closure.  It still cannot prove
    # that submitting a new entry is safe, but an existing day-tier lot must not be left to cross the
    # close unmanaged.  force_flat_all is tier-scoped and no-ops when the durable log/state has no
    # owned target, so this cannot sell an unrelated tier's position.
    if clock_state == "unknown":
        targets = dtm._flatten_targets()
        flattened = dtm.force_flat_all(reason="clock_unavailable") if targets else 0
        _touch_heartbeat("clock_unknown_force_flat" if targets else "clock_unknown")
        return {"phase": "clock_unknown", "flattened": flattened,
                "owned_targets": len(targets), "reconcile": recon}
    if clock_state != "open":
        _touch_heartbeat("market_closed")
        return {"skipped": "market_closed", "reconcile": recon}

    # Force-flat window: flatten the tier + NO new entries in the final N min before the real close.
    # An UNKNOWN close-distance (mins_to_close None) is treated as IN-window → fail-CLOSED (flatten,
    # no entries) — the lone must-not-trade path that was otherwise fail-open (masked-loss #2 / cold-2nd).
    ff_min = float(getattr(config, "DAYTRADE_FORCE_FLAT_MINUTES", 20))
    if mins_to_close is None or mins_to_close <= ff_min:
        n = dtm.force_flat_all(reason="eod_force_flat")
        _touch_heartbeat("force_flat")
        return {"phase": "force_flat", "flattened": n,
                "mins_to_close": round(mins_to_close, 1) if mins_to_close is not None else None,
                "reconcile": recon}

    # Tier liquidation and a latched-kill retry are position management, so they run before every
    # account-level entry gate. A main-book halt must never strand a day-tier residual intraday.
    if dtm.tier_kill_check(equity, day_start_equity=day_start_equity):
        _touch_heartbeat("tier_killed")
        return {"phase": "tier_killed", "reconcile": recon}

    # Account-data and entry-halt failures block only NEW risk. They run after reconcile/EOD/tier
    # risk management so a zero-BP or killed account cannot bypass liquidation.
    if not all(math.isfinite(v) and v > 0 for v in (equity, day_start_equity, buying_power)):
        logger.error("account/equity fields invalid — no new entries this tick (fail-closed)")
        _touch_heartbeat("account_invalid")
        return {"skipped": "account_invalid", "reconcile": recon}
    try:
        from execution.risk_manager import RiskManager
        if dtm._account_entry_halt_reason(_acct) or RiskManager(
            equity, daily_start_value=day_start_equity
        ).check_kill_switch():
            _touch_heartbeat("account_halted")
            return {"phase": "account_halted", "reconcile": recon}
    except Exception as e:  # noqa: BLE001
        logger.error("account kill evaluation failed — no new entries this tick (fail-closed): %s", e)
        _touch_heartbeat("account_kill_unknown")
        return {"skipped": "account_kill_unknown", "reconcile": recon}

    # 30-min price sampling for the open price PATH.
    _maybe_sample_prices()

    # Entry loop — Track A (GEX-core) FIRST, then Track B (movers) below when DAYTRADE_TRACK_B_ENABLED.
    # Idempotent per (symbol, bar_id). BOUNDED by a
    # per-tick Alpaca trading-API call budget so the fast loop never starves the 5-min main scan's
    # shared quota (reliability seat: ANTI-SILO §7b.2). place_entry is the heavy trading-API consumer
    # (submit + fill polls + stop + book reads); the pipeline reads (decision/trigger/size) are data-
    # API, governed by the shared data limiter, not this trading-API budget.
    call_budget = int(getattr(config, "DAYTRADE_MAX_API_CALLS_PER_RUN", 60))
    per_entry_est = int(getattr(config, "DAYTRADE_CALLS_PER_ENTRY_EST", 15))
    calls_used = 4 + 2 * int((recon or {}).get("checked", 0) or 0)  # clock+equity + ~2/owned reconcile + kill/sample
    universe = list(getattr(config, "DAYTRADE_UNIVERSE", []))
    bar_id = dtm.bar_id_for()
    entered = 0
    capped = False
    _held_other = _held_by_other_tiers()   # ONCE per tick (co-hold routing); place_entry re-checks before submit
    _dt_exp = _day_tier_exposure()          # ONCE per tick (one direction per stock); updated on each new entry
    # TRACK M — QQQ Monday weekend-gap-down buy (Rafael-approved 2026-10-05). Runs FIRST inside its 09:45-10:15 ET
    # window (one shot per day); a no-op on every other tick. Same reconcile/force-flat/kill/budget machinery.
    entered_m, track_m_note = 0, "disabled"
    if bool(getattr(config, "DAYTRADE_TRACK_M_ENABLED", False)) and calls_used + per_entry_est <= call_budget:
        entered_m, track_m_note = _run_track_m(dtm, equity, mins_to_close, bar_id.split("-", 1)[0])
        if track_m_note in ("entered", "not_entered"):
            calls_used += per_entry_est
    for sym in universe:
        try:
            decision = compute_day_tier_decision(sym)
            if not (isinstance(decision, dict) and decision.get("would_consider")):
                continue
            trigger = compute_entry_trigger(sym, decision)
            if trigger.get("trigger") != "ENTER":
                continue
            # Owner rules 2026-10-03/04: clear daily trend + 2m/5m alignment; counter-trend only as a fade of a
            # FAILED intraday trend (15m lead + 30m confirmation).
            _gate = _entry_direction_gate(sym, decision.get("side"), trigger.get("direction"),
                                          trigger.get("mode"), allow_counter_fade=True)
            if not _gate["ok"] and _gate_blocks():
                _log_direction_skip(sym, decision, {**trigger, "alignment": _gate["alignment"],
                                                    "trend_failure": _gate["trend_failure"]},
                                    _gate["reason"], bar_id)
                continue
            # CEO order 2026-10-06 ("the day tier must trade"): the trend/alignment verdict is RECORDED on the
            # entry's decision record (Rule D) but no longer blocks. A trade against the daily side is tagged
            # counter_trend so its P&L stays attributable.
            _counter = bool(_gate["counter_trend"]) or _is_counter(decision.get("side"), trigger.get("direction"))
            trigger = {**trigger, "counter_trend": _counter, "alignment": _gate["alignment"],
                       "trend_failure": _gate["trend_failure"], "gate_ok": _gate["ok"],
                       "gate_reason": _gate["reason"]}
            _gate = {**_gate, "counter_trend": _counter}
            _wd_ok, _wd_why = _watch_day_ok(sym, decision.get("side"), trigger.get("direction"))
            if not _wd_ok:
                _log_direction_skip(sym, decision, trigger, _wd_why, bar_id)
                continue
            if _counter and str(trigger.get("direction")) == "short":
                trigger = {**trigger, "watch_day": True, "watch_day_reason": _wd_why}
            _ct_decision_id = ""
            if _gate["counter_trend"]:
                # Never-mask-a-loss: the counter-trend tag must be DURABLE before any fade order exists, so its
                # P&L always reaches the fade reversal criterion (entry_fill carries this decision_id).
                _ct_decision_id = _record_counter_trend_intent(sym, decision, trigger, bar_id)
                if not _ct_decision_id:
                    continue
            size = compute_day_tier_size(sym, decision, trigger.get("entry_ref"), equity,
                                         buying_power=buying_power, track="A")
            if not size.get("size_ok"):
                continue
            order_sym_a = sym
            # _held_other None (book unreadable) -> no pivot here, and the entry is NOT unguarded: place_entry
            # re-reads live positions + open orders right before submit, returns False if either is unreadable,
            # and refuses a symbol another tier holds or has an order on (day_trade_manager NO SAME-SIDE CO-HOLD).
            if _held_other is not None and sym in _held_other:
                # NO CO-HOLD (interim 2026-10-07): another tier holds this stock -> a long switches to a free 2x bull
                # ETF; a short BUYS a free liquid inverse ETF (Rafael 2026-10-07); no route -> skipped, reason logged.
                _piv_a = None
                _why_a = "another tier holds this symbol and no free ETF route (no co-hold)"
                try:
                    from strategy import day_tier_leverage as lev
                    if trigger.get("direction") == "long":
                        _etf_a = lev.etf_for_order(sym, _held_other) if lev.pivot_enabled() else None
                        if _etf_a:
                            from data.live_price import DELAYED_FEED_AGE_S, live_price
                            _ua, _ea = live_price(sym), live_price(_etf_a, max_age_s=DELAYED_FEED_AGE_S)
                            _piv_a = lev.leveraged_entry(decision, trigger, _etf_a,
                                                         _ua.price if _ua else None, _ea.price if _ea else None,
                                                         prior_close=_prior_close(sym))
                            if _piv_a is not None:
                                decision, trigger = _piv_a
                                order_sym_a = _etf_a
                                size = compute_day_tier_size(_etf_a, decision, trigger.get("entry_ref"), equity,
                                                             buying_power=buying_power, track="A")
                    elif lev.inverse_pivot_enabled():
                        _inv_a, _why_inv = _inverse_pivot(sym, decision, trigger, _held_other, equity, buying_power,
                                                          "A")
                        if _inv_a is not None:
                            decision, trigger, size, order_sym_a = _inv_a
                            _piv_a = (decision, trigger)
                        else:
                            _why_a = f"another tier holds this symbol; {_why_inv}"
                except Exception as _pa:  # noqa: BLE001
                    logger.warning("[%s] track-A co-hold pivot error (skip): %s", sym, _pa)
                    _piv_a = None
                if _piv_a is None or not size.get("size_ok"):
                    _log_direction_skip(sym, decision, trigger, _why_a, bar_id)
                    continue
                logger.info("[%s] track-A CO-HOLD PIVOT -> %s: %s", sym, order_sym_a, size.get("reason"))
            _dc_a = _direction_conflict(_dt_exp, order_sym_a, trigger.get("direction"))
            if _dc_a:
                _log_direction_skip(sym, decision, trigger, _dc_a, bar_id)
                continue
            if calls_used + per_entry_est > call_budget:
                capped = True
                break  # defer the remaining ENTERs to the next tick (bar_id idempotency preserves them)
            _pe_kw = {"decision_id": _ct_decision_id} if _ct_decision_id else {}
            if dtm.place_entry(order_sym_a, decision, trigger, size, bar_id=bar_id, equity=equity, **_pe_kw):
                entered += 1
                if _dt_exp is not None:
                    from strategy import day_tier_leverage as _lv
                    _u, _sg = _lv.exposure_sign(order_sym_a, str(trigger.get("direction") or "long"))
                    _dt_exp[_u] = _sg
            calls_used += per_entry_est
        except Exception as e:  # noqa: BLE001 — one symbol must never abort the tick
            logger.warning("[%s] day-tier entry loop error (non-fatal): %s", sym, e)

    # TRACK B — dynamic-mover MOMENTUM tier (LIVE, SPLIT decision 2026-09-22; behind DAYTRADE_TRACK_B_ENABLED,
    # no-op when False). Runs AFTER Track A so A keeps trading-API-budget priority, and only if A did not
    # already exhaust it (capped), and only inside track_b_in_window (the ~09:50-11:05 ET span where the
    # momentum trigger CAN fire — derived from its bar bounds; outside it every symbol WAITs, so skipping saves
    # ~15 data calls/tick for no lost signal). Per pre-registered mover: verified from-open IEX frame -> settled
    # daily context -> mover screen -> momentum trigger -> adapter -> size(track="B") -> place_entry. Track B
    # rides the SAME daytrade-tagged machinery Track A does (reconcile / flatten / EOD force-flat / shared -5%
    # tier kill / gross cap / SHARED 3-concurrency cap / OCO stop+2R / min-stop ATR gate), and place_entry caps a
    # Track-B entry at its budget share count AND caps open Track-B notional at that budget (exposure cap)
    # (DAYTRADE_TRACK_B_CASH_ONLY — never risk-upsized; note B shorts / a negative-cash account are still
    # margin-financed, the cap bounds EXPOSURE). One Track-B entry per symbol per ET day; a wall-clock budget
    # bounds the loop. The per-track B SUB-KILL is the deferred fast-follow (the `track` stamp shipped here records the
    # data it will be validated against). Frame + daily fetches are data-API calls (this process's _rate_gate);
    # place_entry is the trading-API budget consumer.
    entered_b = 0
    track_b_on = bool(getattr(config, "DAYTRADE_TRACK_B_ENABLED", False))
    track_b_window = False
    track_b_note = "api_budget" if (track_b_on and capped) else ""  # Track A already spent the call budget
    if track_b_on and not capped:
        try:  # an import failure disables Track B for THIS tick; it must never escape run_tick (exec seat F5)
            from strategy import day_tier_track_b as tb
            from strategy.day_tier_momentum_trigger import compute_momentum_trigger
            track_b_window = tb.track_b_in_window(datetime.now(ET))
        except Exception as e:  # noqa: BLE001
            logger.error("track-B unavailable this tick (import/window error): %s", e)
            track_b_window = False
            track_b_note = "import_error"
        b_today: set = set()
        b_day = datetime.now(ET).strftime("%Y%m%d")
        prev_session = None
        if track_b_window and time.monotonic() - _tick_t0 > _TRACK_B_TICK_BUDGET_S:
            track_b_window = False
            track_b_note = "tick_budget"  # no time left for Track B (incl. the calendar call) this tick
        if track_b_window:
            b_today = _track_b_symbols_today(dtm._load_state(), b_day)
            prev_session = _prev_session_date(datetime.now(ET))  # ONCE per tick (a failure is remembered)
            if not prev_session:
                track_b_window = False
                track_b_note = "calendar_unavailable"  # fail closed: no settled-context check -> no Track B
        for sym in (tb.track_b_universe() if track_b_window else []):
            if time.monotonic() - _tick_t0 > _TRACK_B_TICK_BUDGET_S:
                track_b_note = "tick_budget"
                logger.warning("track-B: tick wall-clock budget %.0fs reached — remaining symbols deferred to "
                               "the next tick", _TRACK_B_TICK_BUDGET_S)
                break
            if sym in b_today:
                continue  # one Track-B shot per symbol per ET day (matches the Rule-C replay)
            if calls_used + per_entry_est > call_budget:
                # Checked BEFORE any evaluation, so a budget-capped tick never USES a symbol's daily shot
                # (exec seat R3): the remaining symbols are genuinely deferred to the next tick.
                capped = True
                track_b_note = "api_budget"
                break
            try:
                now_et = datetime.now(ET)  # fresh per symbol: freshness + entry_ref reflect real time
                frame = tb.build_session_frame(sym, now_et)
                if frame is None:
                    continue  # pre-open / halt / non-contiguous / stale frame -> skip (fail-safe)
                if time.monotonic() - _tick_t0 > _TRACK_B_TICK_BUDGET_S:
                    track_b_note = "tick_budget"
                    break  # re-checked before the (possibly 2-fetch) daily context
                prior_close, avg_vol = _track_b_daily_context(sym, now_et, prev_session=prev_session)
                if prior_close is None:
                    continue
                screen = tb.screen_mover(sym, frame, prior_close, avg_vol, now_et)
                if not screen.get("is_mover"):
                    continue
                mom = compute_momentum_trigger(sym, screen.get("gap_direction"), frame)
                if mom.get("trigger") != "ENTER":
                    continue
                # The Track-B shot for this symbol is USED at the ENTER signal (before sizing / min-stop /
                # caps), persisted so later ticks skip it — exactly the Rule-C replay's rule (exec seat R2).
                b_today.add(sym)
                _mark_track_b_signal(dtm, sym, b_day)
                decision_b, trigger_b = tb.momentum_to_entry(mom, screen.get("gap_direction"))
                # Same owner rules as Track A, without the counter-trend fade exception (Track B is momentum):
                # the direction must match the symbol's Layer-A trend side (read here — Track B has no Layer-A
                # read of its own) and the short-term indicators must agree. Unreadable -> no trade (fail closed).
                _side_b = _side_for(sym)
                _gate_b = _entry_direction_gate(sym, _side_b, trigger_b.get("direction"), trigger_b.get("mode"),
                                                allow_counter_fade=False)
                if not _gate_b["ok"] and _gate_blocks():
                    _log_direction_skip(sym, {**decision_b, "side": _side_b},
                                        {**trigger_b, "alignment": _gate_b["alignment"]}, _gate_b["reason"], bar_id)
                    continue
                # CEO order 2026-10-06: the gate verdict is recorded, not blocking (see _gate_blocks).
                trigger_b = {**trigger_b, "counter_trend": _is_counter(_side_b, trigger_b.get("direction")),
                             "alignment": _gate_b["alignment"], "gate_ok": _gate_b["ok"],
                             "gate_reason": _gate_b["reason"]}
                decision_b = {**decision_b, "side": _side_b}
                _wd_ok_b, _wd_why_b = _watch_day_ok(sym, _side_b, trigger_b.get("direction"))
                if not _wd_ok_b:
                    _log_direction_skip(sym, decision_b, trigger_b, _wd_why_b, bar_id)
                    continue
                if trigger_b.get("counter_trend") and str(trigger_b.get("direction")) == "short":
                    trigger_b = {**trigger_b, "watch_day": True, "watch_day_reason": _wd_why_b}
                size_b = compute_day_tier_size(sym, decision_b, trigger_b.get("entry_ref"), equity,
                                               buying_power=buying_power, track="B")
                # LEVERAGED PIVOT (Rafael CEO directive 2026-10-06; strategy/day_tier_leverage.py): a Track-B LONG trades
                # its 2x bull ETF when the setup is 10/10 or the budget cannot buy one share of the stock. Any failure
                # -> no pivot (today's behaviour). The 10/10 verdict is recorded on the decision.
                order_sym = sym
                try:
                    from strategy import day_tier_leverage as lev
                    # 10/10 applies to longs AND shorts (CEO 2026-10-07: "full size, long or short"); the alignment
                    # and trend checks inside are computed for the trade's own direction.
                    _ten, _ten_why = lev.is_ten_of_ten(screen, mom, _gate_b)
                    decision_b = {**decision_b, "ten_of_ten": _ten, "ten_of_ten_reason": _ten_why}
                    _etf = None
                    if lev.pivot_enabled() and trigger_b.get("direction") == "long" and lev.bull_etf_for(sym):
                        # ETFs already held by ANOTHER tier are skipped (co-hold close hazard); unreadable book -> no pivot
                        _etf = lev.etf_for_order(sym, _held_other)
                    # Rafael 2026-10-06: trade the STOCK when the budget buys 2+ shares; switch to the 2x ETF only
                    # when it buys 0 or 1. For a 10/10 setup the budget is the max-size notional (single-name cap,
                    # and the thin-name cap for non-deep names) — the same rooms place_entry sizes max_size to.
                    try:
                        _px_u = float(trigger_b.get("entry_ref") or 0.0)
                        if _ten:
                            _bud = float(getattr(config, "DAYTRADE_MAX_SINGLE_NAME_NOTIONAL_PCT", 0.65)) * equity
                            if sym not in set(getattr(config, "DAYTRADE_DEEP_LIQUIDITY_SYMBOLS", []) or []):
                                _bud = min(_bud, float(getattr(config, "DAYTRADE_THIN_NAME_MAX_NOTIONAL_USD", 1300.0)))
                        else:
                            _bud = float(size_b.get("budget") or 0.0)
                        _stock_shares = int(_bud // _px_u) if _px_u > 0 else 0
                    except (TypeError, ValueError, ZeroDivisionError):
                        _stock_shares = 0
                    if _ten and _stock_shares >= 2 and size_b.get("size_ok"):
                        size_b = {**size_b, "max_size": True}   # 10/10 on the stock itself, at maximum size
                    # ...and ALWAYS switch when another tier holds the stock (no co-hold, interim 2026-10-07)
                    if _etf and (_stock_shares <= 1 or (_held_other is not None and sym in _held_other)):
                        from data.live_price import DELAYED_FEED_AGE_S, live_price
                        # thin ETFs (GGLL, AVL, ...) can go minutes without an IEX print: accept up to the delayed-feed age
                        _up, _ep = live_price(sym), live_price(_etf, max_age_s=DELAYED_FEED_AGE_S)
                        _piv = lev.leveraged_entry(decision_b, trigger_b, _etf,
                                                   _up.price if _up else None, _ep.price if _ep else None,
                                                   prior_close=prior_close)
                        if _piv is not None:
                            decision_b, trigger_b = _piv
                            order_sym = _etf
                            size_b = compute_day_tier_size(_etf, decision_b, trigger_b.get("entry_ref"), equity,
                                                           buying_power=buying_power, track="B")
                            if _ten and size_b.get("size_ok"):
                                size_b = {**size_b, "max_size": True}   # 10/10 -> maximum size (place_entry)
                            logger.info("[%s] track-B LEVERAGED PIVOT -> %s (%s; stock buys %d sh): %s", sym, _etf,
                                        "10/10" if _ten else "budget", _stock_shares, size_b.get("reason"))
                        else:
                            logger.info("[%s] track-B leveraged pivot to %s not possible (price/geometry) — stock path",
                                        sym, _etf)
                    # SHORT -> BUY the inverse ETF (Rafael 2026-10-07): when another tier holds the stock, or the budget
                    # buys 0-1 shares. Otherwise the stock itself is shorted. A failed route on a co-held stock is
                    # skipped (place_entry would refuse the co-hold anyway); on a budget route it keeps the stock path.
                    _coheld_b = _held_other is not None and sym in _held_other
                    if (trigger_b.get("direction") == "short" and lev.inverse_pivot_enabled()
                            and (_coheld_b or _stock_shares <= 1)):
                        _inv_b, _why_inv_b = _inverse_pivot(sym, decision_b, trigger_b, _held_other, equity,
                                                            buying_power, "B", prior_close=prior_close)
                        if _inv_b is not None:
                            decision_b, trigger_b, size_b, order_sym = _inv_b
                            if _ten and size_b.get("size_ok"):   # never on a size_ok=False dict
                                size_b = {**size_b, "max_size": True}   # 10/10 short -> maximum size on the ETF
                            logger.info("[%s] track-B INVERSE ROUTE -> %s (%s; stock buys %d sh): %s", sym, order_sym,
                                        "co-held" if _coheld_b else "budget", _stock_shares, size_b.get("reason"))
                        elif _coheld_b:
                            _log_direction_skip(sym, decision_b, trigger_b, f"another tier holds this symbol; "
                                                f"{_why_inv_b}", bar_id)
                            continue
                        else:
                            logger.info("[%s] track-B inverse route not taken (%s) — shorting the stock", sym,
                                        _why_inv_b)
                except Exception as _pe:  # noqa: BLE001 — a pivot failure keeps the stock path
                    logger.warning("[%s] track-B leveraged pivot error (stock path): %s", sym, _pe)
                if size_b.get("size_ok"):
                    # CEO order 2026-10-06: the budget is never the reason a Track-B trade is skipped. The Track-B budget
                    # is floored at ONE share of the instrument actually ordered (stock or ETF) at the worst marketable
                    # limit (live-price band + entry slippage), so the min-1-share order survives the Track-B budget
                    # cap; the account caps and the daily dollar budget still decide.
                    try:
                        _one = (float(trigger_b.get("entry_ref") or 0.0)
                                * (1.0 + float(getattr(config, "DAYTRADE_LIVE_PRICE_SANITY_PCT", 0.05)))
                                * (1.0 + float(getattr(config, "DAYTRADE_ENTRY_SLIPPAGE_PCT", 0.002))))
                        if _one > float(size_b.get("budget") or 0.0):
                            size_b = {**size_b, "budget": round(_one, 2)}
                    except (TypeError, ValueError):
                        pass
                if not size_b.get("size_ok"):
                    continue
                _dc_b = _direction_conflict(_dt_exp, order_sym, trigger_b.get("direction"))
                if _dc_b:
                    _log_direction_skip(sym, decision_b, trigger_b, _dc_b, bar_id)
                    continue
                if time.monotonic() - _tick_t0 > _TICK_CADENCE_S - _PLACE_ENTRY_RESERVE_S:
                    track_b_note = "tick_budget"
                    logger.warning("[%s] track-B ENTER not placed — < %.0fs left in the tick (the signal's "
                                   "daily shot is already used)", sym, _PLACE_ENTRY_RESERVE_S)
                    break
                _pe_kw_b: dict = {}
                if trigger_b.get("counter_trend"):
                    # never-mask-a-loss: a counter-trend Track-B trade's tag must be DURABLE before the order exists
                    _ct_b = _record_counter_trend_intent(order_sym, decision_b, trigger_b, bar_id)
                    if not _ct_b:
                        continue
                    _pe_kw_b["decision_id"] = _ct_b
                if dtm.place_entry(order_sym, decision_b, trigger_b, size_b, bar_id=bar_id, equity=equity, **_pe_kw_b):
                    entered_b += 1
                    if _dt_exp is not None:
                        from strategy import day_tier_leverage as _lv_b
                        _u_b, _sg_b = _lv_b.exposure_sign(order_sym, str(trigger_b.get("direction") or "long"))
                        _dt_exp[_u_b] = _sg_b
                calls_used += per_entry_est
            except Exception as e:  # noqa: BLE001 — one symbol must never abort the tick
                logger.warning("[%s] track-B entry loop error (non-fatal): %s", sym, e)

    if capped:
        logger.warning("day-tier: per-tick API-call budget (%d, ~%d used) reached — deferred remaining "
                       "entries to the next tick", call_budget, calls_used)
    _touch_heartbeat("scan")
    return {"phase": "scan", "entered": entered, "entered_b": entered_b, "entered_m": entered_m,
            "track_m_note": track_m_note, "track_b": track_b_on,
            "track_b_window": track_b_window, "track_b_note": track_b_note,
            "universe": len(universe), "capped": capped, "calls_est": calls_used,
            "mins_to_close": round(mins_to_close, 1) if mins_to_close is not None else None,
            "reconcile": recon}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Apply the PAPER profile so THIS process's config matches the running paper account (the day-tier
    # is paper-only — broker.paper=True is hardcoded; DAYTRADE_ENABLED is armed only under --profile
    # paper). This sets MAX_DAILY_LOSS_PCT=0.07 here so the account-terms kill nesting is coherent in
    # this process and does not depend on main.py's separate process (masked-loss seat 2026-09-08).
    # Pure attribute mutation (mirrors main.py:290-293) — no side effects; the day-tier reads only
    # DAYTRADE_* constants, so this changes NO day-tier behavior, only what the assertion below sees.
    import config
    try:
        for _k, _v in config.PROFILES.get("paper", {}).items():
            setattr(config, _k, _v)
        config.ACTIVE_PROFILE = "paper"
    except Exception as e:  # noqa: BLE001
        logger.error("day-tier runner: failed to apply paper profile — refusing to run: %s", e)
        return 1
    # Carry our OWN account-terms kill-nesting check (do NOT depend on main.py's validate_config): the
    # day-tier kill MUST be nested strictly below the account daily kill, or a mis-set could let the tier
    # lose more than the account tolerates before halting. Fail CLOSED.
    _tier_kill = float(getattr(config, "DAYTRADE_TIER_KILL_EQUITY_PCT", 0.04))
    _acct_kill = float(getattr(config, "MAX_DAILY_LOSS_PCT", 0.03))
    if not (0 < _tier_kill < _acct_kill):
        logger.error("day-tier kill (%.4f) not nested below account kill (%.4f) — refusing to run",
                     _tier_kill, _acct_kill)
        return 1
    # flock singleton: if a prior 2-min tick still runs (holding the Alpaca connection / a re-peg),
    # exit immediately rather than overlap and double-submit (reliability seat C1).
    try:
        _lf = open(_LOCK, "w")
        fcntl.flock(_lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        logger.info("day-tier runner: another instance holds the lock — exiting")
        return 0
    try:
        s = run_tick()
        print(json.dumps(s, default=str, indent=2))
        return 0
    except Exception as e:  # noqa: BLE001 — a runner must exit cleanly, never traceback
        logger.error("day-tier runner tick failed fatally: %s", e)
        return 1
    finally:
        try:
            fcntl.flock(_lf.fileno(), fcntl.LOCK_UN)
            _lf.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
