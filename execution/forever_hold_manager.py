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
        return {"placed": placed, "skipped": skipped, "spent": planned_spent, "reason": "ok"}

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
