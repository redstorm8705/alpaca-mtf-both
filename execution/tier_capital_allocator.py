# ruff: noqa: E501
"""
execution/tier_capital_allocator.py — shared live tier-capital admission control.

Signed: ChatGPT/Codex, 2026-09-27.
Only increasing orders may acquire capital.  This module deliberately has no exit,
cancel, stop, flatten, or kill-switch API: protective order paths must remain live
when allocation state or broker reads are unavailable.

A reservation is durable before a caller submits an order.  The flock covers broker
snapshot, cap calculation and reservation write, preventing two independent runners
from consuming the same capacity.  Unknown broker/ledger/state values deny new risk.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

Tier = Literal["daytrade", "swing", "qhm", "forever6"]
_CASH_TIERS = frozenset({"qhm", "forever6"})
_SCHEMA = 1
_OPEN_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
        "open",
        "held",
        "pending_replace",
        "pending_cancel",
        "accepted_for_bidding",
        "stopped",
        "calculated",
    }
)
_TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})
_DEFAULT_TARGETS = {
    "normal": {
        "daytrade": 0.40,
        "swing": 0.20,
        "qhm": 0.30,
        "forever6": 0.10,
    },  # PROV:live-tier-capital-allocator-2026-09-27
    "risk_on": {
        "daytrade": 0.45,
        "swing": 0.25,
        "qhm": 0.20,
        "forever6": 0.10,
    },  # PROV:live-tier-capital-allocator-2026-09-27
    "stressed": {
        "daytrade": 0.30,
        "swing": 0.15,
        "qhm": 0.45,
        "forever6": 0.10,
    },  # PROV:live-tier-capital-allocator-2026-09-27
    "crash": {
        "daytrade": 0.15,
        "swing": 0.10,
        "qhm": 0.45,
        "forever6": 0.30,
    },  # PROV:live-tier-capital-allocator-2026-09-27
}
_GROSS_MULT = {
    "normal": 1.25,
    "risk_on": 1.40,
    "stressed": 1.10,
    "crash": 1.10,
}  # PROV:live-tier-capital-allocator-2026-09-27


@dataclass(frozen=True)
class EntryRequest:
    tier: Tier
    owner_tier: str
    symbol: str
    side: str
    qty: int
    price_bound: float
    client_order_id: str = ""
    stop_price: float = 0.0
    risk_price_bound: float = 0.0
    overnight: bool = False
    exempt_existing_notional: float = 0.0
    regime_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Lease:
    id: str
    tier: Tier
    symbol: str
    qty: int
    price_bound: float
    notional: float
    cash_reserve: float
    maintenance_reserve: float
    worst_loss: float
    created_at: float
    expires_at: float
    client_order_id: str = ""


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str
    lease: Lease | None = None


@dataclass
class CapitalPolicy:
    regime: str = "normal"
    targets: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            name: dict(values) for name, values in _DEFAULT_TARGETS.items()
        }
    )
    cash_floor_usd: float = 200.0  # PROV:live-tier-capital-allocator-2026-09-27
    cash_floor_equity_pct: float = 0.10  # PROV:live-tier-capital-allocator-2026-09-27
    maintenance_floor_usd: float = 650.0  # PROV:live-tier-capital-allocator-2026-09-27
    maintenance_floor_equity_pct: float = (
        0.25  # PROV:live-tier-capital-allocator-2026-09-27
    )
    f6_slippage_buffer: float = 1.01
    lease_ttl_seconds: int = 180
    gross_multipliers: dict[str, float] = field(
        default_factory=lambda: dict(_GROSS_MULT)
    )


class TierCapitalAllocator:
    """Durable, fail-closed capital admission authority for increasing entries only."""

    def __init__(
        self,
        broker: Any,
        *,
        state_path: Path | None = None,
        audit_path: Path | None = None,
        policy: CapitalPolicy | None = None,
        clock: Callable[[], float] = time.time,
        ledger_loader: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        self.broker = broker
        self.state_path = (
            state_path or root / "data" / "state" / "tier_capital_allocator.json"
        )
        self.audit_path = audit_path or root / "logs" / "tier_capital_allocator.jsonl"
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self.policy = policy or CapitalPolicy()
        self.clock = clock
        self.ledger_loader = ledger_loader or self._default_ledger

    @staticmethod
    def _default_ledger() -> dict[str, Any]:
        from execution.ownership_guard import load_ledger

        return load_ledger()

    def admit_entry(self, request: EntryRequest) -> Decision:
        """Atomically reserve capacity for one increasing entry or deny it."""
        bad = self._validate_request(request)
        if bad:
            return self._decision(False, bad, request)
        with self._locked():
            state = self._load_state()
            if state is None:
                return self._decision(False, "allocator state unreadable", request)
            snapshot, why = self._snapshot()
            if snapshot is None:
                return self._decision(False, why, request)
            ledger, why = self._ledger()
            if ledger is None:
                return self._decision(False, why, request)
            # Reconcile while holding the same lock. Filled leases clear only after
            # broker and tier ownership quantities both prove the fill is accounted.
            self._reconcile_locked(state, snapshot, ledger)
            decision = self._evaluate(state, snapshot, ledger, request)
            if not decision.approved:
                self._audit(request, decision, snapshot)
                return decision
            assert decision.lease is not None
            state["reservations"][decision.lease.id] = {
                **asdict(decision.lease),
                "owner_tier": request.owner_tier,
                "side": request.side.lower(),
                "client_order_id": decision.lease.client_order_id,
                "baseline_tier_qty": self._ledger_tier_qty(
                    ledger, request.tier, request.symbol
                ),
                "status": "reserved",
                "broker_order_id": None,
            }
            state["updated_at"] = self.clock()
            if not self._save_state(state):
                return self._decision(
                    False, "allocator reservation write failed", request
                )
            self._audit(request, decision, snapshot)
            return decision

    def bind_submitted(self, lease_id: str, broker_order_id: str) -> bool:
        """Durably bind a successfully submitted broker order to an existing lease."""
        if not lease_id or not broker_order_id:
            return False
        with self._locked():
            state = self._load_state()
            if state is None:
                return False
            row = state.get("reservations", {}).get(lease_id)
            if not isinstance(row, dict) or row.get("status") != "reserved":
                return False
            row["broker_order_id"] = str(broker_order_id)
            row["status"] = "submitted"
            state["updated_at"] = self.clock()
            return self._save_state(state)

    def release(self, lease_id: str, reason: str) -> bool:
        """Release only after a caller has positively identified a non-increasing terminal outcome."""
        if not lease_id or not reason:
            return False
        with self._locked():
            state = self._load_state()
            if state is None:
                return False
            row = state.get("reservations", {}).get(lease_id)
            if not isinstance(row, dict) or row.get("status") not in {
                "reserved",
                "submitted",
            }:
                return False
            row["status"] = "released"
            row["release_reason"] = reason
            row["released_at"] = self.clock()
            state["updated_at"] = self.clock()
            return self._save_state(state)

    def _reconcile_locked(self, state: dict, snapshot: dict, ledger: dict) -> bool:
        """Reconcile leases against exact broker IDs; caller holds allocator flock.

        Filled quantities are already present in broker positions and the ownership
        ledger, so their lease must stop consuming reserved capacity. Unknown lookups
        remain reserved to prevent a second order after an ambiguous transport result.
        """
        changed = False
        by_id = snapshot["orders_by_id"]
        for row in state["reservations"].values():
            if not isinstance(row, dict) or row.get("status") not in {
                "reserved",
                "submitted",
            }:
                continue
            broker_id = str(row.get("broker_order_id") or "")
            parsed = by_id.get(broker_id)
            raw_order = None
            if parsed is None:
                try:
                    if broker_id:
                        raw_order = self.broker.get_order(broker_id)
                    elif row.get("client_order_id"):
                        raw_order = self.broker.get_order_by_client_order_id(
                            str(row["client_order_id"])
                        )
                except Exception:
                    raw_order = None
                if raw_order is not None:
                    status = str(getattr(raw_order, "status", "") or "").lower()
                    filled = float(getattr(raw_order, "filled_qty", 0) or 0)
                    oid = str(getattr(raw_order, "id", "") or "")
                    if (
                        row.get("status") == "reserved"
                        and oid
                        and status in _OPEN_STATUSES
                    ):
                        row.update({"status": "submitted", "broker_order_id": oid})
                        changed = True
                        continue
                    parsed = {"status": status, "filled_qty": filled}
            if parsed is None:
                continue
            status = parsed["status"]
            filled = float(parsed["filled_qty"])
            terminal = status == "filled" or status in {
                "rejected",
                "canceled",
                "expired",
            }
            accounted = filled <= 0 or self._filled_is_accounted(
                row, filled, snapshot, ledger
            )
            if terminal and accounted:
                row.update(
                    {
                        "status": "released",
                        "release_reason": (
                            "broker_filled_position_accounted"
                            if status == "filled" or filled > 0
                            else "broker_terminal_zero_fill"
                        ),
                        "released_at": self.clock(),
                    }
                )
                changed = True
        if changed:
            state["updated_at"] = self.clock()
            return self._save_state(state)
        return True

    def reconcile(self) -> bool:
        """Mark only positively terminal broker-bound leases released; unbound leases stay reserved."""
        with self._locked():
            state = self._load_state()
            if state is None:
                return False
            snapshot, _ = self._snapshot()
            if snapshot is None:
                return False
            ledger, _ = self._ledger()
            if ledger is None:
                return False
            return self._reconcile_locked(state, snapshot, ledger)

    def _filled_is_accounted(
        self, row: dict, filled: float, snapshot: dict, ledger: dict
    ) -> bool:
        """Prove a terminal fill is present in both broker net and its owner tier."""
        try:
            symbol = str(row["symbol"]).upper()
            tier = str(row["tier"])
            baseline = float(row["baseline_tier_qty"])
            sign = 1.0 if str(row["side"]).lower() == "buy" else -1.0
            current = self._ledger_tier_qty(ledger, tier, symbol)
            expected = baseline + sign * filled
            if (
                current is None
                or (sign > 0 and current + 1e-6 < expected)
                or (sign < 0 and current - 1e-6 > expected)
            ):
                return False
            # This also proves every broker symbol and signed net equals the ledger.
            return (
                self._ledger_tier_gross(
                    ledger, tier, snapshot["prices"], snapshot["signed_qty"]
                )
                is not None
            )
        except Exception:
            return False

    def _evaluate(
        self, state: dict, snap: dict, ledger: dict, req: EntryRequest
    ) -> Decision:
        regime = str(req.regime_evidence.get("regime", ""))
        targets = self.policy.targets.get(regime)
        if (
            not isinstance(targets, dict)
            or req.tier not in targets
            or regime not in self.policy.gross_multipliers
        ):
            return self._decision(False, "invalid allocator regime/policy", req)
        equity, cash, bp, maint = (
            snap[k] for k in ("equity", "cash", "buying_power", "maintenance_margin")
        )
        notional = req.qty * req.price_bound
        risk_price = req.risk_price_bound or req.price_bound
        worst_loss = req.qty * abs(risk_price - req.stop_price)
        if not math.isfinite(worst_loss) or worst_loss <= 0:
            return self._decision(False, "invalid stop risk", req)
        # Regime evidence is mandatory and selects the policy dynamically.
        if not isinstance(req.regime_evidence, dict) or not req.regime_evidence.get(
            "verified"
        ):
            return self._decision(False, "unverified regime evidence", req)
        cash_reserve = (
            notional
            * (self.policy.f6_slippage_buffer if req.tier == "forever6" else 1.0)
            if req.tier in _CASH_TIERS and req.side.lower() == "buy"
            else 0.0
        )
        if req.tier in _CASH_TIERS and req.side.lower() != "buy":
            return self._decision(False, "cash-only tier permits long buys only", req)
        # Submitted leases with a currently open broker order are represented by
        # snapshot.pending_gross.  Count only unbound/ambiguous reservations here.
        active_reserves = [
            r
            for r in state["reservations"].values()
            if isinstance(r, dict) and r.get("status") in {"reserved", "submitted"}
        ]
        gross_only_reserves = [
            r
            for r in active_reserves
            if not (
                r.get("status") == "submitted"
                and str(r.get("broker_order_id") or "") in snap["orders_by_id"]
                and snap["orders_by_id"][str(r.get("broker_order_id"))]["status"]
                in _OPEN_STATUSES
            )
        ]
        reserve_gross = sum(float(r["notional"]) for r in gross_only_reserves)
        reserve_cash = sum(float(r.get("cash_reserve", 0.0)) for r in active_reserves)
        reserve_maint = sum(
            float(r.get("maintenance_reserve", 0.0)) for r in active_reserves
        )
        cash_floor = max(
            self.policy.cash_floor_usd, equity * self.policy.cash_floor_equity_pct
        )
        if req.tier in _CASH_TIERS and cash - reserve_cash - cash_reserve < cash_floor:
            return self._decision(False, "settled cash floor", req)
        tier_existing = self._ledger_tier_gross(
            ledger, req.tier, snap["prices"], snap["signed_qty"]
        )
        if tier_existing is None:
            return self._decision(
                False, "tier ownership/price unreadable or drifted", req
            )
        exempt_existing = float(req.exempt_existing_notional or 0.0)
        if (not math.isfinite(exempt_existing) or exempt_existing < 0
                or (req.tier != "qhm" and exempt_existing > 0)):
            return self._decision(False, "invalid existing-notional exemption", req)
        if exempt_existing > tier_existing + 1e-9:
            return self._decision(False, "existing-notional exemption exceeds ownership", req)
        tier_existing = max(tier_existing - exempt_existing, 0.0)
        tier_reserved = sum(
            float(r["notional"]) for r in active_reserves if r.get("tier") == req.tier
        )
        # Cash tiers are equity envelopes. Day Trade and Swing are shares of the
        # regime's bounded gross capacity, allowing the approved paper-account margin
        # without treating buying power itself as free capital.
        tier_base = (
            equity
            if req.tier in _CASH_TIERS
            else equity * float(self.policy.gross_multipliers[regime])
        )
        tier_cap = tier_base * float(targets[req.tier])
        if tier_existing + tier_reserved + notional > tier_cap + 1e-9:
            return self._decision(False, "tier capital cap", req)
        account_cap = equity * float(self.policy.gross_multipliers[regime])
        if (
            max(snap["gross"] - exempt_existing, 0.0)
            + snap["pending_gross"] + reserve_gross + notional
            > account_cap + 1e-9
        ):
            return self._decision(False, "account gross cap", req)
        if req.tier not in _CASH_TIERS and bp < notional:
            return self._decision(False, "buying power unavailable", req)
        maintenance_floor = max(
            self.policy.maintenance_floor_usd,
            equity * self.policy.maintenance_floor_equity_pct,
        )
        maintenance_reserve = notional
        if (
            req.overnight
            and (snap["gross"] + snap["pending_gross"] + reserve_gross + notional)
            > equity * 0.40
        ):
            return self._decision(False, "overnight exposure cap", req)
        if equity - maint - reserve_maint - maintenance_reserve < maintenance_floor:
            return self._decision(False, "maintenance cushion", req)
        now = self.clock()
        _lease_id = str(uuid.uuid4())
        _prefix = {"daytrade": "DT", "swing": "IN", "qhm": "QH", "forever6": "F6"}[
            req.tier
        ]
        lease = Lease(
            _lease_id,
            req.tier,
            req.symbol,
            req.qty,
            req.price_bound,
            notional,
            cash_reserve,
            maintenance_reserve,
            worst_loss,
            now,
            now + self.policy.lease_ttl_seconds,
            f"{_prefix}-{req.symbol}-{_lease_id[:20]}",
        )
        return Decision(True, "approved", lease)

    def _snapshot(self) -> tuple[dict | None, str]:
        try:
            account = self.broker.get_account()

            def num(name: str, *, allow_negative: bool = False) -> float:
                raw = getattr(account, name, None)
                if raw is None:
                    raise ValueError(name)
                v = float(raw)
                if not math.isfinite(v) or (v < 0 and not allow_negative):
                    raise ValueError(name)
                return v

            equity, cash, bp, maint = (
                num("equity"),
                num("cash", allow_negative=True),
                num("buying_power"),
                num("maintenance_margin"),
            )
            if equity <= 0:
                raise ValueError("equity")
            positions = self.broker.get_open_positions()
            orders = self.broker.get_open_orders()
            if positions is None or orders is None:
                raise ValueError("positions/orders unavailable")
            prices: dict[str, float] = {}
            signed_qty: dict[str, float] = {}
            gross = 0.0
            for p in positions:
                sym = str(getattr(p, "symbol", "") or "").upper()
                px = float(getattr(p, "current_price", 0) or 0)
                qty = float(getattr(p, "qty", 0) or 0)
                side = str(getattr(p, "side", "") or "").lower()
                if (
                    not sym
                    or not math.isfinite(px)
                    or px <= 0
                    or not math.isfinite(qty)
                    or side not in {"long", "short"}
                    or (side == "long" and qty <= 0)
                    or (side == "short" and qty >= 0)
                ):
                    raise ValueError("malformed position")
                if side == "short":
                    qty = -abs(qty)
                prices[sym] = px
                signed_qty[sym] = qty
                market_value = float(getattr(p, "market_value", qty * px))
                if not math.isfinite(market_value):
                    raise ValueError("malformed market value")
                # Quantity and validated current price are the auditable exposure
                # source. market_value is checked only for numeric corruption.
                gross += abs(qty) * px
            pending_gross, parsed_orders = 0.0, {}
            for o in orders:
                parsed = self._parse_order(o, signed_qty, prices)
                parsed_orders[parsed["id"]] = parsed
                if parsed["status"] in _OPEN_STATUSES:
                    pending_gross += parsed["increasing_notional"]
            payload = {
                "equity": equity,
                "cash": cash,
                "buying_power": bp,
                "maintenance_margin": maint,
                "gross": gross,
                "pending_gross": pending_gross,
                "prices": prices,
                "signed_qty": signed_qty,
                "orders_by_id": parsed_orders,
            }
            payload["hash"] = hashlib.sha256(
                json.dumps(
                    {k: v for k, v in payload.items() if k != "orders_by_id"},
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
            return payload, ""
        except Exception as exc:
            return None, f"broker snapshot unreadable: {type(exc).__name__}"

    @staticmethod
    def _parse_order(
        order: Any, signed_qty: dict[str, float], position_prices: dict[str, float]
    ) -> dict:
        oid = str(getattr(order, "id", "") or "")
        status = str(getattr(order, "status", "") or "").lower()
        sym = str(getattr(order, "symbol", "") or "").upper()
        side = str(getattr(order, "side", "") or "").lower()
        qty = float(getattr(order, "qty", 0) or 0)
        filled = float(getattr(order, "filled_qty", 0.0) or 0.0)
        price = (
            getattr(order, "limit_price", None)
            or getattr(order, "stop_price", None)
            or getattr(order, "filled_avg_price", None)
            or position_prices.get(sym)
        )
        price = float(price or 0)
        if (
            not oid
            or not sym
            or status not in _OPEN_STATUSES | _TERMINAL_STATUSES
            or side not in {"buy", "sell", "sell_short"}
            or not all(math.isfinite(x) for x in (qty, filled, price))
            or qty < 0
            or filled < 0
            or filled > qty
            or price <= 0
        ):
            raise ValueError("malformed order")
        rem = qty - filled
        held = signed_qty.get(sym, 0.0)
        if side == "buy":
            increasing = max(0.0, rem - max(0.0, -held))
        else:
            increasing = max(0.0, rem - max(0.0, held))
        return {
            "id": oid,
            "status": status,
            "filled_qty": filled,
            "increasing_notional": increasing * price,
        }

    def _ledger(self) -> tuple[dict | None, str]:
        try:
            ledger = self.ledger_loader()
            if not isinstance(ledger, dict) or not isinstance(
                ledger.get("positions"), dict
            ):
                raise ValueError("bad ledger")
            return ledger, ""
        except Exception:
            return None, "tier ownership ledger unreadable"

    @staticmethod
    def _ledger_tier_qty(ledger: dict, tier: str, symbol: str) -> float | None:
        owner = "intraday" if tier == "swing" else tier
        try:
            entry = ledger["positions"].get(str(symbol).upper())
            if entry is None:
                return 0.0
            qty = float(entry["tiers"][owner]["qty"])
            return qty if math.isfinite(qty) else None
        except Exception:
            return None

    @staticmethod
    def _ledger_tier_gross(
        ledger: dict, tier: str, prices: dict[str, float], signed_qty: dict[str, float]
    ) -> float | None:
        # Exact quantity reconciliation is a precondition: an allocator must never
        # treat unexplained broker shares as free capacity or silently reassign them.
        owner = "intraday" if tier == "swing" else tier
        gross = 0.0
        try:
            positions = ledger["positions"]
            active_ledger: dict[str, dict] = {}
            for sym, entry in positions.items():
                tiers = entry["tiers"]
                tier_quantities = [
                    float(tiers[name]["qty"])
                    for name in ("intraday", "daytrade", "qhm", "forever6")
                ]
                if not all(math.isfinite(q) for q in tier_quantities):
                    return None
                # Historical rows are ignorable only when every owner is flat.
                # Opposing nonzero claims that net to zero are unresolved drift.
                if any(
                    abs(q) > 1e-6 for q in tier_quantities
                ):  # PROV:ledger-quantity-epsilon
                    active_ledger[str(sym).upper()] = entry
            # The ownership ledger intentionally retains flat historical symbols.
            # Compare only nonzero claims, while requiring every live broker symbol.
            if set(active_ledger) != set(signed_qty):
                return None
            for key, entry in active_ledger.items():
                tiers = entry["tiers"]
                all_qty = sum(
                    float(tiers[name]["qty"])
                    for name in ("intraday", "daytrade", "qhm", "forever6")
                )
                net = float(signed_qty[key])
                if (
                    not (math.isfinite(all_qty) and math.isfinite(net))
                    or abs(all_qty - net) > 1e-6  # PROV:ledger-quantity-epsilon
                ):
                    return None
                q = float(tiers[owner]["qty"])
                if abs(q) > 1e-9:  # PROV:ledger-quantity-epsilon
                    px = prices.get(key)
                    if px is None or not math.isfinite(px) or px <= 0:
                        return None
                    gross += abs(q) * px
            return gross
        except Exception:
            return None

    @staticmethod
    def _validate_request(req: EntryRequest) -> str:
        if req.tier not in {"daytrade", "swing", "qhm", "forever6"}:
            return "unknown tier"
        if (
            not req.owner_tier
            or not req.symbol
            or req.side.lower() not in {"buy", "sell", "sell_short"}
        ):
            return "malformed entry request"
        if not isinstance(req.qty, int) or isinstance(req.qty, bool) or req.qty < 1:
            return "invalid quantity"
        if not math.isfinite(req.price_bound) or req.price_bound <= 0:
            return "invalid price bound"
        if not math.isfinite(req.stop_price) or req.stop_price <= 0:
            return "invalid stop price"
        if req.risk_price_bound and (
            not math.isfinite(req.risk_price_bound) or req.risk_price_bound <= 0
        ):
            return "invalid risk price bound"
        return ""

    def _decision(self, approved: bool, reason: str, request: EntryRequest) -> Decision:
        return Decision(approved, reason)

    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        class _Lock:
            def __init__(inner, outer):
                inner.outer, inner.f = outer, None

            def __enter__(inner):
                inner.f = open(inner.outer.lock_path, "a+", encoding="utf-8")
                fcntl.flock(inner.f.fileno(), fcntl.LOCK_EX)
                return inner

            def __exit__(inner, *_):
                fcntl.flock(inner.f.fileno(), fcntl.LOCK_UN)
                inner.f.close()

        return _Lock(self)

    def _load_state(self) -> dict | None:
        try:
            if not self.state_path.exists():
                return {
                    "schema": _SCHEMA,
                    "reservations": {},
                    "updated_at": self.clock(),
                }
            raw = json.loads(self.state_path.read_text())
            if raw.get("schema") != _SCHEMA or not isinstance(
                raw.get("reservations"), dict
            ):
                return None
            for lease_id, row in raw["reservations"].items():
                if not isinstance(lease_id, str) or not isinstance(row, dict):
                    return None
                status = row.get("status")
                if status not in {"reserved", "submitted", "released"}:
                    return None
                if status == "released":
                    continue
                for key in ("tier", "symbol", "side", "client_order_id"):
                    if not isinstance(row.get(key), str) or not row.get(key):
                        return None
                for key in (
                    "qty",
                    "price_bound",
                    "notional",
                    "cash_reserve",
                    "maintenance_reserve",
                    "worst_loss",
                    "created_at",
                    "expires_at",
                    "baseline_tier_qty",
                ):
                    value = row.get(key)
                    if (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(float(value))
                    ):
                        return None
                if row["tier"] not in {"daytrade", "swing", "qhm", "forever6"}:
                    return None
                if row["side"] not in {"buy", "sell", "sell_short"}:
                    return None
                if not isinstance(row["qty"], int) or isinstance(row["qty"], bool):
                    return None
                if (
                    row["qty"] < 1
                    or float(row["price_bound"]) <= 0
                    or float(row["notional"]) <= 0
                    or float(row["maintenance_reserve"]) <= 0
                    or float(row["cash_reserve"]) < 0
                    or float(row["worst_loss"]) < 0
                    or float(row["created_at"]) < 0
                    or float(row["expires_at"]) < float(row["created_at"])
                ):
                    return None
                notional = float(row["qty"]) * float(row["price_bound"])
                tolerance = max(1.0, notional) * 1e-9
                if abs(float(row["notional"]) - notional) > tolerance:
                    return None
                if abs(float(row["maintenance_reserve"]) - notional) > tolerance:
                    return None
                cash_reserve = float(row["cash_reserve"])
                if row["tier"] == "qhm" and abs(cash_reserve - notional) > tolerance:
                    return None
                if row["tier"] == "forever6" and cash_reserve + tolerance < notional:
                    return None
                if row["tier"] in {"daytrade", "swing"} and cash_reserve != 0:
                    return None
                if status == "submitted" and not row.get("broker_order_id"):
                    return None
            return raw
        except Exception:
            return None

    def _save_state(self, state: dict) -> bool:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(
                self.state_path.suffix + f".{os.getpid()}.tmp"
            )
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    state, f, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
            return True
        except Exception:
            return False

    def _audit(self, request: EntryRequest, decision: Decision, snapshot: dict) -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "ts": self.clock(),
                "request": asdict(request),
                "approved": decision.approved,
                "reason": decision.reason,
                "lease": asdict(decision.lease) if decision.lease else None,
                "snapshot_hash": snapshot.get("hash"),
            }
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            # Audit failure is never allowed to convert an already-durable reservation into an unreserved order.
            pass


_LIVE_ALLOCATOR: TierCapitalAllocator | None = None


def live_admit(
    tier: Tier,
    owner_tier: str,
    symbol: str,
    side: str,
    qty: int,
    price_bound: float,
    *,
    stop_price: float,
    overnight: bool = False,
    exempt_existing_notional: float = 0.0,
    regime_evidence: dict[str, Any] | None = None,
    risk_price_bound: float = 0.0,
) -> Decision:
    """Live caller bridge. Disabled only for backwards-compatible unit imports; enabled config fails closed."""
    import config

    if not bool(getattr(config, "TIER_CAPITAL_ALLOCATOR_ENABLED", False)):
        return Decision(True, "allocator disabled")
    global _LIVE_ALLOCATOR
    if _LIVE_ALLOCATOR is None:
        from execution import broker

        _targets = {name: dict(values) for name, values in _DEFAULT_TARGETS.items()}
        _targets["normal"].update(
            {
                "daytrade": float(
                    getattr(config, "TIER_CAPITAL_DAYTRADE_TARGET_PCT", 0.40)
                ),
                "swing": float(getattr(config, "TIER_CAPITAL_SWING_TARGET_PCT", 0.20)),
                "qhm": float(getattr(config, "TIER_CAPITAL_QHM_TARGET_PCT", 0.30)),
                "forever6": float(
                    getattr(config, "TIER_CAPITAL_FOREVER6_TARGET_PCT", 0.10)
                ),
            }
        )
        _LIVE_ALLOCATOR = TierCapitalAllocator(
            broker,
            policy=CapitalPolicy(
                targets=_targets,
                cash_floor_usd=float(
                    getattr(config, "TIER_CAPITAL_CASH_FLOOR_USD", 200.0)
                ),
                maintenance_floor_usd=float(
                    getattr(config, "TIER_CAPITAL_MAINT_FLOOR_USD", 650.0)
                ),
                gross_multipliers={
                    **_GROSS_MULT,
                    "normal": float(
                        getattr(config, "TIER_CAPITAL_ACCOUNT_GROSS_NORMAL", 1.25)
                    ),
                },
            ),
        )
    _evidence = regime_evidence or _load_regime_evidence()
    return _LIVE_ALLOCATOR.admit_entry(
        EntryRequest(
            tier,
            owner_tier,
            symbol,
            side,
            qty,
            price_bound,
            stop_price=stop_price,
            risk_price_bound=risk_price_bound,
            overnight=overnight,
            exempt_existing_notional=exempt_existing_notional,
            regime_evidence=_evidence,
        )
    )


def live_order_id(lease: Lease | None) -> str | None:
    return lease.client_order_id if lease is not None else None


def live_bind(lease: Lease | None, order: Any) -> bool:
    if lease is None:
        return True
    allocator = _LIVE_ALLOCATOR
    if allocator is None or not getattr(order, "id", None):
        return False
    return bool(allocator.bind_submitted(lease.id, str(order.id)))


def live_release(lease: Lease | None, reason: str) -> Any | None:
    """Release only after exact client-id recovery proves terminal zero-fill.
    Ambiguous submit results retain their durable reservation and page through logs.
    """
    if lease is None or _LIVE_ALLOCATOR is None:
        return None
    if reason == "submit_none":
        try:
            # The allocator owns the same broker adapter used for its account snapshot.
            # Keeping recovery on that adapter also makes the ambiguous transport path
            # independently testable without importing an SDK-backed global module.
            order = _LIVE_ALLOCATOR.broker.get_order_by_client_order_id(
                lease.client_order_id
            )
            if order is not None:
                status = str(getattr(order, "status", "") or "").lower()
                filled = float(getattr(order, "filled_qty", 0) or 0)
                oid = getattr(order, "id", None)
                # A terminal order can still own a real partial fill. Return every
                # exact recovered order with positive cumulative fills so the
                # strategy installs tracking/protection; release only terminal zero.
                if oid and (status in _OPEN_STATUSES | {"filled"} or filled > 0):
                    _LIVE_ALLOCATOR.bind_submitted(lease.id, str(oid))
                    return order
                if status in {"rejected", "canceled", "expired"} and filled <= 0:
                    _LIVE_ALLOCATOR.release(lease.id, "broker_terminal_zero_fill")
                    return None
        except Exception:
            pass
        # lookup unknown: retain lease; it is safer than a duplicate entry.
        return None
    _LIVE_ALLOCATOR.release(lease.id, reason)
    return None


def _load_regime_evidence() -> dict[str, Any]:
    """Map the bot's canonical regime ledger to allocator policy evidence."""
    try:
        from datetime import datetime, timezone

        path = Path(__file__).resolve().parent.parent / "logs" / "regime_state.json"
        raw = json.loads(path.read_text())
        ts_text = str(raw["ts"])
        stamped = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        age = (
            datetime.now(timezone.utc) - stamped.astimezone(timezone.utc)
        ).total_seconds()
        # Production writes this canonical snapshot once per trading day at
        # 16:12 ET. Four days covers a holiday weekend; older means the cron is dead.
        if age < -5 or age > 96 * 3600:
            return {}
        vol = raw.get("vol")
        summary = raw.get("summary")
        if (
            not isinstance(vol, dict)
            or not isinstance(summary, dict)
            or not vol.get("fresh")
        ):
            return {}
        vol_regime = str(vol.get("regime", "")).lower()
        composite = str(vol.get("composite", "")).upper()
        measured_vol = float(vol.get("realized_vol") or 0)
        if not math.isfinite(measured_vol):
            return {}
        spy_raw, term_raw = vol.get("spy_vs_50sma_pct"), vol.get("vix_term_ratio")
        spy_vs_50 = float(spy_raw) if spy_raw is not None else None
        term_ratio = float(term_raw) if term_raw is not None else None
        complete_composite = (
            spy_vs_50 is not None
            and term_ratio is not None
            and math.isfinite(spy_vs_50)
            and math.isfinite(term_ratio)
        )
        spy_value = spy_vs_50 if spy_vs_50 is not None else math.inf
        term_value = term_ratio if term_ratio is not None else -math.inf
        # A crash needs three independent measurements. HIGH_VOL alone only
        # activates the smaller stressed allocation.
        if (
            summary.get("any_stale") is False
            and complete_composite
            and measured_vol >= 35
            and spy_value <= -8
            and term_value >= 1.10
        ):
            regime = "crash"
        elif vol_regime == "high_vol" or composite in {"BEAR", "HIGH_VOL"}:
            regime = "stressed"
        elif (
            summary.get("any_stale") is False
            and vol_regime == "low_vol"
            and composite == "BULL"
        ):
            regime = "risk_on"
        else:
            regime = "normal"
        return {
            "regime": regime,
            "verified": True,
            "ts": ts_text,
            "source": "logs/regime_state.json",
            "measurements": {
                "realized_vol": measured_vol,
                "spy_vs_50sma_pct": spy_vs_50,
                "vix_term_ratio": term_ratio,
            },
        }
    except Exception:
        return {}
