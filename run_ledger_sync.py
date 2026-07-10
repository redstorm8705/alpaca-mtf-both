#!/usr/bin/env python3
"""
run_ledger_sync.py — per-tier ownership-ledger MAINTAINER (standalone, cron-driven).

Design: logs/ownership_ledger_design_2026-07-10.md. Maintainer fork resolved UNANIMOUS
to OPTION 1 (full-replay via sync_ledger) — Gro + GAI + cold board (Kim/Majors +
Thorp/Taleb). The incremental-delta engine was ELIMINATED: sync_ledger's
refuse-to-shrink-a-protected-floor guard makes full-replay safe, so incremental buys
zero correctness — only cost — while its cursor failure modes are a fail-closed
violation full-replay does not have.

WHAT IT DOES (one pass): fetch the complete Alpaca fill history + all orders + live
positions, join fill.order_id -> order.client_order_id -> tier (build_coid_map), then
sync_ledger() rebuilds the per-tier ownership ledger (data/state/ownership_ledger.json)
and reconciles to Alpaca net. Idempotent + self-healing; a truncated replay that would
shrink a protected floor ABORTS (writes nothing, CRITICAL alert) — never silently.

WHY STANDALONE (not inside run_cycle): it only WRITES the ledger; nothing reads it to
gate a live sell yet (the guard is unwired). Keeping it out of the RTH hot loop means
NO RTH-hotspot edit and NO bot restart. Cadence is a cron concern (design: every
~15-30 min RTH + at market open + event-triggered on a guard reject), throttled to
spare the shared 175-req/min Alpaca quota.

INSTRUMENTATION (reliability seat mandate): per-run wall-time (WARN>10s / CRIT>30s),
healed=False streak (CRIT + Slack at >=3 consecutive — ledger may be stale), drift
count, per-tier ownership sanity + protected floor per symbol. All to logs/ + a small
streak-state file.

Data tier: T1 (Alpaca Paper REST via reporting.pnl_ledger — read-only; no SDK).
Output: data/state/ownership_ledger.json (the ledger, RC-5 atomic via ownership_guard),
logs/mtf_bot.log, logs/.ledger_sync_streak.json.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_LOGS = _ROOT / "logs"
_STREAK_PATH = _LOGS / ".ledger_sync_streak.json"

logger = logging.getLogger("ledger_sync")

_WALLTIME_WARN_S = 10.0
_WALLTIME_CRIT_S = 30.0
_HEALED_FALSE_STREAK_ALERT = 3


def _load_env() -> None:
    """Load .env for standalone/cron runs (mirrors reporting.pnl_ledger.__main__)."""
    envp = _ROOT / ".env"
    if not envp.exists():
        return
    for line in envp.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _slack(msg: str) -> None:
    """Best-effort Slack alert; never raises into the maintainer."""
    try:
        from alerts import send_slack
        send_slack(msg)
    except Exception as e:  # RC-3: logged, not swallowed silently
        logger.warning("slack alert failed: %s", e)


def _read_streak() -> dict:
    try:
        if _STREAK_PATH.exists():
            d = json.loads(_STREAK_PATH.read_text())
            if isinstance(d, dict):
                return d
    except Exception as e:
        logger.warning("streak read failed (resetting): %s", e)
    return {"count": 0, "reasons": [], "last_utc": None}


def _write_streak(streak: dict) -> None:
    """Atomic tmp->replace (RC-5)."""
    try:
        _LOGS.mkdir(parents=True, exist_ok=True)
        tmp = _STREAK_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(streak, indent=2))
        tmp.replace(_STREAK_PATH)
    except Exception as e:
        logger.warning("streak write failed: %s", e)


def sync_once() -> dict:
    """Run one full-replay maintenance pass. Returns a result dict for logging/tests.
    NEVER raises into a cron caller — the ENTIRE body (imports, fetch, attribute, sync,
    instrumentation) is wrapped in one try/except with a `stage` tag; any error is
    caught, logged CRITICAL + best-effort Slack, and reported as ok=False so the ledger
    is left at its last-good state. (Cold-2nd 2026-07-10: the earlier version wrapped
    only the fetch, so a raise in build_coid_map / sync_ledger's unguarded save_ledger /
    the instrumentation loop would crash cron — invariant now enforced end-to-end.)"""
    t0 = time.monotonic()
    stage = "import"
    try:
        from reporting.pnl_ledger import (
            fetch_all_fills, fetch_all_orders, fetch_positions, build_coid_map,
        )
        from execution.ownership_guard import sync_ledger, tier_of_coid

        stage = "fetch"
        fills = fetch_all_fills()
        orders = fetch_all_orders()
        positions = fetch_positions()

        stage = "attribute"
        coid_map = build_coid_map(orders)

        stage = "sync"
        led = sync_ledger(fills, positions, coid_by_order_id=coid_map)

        stage = "instrument"
        elapsed = round(time.monotonic() - t0, 2)

        # ── wall-time instrumentation ────────────────────────────────────────
        if elapsed > _WALLTIME_CRIT_S:
            logger.critical("ledger_sync wall-time %.1fs > %ss — Alpaca slow / "
                            "pagination hang?", elapsed, _WALLTIME_CRIT_S)
            _slack(f":warning: ledger_sync slow: {elapsed}s (fills={len(fills)})")
        elif elapsed > _WALLTIME_WARN_S:
            logger.warning("ledger_sync wall-time %.1fs (fills=%d orders=%d)",
                           elapsed, len(fills), len(orders))

        healed = led.get("healed") is True
        streak = _read_streak()

        if not healed:
            # sync_ledger refused (protected-floor shrink / truncated replay) — ledger
            # NOT updated. Track the consecutive-failure streak and escalate.
            reason = led.get("reason", "unknown")
            streak["count"] = int(streak.get("count", 0)) + 1
            streak["reasons"] = (streak.get("reasons", []) + [reason])[-10:]
            streak["last_utc"] = datetime.now(timezone.utc).isoformat()
            logger.critical("ledger_sync NOT healed (reason=%s, streak=%d): %s",
                            reason, streak["count"], led.get("shrink"))
            if streak["count"] >= _HEALED_FALSE_STREAK_ALERT:
                _rs = set(streak["reasons"])
                _slack(f":rotating_light: ledger_sync FAILING: {streak['count']} "
                       f"consecutive non-heals (reasons={_rs}) — ledger STALE")
            _write_streak(streak)
            return {"ok": False, "healed": False, "reason": reason,
                    "elapsed_s": elapsed, "fills": len(fills), "orders": len(orders)}

        # ── healed OK — reset streak, emit per-symbol sanity ─────────────────
        if streak.get("count", 0):
            logger.info("ledger_sync recovered after %d non-heal(s)", streak["count"])
        _write_streak({"count": 0, "reasons": [], "last_utc": None})

        positions_out = led.get("positions", {})
        drift_syms = {s: e.get("drift", 0.0) for s, e in positions_out.items()
                      if abs(float(e.get("drift", 0.0) or 0.0)) > 1e-6}
        tier_dist: dict = {}
        for f in fills:
            t = tier_of_coid(coid_map.get(f.get("order_id"))) or "intraday"
            tier_dist[t] = tier_dist.get(t, 0) + 1

        logger.info("ledger_sync OK: %d fills, %d orders, %d positions, %.2fs; "
                    "drift_syms=%d; tier_dist=%s",
                    len(fills), len(orders), len(positions_out), elapsed,
                    len(drift_syms), tier_dist)
        for sym, ent in sorted(positions_out.items()):
            tiers = ent.get("tiers", {})
            floor = sum(float(tiers.get(t, {}).get("qty", 0.0) or 0.0)
                        for t in ("qhm", "forever6"))
            logger.info("  [%s] net=%s intraday=%s qhm=%s forever6=%s "
                        "protected_floor=%s drift=%s", sym, ent.get("alpaca_net_qty"),
                        tiers.get("intraday", {}).get("qty"),
                        tiers.get("qhm", {}).get("qty"),
                        tiers.get("forever6", {}).get("qty"), floor, ent.get("drift"))
        if drift_syms:
            logger.critical("ledger_sync drift on %d symbol(s) %s — sells would be "
                            "FROZEN there once the guard is wired",
                            len(drift_syms), drift_syms)

        return {"ok": True, "healed": True, "elapsed_s": elapsed, "fills": len(fills),
                "orders": len(orders), "positions": len(positions_out),
                "drift_syms": list(drift_syms), "tier_dist": tier_dist}
    except Exception as e:
        # Hard invariant: a cron-driven maintainer must NEVER raise. Any failure leaves
        # the ledger at its last-good state (sync_ledger's tmp→replace means the live
        # ledger is never partially written).
        logger.critical("ledger_sync: unhandled error at stage=%s — ledger left "
                        "stale: %s", stage, e)
        _slack(f":rotating_light: ledger_sync failed at stage={stage}: {e}")
        return {"ok": False, "stage": stage, "error": str(e)}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(_LOGS / "mtf_bot.log")],
    )
    _load_env()
    res = sync_once()
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
