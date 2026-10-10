# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/day_promotion.py — Day-tier -> Swing-tier promotion (RISK-PATH; CEO order 2026-10-09).

Design: logs/design_records/tier_safety_and_cohold_plan_2026-10-08.md (D5 + round-2 timing) and the 2026-10-09
build review (board Thorp/Brandt + Taleb/Harris, Gro, GAI). Three steps, each restart-safe:

  1. DECIDE (Day runner, 3:56 PM ET window, once per day) — select_promotions(): an open Day-tier STOCK LONG lot is
     promoted when (P2) it is at least DAYTRADE_PROMOTION_MIN_R x its Day-stop distance in profit on the last trade,
     (P3) the daily trend side is LONG, (P4) shares x (price - Swing stop) <= DAYTRADE_PROMOTION_MAX_RISK_PCT of equity,
     with Swing stop = Day entry - INTRADAY_STOP_ATR_MULT x daily ATR (the CEO's "Swing stop from the Day cost basis"),
     (P5) both overnight limbs of Architecture Invariant #11 hold with the lot added, the aggregate overnight
     at-risk of promoted + existing Swing lots stays <= DAYTRADE_PROMOTION_AGG_RISK_PCT of equity, and at most
     DAYTRADE_PROMOTION_MAX_PER_DAY lots. No partial promotion. Never a leveraged/inverse ETF; no shorts in v1.
     The lot is only MARKED (state "promote_pending"): the Day tier's 3:58 exit and after-hours exit skip it and its
     own DAY stop keeps protecting it until 4:00.
  2. HAND OFF (Day runner, first after-close ticks) — complete_handoffs(): once the Day exit legs are confirmed
     terminal, a stop/target that FILLED before 4:00 is booked from its real fill and the lot is NOT promoted; a lot
     still fully held is booked closed in the Day journal at the live mark ("promoted_to_swing") and a hand-off
     record is written (written FIRST, completed after the booking, so a crash at any point resumes, never strands).
     A lot that is flat or partly gone without a recoverable fill reverts to the Day tier's own after-hours handling.
  3. ADOPT (main bot, every cycle) — adopt_promotions(): a "ready" hand-off becomes a Swing tracker trade (entry =
     take-over mark, so the Day/Swing P&L never double-counts; Day cost kept on record; stop = the Swing stop from
     the Day cost; _promoted_from_day_tier=True so no break-even move shakes it out) and gets its GTC stop at once.

Ownership-ledger note: the ledger attributes shares by order tag (DT-); a completed hand-off records a Day -> Swing
transfer (execution/tier_transfers.py) that the ledger replay applies at the hand-off time.
Kill flag: DAYTRADE_PROMOTION_ENABLED (only an explicit False disables). Never raises into a caller.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
_HANDOFF = Path(__file__).resolve().parent.parent / "data" / "state" / "day_promotions.json"
_DECIDED_KEY = "_promotion_decided_date"


def _cfg(name: str, default):
    return getattr(config, name, default)


def enabled() -> bool:
    return _cfg("DAYTRADE_PROMOTION_ENABLED", True) is not False


# ── hand-off file (atomic; writers: the flock'd Day runner (pending/ready) and the main bot (adopted)) ────────────
def _load() -> "dict | None":
    """The hand-off records keyed by Day trade_id, or None when the file exists but cannot be read (callers then
    do nothing — never treat an unreadable hand-off as empty). Never raises."""
    try:
        if not _HANDOFF.exists():
            return {}
        d = json.loads(_HANDOFF.read_text())
        return d if isinstance(d, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.error("day-promotion hand-off unreadable: %s", e)
        return None


def _write(d: dict) -> bool:
    os.makedirs(_HANDOFF.parent, exist_ok=True)
    tmp = _HANDOFF.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _HANDOFF)
    return True


def _update(tid: str, fields: dict) -> bool:
    """Merge `fields` into the hand-off record `tid` under an exclusive file lock: re-read, merge, write. Both
    processes (the Day runner and the main bot) write this file, so a whole-file rewrite from a stale copy could
    drop another lot's record (cold-2nd 2026-10-09). False on any failure (callers retry next tick). Never raises."""
    import fcntl
    try:
        os.makedirs(_HANDOFF.parent, exist_ok=True)
        with open(str(_HANDOFF) + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                cur = _load()
                if cur is None:
                    return False
                rec = dict(cur.get(tid) or {})
                rec.update(fields)
                cur[tid] = rec
                return _write(cur)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
    except Exception as e:  # noqa: BLE001
        logger.error("day-promotion hand-off write FAILED (%s): %s", tid, e)
        return False


def _set_day_state(tid: str, **fields) -> bool:
    """Re-read the Day state file and update ONLY the entry record whose coid is `tid` (never a stale whole-file copy
    — cold-2nd 2026-10-09). A field set to None is removed. Never raises."""
    from execution import day_trade_manager as dtm
    try:
        st = dtm._load_state()
        for k, v in st.items():
            if k.startswith("entry::") and isinstance(v, dict) and v.get("coid") == tid:
                for fk, fv in fields.items():
                    if fv is None:
                        v.pop(fk, None)
                    else:
                        v[fk] = fv
                return dtm._save_state(st)
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("day-promotion state write failed (%s): %s", tid, e)
        return False


def _slack(msg: str) -> None:
    try:
        from alerts import send_slack
        send_slack(msg)
    except Exception as e:  # noqa: BLE001
        logger.error("day-promotion Slack failed: %s | %s", e, msg)


def _is_etf(symbol: str) -> bool:
    """True for any leveraged/inverse ETF the bot knows (never promoted overnight — CEO D5). Unreadable map -> True
    (fail toward the Day exit)."""
    sym = str(symbol or "").upper()
    if sym in set(_cfg("LEVERAGED_3X_TICKERS", set()) or set()):
        return True
    try:
        from strategy import day_tier_leverage as _lev
        return _lev.exposure_sign(sym, "long")[0] != sym
    except Exception:  # noqa: BLE001
        return True


def _daily_atr(symbol: str, price: float) -> "float | None":
    """Daily ATR in dollars (same formula as orphan adoption: calculate_atr % x price). None when unavailable."""
    try:
        from data.fetcher import fetch_bars
        from data.premarket import calculate_atr
        df = fetch_bars(symbol, config.TF_DAILY, num_bars=config.ATR_PERIOD + 5)
        if df is None or df.empty:
            return None
        a = calculate_atr(df, config.ATR_PERIOD) / 100.0 * float(price)
        return a if math.isfinite(a) and a > 0 else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] day-promotion ATR unavailable: %s", symbol, e)
        return None


def _swing_open_risk() -> "float | None":
    """Dollars at risk on the Swing tracker's open trades (qty x |entry - stop|) from trade_log.json; None when
    unreadable (callers then promote nothing)."""
    try:
        from execution.portfolio_tracker import TRADE_LOG_FILE
        d = json.loads(Path(TRADE_LOG_FILE).read_text())
        tot = 0.0
        for t in (d.get("open") or {}).values() if isinstance(d.get("open"), dict) else (d.get("open") or []):
            q = abs(float(t.get("qty_remaining", t.get("qty", 0)) or 0))
            e = float(t.get("entry_price") or 0)
            s = float(t.get("trail_stop") or t.get("stop") or 0)
            if q > 0 and e > 0 and s > 0:
                tot += q * abs(e - s)
        return tot
    except Exception as e:  # noqa: BLE001
        logger.warning("day-promotion: Swing open risk unreadable (%s) — no promotions", e)
        return None


# ── 1. DECIDE (Day runner, 3:56 window) ───────────────────────────────────────────────────────────────────────────
def select_promotions(equity: float) -> list:
    """Mark qualifying open Day-tier lots "promote_pending" (once per ET day). Returns the promoted symbols. Every
    rejection is logged with its reason. Never raises."""
    out: list = []
    if not enabled():
        return out
    from execution import broker, day_trade_manager as dtm
    try:
        state = dtm._load_state()
        try:
            _unreadable = not state and dtm._STATE.exists() and dtm._STATE.stat().st_size > 2
        except Exception:  # noqa: BLE001
            _unreadable = True
        if _unreadable:                      # never latch over (and wipe) a state file that failed to read
            logger.error("day-promotion: Day state unreadable — no promotions (normal Day exit)")
            return out
        today = f"{datetime.now(ET):%Y%m%d}"
        if state.get(_DECIDED_KEY) == today:
            return out
        state[_DECIDED_KEY] = today
        if not dtm._save_state(state):
            return out                       # cannot latch the decision -> no promotion (fail toward the Day exit)
        if not (math.isfinite(equity) and equity > 0):
            logger.warning("day-promotion: equity invalid — no promotions")
            return out
        targets = dtm._flatten_targets()
        if not targets:
            return out
        positions = broker.get_open_positions() or []
        pos_by = {getattr(p, "symbol", None): p for p in positions}
        gross_all = sum(abs(float(getattr(p, "market_value", 0) or 0)) for p in positions)
        day_mv = 0.0                         # ONLY the Day lot's own shares leave the overnight book (cold-2nd 2026-10-09:
        for s, t in targets.items():         # a co-held Swing/QHM share on the same symbol stays overnight)
            p_ = pos_by.get(s)
            try:
                held = abs(float(getattr(p_, "qty", 0) or 0)) if p_ is not None else 0.0
                if held > 0:
                    day_mv += min(float(t.get("qty") or 0), held) * abs(float(getattr(p_, "market_value", 0) or 0)) / held
            except Exception:  # noqa: BLE001
                continue                     # unreadable -> nothing subtracted (stricter: fewer promotions)
        try:
            from execution.swing_breakout_manager import _protected_notional
            protected = _protected_notional(positions)
        except Exception:  # noqa: BLE001
            protected = 0.0                  # unreadable -> QHM/F6 count in the stricter limb (fewer promotions)
        overnight = gross_all - day_mv       # what stays overnight if no Day lot is promoted
        swing_risk = _swing_open_risk()
        if swing_risk is None:
            return out
        agg_risk = swing_risk
        max_n = int(_cfg("DAYTRADE_PROMOTION_MAX_PER_DAY", 3))
        k_total = float(_cfg("SWING_BREAKOUT_TOTAL_OVERNIGHT_K", 1.75))
        from strategy.day_tier_side import compute_side_bias
        from data.live_price import live_price
        for sym, tgt in targets.items():
            if len(out) >= max_n:
                logger.info("[%s] day-promotion: daily cap %d reached — Day exit", sym, max_n)
                continue
            why = ""
            px = atr = swing_stop = at_risk = mv = 0.0
            try:
                rec = next((v for k, v in state.items() if k.startswith("entry::") and isinstance(v, dict)
                            and v.get("coid") == tgt.get("trade_id")), None)
                pos = pos_by.get(sym)
                qty = int(tgt.get("qty") or 0)
                entry = float(tgt.get("entry_price") or 0)
                day_stop = float((rec or {}).get("stop_px") or 0)
                if str(tgt.get("side")) != "long":
                    why = "short lot (v1 promotes longs only)"
                elif _is_etf(sym):
                    why = "leveraged/inverse ETF (never held overnight)"
                elif rec is None or rec.get("state") not in ("filled", "protected"):
                    why = f"state {(rec or {}).get('state')!r} is not a settled open lot"
                elif rec.get("pending_exit_order_id"):
                    why = "an exit order is pending"
                elif pos is None or getattr(pos, "side", None) != "long" or abs(int(float(pos.qty))) != qty or qty < 1:
                    why = "broker position does not equal the Day lot (co-held or partial)"
                elif not (entry > 0 and 0 < day_stop < entry):
                    why = "Day entry/stop unreadable"
                if not why:
                    lp = live_price(sym)
                    px = float(lp.price) if lp is not None else 0.0
                    r_day = entry - day_stop
                    min_r = float(_cfg("DAYTRADE_PROMOTION_MIN_R", 0.5))
                    if not (px > 0):
                        why = "no fresh last trade"
                    elif px - entry < min_r * r_day:
                        why = f"profit ${px - entry:.2f} < {min_r}R (${min_r * r_day:.2f})"
                if not why:
                    side = (compute_side_bias(sym) or {}).get("side")
                    if side != "LONG":
                        why = f"daily trend side {side!r} is not LONG"
                if not why:
                    _atr = _daily_atr(sym, entry)
                    if _atr is None:
                        why = "daily ATR unavailable"
                    else:
                        atr = _atr
                if not why:
                    swing_stop = round(entry - float(_cfg("INTRADAY_STOP_ATR_MULT", 1.25)) * atr, 2)
                    at_risk = qty * (px - swing_stop)
                    mv = abs(float(getattr(pos, "market_value", 0) or 0)) or qty * px
                    if not (swing_stop > 0 and swing_stop < px):
                        why = f"Swing stop ${swing_stop:.2f} invalid vs price ${px:.2f}"
                    elif at_risk > float(_cfg("DAYTRADE_PROMOTION_MAX_RISK_PCT", 0.02)) * equity:
                        why = f"risk to the Swing stop ${at_risk:.2f} > {_cfg('DAYTRADE_PROMOTION_MAX_RISK_PCT', 0.02):.0%} of equity"
                    elif agg_risk + at_risk > float(_cfg("DAYTRADE_PROMOTION_AGG_RISK_PCT", 0.06)) * equity:
                        why = f"aggregate overnight risk ${agg_risk + at_risk:.2f} would exceed {_cfg('DAYTRADE_PROMOTION_AGG_RISK_PCT', 0.06):.0%} of equity"
                    elif overnight + mv - protected > equity:
                        why = "Invariant #11 swing/day overnight limb (100% of equity) would be exceeded"
                    elif overnight + mv > k_total * equity:
                        why = f"Invariant #11 total overnight limb ({k_total}x equity) would be exceeded"
                if why or rec is None:
                    logger.info("[%s] day-promotion: NOT promoted — %s", sym, why or "no state record")
                    continue
                _promo = {"decided_ts": datetime.now(PT).isoformat(), "decision_price": px, "day_entry": entry,
                          "day_stop": day_stop, "atr": atr, "swing_stop": swing_stop,
                          "target": round(entry + float(_cfg("INTRADAY_TARGET_ATR_MULT", 2.5)) * atr, 2), "qty": qty,
                          "prior_state": rec.get("state")}
                if not _set_day_state(str(tgt.get("trade_id") or ""), state="promote_pending", promote=_promo):
                    logger.error("[%s] day-promotion: state write failed — Day exit", sym)
                    continue
                overnight += mv
                agg_risk += at_risk
                out.append(sym)
                logger.warning("[%s] day-promotion: PROMOTE PENDING — %d sh, last $%.2f vs Day entry $%.2f, Swing stop "
                               "$%.2f (risk $%.2f); the Day stop protects it until 4:00, hand-off after the close",
                               sym, qty, px, entry, swing_stop, at_risk)
                try:
                    import trade_logger
                    trade_logger.log_event("promotion_decision", symbol=sym, price=px, size=qty, tier="daytrade",
                                           day_entry=entry, day_stop=day_stop, swing_stop=swing_stop,
                                           at_risk=round(at_risk, 2), trade_id=tgt.get("trade_id"))
                except Exception as _le:  # noqa: BLE001
                    logger.warning("[%s] promotion_decision event not logged: %s", sym, _le)
            except Exception as e:  # noqa: BLE001 — one lot never blocks the others; unpromoted = normal Day exit
                logger.warning("[%s] day-promotion decision failed (Day exit): %s", sym, e)
        return out
    except Exception as e:  # noqa: BLE001
        logger.error("day-promotion select failed (all lots take the Day exit): %s", e)
        return out


# ── 2. HAND OFF (Day runner, after the close) ─────────────────────────────────────────────────────────────────────
def _revert(tid: str, rec: dict, sym: str, why: str) -> None:
    """Hand the lot back to the Day tier (its after-hours exit / reconcile see it again) and page."""
    prior = (rec.get("promote") or {}).get("prior_state") or ("protected" if rec.get("stop_order_id") else "filled")
    _set_day_state(tid, state=prior, promote=None)
    _slack(f":warning: [{sym}] Day-tier promotion CANCELLED — {why}. The Day tier's after-hours exit takes the lot.")


def _booked_promotion(tid: str) -> bool:
    """True when the Day journal already holds OUR "promoted_to_swing" exit for this trade (so a crash after the journal
    write is never re-read as "its stop filled before 4:00", nor booked twice). False on any doubt."""
    try:
        from strategy import day_tier_logger
        events, _readable = day_tier_logger.read_events_checked(tid)   # positive proof needs no fully clean file
        return any(e.get("event") == "exit_fill" and e.get("exit_reason") == "promoted_to_swing"
                                      for e in events)
    except Exception:  # noqa: BLE001
        return False


def _record_ledger_transfer(tid: str, h: dict) -> bool:
    """Record the Day -> Swing hand-over in the tier-transfer journal so the ownership ledger moves these shares
    from Day to Swing at the take-over mark (execution/tier_transfers.py). Idempotent by trade id. Recorded only once
    the Swing tracker has ADOPTED the lot (cold-2nd 2026-10-10: a ledger that shows the lot as non-Day while it is
    only "ready" lets a main-bot restart adopt it as an orphan at the Day cost — double-counting the Day result).
    Called by adopt_promotions and retried on every Day-runner pass. Never raises."""
    try:
        from execution import tier_transfers
        qty = int(h.get("adopted_qty") or h.get("qty") or 0)      # the shares the Swing tier actually adopted
        mark = float(h.get("take_over_mark") or 0)
        if qty < 1 or not mark > 0:
            return False
        try:
            when = datetime.fromisoformat(str(h.get("adopted_ts") or h.get("ready_ts") or h.get("ts")))
        except (TypeError, ValueError):
            return False                    # no hand-over time -> never stamp "now" (could move a later Day lot)
        return tier_transfers.record_transfer(tid, str(h.get("symbol") or ""), float(qty), "daytrade", "intraday",
                                              mark, when=when, source="day_promotion")
    except Exception as e:  # noqa: BLE001
        logger.error("[%s] promotion ledger transfer not recorded: %s", h.get("symbol"), e)
        return False


def _past_close_min(now_et: datetime) -> float:
    """Minutes since 4:00 PM ET today (negative before). Half days are not modelled; the deadline is only a backstop."""
    return (now_et.hour * 60 + now_et.minute + now_et.second / 60.0) - 16 * 60


def complete_handoffs() -> dict:
    """Finish every "promote_pending" lot once the market is closed. Returns a summary. Never raises."""
    summary = {"promoted": 0, "exited": 0, "reverted": 0, "waiting": 0}
    from execution import broker, day_trade_manager as dtm
    from strategy import day_tier_logger
    try:
        pend = [dict(v) for k, v in dtm._load_state().items() if k.startswith("entry::") and isinstance(v, dict)
                and v.get("state") == "promote_pending"]
        deadline = float(_cfg("DAYTRADE_PROMOTION_HANDOFF_DEADLINE_MIN", 20))
        now_et = datetime.now(ET)
        past_deadline = _past_close_min(now_et) > deadline
        for rec in pend:
            sym = str(rec.get("symbol") or "")
            tid = str(rec.get("coid") or "")
            p = rec.get("promote") or {}
            try:   # a lot decided on an earlier day (stuck over a restart / overnight) is past its deadline too
                _old = datetime.fromisoformat(str(p.get("decided_ts"))).astimezone(ET).date() < now_et.date()
            except Exception:  # noqa: BLE001
                _old = False
            late = past_deadline or _old
            qty = int(p.get("qty") or rec.get("fill_qty") or 0)
            entry = float(p.get("day_entry") or rec.get("fill_px") or 0)
            hand = _load()
            if hand is None:                      # hand-off file present but unreadable: never guess its status
                summary["waiting"] += 1
                continue
            h = hand.get(tid) or {}
            try:
                tgt = {"symbol": sym, "side": "long", "qty": qty, "entry_price": entry, "trade_id": tid,
                       "stop_order_id": str(rec.get("stop_order_id") or ""), "tp_order_id": str(rec.get("tp_order_id") or ""),
                       "oco_order_id": str(rec.get("oco_order_id") or "")}
                if h.get("status") in ("ready", "adopted"):
                    # Already booked and handed over: only the Day state marker may be missing. Never rebook or revert.
                    _set_day_state(tid, state="promoted")
                    continue
                ours = _booked_promotion(tid)
                closed = dtm._closed_in_log(tid) or ours
                if closed and not ours and h.get("status") != "booking":
                    # Booked closed WITHOUT a promotion hand-off (its own stop/target filled): never promote it.
                    _set_day_state(tid, state="flattened_no_stop")
                    summary["exited"] += 1
                    continue
                if not closed:
                    if h.get("status") != "booking" and late:
                        # Still unresolved well after the close (legs/position unreadable): never leave it ownerless.
                        _revert(tid, rec, sym, f"hand-off not completed within {deadline:.0f} min of the close")
                        summary["reverted"] += 1
                        continue
                    if dtm._cancel_recorded_exit_legs_confirmed(sym) is not True:
                        summary["waiting"] += 1            # a Day leg may still be live — never hand over a lot it can sell
                        continue
                    sf = dtm._record_confirmed_stop_exit(tgt)
                    if sf is None:
                        summary["waiting"] += 1
                        continue
                    if sf is True:                         # its stop/target filled before 4:00 — booked from the fill
                        _set_day_state(tid, state="flattened_no_stop")
                        summary["exited"] += 1
                        _slack(f":information_source: [{sym}] Day lot marked for promotion closed on its own stop/target "
                               f"before 4:00 — booked from the real fill, not promoted.")
                        continue
                    try:
                        pos = broker.get_open_position(sym)
                    except Exception as _pe:  # noqa: BLE001
                        logger.warning("[%s] day-promotion hand-off: position unreadable (%s) — next tick", sym, _pe)
                        summary["waiting"] += 1
                        continue
                    if pos is None or getattr(pos, "side", None) != "long" or abs(int(float(pos.qty))) != qty:
                        _revert(tid, rec, sym, "the broker position no longer equals the Day lot")
                        summary["reverted"] += 1
                        continue
                    mark = float(h.get("take_over_mark") or 0) or abs(float(getattr(pos, "current_price", 0) or 0))
                    if not (mark > 0 and entry > 0):
                        summary["waiting"] += 1
                        continue
                    if h.get("status") != "booking" and not (0 < float(p.get("swing_stop") or 0) < mark):
                        # The take-over price is already at/through the Swing stop (e.g. an after-close drop): the Swing
                        # tier could not hold it with a valid stop -> the Day tier's after-hours exit sells it.
                        _revert(tid, rec, sym, f"take-over price ${mark:.2f} is at/below the Swing stop "
                                               f"${float(p.get('swing_stop') or 0):.2f}")
                        summary["reverted"] += 1
                        continue
                    if h.get("status") != "booking":
                        if not _update(tid, {**p, "symbol": sym, "trade_id": tid, "qty": qty, "day_entry": entry,
                                             "take_over_mark": mark, "status": "booking",
                                             "ts": datetime.now(PT).isoformat()}):
                            summary["waiting"] += 1
                            continue
                    realized = round((mark - entry) * qty, 2)
                    if not day_tier_logger.log_exit_fill(tid, sym, order_id="", exit_reason="promoted_to_swing",
                                                         fill_price=round(mark, 4), fill_qty=float(qty),
                                                         market_price_at_exit=round(mark, 4), realized_pnl=realized):
                        summary["waiting"] += 1
                        if late:
                            dtm._page_once_today(sym, "promotion_booking_stuck",
                                                 f"[{sym}] promoted Day lot could not be booked over to the Swing tier "
                                                 f"(journal write failing) — no stop after 4:00. Check now.")
                        continue
                    try:
                        import trade_logger
                        trade_logger.log_event("exit", symbol=sym, price=mark, size=qty, data_source="daytrade",
                                               tier="daytrade", exit_reason="promoted_to_swing", trade_id=tid,
                                               realized_pnl=realized)
                    except Exception as _le:  # noqa: BLE001
                        logger.warning("[%s] promotion exit event not logged: %s", sym, _le)
                # Booked as promoted (this tick or a previous one): hand-off "ready" FIRST, then the Day state marker —
                # a failure between the two leaves "ready" + promote_pending, which the branch above finishes (cold-2nd
                # 2026-10-09: the reverse order could strand a booked lot with no owner).
                h = (_load() or {}).get(tid) or {}
                if h.get("status") == "booking":
                    if not _update(tid, {"status": "ready", "ready_ts": datetime.now(PT).isoformat()}):
                        summary["waiting"] += 1
                        continue
                    summary["promoted"] += 1
                    _mk = float(h.get("take_over_mark") or 0)
                    _slack(f":arrow_up: [{sym}] PROMOTED Day -> Swing: {qty} sh, Day cost ${entry:.2f}, take-over "
                           f"${_mk:.2f} (Day result ${(_mk - entry) * qty:+.2f}), Swing stop "
                           f"${float(h.get('swing_stop') or 0):.2f}.")
                _set_day_state(tid, state="promoted")
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] day-promotion hand-off failed (next tick): %s", sym, e)
                summary["waiting"] += 1
        # After the close the main bot cycles every ~30 min (main.py), so a ready lot is adopted within ~35 min; one still
        # unadopted after 45 min has no Swing stop (Alpaca stops only trigger in regular hours, so this is a page, not
        # a loss of protection overnight).
        now = datetime.now(PT)
        for _tid, h in (_load() or {}).items():
            if not isinstance(h, dict):
                continue
            if h.get("status") == "adopted":
                _record_ledger_transfer(_tid, h)      # idempotent: a no-op once recorded
            try:
                if h.get("status") == "ready" and (now - datetime.fromisoformat(str(h.get("ready_ts")))).total_seconds() > 2700:
                    dtm._page_once_today(str(h.get("symbol")), "promotion_unadopted",
                                         f"[{h.get('symbol')}] promoted Day lot NOT adopted by the Swing tier after 45 min "
                                         f"— no Swing stop yet. Check the mtf-bot service (Swing tier).")
                elif h.get("status") == "booking" and (now - datetime.fromisoformat(str(h.get("ts")))).total_seconds() > 600:
                    dtm._page_once_today(str(h.get("symbol")), "promotion_booking_stalled",
                                         f"[{h.get('symbol')}] Day->Swing promotion hand-off stalled in booking for 10+ min "
                                         f"— lot may have no stop. Check the Day runner.")
            except Exception:  # noqa: BLE001
                continue
        return summary
    except Exception as e:  # noqa: BLE001
        logger.error("day-promotion hand-off pass failed: %s", e)
        return summary


