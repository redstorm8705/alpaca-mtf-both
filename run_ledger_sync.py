#!/usr/bin/env python3
# ruff: noqa: E501
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

ON-DEMAND REBUILD (KILL-JOIN-1107 rev 4 + 4.1, owner-approved; logs/design_records/kill_join_2026-11-07.md):
  * The kill check writes data/state/ledger_rebuild_request.json (a generation id) when its exact ledger/broker join
    fails. `run_ledger_sync.py --if-requested` (every-minute cron) exits at once unless that generation is not yet
    done, so the ledger catches up within about a minute of a fill instead of up to 20.
  * ONE non-blocking file lock covers EVERY pass (scheduled, on-request, manual, startup, F6 callers of sync_once).
    A pass that finds it held logs "busy; request remains pending" and returns without touching the request.
  * `last_rebuild_utc` (the time captured just before the fill fetch) and the done generation are written to the
    streak file only after a healed pass, i.e. after the ledger's atomic replace. A refusal or an error never
    advances them, and a request written during a pass stays pending for the next one.
  * Back-off: an on-request pass is skipped while the last pass was a refused shrink and the request still carries
    the same fills/positions fingerprint and no new operator confirmation exists. The scheduled pass always runs.
  * Only scheduled passes page and count toward the 3-pass "ledger may be stale" page (paging timing unchanged).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent
_LOGS = _ROOT / "logs"
_STREAK_PATH = _LOGS / ".ledger_sync_streak.json"
_STATE_DIR = _ROOT / "data" / "state"
# KILL-JOIN-1107 plumbing: the rebuild request written by execution/risk_manager.py, the one lock every pass takes,
# and the operator heal-confirmation file (a new confirmation ends a refusal back-off).
_REQUEST_PATH = _STATE_DIR / "ledger_rebuild_request.json"
_LOCK_PATH = _STATE_DIR / "ledger_sync.lock"
_HEAL_CONFIRM_PATH = _STATE_DIR / "ledger_heal_confirmations.json"

logger = logging.getLogger("ledger_sync")

_WALLTIME_WARN_S = 10.0
_WALLTIME_CRIT_S = 30.0
_HEALED_FALSE_STREAK_ALERT = 3
_ET = ZoneInfo("America/New_York")
_NET_STABILITY_SAMPLE_GAP_S = 3.0   # gap between the two /v2/positions samples (a settling leg moves)
# KILL-JOIN-1107 cold-2nd: a scheduled pass waits this long for the lock instead of exiting busy, so an on-request pass
# that holds it cannot make the 20-minute rebuild (and its stale-page count) skip. Bound = one pass at its CRITICAL
# walltime plus the settle sample gap.
_SCHEDULED_LOCK_TIMEOUT_S = _WALLTIME_CRIT_S + _NET_STABILITY_SAMPLE_GAP_S
_UNSETTLED_ORDER_STATUSES = frozenset({
    "partially_filled", "pending_new", "pending_cancel", "pending_replace",
    "pending_review", "calculated",
})


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


@contextmanager
def _rebuild_lock(wait_timeout_s: float = 0.0) -> Iterator["bool | None"]:
    """KILL-JOIN-1107 rev 4.1 item 1: the one non-blocking lock every rebuild pass takes.

    Yields True when held, False when another pass holds it (busy), None when the lock file cannot be opened. flock is
    per open file, so two passes in one process exclude each other as well. Released on exit (and by the OS if the
    process dies). wait_timeout_s > 0 retries the non-blocking lock until that many seconds have passed (cron passes only,
never the trading thread). Never raises on acquisition."""
    import fcntl
    handle = None
    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = open(_LOCK_PATH, "a+", encoding="utf-8")
    except OSError as e:
        logger.critical("ledger_sync: lock file unavailable (%s) — pass skipped [change=kill-join-1107]", e)
        yield None
        return
    try:
        deadline = time.monotonic() + max(0.0, wait_timeout_s)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.5)
        try:
            yield True
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as e:
                logger.warning("ledger_sync: lock release failed (%s)", e)
    finally:
        handle.close()


