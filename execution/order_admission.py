# ruff: noqa: E501
"""Cross-process, per-symbol broker-order admission gate.

The lock is deliberately wider than a state-file lock: callers hold it from their
last ownership/reservation decision through broker acknowledgement.  This closes
the check-then-submit race between an F6 protected buy and a reducing order.

Built and signed by OpenAI Codex, 2026-10-13.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import threading
import json
import math
import uuid
import inspect
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LOCK_DIR = _ROOT / "data" / "state" / "order_admission"
_LOCAL: dict[str, threading.RLock] = {}
_LOCAL_GUARD = threading.Lock()
_TLS = threading.local()
_REDUCTION_PATH = _ROOT / "data" / "state" / "reducing_order_admissions.json"
_REDUCTION_LOCK = _ROOT / "data" / "state" / ".reducing_order_admissions.lock"
_REDUCTION_SCHEMA = 1
_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped"}


def _key(symbol: str) -> str:
    value = re.sub(r"[^A-Z0-9._-]", "_", str(symbol or "").upper())
    if not value:
        raise ValueError("symbol is required for order-admission lock")
    return value


@contextlib.contextmanager
def symbol_order_gate(symbol: str):
    """Serialize order admission for one symbol across threads and processes.

    Nested calls in the same thread are reentrant; only the outer call owns the
    advisory file lock.  Lock creation/open failure propagates so order paths fail
    before reaching the broker.
    """
    key = _key(symbol)
    with _LOCAL_GUARD:
        lock = _LOCAL.setdefault(key, threading.RLock())
    with lock:
        depths = getattr(_TLS, "depths", None)
        if depths is None:
            depths = {}
            _TLS.depths = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = _LOCK_DIR / f"{key}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _identity(value) -> str:
    """Accept broker-native string/UUID identities; reject coercible scalars."""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    return ""


@contextlib.contextmanager
def _reduction_file_lock():
    _REDUCTION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(_REDUCTION_LOCK, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _load_reductions() -> dict:
    if not _REDUCTION_PATH.exists():
        return {"schema": _REDUCTION_SCHEMA, "rows": {}}
    data = json.loads(_REDUCTION_PATH.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or isinstance(data.get("schema"), bool)
            or data.get("schema") != _REDUCTION_SCHEMA
            or not isinstance(data.get("rows"), dict)):
        raise ValueError("reducing-order admission journal is invalid")
    for rid, row in data["rows"].items():
        if (not isinstance(rid, str) or not isinstance(row, dict)
                or row.get("status") not in {"admitted", "submitted", "released"}
                or not isinstance(row.get("symbol"), str) or not row["symbol"]
                or not isinstance(row.get("created_at"), str)):
            raise ValueError("reducing-order admission row is invalid")
        if row["status"] == "submitted" and (not isinstance(row.get("order_id"), str)
                                               or not row["order_id"]):
            raise ValueError("submitted reducing-order admission has no order ID")
    return data


def _save_reductions(data: dict) -> None:
    _REDUCTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _REDUCTION_PATH.with_name(f"{_REDUCTION_PATH.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _REDUCTION_PATH)
        dfd = os.open(_REDUCTION_PATH.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _begin_reduction(symbol: str, source: str) -> str:
    rid = str(uuid.uuid4())
    with _reduction_file_lock():
        data = _load_reductions()
        data["rows"][rid] = {
            "status": "admitted", "symbol": _key(symbol), "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "order_id": None, "client_order_id": None,
        }
        _save_reductions(data)
    return rid


def note_reduction_ack(order) -> None:
    """Expose an acknowledged order to an enclosing reducing-order decorator."""
    stack = getattr(_TLS, "reduction_stack", [])
    if stack:
        stack[-1]["order"] = order


def note_reduction_not_submitted() -> None:
    """Mark a proven pre-submit/position-absent outcome safe to release."""
    stack = getattr(_TLS, "reduction_stack", [])
    if stack:
        stack[-1]["not_submitted"] = True


def note_reduction_attempted() -> None:
    """Mark that an enclosing reducer reached a broker mutation boundary.

    A failure after this point is ambiguous and must retain its admission until
    exact reconciliation.  A return before this point is positively pre-submit
    and the decorator can release it without leaking a permanent admitted row.
    """
    stack = getattr(_TLS, "reduction_stack", [])
    if stack:
        stack[-1]["attempted"] = True


def _release_reduction(rid: str, reason: str) -> None:
    with _reduction_file_lock():
        data = _load_reductions()
        row = data["rows"].get(rid)
        if not row or row["status"] != "admitted":
            raise ValueError("reducing-order admission cannot release")
        row.update(status="released", released_at=datetime.now(timezone.utc).isoformat(),
                   terminal_status=reason)
        _save_reductions(data)


def _bind_reduction(rid: str, order) -> None:
    oid = _identity(_value(order, "id", ""))
    symbol = str(_value(order, "symbol", "") or "").upper()
    coid = _value(order, "client_order_id", None)
    status = str(getattr(_value(order, "status", ""), "value", _value(order, "status", ""))).lower()
    side = str(getattr(_value(order, "side", ""), "value", _value(order, "side", ""))).lower()
    try:
        qty = float(_value(order, "qty", 0) or 0)
    except (TypeError, ValueError):
        qty = float("nan")
    if (not oid or not symbol or not status or side not in {"buy", "sell", "sell_short"}
            or not math.isfinite(qty) or qty <= 0):
        raise ValueError("acknowledged reducing order identity is incomplete")
    with _reduction_file_lock():
        data = _load_reductions()
        row = data["rows"].get(rid)
        if not row or row["status"] != "admitted" or row["symbol"] != symbol:
            raise ValueError("reducing-order admission cannot bind acknowledgement")
        row.update(status="submitted", order_id=oid,
                   client_order_id=str(coid) if coid else None, broker_status=status,
                   side=side, qty=qty)
        _save_reductions(data)


def active_reductions(symbol: str) -> tuple[dict, ...]:
    """Return unresolved reducing admissions without mutating their state."""
    symbol = _key(symbol)
    with _reduction_file_lock():
        data = _load_reductions()
        return tuple(dict(row) for row in data["rows"].values()
                     if row["symbol"] == symbol and row["status"] != "released")


def reconcile_reductions(fills: list, orders: list, coid_map: dict,
                         ledger_result: dict) -> dict:
    """Release reducers only after an exact, clean canonical ownership replay.

    A terminal broker status is not settlement evidence.  Filled and
    canceled-after-partial orders remain active until their exact fills are joined
    to the exact acknowledged order and the same replay proves a zero-drift tier
    sum.  This keeps a later F6 buy from taking a baseline that predates a sell.
    """
    by_id: dict[str, list] = {}
    for order in orders or []:
        oid = _identity(_value(order, "id", ""))
        if oid:
            by_id.setdefault(oid, []).append(order)
    released: list[str] = []
    pending: list[str] = []
    with _reduction_file_lock():
        data = _load_reductions()
        for rid, row in data["rows"].items():
            if row["status"] == "released":
                continue
            if row["status"] != "submitted":
                pending.append(rid)
                continue
            matches = by_id.get(row["order_id"], [])
            if len(matches) != 1:
                pending.append(rid)
                continue
            order = matches[0]
            oid = _identity(_value(order, "id", ""))
            osym = str(_value(order, "symbol", "") or "").upper()
            status = str(getattr(_value(order, "status", ""), "value", _value(order, "status", ""))).lower()
            side = str(getattr(_value(order, "side", ""), "value", _value(order, "side", ""))).lower()
            try:
                qty = float(_value(order, "qty", 0) or 0)
                filled = float(_value(order, "filled_qty", 0) or 0)
            except (TypeError, ValueError):
                qty = filled = float("nan")
            coid = str(_value(order, "client_order_id", "") or "")
            if (oid != row["order_id"] or osym != row["symbol"] or status not in _TERMINAL
                    or side != row.get("side") or not math.isfinite(qty) or qty != row.get("qty")
                    or not math.isfinite(filled) or not 0 <= filled <= qty
                    or not row.get("client_order_id") or coid != row["client_order_id"]
                    or coid_map.get(oid) != row["client_order_id"]):
                pending.append(rid)
                continue

            exact_fills = [
                f for f in (fills or [])
                if _identity(_value(f, "order_id", "")) == oid
            ]
            ids = [_identity(_value(f, "id", "")) for f in exact_fills]
            try:
                fill_qtys = [float(_value(f, "qty", 0) or 0) for f in exact_fills]
                fill_qty = sum(fill_qtys)
            except (TypeError, ValueError):
                fill_qtys = []
                fill_qty = float("nan")
            entry = (ledger_result.get("positions") or {}).get(osym, {})
            try:
                drift = float(entry["drift"])
                net = float(entry["alpaca_net_qty"])
                tier_sum = sum(float(entry["tiers"][tier]["qty"])
                               for tier in ("intraday", "daytrade", "qhm", "forever6"))
            except (KeyError, TypeError, ValueError):
                drift = net = tier_sum = float("nan")
            replay_clean = (
                ledger_result.get("healed") is True
                and math.isfinite(drift) and abs(drift) <= 1e-6
                and math.isfinite(net) and math.isfinite(tier_sum)
                and abs(tier_sum - net) <= 1e-6
            )
            fills_exact = (
                bool(exact_fills) and all(ids) and len(ids) == len(set(ids))
                and len(fill_qtys) == len(exact_fills)
                and all(math.isfinite(q) and q > 0 and abs(q - round(q)) <= 1e-6
                        for q in fill_qtys)
                and all(isinstance(_value(f, "symbol", ""), str)
                        and _value(f, "symbol", "").upper() == osym
                        and isinstance(_value(f, "side", ""), str)
                        and _value(f, "side", "").lower() == side
                        for f in exact_fills)
                and math.isfinite(fill_qty) and abs(fill_qty - filled) <= 1e-6
            )
            if filled == 0:
                proven = status in {"canceled", "expired", "rejected"} and not exact_fills and replay_clean
            else:
                proven = replay_clean and fills_exact and not (status == "filled" and abs(filled - qty) > 1e-6)
            if not proven:
                pending.append(rid)
                continue
            row.update(status="released", released_at=datetime.now(timezone.utc).isoformat(),
                       terminal_status=status, fill_ids=sorted(ids), accounted_qty=filled)
            released.append(rid)
        _save_reductions(data)
    return {"released": released, "pending": pending}


def serialized_symbol_order(*, failure_value=None, reducing: bool = False,
                            side_parameter: str | None = None):
    """Decorator for broker paths whose first positional argument is ``symbol``."""
    def decorate(fn):
        signature = inspect.signature(fn)

        @wraps(fn)
        def wrapped(symbol, *args, **kwargs):
            is_reducing = reducing
            if side_parameter is not None:
                # Bind by the declared parameter name instead of indexing the
                # post-symbol ``args`` tuple.  This makes positional and keyword
                # calls equivalent and prevents signature edits from silently
                # changing which value is interpreted as the order side.
                bound = signature.bind_partial(symbol, *args, **kwargs)
                raw_side = bound.arguments.get(side_parameter, "")
                is_reducing = str(raw_side).lower() in {"sell", "sell_short", "short"}
            if is_reducing:
                import config
                # With F6 dark and no protected-buy reservation possible, retain
                # the legacy broker path without filesystem locks or journals.
                is_reducing = bool(getattr(config, "FOREVER6_ENABLED", False))
            if not is_reducing:
                return fn(symbol, *args, **kwargs)
            try:
                with symbol_order_gate(symbol):
                    # Make the admission wrapper itself the first fail-closed
                    # barrier.  Broker functions retain their local assertions
                    # as defense in depth, but a newly decorated reducing path
                    # cannot omit the pending protected-buy check.
                    try:
                        from execution.pending_protected_buys import assert_reduction_clear
                        assert_reduction_clear(symbol)
                    except Exception:
                        return failure_value
                    stack = getattr(_TLS, "reduction_stack", None)
                    if stack is None:
                        stack = []
                        _TLS.reduction_stack = stack
                    if stack:
                        return fn(symbol, *args, **kwargs)
                    rid = _begin_reduction(symbol, fn.__name__)
                    frame = {"rid": rid, "order": None, "not_submitted": False,
                             "attempted": False}
                    stack.append(frame)
                    try:
                        result = fn(symbol, *args, **kwargs)
                        order = frame["order"] or (result if _value(result, "id", None) else None)
                        if order is not None:
                            _bind_reduction(rid, order)
                        elif frame["not_submitted"] or not frame["attempted"]:
                            _release_reduction(rid, "not_submitted")
                        return result
                    except Exception:
                        if not frame["attempted"]:
                            _release_reduction(rid, "pre_submit_exception")
                        raise
                    finally:
                        stack.pop()
            except (OSError, ValueError):
                return failure_value
        return wrapped
    return decorate
