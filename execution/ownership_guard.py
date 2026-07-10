"""
execution/ownership_guard.py — per-tier shared-lot ownership ledger + never-sell floor.

Phase 0-a foundation (board + Gro + GAI design 2026-07-09; spec in
logs/phase0_ownership_guard_spec_2026-07-09.md). NOT YET WIRED — this module is the
foundation; broker.py / exit_logic.py / entry_logic.py call sites are rewired in
later increments. Importing it has zero runtime effect until a caller invokes it.

WHY: Alpaca holds ONE net position per symbol with no per-strategy tag. Multiple tiers
(intraday, qhm, forever6) may hold the same symbol. This ledger tracks each tier's
share count as a CLAIM; Alpaca's net position is GROUND TRUTH. A single chokepoint,
check_never_sell_floor(), gates every position-REDUCING order so the net Alpaca long
can NEVER drop below the protected floor (forever6_qty + qhm_qty) — the never-sell book.

Two catastrophic-mode defenses baked in:
  1. The floor is LEDGER-derived ONLY, never recomputed from Alpaca's (possibly drifted)
     net position — a drift must FREEZE sells, not silently shrink the floor.
  2. Fail-CLOSED on any ambiguity (stale/unreadable ledger, Alpaca read failure, drift,
     unknown tier) — never sell when unsure.

Rafael-locked (2026-07-09): ring-fenced names are LONG-ONLY for the share tiers
(bearish exposure → options program). So a short on a floor>0 symbol is REJECTED here.

Data tier: reads Alpaca positions via execution.broker (T1). State file RC-5 atomic.
"""
from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_LEDGER_PATH = _ROOT / "data" / "state" / "ownership_ledger.json"

Tier = Literal["intraday", "qhm", "forever6"]
_TIERS: tuple[str, ...] = ("intraday", "qhm", "forever6")
# 2-char client_order_id tier prefixes (parse-friendly, short).
_TIER_CODE = {"intraday": "IN", "qhm": "QH", "forever6": "F6"}
_CODE_TIER = {v: k for k, v in _TIER_CODE.items()}

# Tiers whose shares are ring-fenced (never sold by another tier). Their combined
# qty in a symbol is the protected floor.
_PROTECTED_TIERS: tuple[str, ...] = ("forever6", "qhm")

_QTY_EPS = 1e-6


# ── client_order_id tagging ───────────────────────────────────────────────────
def make_coid(tier: str, symbol: str, side: str, epoch_ms: int, uniq: str) -> str:
    """Build a tier-tagged client_order_id: {TIER2}-{symbol}-{side1}-{epoch_ms}-{uniq}.
    Generated ONCE per order and reused across retries (preserves broker idempotency).
    `side1` is a single char b(uy)/s(ell). Parse the tier back with tier_of_coid()."""
    code = _TIER_CODE.get(tier)
    if code is None:
        raise ValueError(f"unknown tier {tier!r} for client_order_id")
    s1 = "b" if str(side).lower().startswith("b") else "s"
    return f"{code}-{symbol}-{s1}-{int(epoch_ms)}-{uniq}"


def tier_of_coid(client_order_id: Optional[str]) -> Optional[str]:
    """Return the tier a client_order_id belongs to, or None if untagged/unparseable.
    Attribution is by the prefix ONLY — never inferred from qty deltas."""
    if not client_order_id or "-" not in client_order_id:
        return None
    return _CODE_TIER.get(client_order_id.split("-", 1)[0])


# ── GuardResult ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GuardResult:
    action: Literal["APPROVE", "QTY_BOUND", "REJECT"]
    qty: float          # the qty the caller may submit (0 on REJECT)
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action in ("APPROVE", "QTY_BOUND") and self.qty > _QTY_EPS


# ── Ledger I/O (RC-5 atomic) ──────────────────────────────────────────────────
def _empty_ledger() -> dict:
    return {"version": 1, "last_reconciled_utc": None, "positions": {}}


def load_ledger() -> dict:
    """Load the ownership ledger. Raises LedgerError on a present-but-corrupt file so
    the guard fails CLOSED (an absent file is a fresh/empty ledger, which is fine)."""
    if not _LEDGER_PATH.exists():
        return _empty_ledger()
    try:
        data = json.loads(_LEDGER_PATH.read_text())
        if not isinstance(data, dict) or "positions" not in data:
            raise LedgerError("ledger schema invalid")
        return data
    except LedgerError:
        raise
    except Exception as e:  # RC-3
        raise LedgerError(f"ledger unreadable/corrupt: {e}") from e


def save_ledger(ledger: dict) -> None:
    """Atomic tmp→replace write (RC-5)."""
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LEDGER_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(_LEDGER_PATH)