def _read_request() -> "dict | None":
    """The pending rebuild request ({generation, requested_utc, fingerprint, ...}) or None (absent / unreadable).
    An unreadable request is logged and treated as absent: the scheduled pass still rebuilds. Never raises."""
    try:
        if not _REQUEST_PATH.exists():
            return None
        d = json.loads(_REQUEST_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("generation"), str) and d["generation"]:
            return d
        logger.warning("ledger_sync: rebuild request malformed — ignored [change=kill-join-1107]")
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001 — RC-3: logged
        logger.warning("ledger_sync: rebuild request unreadable (%s) — ignored [change=kill-join-1107]", e)
    return None


def _confirmations_stamp() -> "float | None":
    """mtime of the operator heal-confirmation file, or None when absent/unreadable. Never raises."""
    try:
        return _HEAL_CONFIRM_PATH.stat().st_mtime if _HEAL_CONFIRM_PATH.exists() else None
    except OSError:
        return None


def _minutes_since(iso_ts) -> "float | None":
    """Minutes from an ISO UTC timestamp to now (the on-request latency metric), or None. Never raises."""
    then = _parse_utc(iso_ts) if iso_ts else None
    if then is None:
        return None
    waited_seconds = (datetime.now(timezone.utc) - then).total_seconds()
    return round(waited_seconds / 60, 2)


def _inherit_leg_tiers(coid_map: dict, orders: list) -> int:
    """An UNTAGGED multi-leg child (OCO/bracket leg — Alpaca generates its client_order_id)
    inherits its parent's TIER-TAGGED client_order_id, by the exact `_parent_id` link from
    fetch_all_orders(nested=True). Never by symbol/time proximity. A tagged leg, or a leg
    whose parent is untagged, is left unchanged. Returns the count re-attributed.
    Why (Claude 2026-10-02): day-tier OCO stop fills were attributed to intraday, leaving
    crossed AMZN/META/MSFT ledger rows that make the tier-capital allocator deny every tier."""
    from execution.ownership_guard import tier_of_coid
    n = 0
    for o in (orders or []):
        oid = o.get("id")
        parent_coid = o.get("_parent_client_order_id")
        if (oid and o.get("_parent_id") and tier_of_coid(parent_coid)
                and tier_of_coid(o.get("client_order_id")) is None):
            coid_map[oid] = parent_coid
            n += 1
    return n


def _parse_utc(ts) -> "datetime | None":
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _owner_exit_explained(pending: dict, fills: list, coid_map: dict, tier_of_coid, since_utc) -> dict:
    """{"SYM/tier": True|False} for each pending protected-floor reduction: True when the drop is fully explained by
    SELL fills AFTER `since_utc` (this maintainer's last HEALED pass — sells before it are already booked and must
    not be reused; cold-2nd 2026-10-08) on orders tagged to that SAME protected tier (QH- for qhm, F6- for
    forever6, incl. multi-leg children inherited via coid_map) — i.e. the owner's own exit (its GTC stop / its own
    sell), which the owner's manager heals within its next cycle. False on any doubt (no readable since_utc, or a
    reduction not covered by the owner's own unbooked sells — a possible breach, which still pages). Never raises.
    Why (CEO 2026-10-08): QHM's NVDA stop filled 13:31 UTC; the 13:40 sync paged for operator confirmation 23 min
    before QHM's own auto-heal ran — a page for the owner's own, already-explained exit."""
    out: dict = {}
    since = _parse_utc(since_utc) if since_utc else None
    for key, v in (pending or {}).items():
        try:
            if since is None:
                out[key] = False      # cannot tell booked from unbooked sells -> page
                continue
            sym, tier = str(key).split("/", 1)
            need = float(v.get("was") or 0.0) - float(v.get("would_be") or 0.0)
            sold = 0.0
            for f in fills or []:
                _ft = _parse_utc(f.get("transaction_time"))
                if (f.get("symbol") == sym and str(f.get("side", "")).lower() in ("sell", "sell_short")
                        and _ft is not None and _ft > since
                        and tier_of_coid(coid_map.get(f.get("order_id"))) == tier):
                    sold += abs(float(f.get("qty") or 0.0))
            out[key] = need > 0 and sold + 1e-9 >= need
        except Exception as e:  # noqa: BLE001 — doubt pages
            logger.warning("ledger_sync: owner-exit check failed for %s (%s) — will page", key, e)
            out[key] = False
    return out


