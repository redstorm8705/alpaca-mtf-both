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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger("forever6")

PT = ZoneInfo("America/Los_Angeles")
_ROOT = Path(__file__).resolve().parent.parent
_STATE = _ROOT / "data" / "state" / "forever6_holds.json"


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
