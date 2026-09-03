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
  4. TIER-KILL (day_trade_manager.tier_kill_check) — force-flat + halt for the day at −25% of tier.
  5. 30-min PRICE SAMPLING — one price_sample per open trade per 30-min bucket (Rafael's price PATH),
     derived restart-safely from the durable log.
  6. ENTRY LOOP — decision → trigger → size → place_entry, per symbol, idempotent per bar_id.
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
import sys
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


def _clock_state() -> "tuple[bool, float | None]":
    """(is_open, minutes_to_real_close) via broker.get_clock (half-day aware). On a clock read
    failure returns (False, None) — fail-safe: treat as CLOSED so no entries fire on a bad clock."""
    from execution import broker
    try:
        clk = broker.get_clock()
        if not clk.get("is_open"):
            return False, None
        nc = clk.get("next_close")
        if nc is None:
            return True, None
        now = datetime.now(getattr(nc, "tzinfo", None) or ET)
        return True, (nc - now).total_seconds() / 60.0
    except Exception as e:  # noqa: BLE001
        logger.warning("clock read failed (treating as closed): %s", e)
        return False, None


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


def run_tick() -> dict:
    """One execution tick. Returns a summary dict. Never raises for a per-symbol error."""
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

    is_open, mins_to_close = _clock_state()
    if not is_open:
        _touch_heartbeat("market_closed")
        return {"skipped": "market_closed"}
    try:
        equity = float(broker.get_portfolio_value())
    except Exception as e:  # noqa: BLE001
        logger.error("equity fetch failed — skipping tick: %s", e)
        _touch_heartbeat("equity_fail")
        return {"skipped": "equity_fail"}

    # Reconcile FIRST — never leave a naked day-tier position across the cron's process boundary.
    recon = dtm.reconcile_open_state()

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

    # Tier-kill (−25% of tier budget → force-flat + halt for the day).
    if dtm.tier_kill_check(equity):
        _touch_heartbeat("tier_killed")
        return {"phase": "tier_killed", "reconcile": recon}

    # 30-min price sampling for the open price PATH.
    _maybe_sample_prices()

    # Entry loop — Track A only (Track B off day-1). Idempotent per (symbol, bar_id). BOUNDED by a
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
    for sym in universe:
        try:
            decision = compute_day_tier_decision(sym)
            if not (isinstance(decision, dict) and decision.get("would_consider")):
                continue
            trigger = compute_entry_trigger(sym, decision)
            if trigger.get("trigger") != "ENTER":
                continue
            size = compute_day_tier_size(sym, decision, trigger.get("entry_ref"), equity, track="A")
            if not size.get("size_ok"):
                continue
            if calls_used + per_entry_est > call_budget:
                capped = True
                break  # defer the remaining ENTERs to the next tick (bar_id idempotency preserves them)
            if dtm.place_entry(sym, decision, trigger, size, bar_id=bar_id, equity=equity):
                entered += 1
            calls_used += per_entry_est
        except Exception as e:  # noqa: BLE001 — one symbol must never abort the tick
            logger.warning("[%s] day-tier entry loop error (non-fatal): %s", sym, e)
    if capped:
        logger.warning("day-tier: per-tick API-call budget (%d, ~%d used) reached — deferred remaining "
                       "entries to the next tick", call_budget, calls_used)
    _touch_heartbeat("scan")
    return {"phase": "scan", "entered": entered, "universe": len(universe), "capped": capped,
            "calls_est": calls_used,
            "mins_to_close": round(mins_to_close, 1) if mins_to_close is not None else None,
            "reconcile": recon}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
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
