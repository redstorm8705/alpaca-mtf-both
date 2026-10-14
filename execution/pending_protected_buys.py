# ruff: noqa: E501
"""Durable unresolved F6-buy reservations.

This journal records broker intent, not shares.  Confirmed ownership remains the
canonical fill-replayed ownership ledger.  Any ambiguity stays active and blocks
new reducing orders for the symbol.

Built and signed by OpenAI Codex, 2026-10-13.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
_CHANGE = "change_id=f6-pending-buy-reservation"

_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = _ROOT / "data" / "state" / "pending_protected_buys.json"
LOCK_PATH = _ROOT / "data" / "state" / ".pending_protected_buys.lock"
SCHEMA = 1
ACTIVE = frozenset({"reserved", "submitted"})
TERMINAL = frozenset({"released_zero", "released_accounted"})
KNOWN_ORDER_STATUSES = frozenset({
    "new", "accepted", "pending_new", "partially_filled", "filled", "done_for_day",
    "canceled", "expired", "replaced", "pending_cancel", "pending_replace", "rejected",
    "suspended", "calculated", "stopped",
})
TERMINAL_ORDER_STATUSES = frozenset({
    "filled", "canceled", "expired", "rejected",
})


class PendingProtectedBuyError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _identity(value) -> str:
    """Accept broker-native string/UUID identities; reject coercible scalars."""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    return ""


def _is_f6_coid(value) -> bool:
    """Validate the canonical F6 prefix without importing the ledger guard.

    The pending-intent journal sits below ownership reconciliation, so keeping
    this parser local makes the dependency one-way: ownership_guard may consult
    this journal, while loading this journal never imports ownership_guard.
    """
    return isinstance(value, str) and value.startswith("F6-")


@contextlib.contextmanager
def _locked():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _empty() -> dict:
    return {"schema": SCHEMA, "updated_at": _now(), "reservations": {}}


def _validate(data: dict) -> dict:
    if (not isinstance(data, dict) or isinstance(data.get("schema"), bool)
            or data.get("schema") != SCHEMA
            or not isinstance(data.get("reservations"), dict)):
        raise PendingProtectedBuyError("pending F6 journal schema is invalid")
    active_coids: set[str] = set()
    active_oids: set[str] = set()
    for rid, row in data["reservations"].items():
        if not isinstance(rid, str) or not rid or not isinstance(row, dict):
            raise PendingProtectedBuyError("pending F6 journal row is invalid")
        if row.get("status") not in ACTIVE | TERMINAL:
            raise PendingProtectedBuyError(f"pending F6 journal {rid} has invalid status")
        if not isinstance(row.get("symbol"), str) or not row["symbol"]:
            raise PendingProtectedBuyError(f"pending F6 journal {rid} has invalid symbol")
        coid = row.get("client_order_id")
        if not isinstance(coid, str) or not _is_f6_coid(coid):
            raise PendingProtectedBuyError(f"pending F6 journal {rid} has invalid client ID")
        qty = row.get("qty")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            raise PendingProtectedBuyError(f"pending F6 journal {rid} has invalid quantity")
        baseline = row.get("baseline_f6_qty")
        if (not isinstance(baseline, (int, float)) or isinstance(baseline, bool)
                or not math.isfinite(float(baseline)) or baseline < 0
                or abs(float(baseline) - round(float(baseline))) > 1e-6):
            raise PendingProtectedBuyError(f"pending F6 journal {rid} has invalid baseline")
        if not isinstance(row.get("created_at"), str) or not row["created_at"]:
            raise PendingProtectedBuyError(f"pending F6 journal {rid} has invalid timestamp")
        oid = row.get("order_id")
        if oid is not None and (not isinstance(oid, str) or not oid):
            raise PendingProtectedBuyError(f"pending F6 journal {rid} has invalid order ID")
        if row["status"] == "reserved" and oid is not None:
            raise PendingProtectedBuyError(f"pending F6 journal {rid} reserved row is already bound")
        if row["status"] == "submitted" and oid is None:
            raise PendingProtectedBuyError(f"pending F6 journal {rid} submitted row is unbound")
        if row["status"] in ACTIVE:
            if coid in active_coids or (oid is not None and oid in active_oids):
                raise PendingProtectedBuyError("duplicate active F6 reservation identity")
            active_coids.add(coid)
            if oid is not None:
                active_oids.add(oid)
    return data


def load() -> dict:
    if not JOURNAL_PATH.exists():
        return _empty()
    try:
        return _validate(json.loads(JOURNAL_PATH.read_text(encoding="utf-8")))
    except PendingProtectedBuyError:
        raise
    except Exception as exc:
        raise PendingProtectedBuyError(f"pending F6 journal unreadable: {exc}") from exc


def _save(data: dict) -> None:
    data = _validate(data)
    data["updated_at"] = _now()
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOURNAL_PATH.with_name(f"{JOURNAL_PATH.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, JOURNAL_PATH)
        dfd = os.open(JOURNAL_PATH.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def reserve(symbol: str, qty: int, client_order_id: str, baseline_f6_qty: float) -> str:
    symbol = str(symbol or "").upper()
    if (not symbol or not isinstance(qty, int) or isinstance(qty, bool) or qty < 1
            or not _is_f6_coid(client_order_id)
            or not isinstance(baseline_f6_qty, (int, float)) or isinstance(baseline_f6_qty, bool)
            or not math.isfinite(float(baseline_f6_qty)) or baseline_f6_qty < 0
            or abs(float(baseline_f6_qty) - round(float(baseline_f6_qty))) > 1e-6):
        raise PendingProtectedBuyError("invalid F6 reservation request")
    with _locked():
        data = load()
        if any(r["status"] in ACTIVE and (r["symbol"] == symbol or r["client_order_id"] == client_order_id)
               for r in data["reservations"].values()):
            raise PendingProtectedBuyError("active F6 reservation already exists")
        rid = client_order_id
        if rid in data["reservations"]:
            raise PendingProtectedBuyError("F6 reservation client ID was already used")
        data["reservations"][rid] = {
            "status": "reserved", "symbol": symbol, "qty": qty,
            "client_order_id": client_order_id, "order_id": None,
            "created_at": _now(), "submitted_at": None, "released_at": None,
            "fill_ids": [], "accounted_qty": 0,
            "baseline_f6_qty": float(baseline_f6_qty),
        }
        _save(data)
        logger.warning("[%s] reserved %s %s share(s) coid=%s baseline=%s",
                       _CHANGE, symbol, qty, client_order_id, baseline_f6_qty)
        return rid


def bind(reservation_id: str, order) -> None:
    with _locked():
        data = load()
        row = data["reservations"].get(reservation_id)
        if not row or row["status"] not in {"reserved", "submitted"}:
            raise PendingProtectedBuyError("F6 reservation cannot transition to submitted")
        oid = _identity(_field(order, "id", ""))
        coid = str(_field(order, "client_order_id", "") or "")
        symbol = str(_field(order, "symbol", "") or "").upper()
        side = str(getattr(_field(order, "side", ""), "value", _field(order, "side", ""))).lower()
        try:
            qty = float(_field(order, "qty", 0) or 0)
            filled = float(_field(order, "filled_qty", 0) or 0)
        except (TypeError, ValueError):
            qty = filled = math.nan
        status = str(getattr(_field(order, "status", ""), "value", _field(order, "status", ""))).lower()
        if (not oid or coid != row["client_order_id"] or symbol != row["symbol"] or side != "buy"
                or not math.isfinite(qty) or qty != row["qty"]
                or not math.isfinite(filled) or not 0 <= filled <= qty
                or status not in KNOWN_ORDER_STATUSES):
            raise PendingProtectedBuyError("broker order does not exactly match F6 reservation")
        if row["status"] == "submitted":
            if row.get("order_id") != oid:
                raise PendingProtectedBuyError(
                    "submitted F6 reservation cannot bind a different broker order"
                )
            # Lost-response recovery can rediscover the exact order after the first
            # bind succeeded but a later allocator bind raised.  Treat the same
            # immutable broker identity as confirmation, never as a second submit.
            return
        row.update(status="submitted", order_id=oid, submitted_at=_now())
        _save(data)
        logger.warning("[%s] bound %s to broker order %s", _CHANGE, reservation_id, oid)


def release_definitive_reject(reservation_id: str, client_order_id: str) -> None:
    """Retire a never-submitted reservation on an explicit broker rejection.

    This path is deliberately unavailable after an order ID has been bound.  A
    timeout, duplicate-client-ID response, missing lookup, or any other ambiguous
    outcome must stay active for exact replay reconciliation.
    """
    with _locked():
        data = load()
        row = data["reservations"].get(reservation_id)
        if (not row or row["status"] != "reserved" or row.get("order_id") is not None
                or row.get("client_order_id") != client_order_id):
            raise PendingProtectedBuyError(
                "F6 reservation cannot release as a definitive broker rejection"
            )
        row.update(
            status="released_zero",
            released_at=_now(),
            terminal_status="broker_definitive_reject",
        )
        _save(data)
        logger.warning("[%s] released broker-rejected reservation %s", _CHANGE, reservation_id)


def active_for_symbol(symbol: str) -> tuple[dict, ...]:
    symbol = str(symbol or "").upper()
    with _locked():
        data = load()
        return tuple(dict(r) for r in data["reservations"].values()
                     if r["status"] in ACTIVE and r["symbol"] == symbol)


def assert_reduction_clear(symbol: str) -> None:
    import config
    # True dormant compatibility: an F6 reservation cannot exist before its
    # journal exists.  With F6 dark and no journal, ordinary exits must not gain
    # a new mkdir/open/flock dependency.  A journal that already exists still
    # loads and fails closed so a flag-off cannot abandon protected intent.
    if not bool(getattr(config, "FOREVER6_ENABLED", False)) and not JOURNAL_PATH.exists():
        return
    active = active_for_symbol(symbol)
    if active:
        raise PendingProtectedBuyError(
            f"unresolved F6 protected buy blocks reductions ({active[0]['client_order_id']})"
        )


def reconcile(fills: list, orders: list, coid_map: dict, ledger_result: dict) -> dict:
    """Release only on exact broker identity and same-pass fill-replay provenance."""
    by_coid: dict[str, list] = {}
    for order in orders or []:
        by_coid.setdefault(str(_field(order, "client_order_id", "") or ""), []).append(order)
    released: list[str] = []
    pending: list[str] = []
    with _locked():
        data = load()
        for rid, row in data["reservations"].items():
            if row["status"] not in ACTIVE:
                continue
            matches = by_coid.get(row["client_order_id"], [])
            if len(matches) != 1:
                pending.append(rid)
                continue
            order = matches[0]
            oid = _identity(_field(order, "id", ""))
            coid = str(_field(order, "client_order_id", "") or "")
            symbol = str(_field(order, "symbol", "") or "").upper()
            side = str(getattr(_field(order, "side", ""), "value", _field(order, "side", ""))).lower()
            status = str(getattr(_field(order, "status", ""), "value", _field(order, "status", ""))).lower()
            try:
                qty = float(_field(order, "qty", 0) or 0)
                filled = float(_field(order, "filled_qty", 0) or 0)
            except (TypeError, ValueError):
                qty = filled = math.nan
            valid = (oid and coid == row["client_order_id"] and symbol == row["symbol"] and side == "buy"
                     and status in KNOWN_ORDER_STATUSES and math.isfinite(qty) and qty == row["qty"]
                     and math.isfinite(filled) and 0 <= filled <= qty)
            if not valid or (row["order_id"] is not None and row["order_id"] != oid):
                pending.append(rid)
                continue
            if row["status"] == "reserved":
                row.update(status="submitted", order_id=oid, submitted_at=_now())
            if status not in TERMINAL_ORDER_STATUSES:
                pending.append(rid)
                continue
            exact_fills = [
                f for f in (fills or [])
                if isinstance(f, dict) and _identity(f.get("order_id")) == oid
            ]
            ids = [_identity(f.get("id")) for f in exact_fills]
            try:
                fill_quantities = [float(f.get("qty", 0) or 0) for f in exact_fills]
                fill_qty = sum(fill_quantities)
            except (TypeError, ValueError):
                fill_quantities = []
                fill_qty = math.nan
            positions = ledger_result.get("positions") or {}
            symbol_absent = symbol not in positions
            entry = positions.get(symbol, {})
            drift = entry.get("drift")
            net = entry.get("alpaca_net_qty")
            try:
                f6_claim = float(entry["tiers"]["forever6"]["qty"])
                tier_sum = sum(float(entry["tiers"][tier]["qty"])
                               for tier in ("intraday", "daytrade", "qhm", "forever6"))
            except (KeyError, TypeError, ValueError):
                f6_claim = tier_sum = math.nan
            ledger_clean = (
                ledger_result.get("healed") is True
                and coid_map.get(oid) == row["client_order_id"]
                and drift is not None and math.isfinite(float(drift)) and abs(float(drift)) <= 1e-6
                and net is not None and math.isfinite(float(net))
                and math.isfinite(tier_sum) and abs(tier_sum - float(net)) <= 1e-6
                and math.isfinite(f6_claim)
            )
            # A broker-reported ``filled`` order with zero fill is contradictory.
            # Canceled/expired/rejected zero-fill orders release only after the same
            # canonical replay proves there were no exact fills and ownership did
            # not move.  This deliberately refuses a terminal status by itself.
            if filled == 0:
                zero_proven = (
                    status in {"canceled", "expired", "rejected"}
                    and not exact_fills
                    and (
                        (ledger_clean
                         and abs(f6_claim - float(row["baseline_f6_qty"])) <= 1e-6)
                        or (ledger_result.get("healed") is True
                            and symbol_absent
                            and float(row["baseline_f6_qty"]) == 0)
                    )
                )
                if not zero_proven:
                    pending.append(rid)
                    continue
                row.update(status="released_zero", released_at=_now(), fill_ids=[], accounted_qty=0)
                released.append(rid)
                logger.warning("[%s] released proven terminal-zero reservation %s", _CHANGE, rid)
                continue
            if status == "filled" and abs(filled - qty) > 1e-6:
                pending.append(rid)
                continue
            provenance_ok = (
                ledger_clean
                and bool(exact_fills) and all(ids) and len(ids) == len(set(ids))
                and len(fill_quantities) == len(exact_fills)
                and all(math.isfinite(q) and q > 0 and abs(q - round(q)) <= 1e-6
                        for q in fill_quantities)
                and all(_identity(f.get("order_id")) == oid
                        and isinstance(f.get("symbol"), str)
                        and f["symbol"].upper() == symbol
                        and isinstance(f.get("side"), str)
                        and f["side"].lower() == "buy"
                        for f in exact_fills)
                and math.isfinite(fill_qty) and abs(fill_qty - filled) <= 1e-6
                and f6_claim + 1e-6 >= float(row["baseline_f6_qty"]) + filled
            )
            if not provenance_ok:
                pending.append(rid)
                continue
            row.update(status="released_accounted", released_at=_now(), fill_ids=sorted(ids), accounted_qty=filled)
            released.append(rid)
            logger.warning("[%s] released accounted reservation %s fills=%s qty=%s",
                           _CHANGE, rid, sorted(ids), filled)
        _save(data)
    return {"released": released, "pending": pending}
