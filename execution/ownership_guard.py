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
  2. Directional fail-safety (board 4-0, 2026-07-11): fail-CLOSED (block the sell) on any
     ambiguity ONLY for a symbol that HAS a protected floor (qhm/forever6 > 0). For a
     symbol with NO protected floor there is nothing to protect, so an ordinary reducing
     order (an intraday stop-loss/target) is APPROVED unconditionally and FAILS OPEN even
     on a ledger/Alpaca read error — because blocking a legitimate exit (unbounded loss)
     is the worse error. A cached protected-symbols set decides this without a live read.

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
# Lightweight cache of symbols that currently have a nonzero protected (qhm/forever6)
# floor. Written by save_ledger; read by the guard ONLY when the main ledger is
# unreadable, so a symbol that was never protected still fails OPEN (never blocks an
# ordinary intraday exit). Today this is empty (all positions intraday).
_PROTECTED_SET_PATH = _ROOT / "data" / "state" / "protected_symbols.json"

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
    """Atomic tmp→replace write (RC-5). Also refreshes the lightweight
    protected-symbols cache the guard uses when the main ledger is unreadable."""
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LEDGER_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(_LEDGER_PATH)
    # Refresh the protected-symbols cache (qhm/forever6 names). RC-5 atomic.
    try:
        _prot = sorted(s for s in ledger.get("positions", {})
                       if protected_floor(ledger, s) > _QTY_EPS)
        _pt = _PROTECTED_SET_PATH.with_suffix(".tmp")
        with open(_pt, "w") as f:
            json.dump(_prot, f)
            f.flush()
            os.fsync(f.fileno())
        _pt.replace(_PROTECTED_SET_PATH)
    except Exception as e:  # RC-3: logged; cache is a non-critical fallback.
        logger.warning("protected_symbols cache write failed: %s", e)


def _cached_protected_symbols() -> set:
    """Symbols with a nonzero protected floor, from the lightweight cache. Read by the
    guard ONLY when the main ledger is unreadable, so a never-protected symbol still
    fails OPEN. Absent/unreadable → empty set (fail OPEN — a genuinely protected symbol
    would be in the cache)."""
    try:
        data = json.loads(_PROTECTED_SET_PATH.read_text())
        if isinstance(data, list):
            return {str(s) for s in data}
    except Exception as e:  # RC-3: logged; missing cache is normal (nothing protected).
        logger.debug("_cached_protected_symbols: read failed, empty: %s", e)
    return set()


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

    # ── KEYSTONE — fail OPEN for a non-protected symbol (board 4-0, 2026-07-11).
    # A symbol with NO protected floor (qhm+forever6 == 0) has nothing to protect, so an
    # ordinary reducing order (an intraday stop-loss/target) MUST be approved — blocking
    # or shrinking a legitimate exit is the catastrophic error, worse than floor risk.
    # Protected status needs no live Alpaca read; on a ledger-read failure we
    # consult the lightweight cache so a never-protected symbol still fails OPEN.
    try:
        ledger = load_ledger()
    except LedgerError as e:
        if symbol not in _cached_protected_symbols():
            return GuardResult("APPROVE", qty,
                               "ledger unreadable, symbol not protected — exit ok")
        return GuardResult("REJECT", 0.0,
                           f"ledger unreadable for PROTECTED {symbol} — closed: {e}")

    floor = protected_floor(ledger, symbol)      # ledger-derived ONLY (forever6 + qhm)
    if floor <= _QTY_EPS:
        # No protected floor → nothing to protect. Approve the full order regardless of
        # ledger drift or Alpaca-net availability (those only bind when a floor exists).
        return GuardResult("APPROVE", qty, "no protected floor — exit allowed")

    # ── The symbol IS protected (floor > 0) → engage the fail-CLOSED protection logic.
    if alpaca_net_qty is None:
        return GuardResult("REJECT", 0.0,
                           "Alpaca net unavailable for PROTECTED symbol — fail closed")
    _ent = _entry(ledger, symbol)
    if _ent is not None:
        _drift = float(_ent.get("drift", 0.0) or 0.0)
        if abs(_drift) > _QTY_EPS:
            return GuardResult(
                "REJECT", 0.0, f"LEDGER_DRIFT {symbol} drift={_drift} — sells frozen")
    # reconciliation: ledger tier-sum must equal Alpaca net, else FREEZE
    ledger_sum = get_combined_symbol_exposure(ledger, symbol)
    if abs(ledger_sum - float(alpaca_net_qty)) > _QTY_EPS:
        return GuardResult(
            "REJECT", 0.0,
            f"drift ledger={ledger_sum} alpaca={alpaca_net_qty} — fail closed")

    own = tier_qty(ledger, symbol, tier)
    net = float(alpaca_net_qty)
    # A protected tier (qhm/forever6) selling its OWN shares reduces its own share
    # to the floor, so it need only stay above the OTHER protected tiers' qty. Example:
    # net=4 intraday=1 qhm=2 f6=1 → a QHM sell of its own 2 respects only forever6(1).
    _own_protected = own if tier in _PROTECTED_TIERS else 0.0
    effective_floor = floor - _own_protected

    # short on a ring-fenced (floor>0) name → REJECT (long-only; shorts → options).
    if str(side).lower() == "short":
        return GuardResult("REJECT", 0.0, "ring-fenced name is long-only (shorts→opts)")

    # sells, per tier
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