def _qty(v) -> str:
    """'1 share' / '3 shares' for a number, the raw value otherwise (plain-English share counts)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:g} share" + ("" if v == 1 else "s")
    return str(v)


def _pending_lines(pending: dict) -> "list[str]":
    """One plain-English line per pending protected reduction: symbol, tier, shares before -> after, confirm command."""
    from tier_names import tier_label
    out = []
    for key, v in (pending or {}).items():
        sym, _, tier = str(key).partition("/")
        # the guard's own command (ownership_guard: target = the broker net); fallback only if it is missing
        _net = v.get("net")
        cmd = v.get("confirm_cmd") or (f"confirm_ledger_heal.py {sym} {tier} {_net:g}"
                                       if isinstance(_net, (int, float)) else "confirm_ledger_heal.py (see logs/mtf_bot.log)")
        out.append(f"• {sym}: the bot's {tier_label(tier)}{' (long-term hold)' if tier == 'qhm' else ''} record says {_qty(v.get('was'))}, "
                   f"the broker now shows {_qty(v.get('net'))}. If you sold them yourself, confirm with: "
                   f"`python3 {cmd}`")
    return out


def _confirmation_message(pending: dict) -> str:
    """First-pass page: protected shares left the account by a sale the owning tier did not make."""
    return ("Shares in a protected tier were sold, and the bot could not confirm the sale was that tier's own order "
            "(for example, a manual sale in the Alpaca app). The bot has paused updating its share-ownership record "
            "until you confirm:\n" + "\n".join(_pending_lines(pending)) + _CONFIRM_FOOTER)


_CONFIRM_FOOTER = ("\nRun the command on the server (in /home/ubuntu/mtf-bot). If you did NOT sell these shares, do not "
                   "confirm: tell the engineering session, because another part of the bot may have sold them.")


def _stale_message(streak: dict, pending: dict) -> str:
    """Escalation: the record has not updated for several passes. Sent once at the threshold, then once a day."""
    since = _parse_utc(streak.get("last_healed_utc"))
    since_txt = (f"since {since.astimezone(ZoneInfo('America/Los_Angeles')):%a %b %d %I:%M %p} PT"
                 if since else "for several runs")
    body = ("\n".join(_pending_lines(pending)) + _CONFIRM_FOOTER if pending
            else f"Reason: {', '.join(sorted({str(r) for r in (streak.get('reasons') or [])})) or 'unknown'}.")
    return (f"The bot's share-ownership record has not updated {since_txt} ({streak.get('count')} runs in a row). "
            "Anything that reads which tier owns which shares sees the old record until this clears:\n" + body)


def _on_request_skip(request: "dict | None", streak: dict) -> "str | None":
    """Why an `--if-requested` pass should not rebuild, or None to rebuild. Runs under the lock.
    "no_request": no request, or its generation is already done. "backoff": the last pass refused a protected-floor
    shrink and nothing it could change has appeared since (same fills/positions fingerprint, no new operator
    confirmation) — logged once per fingerprint; the scheduled pass still runs."""
    if request is None or request.get("generation") == streak.get("request_done_generation"):
        return "no_request"
    fingerprint = request.get("fingerprint")
    if (streak.get("last_pass_refused") is True and fingerprint
            and fingerprint == streak.get("refused_fingerprint")
            and _confirmations_stamp() == streak.get("refused_confirm_stamp")):
        if streak.get("backoff_logged_fingerprint") != fingerprint:
            logger.info("ledger_sync on request: skipped (back-off) — the last pass refused a protected-floor shrink "
                        "and no new fill, position change or operator confirmation has appeared since; request "
                        "%s stays pending for the scheduled pass [change=kill-join-1107]", request.get("generation"))
            streak["backoff_logged_fingerprint"] = fingerprint
            _write_streak(streak)
        return "backoff"
    return None


def sync_once(*, scheduled: bool = True, if_requested: bool = False, lock_wait_s: float = 0.0) -> dict:
    """Run one full-replay maintenance pass under the one rebuild lock (KILL-JOIN-1107). Returns a result dict.

    scheduled=True (the default: the 20-minute cron, manual and startup runs, the F6 callers) keeps today's paging and
    streak counting. scheduled=False (the every-minute `--if-requested` cron) never pages and never counts toward the
    stale page. if_requested=True returns at once unless a rebuild request generation is pending and not backed off.
    A pass that finds the lock held (after lock_wait_s) returns {"ok": False, "busy": True} without touching the request.
    An if_requested pass with nothing pending returns before taking the lock, so the every-minute cron never holds it
    when it has no work (cold-2nd: it used to take the lock first and make a simultaneous 20-minute pass exit busy).
    Never raises."""
    try:
        if if_requested:
            _pre = _read_request()
            if _pre is None or _pre.get("generation") == _read_streak().get("request_done_generation"):
                return {"ok": True, "healed": False, "skipped": "no_request"}   # lock-free: files are atomically replaced
        with _rebuild_lock(lock_wait_s) as held:
            if held is None:
                return {"ok": False, "healed": False, "stage": "lock", "error": "lock file unavailable"}
            request = _read_request()
            if not held:
                if if_requested and request is None:
                    return {"ok": False, "healed": False, "busy": True}   # nothing requested: no log line every minute
                logger.info("ledger_sync busy; request remains pending (generation %s) [change=kill-join-1107]",
                            (request or {}).get("generation"))
                return {"ok": False, "healed": False, "busy": True}
            if if_requested:
                skip = _on_request_skip(request, _read_streak())
                if skip:
                    return {"ok": True, "healed": False, "skipped": skip}
                logger.info("ledger_sync on request: rebuilding for generation %s, requested %s min ago "
                            "[change=kill-join-1107]", (request or {}).get("generation"),
                            _minutes_since((request or {}).get("requested_utc")))
            return _sync_locked(scheduled=scheduled, request=request)
    except Exception as e:  # noqa: BLE001 — the cron maintainer must never raise
        logger.critical("ledger_sync: unhandled error around the rebuild lock: %s", e)
        return {"ok": False, "healed": False, "stage": "lock", "error": str(e)}


def _sync_locked(*, scheduled: bool, request: "dict | None") -> dict:
    """Run one full-replay maintenance pass (the lock is held by sync_once). Returns a result dict for logging/tests.
    NEVER raises into a cron caller — the ENTIRE body (imports, fetch, attribute, sync,
    instrumentation) is wrapped in one try/except with a `stage` tag; any error is
    caught, logged CRITICAL + best-effort Slack, and reported as ok=False so the ledger
    is left at its last-good state. (Cold-2nd 2026-07-10: the earlier version wrapped
    only the fetch, so a raise in build_coid_map / sync_ledger's unguarded save_ledger /
    the instrumentation loop would crash cron — invariant now enforced end-to-end.)"""
    t0 = time.monotonic()
    stage = "import"
    # KILL-JOIN-1107: only scheduled passes page (an on-request pass may run every minute).
    page = _slack if scheduled else (lambda msg: logger.warning("ledger_sync on request (no page): %s", msg))
    request_generation = (request or {}).get("generation")
    request_fingerprint = (request or {}).get("fingerprint")
    try:
        from reporting.pnl_ledger import (
            fetch_all_fills, fetch_all_orders, fetch_positions, build_coid_map,
        )
        from execution.ownership_guard import sync_ledger, tier_of_coid
        from execution.quarterly_hold_manager import get_quarterly_hold_quantities

        stage = "fetch"
        # KILL-JOIN-1107 rev 4.1 item 2: the rebuild watermark is the time just before the fill fetch (every fill at or
        # before it is in this replay); an operator confirmation written after this point ends a refusal back-off.
        rebuild_started_utc = datetime.now(timezone.utc).isoformat()
        confirm_stamp_at_start = _confirmations_stamp()
        fills = fetch_all_fills()
        orders = fetch_all_orders(nested=True)   # legs carry their parent link (see _inherit_leg_tiers)
        positions = fetch_positions()

        # SETTLED/STABLE/COMPLETE net contract — an OPERATOR-CONFIRMED protected-floor down-heal
        # is applied by sync_ledger ONLY when positions_settled=True. That needs more than HTTP 200:
        # (a) STABLE — a second fetch_positions() a few seconds later matches the first EXACTLY per
        # symbol (a settling leg moves between samples); (b) quiescent — no in-flux orders
        # (partially_filled/pending_*/calculated; a resting GTC stop is 'new'/'held' → fine). Any
        # doubt → False → sync_ledger refuses the shrink (stale-but-safe).
        positions_settled = False
        try:
            time.sleep(_NET_STABILITY_SAMPLE_GAP_S)
            _positions_2 = fetch_positions()

            def _net_map(_ps):
                return {p.get("symbol"): round(float(p.get("qty", 0.0) or 0.0), 6)
                        for p in (_ps or []) if p.get("symbol")}
            _stable = _net_map(positions) == _net_map(_positions_2)
            _in_flux = any(str(o.get("status", "")).lower() in _UNSETTLED_ORDER_STATUSES
                           for o in (orders or []))
            positions_settled = bool(_stable and not _in_flux)
            if not positions_settled:
                logger.info("ledger_sync: net NOT settled (stable=%s in_flux=%s) — confirmed "
                            "down-heal disabled this pass.", _stable, _in_flux)
        except Exception as _se:  # RC-3
            logger.warning("ledger_sync: settled-net check failed (%s) — down-heal disabled.", _se)
            positions_settled = False

        stage = "attribute"
        coid_map = build_coid_map(orders)
        _legs_inherited = _inherit_leg_tiers(coid_map, orders)
        if _legs_inherited:
            logger.info("ledger_sync: %d untagged multi-leg child order(s) attributed to "
                        "their parent's tier", _legs_inherited)
        # Authoritative qhm-tier share counts. Legacy QHM buys are untagged → they land
        # in the intraday tier of the fill-based ledger; the QHM manager knows the real
        # quarterly holdings, giving NVDA/GOOGL etc. a true never-sell floor. Absent
        # file / no active holds → {} (nothing to attribute). A corrupt-but-present
        # state file RAISES (fail-closed) — caught by sync_once's try/except, leaving
        # the ledger at last-good rather than dropping a hold's floor to 0.
        qhm_holdings = get_quarterly_hold_quantities()
        # Day -> Swing hand-overs (promotion / adopted Day lots) — execution/tier_transfers.py. An unreadable journal
        # raises here, so the pass is skipped and the ledger stays at its last-good state (never re-attributed blind).
        from execution.tier_transfers import load_transfers
        transfers = load_transfers()
        if transfers is None:
            raise RuntimeError("tier transfer journal unreadable")

        stage = "sync"
        led = sync_ledger(fills, positions, coid_by_order_id=coid_map,
                          qhm_holdings=qhm_holdings, positions_settled=positions_settled,
                          transfers=transfers)

        # Stay filesystem-dormant while F6 is dark.  If a journal already exists
        # (for example after an emergency flag-off), continue exact reconciliation
        # so active protection cannot be abandoned by configuration alone.
        import config as _config
        from execution import order_admission as _oa
        from execution import pending_protected_buys as _ppb
        if bool(getattr(_config, "FOREVER6_ENABLED", False)) or _oa._REDUCTION_PATH.exists():
            stage = "reducing_admission_reconcile"
            _pending_reducers = _oa.reconcile_reductions(fills, orders, coid_map, led)
            if _pending_reducers["pending"]:
                logger.warning("ledger_sync: %d reducing admission(s) remain unresolved",
                               len(_pending_reducers["pending"]))
        if bool(getattr(_config, "FOREVER6_ENABLED", False)) or _ppb.JOURNAL_PATH.exists():
            stage = "pending_f6_reconcile"
            _pending_f6 = _ppb.reconcile(fills, orders, coid_map, led)
            if _pending_f6["pending"]:
                logger.warning("ledger_sync: %d F6 protected-buy reservation(s) remain unresolved",
                               len(_pending_f6["pending"]))

        stage = "instrument"
        elapsed = round(time.monotonic() - t0, 2)

        # ── wall-time instrumentation ────────────────────────────────────────
        if elapsed > _WALLTIME_CRIT_S:
            logger.critical("ledger_sync wall-time %.1fs > %ss — Alpaca slow / "
                            "pagination hang?", elapsed, _WALLTIME_CRIT_S)
            page(f"The share-ownership record update took {elapsed}s (normal is a few seconds) — Alpaca may be slow. "
                 "No action needed unless this repeats.")
        elif elapsed > _WALLTIME_WARN_S:
            logger.warning("ledger_sync wall-time %.1fs (fills=%d orders=%d)",
                           elapsed, len(fills), len(orders))

        healed = led.get("healed") is True
        streak = _read_streak()

        if not healed:
            reason = led.get("reason", "unknown")
            # KILL-JOIN-1107 rev 4.1 item 4: remember the refusal and what it was refused on, so on-request passes back
            # off until a new fill, position change or operator confirmation appears. Neither the watermark nor the
            # done generation moves (the request stays pending).
            streak["last_pass_refused"] = True
            streak["refused_fingerprint"] = request_fingerprint
            streak["refused_confirm_stamp"] = confirm_stamp_at_start
            if not scheduled:
                # On-request passes never page and never count toward the stale page (the scheduled pass does both,
                # on its unchanged timing).
                logger.warning("ledger_sync on request: NOT healed (reason=%s) — generation %s remains pending; the "
                               "scheduled pass pages [change=kill-join-1107]", reason, request_generation)
                _write_streak(streak)
                return {"ok": False, "healed": False, "reason": reason, "on_request": True,
                        "elapsed_s": elapsed, "fills": len(fills), "orders": len(orders)}
            # sync_ledger refused (protected-floor shrink / truncated replay) — ledger
            # NOT updated. Track the consecutive-failure streak and escalate.
            streak["count"] = int(streak.get("count", 0)) + 1
            streak["reasons"] = (streak.get("reasons", []) + [reason])[-10:]
            streak["last_utc"] = datetime.now(timezone.utc).isoformat()
            logger.critical("ledger_sync NOT healed (reason=%s, streak=%d): %s",
                            reason, streak["count"], led.get("shrink"))
            _pending = led.get("pending_heal") or {}
            if _pending and streak["count"] == 1:
                # The owner tier's own exit (a QH-/F6- tagged sell today) is not a breach: its manager heals the
                # ledger within its next cycle, so no operator page for it. If the heal never lands, the
                # consecutive non-heal escalation below still fires (CEO 2026-10-08).
                # The last HEALED pass of this maintainer (recorded in the streak file below). The ledger's own
                # last_reconciled_utc is not stamped by sync_ledger (cold-2nd 2026-10-08), so it cannot be used.
                # Absent (first pass after deploy / unreadable) -> None -> page (fail-safe).
                _since = streak.get("last_healed_utc")
                _explained = _owner_exit_explained(_pending, fills, coid_map, tier_of_coid, _since)
                for _k in [k for k, ok in _explained.items() if ok]:
                    logger.warning("ledger_sync: %s reduction is the owner tier's own exit (its tagged sell since the last healed sync) "
                                   "— no operator page; awaiting the owner's auto-heal.", _k)
                _pending = {k: v for k, v in _pending.items() if not _explained.get(k)}
            if _pending and streak["count"] == 1:
                _slack(_confirmation_message(_pending))
            # Stale-record reminder (CEO 2026-10-16: a manual LLY sale paged on every 20-min pass): page once when the
            # streak reaches the threshold, then at most once per ET day while it stays unresolved — never every pass.
            _today_et = f"{datetime.now(_ET):%Y-%m-%d}"
            if (streak["count"] >= _HEALED_FALSE_STREAK_ALERT
                    and (streak["count"] == _HEALED_FALSE_STREAK_ALERT
                         or streak.get("stale_paged_et_date") != _today_et)):
                streak["stale_paged_et_date"] = _today_et
                try:
                    _msg = _stale_message(streak, led.get("pending_heal") or {})
                except Exception as _me:  # noqa: BLE001 — a formatting error must not skip _write_streak (re-page flood)
                    _msg = f"The bot's share-ownership record has not updated ({streak.get('count')} runs in a row): {_me}"
                _slack(_msg)
            _write_streak(streak)   # keeps last_healed_utc (unchanged by a non-heal)
            return {"ok": False, "healed": False, "reason": reason,
                    "elapsed_s": elapsed, "fills": len(fills), "orders": len(orders)}

        # ── healed OK — reset streak, emit per-symbol sanity ─────────────────
        if streak.get("count", 0):
            logger.info("ledger_sync recovered after %d non-heal(s)", streak["count"])
        # KILL-JOIN-1107 rev 4.1 items 2-3: sync_ledger has already replaced the ledger atomically (healed=True), so the
        # watermark and the generation this pass started from are recorded now — and only now. A request written
        # during the pass carries a different generation and stays pending.
        _write_streak({"count": 0, "reasons": [], "last_utc": None,
                       "last_healed_utc": datetime.now(timezone.utc).isoformat(),
                       "last_rebuild_utc": rebuild_started_utc,
                       "request_done_generation": request_generation or streak.get("request_done_generation")})
        if request_generation:
            logger.info("ledger_sync%s: rebuild request %s done %s min after the request (%.2fs pass) "
                        "[change=kill-join-1107]", "" if scheduled else " on request", request_generation,
                        _minutes_since((request or {}).get("requested_utc")), elapsed)

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
        page(f"The share-ownership record update failed ({stage} step: {e}). The last good record stays in use; "
             "it retries on its next run. If this repeats, check logs/mtf_bot.log on the server.")
        return {"ok": False, "stage": stage, "error": str(e)}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild the per-tier ownership ledger from Alpaca fills.")
    parser.add_argument("--if-requested", action="store_true",
                        help="KILL-JOIN-1107: rebuild only when the kill check has requested it (every-minute cron); "
                             "never pages and never counts toward the stale page.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(_LOGS / "mtf_bot.log")],
    )
    _load_env()
    # The scheduled cron pass waits for the lock (an on-request pass may hold it); the on-request pass never waits.
    res = sync_once(scheduled=not args.if_requested, if_requested=args.if_requested,
                    lock_wait_s=0.0 if args.if_requested else _SCHEDULED_LOCK_TIMEOUT_S)
    # A busy pass (another rebuild holds the lock) is not a failure: the request stays pending for the next pass.
    return 0 if res.get("ok") or res.get("busy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