class LedgerError(Exception):
    """Raised on a present-but-corrupt ledger — callers must fail closed."""


# ── Accessors ─────────────────────────────────────────────────────────────────
def _entry(ledger: dict, symbol: str) -> Optional[dict]:
    return ledger.get("positions", {}).get(symbol)


def tier_qty(ledger: dict, symbol: str, tier: str) -> float:
    e = _entry(ledger, symbol)
    if not e:
        return 0.0
    return float(e.get("tiers", {}).get(tier, {}).get("qty", 0.0) or 0.0)


def protected_floor(ledger: dict, symbol: str) -> float:
    """forever6_qty + qhm_qty — LEDGER-derived ONLY (never from Alpaca net)."""
    return sum(tier_qty(ledger, symbol, t) for t in _PROTECTED_TIERS)


def get_combined_symbol_exposure(ledger: dict, symbol: str) -> float:
    """Total shares across ALL tiers for a symbol (the single source for the
    correlated-exposure and overnight-budget invariants — counted once, never
    double-counted per tier)."""
    e = _entry(ledger, symbol)
    if not e:
        return 0.0
    return sum(float(t.get("qty", 0.0) or 0.0) for t in e.get("tiers", {}).values())


# ── The chokepoint ────────────────────────────────────────────────────────────
def check_never_sell_floor(
    symbol: str,
    tier: str,
    qty: float,
    side: str,
    alpaca_net_qty: Optional[float],
    is_authorized_f6_trim: bool = False,
) -> GuardResult:
    """The ONLY sanction for a position-REDUCING order (sell of a long / short entry /
    partial or full close). Callers pass the LIVE Alpaca net qty for the symbol (or None
    if the read failed). Fails CLOSED on every ambiguity.

    side: "sell" (reduce a long) or "short" (open/increase a short).
    """
    if qty is None or qty <= _QTY_EPS:
        return GuardResult("REJECT", 0.0, "non-positive qty")
    if tier not in _TIERS:
        return GuardResult("REJECT", 0.0, f"unknown tier {tier!r}")

    # STEP 0 — fail-closed preconditions
    try:
        ledger = load_ledger()
    except LedgerError as e:
        return GuardResult("REJECT", 0.0, f"ledger unreadable — fail closed: {e}")
    if alpaca_net_qty is None:
        return GuardResult("REJECT", 0.0, "Alpaca net qty unavailable — fail closed")

    _ent = _entry(ledger, symbol)
    if _ent is not None:
        _drift = float(_ent.get("drift", 0.0) or 0.0)
        if abs(_drift) > _QTY_EPS:
            return GuardResult(
                "REJECT", 0.0, f"LEDGER_DRIFT {symbol} drift={_drift} — sells frozen")

    # STEP 1 — reconciliation: ledger tier-sum must equal Alpaca net, else FREEZE
    ledger_sum = get_combined_symbol_exposure(ledger, symbol)
    if abs(ledger_sum - float(alpaca_net_qty)) > _QTY_EPS:
        return GuardResult(
            "REJECT", 0.0,
            f"drift ledger={ledger_sum} alpaca={alpaca_net_qty} — fail closed")

    floor = protected_floor(ledger, symbol)      # ledger-derived ONLY (forever6 + qhm)
    own = tier_qty(ledger, symbol, tier)
    net = float(alpaca_net_qty)
    # A protected tier (qhm / forever6) selling its OWN shares reduces its own
    # contribution to the floor, so its sell must only stay above the OTHER protected
    # tiers' qty — NOT the full floor (which would wrongly block QHM from exiting its
    # own shares). For a non-protected tier (intraday) the effective floor is the full
    # forever6+qhm. Example: net=4 intraday=1 qhm=2 f6=1 → a QHM sell of its own 2 must
    # only respect forever6(1), so effective_floor=1 and QHM can sell both.
    _own_protected = own if tier in _PROTECTED_TIERS else 0.0
    effective_floor = floor - _own_protected

    # STEP 2 — short on a ring-fenced (floor>0) name: REJECTED (long-only,
    # Rafael 2026-07-09; bearish exposure → options program).
    if str(side).lower() == "short":
        if floor > _QTY_EPS:
            return GuardResult(
                "REJECT", 0.0, "ring-fenced name is long-only (shorts→options)")
        # floor==0 → no protected exposure → ordinary short (net may go negative).
        return GuardResult("APPROVE", qty, "no protected floor — short permitted")

    # STEP 3 — sells, per tier
    if tier == "forever6":
        if not is_authorized_f6_trim:
            return GuardResult(
                "REJECT", 0.0, "forever6 reduces only via authorized +1000/+2000 trim")
        bounded = min(qty, own)
        if bounded <= _QTY_EPS:
            return GuardResult("REJECT", 0.0, "forever6 trim exceeds own qty")
        return GuardResult("QTY_BOUND" if bounded < qty else "APPROVE", bounded,
                           "f6 authorized trim")

    # intraday / qhm sell: bound to the tier's OWN qty AND keep net >= effective_floor.
    bounded = min(qty, own)                       # never sell more than the tier owns
    if net - bounded < effective_floor - _QTY_EPS:
        allowed = net - effective_floor           # max sellable before breaching floor
        if allowed <= _QTY_EPS:
            return GuardResult(
                "REJECT", 0.0,
                f"floor binding (net={net} floor={effective_floor}) — 0 sellable")
        bounded = min(bounded, allowed)
    if bounded <= _QTY_EPS:
        return GuardResult("REJECT", 0.0, f"{tier} has no sellable qty (own={own})")
    return GuardResult("QTY_BOUND" if bounded < qty else "APPROVE", bounded,
                       f"{tier} sell bounded to {bounded}")