# ── Heal / audit tool — full replay, refuse to shrink a protected floor ────────
def _fill_time_key(f: dict) -> str:
    """Sort key for a fill — Alpaca activities carry `transaction_time` (ISO-8601,
    lexicographically sortable). Missing/unknown sorts last so a malformed fill can
    never jump ahead of a real, timestamped one. Never raises."""
    t = f.get("transaction_time") or f.get("filled_at") or f.get("timestamp")
    if not t:                    # None / "" / absent → sort LAST
        return "~"               # "~" (0x7E) > any digit or 'T' lexicographically
    return str(t)


def sync_ledger(fills: list, positions: list,
                coid_by_order_id: dict | None = None) -> dict:
    """OPTION-C HEAL/AUDIT TOOL (board-blessed 2026-07-10; NOT the per-cycle
    authority). Deliberately-run only. Rebuilds the per-tier ownership ledger by
    replaying the FULL Alpaca fill history, attributing each fill to its tier, then
    reconciling per-symbol against live Alpaca positions.

    NEVER-SHRINK-A-PROTECTED-FLOOR GUARD (why this is safe as a heal tool): if the
    rebuilt ledger would REDUCE any protected-tier (qhm/forever6) qty for any symbol
    vs the CURRENT persisted ledger, fills establishing the floor have aged out of
    Alpaca's retrievable window — a truncated replay. The rebuild is ABORTED: nothing
    written, a CRITICAL alert fires, caller gets {"healed": False, ...}. The floor is
    thus never silently shrunk by a replay (the locked invariant). An INCREASE (0→real,
    or seeding a new protected lot) is allowed; only a decrease aborts.

    Attribution (RC-6 JOIN): Alpaca FILL activities do NOT carry client_order_id —
    only order_id — so a fill's tier is resolved via the order_id→client_order_id join.
    Pass `coid_by_order_id` = {order_id: client_order_id} (from
    reporting.pnl_ledger.build_coid_map(fetch_all_orders())); tier =
    tier_of_coid(coid_by_order_id[fill.order_id]). Falls back to fill['client_order_id']
    when the map is absent or lacks the order_id (e.g. pre-joined fills / tests).
    UNTAGGED fills attribute to INTRADAY — correct for the pre-Phase-0 legacy book; a
    post-launch untagged fill (external/manual trade) is quarantined by the incremental
    engine, not here. NOTE: without `coid_by_order_id`, raw Alpaca fills have no
    client_order_id → everything no-ops to intraday (safe, but not real attribution).

    Per tier, net qty = sum(buy/cover) - sum(sell/short); avg_cost is the FIFO-weighted
    average of the tier's remaining open long lots. Fills are sorted chronologically
    here (defensive — never assumes the caller sorted). Returns the ledger dict on
    success, or {"healed": False, "reason":..., "shrink":{...}} on a refused replay.
    """
    tiers_qty: dict = {}   # symbol -> {tier: net_qty}
    lots: dict = {}        # symbol -> {tier: list[[qty, price]]} open longs, FIFO
    for f in sorted((fills or []), key=_fill_time_key):
        sym = f.get("symbol")
        side = str(f.get("side", "")).lower()
        try:
            q = float(f.get("qty", 0.0) or 0.0)
            px = float(f.get("price", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if not sym or q <= 0:
            continue
        _oid = f.get("order_id")
        if coid_by_order_id is not None and _oid in coid_by_order_id:
            # AUTHORITATIVE join: the map's value for this order_id is final — even a
            # mapped None (a real order with no client_order_id) means "confirmed
            # untagged" → intraday. Must NOT fall through to the fill's own coid here,
            # or an authoritative-untagged order would wrongly inherit a stale coid.
            _coid = coid_by_order_id[_oid]
        else:
            # No map, or this order_id was not joined → fall back to the fill's own coid
            # (pre-joined fills / tests). Raw Alpaca fills carry none → intraday.
            _coid = f.get("client_order_id")
        tier = tier_of_coid(_coid) or "intraday"
        tq = tiers_qty.setdefault(sym, {})
        tq[tier] = tq.get(tier, 0.0) + (q if side in ("buy", "buy_to_cover") else -q)
        # FIFO long-lot tracking for avg_cost (buys add lots; sells consume oldest).
        lt = lots.setdefault(sym, {}).setdefault(tier, [])
        if side in ("buy", "buy_to_cover"):
            lt.append([q, px])
        else:
            rem = q
            while rem > _QTY_EPS and lt:
                if lt[0][0] <= _QTY_EPS:      # defensive: drop a degenerate/empty
                    lt.pop(0)                 # lot so the loop always makes progress
                    continue
                if lt[0][0] <= rem + _QTY_EPS:
                    rem -= lt[0][0]
                    lt.pop(0)
                else:
                    lt[0][0] -= rem
                    rem = 0.0

    alp = {}
    for p in (positions or []):
        try:
            alp[p.get("symbol")] = float(p.get("qty", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue

    ledger = _empty_ledger()
    for sym in set(tiers_qty) | set(alp):
        tq = tiers_qty.get(sym, {})
        tier_entries: dict = {}
        for t in _TIERS:
            q = round(tq.get(t, 0.0), 6)
            _lt = lots.get(sym, {}).get(t, [])
            _open = sum(lot[0] for lot in _lt)
            avg = (round(sum(lot[0] * lot[1] for lot in _lt) / _open, 4)
                   if _open > _QTY_EPS else 0.0)
            tier_entries[t] = {"qty": q, "avg_cost": avg, "last_fill_id": None}
        ledger_sum = round(sum(tier_entries[t]["qty"] for t in _TIERS), 6)
        net = alp.get(sym, 0.0)
        ledger["positions"][sym] = {
            "alpaca_net_qty": net,
            "tiers": tier_entries,
            "drift": round(net - ledger_sum, 6),
        }

    # NEVER-SHRINK-A-PROTECTED-FLOOR: compare the rebuild to the persisted baseline.
    # A protected tier (qhm/forever6) going DOWN vs the current ledger = truncated
    # replay → abort, never write, alert. (Absent/corrupt baseline → protected qty
    # treated as 0, so any rebuild only ever increases; can't falsely trip.)
    try:
        _base = load_ledger()
    except LedgerError:
        _base = _empty_ledger()
    _new_positions = ledger["positions"]
    _base_positions = _base.get("positions", {})
    # Iterate the UNION of new + baseline symbols: a symbol that had a protected floor
    # in the baseline but VANISHES from the rebuild (all its fills aged out AND it is
    # absent from current Alpaca positions) must still be caught as a shrink-to-0 —
    # otherwise a disappeared floor evades the guard entirely.
    _shrink: dict = {}
    for sym in set(_new_positions) | set(_base_positions):
        _new_tiers = _new_positions.get(sym, {}).get("tiers", {})
        _base_tiers = _base_positions.get(sym, {}).get("tiers", {})
        for pt in _PROTECTED_TIERS:
            new_q = float(_new_tiers.get(pt, {}).get("qty", 0.0) or 0.0)
            old_q = float(_base_tiers.get(pt, {}).get("qty", 0.0) or 0.0)
            if new_q < old_q - _QTY_EPS:
                _shrink[f"{sym}/{pt}"] = {"was": old_q, "would_be": new_q}
    if _shrink:
        logger.critical(
            "sync_ledger REFUSED — replay would shrink protected floor(s) %s "
            "(fills likely aged out of Alpaca window). Ledger NOT written.", _shrink)
        return {"healed": False, "reason": "protected-floor shrink — replay truncated",
                "shrink": _shrink}

    save_ledger(ledger)
    _drifted = {s: e["drift"] for s, e in ledger["positions"].items()
               if abs(e["drift"]) > _QTY_EPS}
    if _drifted:
        logger.critical("sync_ledger: drift on %d symbol(s) %s — sells frozen there",
                        len(_drifted), _drifted)
    ledger["healed"] = True
    return ledger


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
