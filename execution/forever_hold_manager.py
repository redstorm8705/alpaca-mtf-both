#!/usr/bin/env python3
# ruff: noqa: E501
"""
execution/forever_hold_manager.py — FOREVER-6 STARTER tier (increment 1a, DARK / LOG-ONLY).

The BGG-locked STARTER rule (Rafael 2026-07-13, logs/f6_starter_bgg_2026-07-13.md): on a market-wide
dip (SPY down >= a DYNAMIC threshold on the close), ESTABLISH starter positions in 1-3 Forever-6 anchor
names. This tier is CASH-ONLY (the cold board proved margin makes a never-sell book hostage to any other
strategy's worst day — a maintenance call would force-liquidate the anchors), breadth-first, catalyst-
SCREENED (skip a name with an active negative catalyst — the RIVN lesson, now enforced by the live
catalyst_engine gate), and funded from a SEGREGATED budget that can never cannibalize the deep crash
ladder's dry powder.

INCREMENT 1a = LOG-ONLY: maybe_start_accumulation() evaluates the trigger + budget + screen + selection
and LOGS the plan it WOULD execute. It places NO live orders (that's the next increment, wired into
run_cycle behind FOREVER6_ENABLED + a mandatory cold masked-loss seat). This lets the whole decision be
validated on real dips before a single dollar is committed.

Mirrors QuarterlyHoldManager's shape (broker in __init__, a run_cycle hook, JSON state). Read-only in 1a.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger("forever6")

PT = ZoneInfo("America/Los_Angeles")
_ROOT = Path(__file__).resolve().parent.parent
_STATE = _ROOT / "data" / "state" / "forever6_holds.json"
# C-3 (design logs/f6_prereq1_syncgap_design_2026-07-17.md): persisted "block further F6
# seeding" flag. Set when a post-buy ledger sync fails to reflect a fresh F6 lot (C-2);
# blocks the NEXT seed until a clean sync clears it. Scoped to SEEDING only — never a
# sell/exit path. Fail-CLOSED on read error (treat as degraded → block), the safe
# direction for a never-sell book.
_SYNC_DEGRADED = _ROOT / "data" / "state" / "forever6_sync_degraded.json"
# Per-symbol trim-rung state (F6 exit increment, 2026-08-05): {SYM: {"trim1": "none"|
# "reserved"|"done", "trim2": ...}}. Durable across restarts so a 10x/20x trim, once
# reserved or executed, can never re-fire on the same rung. Separate from _STATE (the
# starter-event log) — different shape, different write cadence (once per
# reserve/confirm transition vs. once per starter event).
_TRIM_STATE = _ROOT / "data" / "state" / "forever6_trims.json"
_EPS = 1e-6


class _TrimStateUnreadable(Exception):
    """Raised by _load_trim_state when forever6_trims.json EXISTS but can't be parsed —
    distinct from a legitimately-absent file (which means 'no trim ever recorded' and is
    safe to treat as {}). Must propagate to skip trim evaluation, never be swallowed and
    treated as empty state."""


def _slack_safe(msg: str) -> None:
    """Best-effort Slack; never raises into the F6 path."""
    try:
        from alerts import send_slack
        send_slack(msg)
    except Exception as e:  # RC-3: logged, not swallowed silently
        logger.warning("forever6: slack alert failed: %s", e)


def _read_sync_degraded() -> bool:
    """True → F6 seeding is blocked. Fail-CLOSED: an unreadable flag returns True (block),
    the safe direction for a never-sell book (never seed on uncertainty)."""
    try:
        if _SYNC_DEGRADED.exists():
            d = json.loads(_SYNC_DEGRADED.read_text())
            return bool(d.get("degraded", False))
    except Exception as e:
        logger.warning("forever6: sync-degraded read failed — fail-closed (block): %s", e)
        return True
    return False


def _write_sync_degraded(degraded: bool, detail: str = "") -> None:
    """Atomic tmp→fsync→replace (RC-5). A write failure is logged LOUDLY: if we meant to
    SET degraded and the write fails, the next seed may proceed unblocked — so escalate."""
    try:
        _SYNC_DEGRADED.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SYNC_DEGRADED.with_suffix(f".json.{os.getpid()}.tmp")
        with open(tmp, "w") as f:
            json.dump({"degraded": bool(degraded), "detail": detail,
                       "ts": datetime.now(PT).isoformat()}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(_SYNC_DEGRADED))
    except Exception as e:
        logger.error("forever6: sync-degraded write failed (degraded=%s): %s", degraded, e)
        if degraded:
            _slack_safe(f":rotating_light: F6 could not persist sync-degraded flag ({e}) "
                        f"— next seed may NOT be auto-blocked; verify manually.")

# Reserve slightly MORE than the planned price per leg so a market-order fill above the planned
# price can never push cumulative spend past `spendable` and breach the cash floor (GAI pre-ship
# catch: a HARD cash limit must reserve against slippage, not the planned price). 1% is very
# conservative for a 1-share order in the most-liquid mega-caps; it only ever SKIPS a marginal name.
_STARTER_SLIPPAGE_BUFFER = 1.01


def starter_trigger_pct(vix: float) -> float:
    """The DYNAMIC starter threshold as a negative % (SPY close move that arms the starter).
    −max(FLOOR, SLOPE×VIX)% — a ~2σ event across regimes (VIX 13→−2% floor, 20→−3%, 27→−4%).
    FLOOR (2.0) and SLOPE (0.15) are both positive config constants, so the max() is a positive
    magnitude that is then negated. A None VIX defaults to 0.0 → the FLOOR governs."""
    v = float(vix) if vix is not None else 0.0
    vix_component = config.FOREVER6_STARTER_TRIGGER_VIX_SLOPE * v
    return -max(config.FOREVER6_STARTER_TRIGGER_FLOOR_PCT, vix_component)


class ForeverHoldManager:
    """Forever-6 never-sell accumulation. 1a: starter evaluation is LOG-ONLY (no orders)."""

    def __init__(self, broker) -> None:
        self.broker = broker
        self.universe = list(config.FOREVER6_UNIVERSE)

    # ── state (per-month event cap) ────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            if _STATE.exists():
                d = json.loads(_STATE.read_text())
                if isinstance(d, dict):
                    return d
        except Exception as e:
            logger.warning("forever6: state read failed: %s", e)
        return {"events": []}

    def _events_this_month(self, state: dict) -> int:
        ym = datetime.now(PT).strftime("%Y-%m")
        return sum(1 for e in state.get("events", []) if str(e.get("date", "")).startswith(ym))

    # ── the starter evaluation (1a: returns a PLAN dict, logs it, places NO orders) ──
    def maybe_start_accumulation(
        self,
        spy_day_close_pct: float,
        vix: float,
        price_by_sym: dict[str, float] | None = None,
        held_qty_by_sym: dict[str, float] | None = None,
        settled_cash: float | None = None,
    ) -> dict:
        """Evaluate the Forever-6 starter on today's SPY close. LOG-ONLY in 1a.
        Returns {"triggered": bool, "reason": str, "plan": [{symbol, price, why}], "budget": float}."""
        thresh = starter_trigger_pct(vix)
        if spy_day_close_pct > thresh:   # not a big enough dip (both negative; > means shallower)
            return {"triggered": False, "reason": f"SPY {spy_day_close_pct:+.2f}% > trigger {thresh:+.2f}% (VIX {vix:.1f})", "plan": [], "budget": 0.0}

        state = self._load_state()
        # Durable per-DAY idempotency: if a starter event was already recorded today, do NOT
        # place again. This is the definitive anti-double-place guard for the run_cycle after-close
        # hook, which re-evaluates every closed-market cycle (and could otherwise re-fire after a
        # mid-evening restart). State-based → survives restarts, unlike an in-process flag.
        _today_str = datetime.now(PT).strftime("%Y-%m-%d")
        if any(isinstance(e, dict) and str(e.get("date", "")) == _today_str
               for e in state.get("events", [])):
            return {"triggered": True, "reason": f"already ran today ({_today_str})", "plan": [], "budget": 0.0}
        n_month = self._events_this_month(state)
        if n_month >= config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH:
            return {"triggered": True, "reason": f"per-month cap hit ({n_month}/{config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH})", "plan": [], "budget": 0.0}

        # Cash-only segregated budget: min(frac×cash, cash − floor), never negative.
        cash = float(settled_cash) if settled_cash is not None else self._fetch_cash()
        budget = min(config.FOREVER6_STARTER_CASH_FRAC_PER_EVENT * cash,
                     cash - config.FOREVER6_STARTER_CASH_FLOOR)
        if budget <= 0:
            return {"triggered": True, "reason": f"insufficient segregated cash (cash ${cash:.0f}, floor ${config.FOREVER6_STARTER_CASH_FLOOR:.0f})", "plan": [], "budget": 0.0}

        prices = price_by_sym or self._fetch_prices()
        held = held_qty_by_sym or {}

        # Candidate filter: catalyst screen (live gate) + affordable within remaining budget.
        try:
            import events.catalyst_engine as _cat
        except Exception:
            _cat = None  # type: ignore[assignment]
        candidates: list[tuple[str, float, int]] = []
        for sym in self.universe:
            px = prices.get(sym)
            if not px or px <= 0:
                continue
            if _cat is not None and _cat.has_blocking_catalyst(sym):
                logger.info("[F6] %s SKIPPED — active negative catalyst (screen)", sym)
                continue
            # held = FOREVER-6-TIER holdings only (0 for all until the tier is established) — NOT
            # QHM/intraday shares; the F6 starter builds the F6 base independently.
            candidates.append((sym, px, int(held.get(sym, 0) or 0)))

        # Breadth-first: F6-tier-0 names first (establish the base), then cheapest (fit more names).
        candidates.sort(key=lambda c: (c[2] > 0, c[1]))

        plan: list[dict] = []
        remaining = budget
        for sym, px, held_q in candidates:
            if len(plan) >= config.FOREVER6_STARTER_MAX_NAMES:
                break
            if px <= remaining:   # cash-only: only if 1 share fits the remaining segregated budget
                plan.append({"symbol": sym, "price": round(px, 2),
                             "why": ("new base" if held_q == 0 else f"add (held {held_q})")})
                remaining -= px

        # LOG-ONLY (1a): no orders placed.
        if plan:
            logger.warning("[F6] STARTER would ACCUMULATE (LOG-ONLY, dark) on SPY %+.2f%% (trigger %+.2f%%, VIX %.1f): "
                           "budget $%.0f → %s",
                           spy_day_close_pct, thresh, vix, budget,
                           ", ".join(f"{p['symbol']}@${p['price']}" for p in plan))
        else:
            logger.info("[F6] STARTER triggered but no fundable name (budget $%.0f) — screen/affordability filtered all.", budget)
        return {"triggered": True, "reason": "ok", "plan": plan, "budget": round(budget, 2)}

    # ── the starter EXECUTION (1b: CASH-ONLY orders, fail-closed) ────────────────
    def execute_starter(self, plan: list[dict], budget: float, settled_cash: float | None = None) -> dict:
        """Increment 1b: place CASH-ONLY market BUY orders for a starter PLAN (1 share per planned
        name), tier-tagged "forever6" so the ownership ledger protects them as never-sell shares.
        NEVER sells. FAIL-CLOSED: stops on the FIRST broker failure and never counts an unconfirmed
        order as placed (never-mask — a data/broker fault must not manufacture or hide a fill).

        PRECONDITION: the CALLER (run_cycle, increment 1c) has already checked config.FOREVER6_ENABLED
        AND the after-close timing. This method does the money-moving part only.
        Returns {"placed": [...], "skipped": [...], "spent": float, "reason": str}."""
        if not plan:
            return {"placed": [], "skipped": [], "spent": 0.0, "reason": "empty plan"}

        # C-3 (design 2026-07-17): refuse to place if a prior post-buy sync left the ledger
        # degraded (a fresh F6 lot unreflected). Blocks a BUY only — never a sell — the safe
        # direction for a never-sell book. Auto-cleared when a later sync verifies clean (C-2).
        if _read_sync_degraded():
            logger.critical("[F6] execute_starter BLOCKED — prior F6 sync degraded (a fresh "
                            "lot was left unreflected in the ledger). Not placing; resolve first.")
            _slack_safe(":rotating_light: F6 seed BLOCKED — prior sync degraded; further F6 "
                        "buys held until a clean ledger sync clears it.")
            return {"placed": [], "skipped": list(plan), "spent": 0.0,
                    "reason": "sync-degraded block"}

        # Re-verify SETTLED CASH at execution time. F6 is CASH-ONLY — margin is FORBIDDEN (the cold
        # board's decisive ruin finding: a maintenance call from ANY other strategy's bad day would
        # force-liquidate the never-sell book). settled_cash comes from account.cash, never buying_power.
        cash = float(settled_cash) if settled_cash is not None else self._fetch_cash()
        # Hard SEGREGATION guard: never spend into the dry powder the deep crash ladder reserves
        # (ammo-cannibalization ruin finding). spendable is bounded by BOTH the per-event budget and
        # the cash floor — whichever is tighter.
        spendable = min(float(budget), cash - config.FOREVER6_STARTER_CASH_FLOOR)
        if spendable <= 0:
            logger.warning("[F6] execute_starter: no spendable cash (cash $%.0f, floor $%.0f, budget $%.0f) — nothing placed",
                           cash, config.FOREVER6_STARTER_CASH_FLOOR, float(budget))
            return {"placed": [], "skipped": list(plan), "spent": 0.0, "reason": "cash-floor/budget guard"}

        from execution import broker as _bk
        placed: list[dict] = []
        skipped: list[dict] = []
        reserved = 0.0      # slippage-buffered UPPER BOUND on spend — the cash-floor guard variable
        for i, p in enumerate(plan):
            sym = str(p.get("symbol", "")).upper()
            try:
                px = float(p.get("price", 0) or 0)
            except (TypeError, ValueError):
                px = 0.0
            if not sym or px <= 0:
                skipped.append({**p, "skip": "bad symbol/price"})
                continue
            # Reserve a slippage-buffered amount; a market fill above the planned price can then NEVER
            # push cumulative spend past `spendable` and breach the cash floor (ruin finding #1).
            px_reserve = px * _STARTER_SLIPPAGE_BUFFER
            if px_reserve > (spendable - reserved):
                skipped.append({**p, "skip": "budget/floor exhausted"})
                continue
            # CASH-ONLY 1-share market buy, tier-tagged "forever6" (never-sell attribution).
            try:
                order = _bk.submit_market_order(sym, 1, "buy", tier="forever6")
            except Exception as e:
                logger.error("[F6] BUY EXCEPTION %s: %s — STOPPING starter (fail-closed)", sym, e)
                skipped.extend({**q, "skip": "stopped after broker exception"} for q in plan[i + 1:])
                break
            if order is None:
                # broker returned None → NOT a confirmed order. Never-mask: do not count it placed,
                # and do NOT keep placing (a systemic fault would repeat down the plan).
                logger.error("[F6] BUY UNCONFIRMED %s (broker returned None) — STOPPING starter (fail-closed)", sym)
                skipped.extend({**q, "skip": "stopped after unconfirmed order"} for q in plan[i + 1:])
                break
            oid = getattr(order, "id", None)
            # The cash-floor guard is enforced ENTIRELY by `reserved` (the slippage-buffered upper
            # bound) above — it does not depend on the fill price. Record the PLANNED price as display
            # metadata; Alpaca holds the authoritative fill / cost-basis (a market order's fill price is
            # not reliably available synchronously at submit, and duplicating it here adds no safety).
            placed.append({"symbol": sym, "qty": 1, "price": round(px, 2),
                           "reserved": round(px_reserve, 2), "order_id": str(oid) if oid else None})
            reserved += px_reserve
            logger.warning("[F6] STARTER BUY placed: %s 1sh planned~$%.2f (reserved $%.2f, order %s)", sym, px, px_reserve, oid)

        planned_spent = round(sum(float(p["price"]) for p in placed), 2)
        if placed:
            self._record_event(placed, planned_spent)
            self._verify_ledger_reflects(placed)   # C-2: sync + verify the F6 floor landed
        return {"placed": placed, "skipped": skipped, "spent": planned_spent, "reason": "ok"}

    def _verify_ledger_reflects(self, placed: list[dict]) -> None:
        """C-2 wrapper — STRUCTURALLY enforces 'never raises into execute_starter': on ANY
        unexpected error it fail-CLOSES (sets the persisted degraded flag + alerts), so a
        crash mid-verify can never leave the buy placed-but-unverified with the next-seed
        block silently disarmed (cold-2nd 2026-07-17). The block-at-entry guarantees the
        flag was clear before we placed, so a fail-closed SET here is always correct."""
        try:
            self._verify_ledger_reflects_inner(placed)
        except Exception as _e:
            logger.critical("[F6] post-buy verify: UNEXPECTED error (%s) — fail-closed: "
                            "setting degraded flag + blocking further F6 seeding.", _e)
            _slack_safe(f":rotating_light: F6 post-buy verify crashed ({_e}) — further F6 "
                        f"seeding BLOCKED (fail-closed). Verify anchor(s) before next restart.")
            _write_sync_degraded(True, f"verify exception: {_e}")

    def _verify_ledger_reflects_inner(self, placed: list[dict]) -> None:
        """C-2 (design logs/f6_prereq1_syncgap_design_2026-07-17.md): after an F6 buy, run
        the authoritative full-replay sync (run_ledger_sync.sync_once) and verify EACH
        placed symbol now has forever6 qty >= bought AND abs(drift) <= eps, retrying with
        backoff (a just-placed market fill can lag the fills feed → healed=True with the lot
        still absent is a real, expected outcome, so the ledger must be positively checked).
        Also alerts if the sync planted NEW drift on a previously-clean protected symbol
        (the multi-tier landmine). A placed order that is TERMINALLY rejected/canceled is
        dropped from the wait-set (no anchor to protect → no false 'unprotected' alert). On
        retry-budget exhaustion with a still-pending fill: CRITICAL + Slack + set the
        persisted degraded flag (blocks the next seed, C-3). Never sells; never raises into
        execute_starter. The block-at-entry guarantees the flag was clear before we placed,
        so writing degraded=False on success only ever CONFIRMS clean (no spurious unblock)."""
        import time as _time
        try:
            import run_ledger_sync as _rls
            from execution import broker as _bk
            from execution import ownership_guard as _og
        except Exception as e:
            logger.critical("[F6] post-buy verify: import failed (%s) — setting degraded flag", e)
            _write_sync_degraded(True, f"import failure: {e}")
            return

        want: dict[str, int] = {}
        oid_by_sym: dict[str, str] = {}
        for p in placed:
            s = str(p.get("symbol", "")).upper()
            if not s:
                continue
            want[s] = want.get(s, 0) + int(p.get("qty", 1) or 1)
            if p.get("order_id"):
                oid_by_sym[s] = str(p["order_id"])

        # Snapshot previously-clean protected symbols (floor>0, drift≈0) BEFORE syncing, so a
        # drift this sync PLANTS on one of them (the multi-tier §2 landmine) is surfaced.
        try:
            _before = _og.load_ledger()
            _clean_before = {
                s for s in _before.get("positions", {})
                if _og.protected_floor(_before, s) > _EPS
                and abs(float(_before["positions"][s].get("drift", 0.0) or 0.0)) <= _EPS
            }
        except Exception:
            _clean_before = set()

        _delays = (2.0, 4.0, 8.0)
        for _attempt in range(len(_delays) + 1):
            # Drop terminally-rejected orders — no anchor exists, so do not wait/alert on them.
            for s in list(want):
                _oid = oid_by_sym.get(s)
                if not _oid:
                    continue
                try:
                    _st = str(getattr(_bk.get_order(_oid), "status", "") or "").lower()
                except Exception:
                    _st = ""
                if _st in ("rejected", "canceled", "cancelled", "expired"):
                    logger.warning("[F6] post-buy verify: %s order %s is %s — no anchor; "
                                   "dropping from wait-set.", s, _oid, _st)
                    want.pop(s, None)
            if not want:
                logger.info("[F6] post-buy verify: no pending anchors to verify.")
                _write_sync_degraded(False, "no pending anchors")
                return

            _rls.sync_once()
            try:
                led = _og.load_ledger()
            except Exception as e:
                logger.warning("[F6] post-buy verify: ledger unreadable after sync: %s", e)
                led = None

            if led is not None:
                _pos = led.get("positions", {})
                _all_ok = True
                for s, q in want.items():
                    _f6 = _og.tier_qty(led, s, "forever6")
                    _drift = abs(float(_pos.get(s, {}).get("drift", 0.0) or 0.0))
                    if _f6 + _EPS < q or _drift > _EPS:
                        _all_ok = False
                        break
                _new_drift = [s for s in _clean_before
                              if abs(float(_pos.get(s, {}).get("drift", 0.0) or 0.0)) > _EPS]
                if _new_drift:
                    logger.critical("[F6] post-buy sync planted drift on previously-clean "
                                    "protected %s — verify before enabling exits.", _new_drift)
                    _slack_safe(f":rotating_light: F6 post-seed sync planted drift on "
                                f"{_new_drift} — verify before enabling exits.")
                if _all_ok:
                    logger.warning("[F6] post-buy verify OK: %s ledger-reflected "
                                   "(forever6 floor set, drift≈0).", dict(want))
                    _write_sync_degraded(False, "verified")
                    return

            if _attempt < len(_delays):
                _time.sleep(_delays[_attempt])

        logger.critical("[F6] post-buy verify FAILED after retries — anchor(s) %s not "
                        "ledger-reflected (floor dormant anyway; verify before next "
                        "restart). Blocking further F6 seeding.", dict(want))
        _slack_safe(f":rotating_light: F6 anchor(s) {list(want)} NOT ledger-reflected after "
                    f"retries — further F6 seeding BLOCKED until a clean sync. Verify before "
                    f"next restart.")
        _write_sync_degraded(True, f"verify failed for {list(want)}")

    def _record_event(self, placed: list[dict], spent: float) -> None:
        """Durably append a starter event — the SOURCE OF TRUTH for the per-month event cap.
        Atomic (RC-5 tmp→fsync→replace). Recorded AFTER confirmed placement so a failed/partial
        placement never phantom-counts against the cap; a persist failure is logged LOUDLY (the cap
        may then under-count, allowing a later extra event — the safe-side error for an anti-overtrade
        cap, vs. blocking legitimate events by over-counting)."""
        try:
            state = self._load_state()
            events = state.get("events", [])
            if not isinstance(events, list):
                events = []
            now_pt = datetime.now(PT)   # capture once — no date/ts skew across a midnight boundary
            events.append({
                "date": now_pt.strftime("%Y-%m-%d"),
                "ts":   now_pt.isoformat(),
                "placed": placed,
                "spent": round(float(spent), 2),
            })
            state["events"] = events
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STATE.with_suffix(f".json.{os.getpid()}.tmp")
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(_STATE))
        except Exception as e:
            logger.error("[F6] _record_event FAILED to persist starter event — per-month cap may "
                         "under-count next trigger: %s", e)

    # ── read helpers (1a) ──────────────────────────────────────────────────────
    def _fetch_cash(self) -> float:
        try:
            acct = self.broker.get_account()
            return float(getattr(acct, "cash", 0) or 0)
        except Exception as e:
            logger.warning("forever6: cash fetch failed: %s", e)
            return 0.0

    def _fetch_prices(self) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            from data.alpaca_data import get_latest_trade
        except Exception:
            return out
        for sym in self.universe:
            try:
                p = get_latest_trade(sym)
                if p and p > 0:
                    out[sym] = float(p)
            except Exception as e:
                logger.debug("forever6: price fetch %s failed: %s", sym, e)
        return out

    # ── EXIT: trims only (F6 exit increment, 2026-08-05) ───────────────────────
    def evaluate_and_execute_trims(self) -> dict:
        """FOREVER-6 EXIT: trim FOREVER6_TRIM_1_FRAC (25%) of current forever6 holdings at
        FOREVER6_TRIM_1_MULT (10x) unrealized gain, another FOREVER6_TRIM_2_FRAC (25%) of
        what remains at FOREVER6_TRIM_2_MULT (20x). NEVER a full sell — the house-money core
        keeps shrinking, never disappears. No stop, no max-hold, no other exit exists for
        this tier (logs/forever6_integration_map_2026-07-09.md §6).

        Sequential within one pass: the 10x trim (if newly triggered) executes and its
        result feeds the 20x check in the SAME call, so a single overnight gap past both
        thresholds still fires both rungs in order, each against the correctly-shrunk qty —
        never a double-sized trim from evaluating both against the pre-trim qty.

        RESERVE-THEN-CONFIRM per rung (GAI preship catch, 2026-08-05): each rung's state is
        "none" → "reserved" → "done". The "reserved" write is durable-confirmed BEFORE the
        sell is even attempted — so a crash/restart between reservation and sell always
        finds "reserved" and refuses to re-fire (fail closed), never "none" again. A sell
        that fails after reservation ALSO stays "reserved" forever (never reverts to
        "none") — this makes a lost/failed _save_trim_state or a failed sell both degrade
        to "block and alert," never to "silently retry and over-trim." Clearing a stuck
        "reserved" back to firing requires a human to edit forever6_trims.json after
        verifying against Alpaca's own order history — there is no auto-retry path.

        A trim that rounds to 0 shares (25% of a 1-2 share position) is SKIPPED, not
        forced up to a full-position sell — trimming a fraction must never become
        liquidating the whole position; the position is re-evaluated next cycle in case a
        deeper crash-ladder rung later adds enough shares to make a fractional trim
        possible. Per-symbol failure is isolated — one symbol's read/compute error never
        blocks evaluation of the others. Returns {symbol: {"gain_mult": float, "fired":
        [(label, qty), ...]}} for symbols where a trim executed this pass.

        KNOWN PRE-LIVE-FLIP GATE (cold-2nd fresh-review, 2026-08-05, fails safe — not
        blocking this ship since FOREVER6_ENABLED stays False): a same-pass 10x-then-20x
        double trim calls the guard twice in quick succession, and the guard's drift-freeze
        check (ownership_guard.check_never_sell_floor) compares the ownership ledger's
        tier-sum (refreshed only by the standalone run_ledger_sync cron, not resynced
        mid-pass here) against Alpaca's live net qty. If trim1's fill reaches Alpaca before
        the ledger resyncs, trim2's guard call can see a transient mismatch and REJECT —
        which this code correctly treats as a sell failure (rung stuck 'reserved',
        alerting, blocking) rather than an over-trim, but means a same-pass double trim may
        not reliably fire both rungs in practice. Fix before flipping FOREVER6_ENABLED:
        force a ledger resync between the two rungs, mirroring the buy-side's
        _verify_ledger_reflects pattern.
        """
        results: dict[str, dict] = {}
        try:
            from execution import ownership_guard as _og
        except Exception as e:
            logger.error("[F6] evaluate_and_execute_trims: ownership_guard import failed "
                        "— skipping this cycle: %s", e)
            return results
        try:
            ledger = _og.load_ledger()
        except Exception as e:
            logger.warning("[F6] evaluate_and_execute_trims: ledger unreadable — "
                           "skipping this cycle: %s", e)
            return results

        # Fail the WHOLE pass closed on a corrupt (not merely absent) trim-state file —
        # checked once, up front, so a corruption doesn't silently read as "no trim has
        # ever happened" for every symbol (see _TrimStateUnreadable's docstring).
        try:
            self._load_trim_state()
        except _TrimStateUnreadable as e:
            logger.critical("[F6] evaluate_and_execute_trims: trim state file is "
                            "corrupt/unreadable — refusing ALL trim evaluation this "
                            "cycle (fail closed) until it's manually repaired: %s", e)
            _slack_safe(f":rotating_light: F6 forever6_trims.json is corrupt/unreadable "
                        f"— ALL trims blocked until manually repaired: {e}")
            return results

        prices = self._fetch_prices()

        for sym in self.universe:
            try:
                own = _og.tier_qty(ledger, sym, "forever6")
                if own <= _EPS:
                    continue
                entry = ledger.get("positions", {}).get(sym, {})
                avg_cost = float(
                    entry.get("tiers", {}).get("forever6", {}).get("avg_cost", 0.0) or 0.0
                )
                if avg_cost <= 0:
                    logger.debug("[F6] %s: forever6 qty=%s but avg_cost<=0 — skipping trim "
                                "eval this cycle.", sym, own)
                    continue
                px = prices.get(sym)
                if not px or px <= 0:
                    continue
                gain_mult = px / avg_cost
                current_qty = own
                fired: list[tuple[str, int]] = []

                if gain_mult >= config.FOREVER6_TRIM_1_MULT:
                    qty1 = int(current_qty * config.FOREVER6_TRIM_1_FRAC)
                    ok1, sold1 = self._try_rung(sym, "trim1", qty1, "10x", gain_mult)
                    if ok1:
                        current_qty -= sold1
                        fired.append(("10x", sold1))

                if gain_mult >= config.FOREVER6_TRIM_2_MULT:
                    _trim1_status = self._load_trim_state().get(sym, {}).get("trim1", "none")
                    if _trim1_status != "done":
                        # 20x must never fire ahead of (or instead of) 10x — "another 25%
                        # of what remains" presupposes the first trim already happened.
                        # Covers: trim1 not yet attempted, trim1's qty rounded to 0 (too
                        # small to trim), and trim1 stuck "reserved" after a failed sell.
                        logger.debug(
                            "[F6] %s: 20x trim trigger met (gain %.1fx) but trim1 status "
                            "is %r, not 'done' — skipping 20x until trim1 completes.",
                            sym, gain_mult, _trim1_status)
                    else:
                        qty2 = int(current_qty * config.FOREVER6_TRIM_2_FRAC)
                        ok2, sold2 = self._try_rung(sym, "trim2", qty2, "20x", gain_mult)
                        if ok2:
                            fired.append(("20x", sold2))

                if fired:
                    results[sym] = {"gain_mult": round(gain_mult, 2), "fired": fired}
                    _slack_safe(f":moneybag: F6 TRIM {sym}: gain {gain_mult:.1f}x — {fired}")
            except Exception as e:
                logger.warning("[F6] evaluate_and_execute_trims: %s failed, skipping: %s",
                               sym, e)
                continue

        return results

    def _try_rung(self, symbol: str, rung: str, qty: int, label: str,
                  gain_mult: float) -> tuple[bool, int]:
        """Attempt ONE trim rung ("trim1" or "trim2") with reserve-then-confirm state.
        Returns (fired: bool, qty_sold: int). Never re-fires a rung whose state is
        anything other than "none" — "reserved" (whether from a persist race or an actual
        sell failure) and "done" both permanently block further auto-attempts for this
        rung on this symbol until a human clears the state file."""
        state = self._load_trim_state()
        sym_state = dict(state.get(symbol, {}))
        status = sym_state.get(rung, "none")

        if status not in ("none", "reserved", "done"):
            # Fail CLOSED on an unrecognized value (corruption, or a typo during the
            # documented manual-clear procedure) — never fall through and treat an
            # unknown status the same as "none" (cold-2nd fresh-review catch, 2026-08-05).
            logger.critical(
                "[F6] %s %s rung has an UNRECOGNIZED state %r — treating as blocked, "
                "NOT as 'none'. Manually correct forever6_trims.json to one of "
                "none/reserved/done.", symbol, label, status)
            _slack_safe(f":rotating_light: F6 {symbol} {label} trim state is "
                        f"unrecognized ({status!r}) — blocked until manually corrected.")
            return False, 0
        if status == "done":
            return False, 0
        if status == "reserved":
            logger.critical(
                "[F6] %s %s rung is STUCK at 'reserved' (gain %.1fx) — a prior attempt "
                "either failed to persist or the sell itself failed. NOT auto-retrying. "
                "Verify against Alpaca's order history, then manually clear "
                "forever6_trims.json before this rung can fire.", symbol, label, gain_mult)
            _slack_safe(f":rotating_light: F6 {symbol} {label} trim STUCK at 'reserved' "
                        f"— manual verification required before it can fire.")
            return False, 0
        if qty < 1:
            logger.info(
                "[F6] %s: %s trim trigger met (gain %.1fx) but the computed qty rounds "
                "to 0 — skipping (position too small to fractionally trim).",
                symbol, label, gain_mult)
            return False, 0

        # Reserve FIRST, durably, before any sell is attempted. A write failure here
        # means we never sell this cycle — fail closed, never sell without a prior
        # confirmed reservation.
        sym_state[rung] = "reserved"
        state[symbol] = sym_state
        if not self._save_trim_state(state):
            logger.error(
                "[F6] %s: %s trim reservation FAILED to persist — refusing to sell this "
                "cycle (fail closed). Will retry reservation next cycle.", symbol, label)
            return False, 0

        ok = self._submit_trim(symbol, qty, label)

        # Whether the sell succeeded or failed, the rung stays "reserved" — success will
        # be upgraded to "done" below; failure deliberately does NOT revert to "none"
        # (never assume a failed-looking order didn't partially execute).
        if ok:
            # Re-load fresh rather than reuse the pre-sell snapshot (cold-2nd fresh-review
            # catch, 2026-08-05): _submit_trim is a blocking network call, and writing back
            # a stale in-memory `state` here could silently clobber a concurrent manual
            # edit to a DIFFERENT symbol/rung made during that window — exactly the kind of
            # edit this design's own alerts tell an operator to make.
            state = self._load_trim_state()
            sym_state = dict(state.get(symbol, {}))
            sym_state[rung] = "done"
            state[symbol] = sym_state
            if not self._save_trim_state(state):
                logger.critical(
                    "[F6] %s: %s trim EXECUTED (sold %s share(s)) but the 'done' state "
                    "write FAILED — rung stays 'reserved', so it will correctly block "
                    "(not re-fire) next cycle, but needs manual confirmation it "
                    "actually completed: %s", symbol, label, qty, symbol)
                _slack_safe(f":rotating_light: F6 {symbol} {label} trim executed but "
                            f"state write failed — verify and clear manually.")
            return True, qty

        logger.error(
            "[F6] %s: %s trim sell FAILED after reservation — rung stays 'reserved' "
            "(blocks future auto-attempts; verify against Alpaca before manual clear).",
            symbol, label)
        _slack_safe(f":rotating_light: F6 {symbol} {label} trim sell FAILED after "
                    f"reservation — verify against Alpaca's order history, then "
                    f"manually clear forever6_trims.json before retrying.")
        return False, 0

    def _submit_trim(self, symbol: str, qty: int, label: str) -> bool:
        """Execute one trim leg via the sole authorized broker path. Never raises."""
        try:
            from execution import broker as _bk
            ok = _bk.submit_f6_trim(symbol, qty)
        except Exception as e:
            logger.error("[F6] TRIM EXCEPTION %s (%s trigger): %s", symbol, label, e)
            return False
        if ok:
            logger.warning("[F6] TRIM EXECUTED %s: sold %s share(s) (%s trigger)",
                           symbol, qty, label)
        else:
            logger.error("[F6] TRIM FAILED %s: broker rejected/failed the %s trim "
                        "sell of %s share(s)", symbol, label, qty)
        return ok

    def _load_trim_state(self) -> dict:
        """Returns {} ONLY when the file is genuinely absent (a legitimate fresh state —
        no trim has ever been recorded). A PRESENT-but-unreadable/corrupt file raises
        _TrimStateUnreadable instead of silently returning {} (GAI preship catch,
        2026-08-05): treating corruption the same as "fresh" would let a successful
        reserve-write for ONE symbol overwrite every OTHER symbol's already-recorded
        "reserved"/"done" state with a blank slate, reopening the exact re-fire risk this
        whole mechanism exists to close. Callers must let this propagate and skip
        evaluation entirely rather than catch-and-treat-as-empty."""
        if not _TRIM_STATE.exists():
            return {}
        try:
            d = json.loads(_TRIM_STATE.read_text())
        except Exception as e:
            raise _TrimStateUnreadable(f"forever6_trims.json unreadable: {e}") from e
        if not isinstance(d, dict):
            raise _TrimStateUnreadable(f"forever6_trims.json is not a dict: {type(d).__name__}")
        return d

    def _save_trim_state(self, state: dict) -> bool:
        """Atomic tmp→fsync→replace (RC-5). Returns True on confirmed success, False on
        failure — callers MUST check this return value: the reserve-then-confirm design
        depends on the caller refusing to proceed (to a sell, or to marking "done") when
        this returns False, not on this function raising."""
        try:
            _TRIM_STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _TRIM_STATE.with_suffix(f".json.{os.getpid()}.tmp")
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(_TRIM_STATE))
            return True
        except Exception as e:
            logger.critical("[F6] trim state persist FAILED: %s", e)
            _slack_safe(f":rotating_light: F6 trim state persist FAILED ({e}) — verify "
                        f"forever6_trims.json manually before the next after-close cycle.")
            return False