# ── Drift reconcile ───────────────────────────────────────────────────────────
def reconcile_drift(alpaca_positions: list) -> dict:
    """Compare ledger tier-sums to Alpaca live positions; set per-symbol `drift`
    (alpaca_net - ledger_sum). Any nonzero drift → that symbol is frozen for sells by
    the guard (STEP 0/1). Returns {symbol: drift} for the drifted symbols. Never raises
    into a caller — on a corrupt ledger it logs and returns {} (guard already fails
    closed on the corrupt read)."""
    try:
        ledger = load_ledger()
    except LedgerError as e:
        logger.critical(
            "reconcile_drift: ledger unreadable (%s) — guard fails closed", e)
        return {}
    alp = {}
    for p in (alpaca_positions or []):
        try:
            alp[p.get("symbol")] = float(p.get("qty", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    drifted: dict = {}
    positions = ledger.setdefault("positions", {})
    _syms = set(positions) | set(alp)
    for sym in _syms:
        ledger_sum = get_combined_symbol_exposure(ledger, sym)
        net = alp.get(sym, 0.0)
        drift = round(net - ledger_sum, 6)
        if sym in positions:
            positions[sym]["alpaca_net_qty"] = net
            positions[sym]["drift"] = drift
        if abs(drift) > _QTY_EPS:
            drifted[sym] = drift
            logger.critical(
                "OWNERSHIP DRIFT %s: alpaca=%s ledger=%s drift=%s — sells FROZEN",
                sym, net, ledger_sum, drift,
            )
    ledger["last_reconciled_utc"] = datetime.now(timezone.utc).isoformat()
    save_ledger(ledger)
    return drifted


# ── Launch init (run ONCE) ────────────────────────────────────────────────────
def launch_init(alpaca_positions: list, force: bool = False) -> dict:
    """Seed the ledger from the current Alpaca book, attributing ALL existing shares to
    INTRADAY (forever6/qhm start at 0 — Rafael 2026-07-09). NO orders sent. Refuses to
    run if the ledger already exists with any nonzero forever6/qhm qty (re-seeding would
    erase real ownership) unless force=True. Idempotent for a fresh/all-intraday ledger.
    """
    if _LEDGER_PATH.exists() and not force:
        try:
            existing = load_ledger()
        except LedgerError as e:
            return {"seeded": False, "reason": f"existing ledger corrupt: {e}"}
        for sym, ent in existing.get("positions", {}).items():
            for pt in _PROTECTED_TIERS:
                _q = float(ent.get("tiers", {}).get(pt, {}).get("qty", 0.0) or 0.0)
                if abs(_q) > _QTY_EPS:
                    return {
                        "seeded": False,
                        "reason": f"ledger already has {pt} qty in {sym} — no reseed",
                    }
    ledger = _empty_ledger()
    stamp = f"SEED-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    seeded = []
    for p in (alpaca_positions or []):
        sym = p.get("symbol")
        if not sym:
            continue
        try:
            q = float(p.get("qty", 0.0) or 0.0)
            avg = float(p.get("avg_entry_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        ledger["positions"][sym] = {
            "alpaca_net_qty": q,
            "tiers": {
                "intraday": {"qty": q, "avg_cost": avg, "last_fill_id": stamp},
                "qhm": {"qty": 0.0, "avg_cost": 0.0, "last_fill_id": None},
                "forever6": {"qty": 0.0, "avg_cost": 0.0, "last_fill_id": None},
            },
            "drift": 0.0,
        }
        seeded.append({"symbol": sym, "qty": q, "avg_cost": avg})
    ledger["last_reconciled_utc"] = datetime.now(timezone.utc).isoformat()
    save_ledger(ledger)
    logger.info("ownership ledger seeded: %d symbol(s) → intraday", len(seeded))
    return {"seeded": True, "symbols": seeded}