# ── 3. ADOPT (main bot, every cycle) ──────────────────────────────────────────────────────────────────────────────
def adopt_promotions(tracker, risk=None) -> list:
    """Adopt every "ready" hand-off into the Swing tracker and place its GTC stop. Idempotent by trade_id; a symbol
    already in the tracker is never overwritten. Returns adopted symbols. Never raises."""
    out: list = []
    try:
        hand = _load()
        if not hand:
            return out
        from execution import broker
        for tid, h in hand.items():
            if h.get("status") != "ready":
                continue
            sym = str(h.get("symbol") or "")
            if not sym:
                continue
            if sym in tracker.open_trades:
                if tracker.open_trades[sym].get("promoted_trade_id") == tid:
                    _update(tid, {"status": "adopted"})
                else:
                    logger.error("[%s] day-promotion: Swing tracker already holds a different trade — not adopted", sym)
                continue
            try:
                pos = broker.get_open_position(sym)
            except Exception as _pe:  # noqa: BLE001
                logger.warning("[%s] day-promotion adopt: position unreadable (%s) — next cycle", sym, _pe)
                continue
            qty = int(h.get("qty") or 0)
            mark = float(h.get("take_over_mark") or 0)
            stop = float(h.get("swing_stop") or 0)
            if not (mark > 0 and 0 < stop < mark):
                _update(tid, {"status": "invalid"})
                _slack(f":rotating_light: [{sym}] promotion hand-off has no valid take-over price / Swing stop — not "
                       f"adopted; check the lot in Alpaca.")
                continue
            held = abs(int(float(pos.qty))) if pos is not None and getattr(pos, "side", None) == "long" else 0
            if held < 1 or qty < 1:
                _update(tid, {"status": "gone"})
                _slack(f":rotating_light: [{sym}] promoted Day lot is no longer at the broker as expected "
                       f"({getattr(pos, 'qty', 'none') if pos is not None else 'flat'}) — not adopted; check Alpaca.")
                continue
            if held < qty:                   # part of the lot left after the booking: the Swing tier owns what remains
                _slack(f":warning: [{sym}] promoted lot is {held} sh at the broker (hand-off said {qty}) — adopting {held}.")
                qty = held
            now_iso = datetime.now(PT).isoformat()
            tracker.open_trades[sym] = {
                "symbol": sym, "direction": "long", "qty": qty, "qty_remaining": qty,
                "entry_price": mark, "stop": stop, "original_stop": stop, "target": h.get("target"),
                "trail_stop": None, "trade_mode": "intraday", "score": 0, "score_16pt": None,
                "atr_value": h.get("atr"), "partial_exited": False, "entry_time": now_iso, "status": "open",
                "reversal_scan_count": 0, "reversal_confirm_count": 0, "overnight": True, "overnight_since": now_iso,
                "gtc_stop_order_id": None, "stop_breached": False, "stop_breach_price": None,
                "_promoted_from_day_tier": True, "promoted_trade_id": tid, "promoted_day_cost": h.get("day_entry"),
            }
            tracker._save_log()
            if risk is not None:
                risk.open_positions = max(risk.open_positions, len(tracker.open_trades))
            try:
                if not bool(broker.get_clock().get("is_open")):
                    o = broker.submit_gtc_stop_order(sym, qty, "sell", stop, tier="intraday")
                    if o is not None and getattr(o, "id", None):
                        tracker.set_gtc_stop_order_id(sym, str(getattr(o, "id", "")))
                        tracker._save_log()
                    else:
                        _slack(f":rotating_light: [{sym}] promoted lot adopted but its GTC stop @ ${stop:.2f} was NOT "
                               f"placed — the after-hours stop block retries; check Alpaca.")
            except Exception as _ge:  # noqa: BLE001
                logger.error("[%s] day-promotion GTC stop failed: %s", sym, _ge)
            _update(tid, {"status": "adopted", "adopted_ts": now_iso, "adopted_qty": qty})
            _record_ledger_transfer(tid, {**h, "qty": qty, "adopted_ts": now_iso})
            out.append(sym)
            logger.warning("[%s] day-promotion: ADOPTED by the Swing tier — %d sh @ take-over $%.2f (Day cost $%.2f), "
                           "stop $%.2f", sym, qty, mark, float(h.get("day_entry") or 0), stop)
            try:
                import trade_logger
                trade_logger.log_event("entry", symbol=sym, price=mark, size=qty, data_source="promotion",
                                       tier="intraday", direction="long", stop=stop, trade_id=tid,
                                       promoted_from_day_tier=True, day_cost=h.get("day_entry"))
            except Exception as _le:  # noqa: BLE001
                logger.warning("[%s] promotion entry event not logged: %s", sym, _le)
        return out
    except Exception as e:  # noqa: BLE001
        logger.error("day-promotion adopt pass failed: %s", e)
        return out
