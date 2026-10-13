"""Broker-authoritative Slack notifications for every Alpaca stock fill.

Strategy components used to own their own Slack calls. That left confirmed
fills silent when another component observed them (startup reconciliation, an
overnight stop, or an external/manual order). This module alerts from Alpaca's
FILL activity ledger itself.

Delivery is at-least-once. A fill ID is durably acknowledged only after Slack
accepts the message. The notifier runs in its own scheduled process; a file
lock prevents overlapping scheduler runs, and the state survives restarts.
"""

from __future__ import annotations

import fcntl
import json
import logging
import hashlib
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from alerts import send_slack_delivery
from execution.fifo_pnl import _fetch_alpaca_fills_for_date
from execution.state_io import _ET

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_STATE = _ROOT / "data" / "state" / "fill_notifications.json"
_KEEP_DAYS = 8
_INITIAL_BACKFILL_DAYS = 4
_MAX_CATCHUP_DAYS = 31
_CLOCK_SKEW = timedelta(minutes=5)
_BROKER_TS = re.compile(
    r"^(?P<head>.+?)(?:\.(?P<fraction>[0-9]+))?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})$"
)

_TIER_LABELS = {
    "daytrade": "Day",
    "intraday": "Swing",
    "qhm": "QHM",
    "forever6": "Forever 6",
}


def _parse_broker_timestamp(value: str) -> datetime:
    """Parse Alpaca timestamps on Python 3.10, including 4/5-digit fractions."""
    match = _BROKER_TS.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid broker timestamp: {value!r}")
    fraction = match.group("fraction")
    if fraction is not None and len(fraction) not in (3, 6):
        logger.info(
            "change_id=fill-timestamp-precision normalized broker fractional "
            "digits=%d",
            len(fraction),
        )
    fraction_text = "" if fraction is None else f".{(fraction + '000000')[:6]}"
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    return datetime.fromisoformat(f"{match.group('head')}{fraction_text}{zone}")


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _load_state(path: Path, now: Optional[datetime] = None) -> Optional[dict]:
    if not path.exists():
        return None
    clock = now or datetime.now(_ET)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data.get("version") != 1
        ):
            raise ValueError("expected version-1 object")
        seen = data.get("seen")
        malformed = data.get("malformed_seen", {})
        if not isinstance(seen, dict) or not isinstance(malformed, dict):
            raise ValueError("seen and malformed_seen must be maps")

        def parsed_ts(value: object) -> Optional[datetime]:
            if not isinstance(value, str) or not value:
                return None
            parsed = _parse_broker_timestamp(value)
            if parsed.tzinfo is None or parsed > clock + _CLOCK_SKEW:
                return None
            return parsed

        retention_floor = clock - timedelta(days=_MAX_CATCHUP_DAYS)
        expired_seen = []
        for fill_id, ack in seen.items():
            if not isinstance(fill_id, str) or not fill_id:
                raise ValueError("seen contains an invalid fill ID")
            if not isinstance(ack, dict) or set(ack) != {"ts", "hash"}:
                raise ValueError(f"seen[{fill_id!r}] has an invalid acknowledgment")
            digest = ack.get("hash")
            acknowledged_at = parsed_ts(ack.get("ts"))
            if (
                acknowledged_at is None
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise ValueError(f"seen[{fill_id!r}] has invalid timestamp/hash")
            if acknowledged_at < retention_floor:
                expired_seen.append(fill_id)
        expired_malformed = []
        for fingerprint, stamp in malformed.items():
            acknowledged_at = parsed_ts(stamp)
            if (
                not isinstance(fingerprint, str)
                or not fingerprint
                or acknowledged_at is None
            ):
                raise ValueError("malformed_seen contains an invalid acknowledgment")
            if acknowledged_at < retention_floor:
                expired_malformed.append(fingerprint)
        for field in ("initialized_at", "last_delivery_at", "last_poll_at"):
            if field in data and parsed_ts(data[field]) is None:
                raise ValueError(f"{field} is not a timezone-aware ISO timestamp")
        for fill_id in expired_seen:
            del seen[fill_id]
        for fingerprint in expired_malformed:
            del malformed[fingerprint]
        return data
    except Exception as exc:
        logger.error("fill notification state unreadable; refusing to advance: %s", exc)
        return {"version": 1, "seen": {}, "degraded": True, "error": str(exc)}


def _fill_id(fill: dict) -> str:
    value = fill.get("id")
    return value.strip() if isinstance(value, str) else ""


def _tier_for_fill(fill: dict) -> str:
    """Return a proved tier, ``untagged``, or ``unavailable``.

    OCO/bracket children are generated by Alpaca without a client-order tag, so
    direct child lookup is followed by a nested recent-order search that inherits
    the parent's tag. Lookup failure is never mislabeled as a manual order.
    """
    order_id = str(fill.get("order_id") or "")
    if not order_id:
        return "unavailable"
    try:
        from execution import broker
        from execution.ownership_guard import tier_of_coid

        order = broker.get_order(order_id, nested=True)
        if order is None:
            return "unavailable"
        tier = tier_of_coid(str(getattr(order, "client_order_id", "") or ""))
        if tier:
            return tier
        parent_id = str(getattr(order, "parent_order_id", "") or "")
        if parent_id:
            parent = broker.get_order(parent_id, nested=True)
            if parent is None:
                return "unavailable"
            parent_tier = tier_of_coid(
                str(getattr(parent, "client_order_id", "") or "")
            )
            return parent_tier or "untagged"

        # Some Alpaca child objects omit parent_order_id. Search recent nested
        # parents for an exact leg ID before declaring the activity untagged.
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        symbol = str(fill.get("symbol") or "")
        stamp = str(fill.get("transaction_time") or "")
        after = _parse_broker_timestamp(stamp) - timedelta(days=2)
        request = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=after,
            symbols=[symbol] if symbol else None,
            nested=True,
            limit=500,
        )
        for parent in broker._get_trading_client().get_orders(filter=request) or []:
            for leg in getattr(parent, "legs", None) or []:
                if str(getattr(leg, "id", "") or "") == order_id:
                    parent_tier = tier_of_coid(
                        str(getattr(parent, "client_order_id", "") or "")
                    )
                    return parent_tier or "untagged"
        # An empty bounded search does not prove this was a manual/untagged
        # order: a long-lived bracket parent can predate the search window.
        return "unavailable"
    except Exception as exc:
        logger.warning(
            "fill notification order attribution failed for %s: %s", order_id, exc
        )
        return "unavailable"


def _message(fill: dict, tier: str) -> str:
    side = str(fill.get("side") or "").lower()
    if side in ("buy", "buy_to_cover"):
        verb = "BUY"
    elif side in ("sell", "sell_short"):
        verb = "SELL"
    else:
        raise ValueError(f"unknown fill side: {side!r}")
    emoji = ":large_green_circle:" if verb == "BUY" else ":red_circle:"
    raw_symbol = fill.get("symbol")
    raw_qty = fill.get("qty")
    raw_price = fill.get("price")
    raw_when = fill.get("transaction_time")
    if not isinstance(raw_symbol, str) or not raw_symbol.strip():
        raise ValueError("missing fill symbol")
    if isinstance(raw_qty, bool) or isinstance(raw_price, bool):
        raise ValueError("boolean fill quantity/price")
    symbol = raw_symbol.strip().upper()
    qty = float(raw_qty or 0)
    price = float(raw_price or 0)
    if not all(math.isfinite(value) and value > 0 for value in (qty, price)):
        raise ValueError(f"invalid fill quantity/price: qty={qty!r} price={price!r}")
    qty_text = str(int(qty)) if qty.is_integer() else f"{qty:g}"
    if tier in _TIER_LABELS:
        tier_text = _TIER_LABELS[tier]
    elif tier == "untagged":
        tier_text = "Not tagged"
    else:
        tier_text = "Attribution unavailable"
    order_id = str(fill.get("order_id") or "")
    order_tail = order_id[-8:] if order_id else "unavailable"
    if not isinstance(raw_when, str) or not raw_when:
        raise ValueError("missing fill timestamp")
    parsed_when = _parse_broker_timestamp(raw_when)
    if parsed_when.tzinfo is None:
        raise ValueError("fill timestamp is not timezone-aware")
    when = raw_when
    return (
        f"{emoji} *BROKER {verb} FILLED — {symbol}*\n"
        f"{qty_text} share{'s' if qty != 1 else ''} @ ${price:,.2f} "
        f"(${qty * price:,.2f})\n"
        f"Tier: *{tier_text}* | Order: `{order_tail}`\n"
        f"Broker fill: {when}"
    )


def poll_fill_notifications(
    *,
    now_et: Optional[datetime] = None,
    state_path: Path = _STATE,
    fetch_fills: Callable[[str], list] = _fetch_alpaca_fills_for_date,
    deliver: Callable[[str], bool] = send_slack_delivery,
    tier_lookup: Callable[[dict], str] = _tier_for_fill,
) -> int:
    """Notify all unseen fills and return the number delivered.

    The first healthy run delivers a bounded four-day backfill. Later calls
    resume from the last fully handled poll (with one overlap day), process
    oldest first, and acknowledge each ID only after confirmed Slack delivery.
    A failed delivery leaves the prior cursor unchanged for a complete retry.
    """
    now = now_et or datetime.now(_ET)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = _load_state(state_path, now)
        recovering_corrupt_state = False
        if state is not None and state.get("degraded"):
            incident = (
                ":rotating_light: *BROKER FILL ALERT STATE CORRUPT*\n"
                f"`{state_path}` could not be read: "
                f"{state.get('error', 'unknown error')}\n"
                "The corrupt file was quarantined. Broker fills will be replayed "
                "from the bounded catch-up window; duplicates are possible."
            )
            if not deliver(incident):
                return 0
            quarantine = state_path.with_name(
                f"{state_path.name}.corrupt.{now:%Y%m%dT%H%M%S}"
            )
            try:
                os.replace(state_path, quarantine)
            except OSError as exc:
                logger.error(
                    "fill notification corrupt-state quarantine failed: %s", exc
                )
                return 0
            state = None
            recovering_corrupt_state = True

        fills: list[dict] = []
        malformed_rows: list[tuple[str, str]] = []
        if state is None:
            backfill_days = (
                _MAX_CATCHUP_DAYS
                if recovering_corrupt_state
                else _INITIAL_BACKFILL_DAYS
            )
            start_date = now.date() - timedelta(days=backfill_days)
        else:
            try:
                last_poll = _parse_broker_timestamp(
                    str(state.get("last_poll_at") or "")
                ).date()
                start_date = last_poll - timedelta(days=1)
            except (TypeError, ValueError):
                start_date = now.date() - timedelta(days=_INITIAL_BACKFILL_DAYS)
        oldest = now.date() - timedelta(days=_MAX_CATCHUP_DAYS)
        if start_date < oldest:
            gap_msg = (
                ":rotating_light: *BROKER FILL ALERT CATCH-UP GAP*\n"
                f"Last successful poll predates {oldest.isoformat()}. "
                "Replaying the most recent "
                f"{_MAX_CATCHUP_DAYS} days; older fills require audit."
            )
            if not deliver(gap_msg):
                return 0
            start_date = oldest
        day = start_date
        while day <= now.date():
            try:
                page = fetch_fills(day.isoformat()) or []
            except Exception as exc:
                logger.warning("fill notification fetch failed for %s: %s", day, exc)
                return 0
            rows = page if isinstance(page, (list, tuple)) else [page]
            for row in rows:
                if isinstance(row, dict):
                    fills.append(row)
                    continue
                encoded = json.dumps(row, sort_keys=True, default=str)
                malformed_rows.append((
                    hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                    type(row).__name__,
                ))
            day += timedelta(days=1)

        fills.sort(key=lambda item: str(item.get("transaction_time") or ""))
        if state is None:
            state = {"version": 1, "initialized_at": now.isoformat(), "seen": {}}

        seen = state["seen"]
        malformed_seen = state.setdefault("malformed_seen", {})
        delivered = 0
        complete = True
        for fingerprint, row_type in malformed_rows:
            if fingerprint in malformed_seen:
                continue
            bad = (
                ":rotating_light: *MALFORMED BROKER FILL ACTIVITY*\n"
                "Alpaca returned a non-object row in FILL activity history. "
                "It was not presented as a buy/sell; valid rows in the same "
                "response will still be processed.\n"
                f"Type: `{row_type}` | Fingerprint: `{fingerprint[:12]}`"
            )
            if not deliver(bad):
                complete = False
                break
            malformed_seen[fingerprint] = now.isoformat()
            _atomic_write(state_path, state)
        if not complete:
            _atomic_write(state_path, state)
            return delivered
        for fill in fills:
            fid = _fill_id(fill)
            payload_hash = hashlib.sha256(
                json.dumps(fill, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if not fid:
                fingerprint = payload_hash
                if fingerprint not in malformed_seen:
                    bad = (
                        ":rotating_light: *MALFORMED BROKER FILL ACTIVITY*\n"
                        "Alpaca returned a FILL without its unique activity ID. "
                        "It was not reported as a buy/sell and was not placed "
                        "in the normal dedupe ledger.\n"
                        f"Fingerprint: `{fingerprint[:12]}`"
                    )
                    if not deliver(bad):
                        complete = False
                        break
                    malformed_seen[fingerprint] = now.isoformat()
                    _atomic_write(state_path, state)
                continue
            if fid in seen:
                prior = seen[fid]
                prior_hash = prior.get("hash") if isinstance(prior, dict) else None
                conflict_key = f"conflict:{fid}:{payload_hash}"
                if (
                    prior_hash
                    and prior_hash != payload_hash
                    and conflict_key not in malformed_seen
                ):
                    conflict = (
                        ":rotating_light: *CONFLICTING BROKER FILL ID*\n"
                        f"Alpaca returned different payloads for activity ID `{fid}`. "
                        "The second payload was not presented as another trade.\n"
                        f"Prior: `{prior_hash[:12]}` | New: `{payload_hash[:12]}`"
                    )
                    if not deliver(conflict):
                        complete = False
                        break
                    malformed_seen[conflict_key] = now.isoformat()
                    _atomic_write(state_path, state)
                continue
            try:
                message = _message(fill, tier_lookup(fill))
            except Exception as exc:
                fingerprint = payload_hash
                if fingerprint not in malformed_seen:
                    bad = (
                        ":rotating_light: *MALFORMED BROKER FILL ACTIVITY*\n"
                        f"Fill `{fid}` was not labeled BUY/SELL: {exc}\n"
                        f"Fingerprint: `{fingerprint[:12]}`"
                    )
                    if not deliver(bad):
                        complete = False
                        break
                    malformed_seen[fingerprint] = now.isoformat()
                    _atomic_write(state_path, state)
                continue
            if not deliver(message):
                # Preserve this and every later unseen fill for the next poll.
                logger.warning("fill notification delivery deferred for %s", fid)
                complete = False
                break
            seen[fid] = {
                "ts": str(fill.get("transaction_time") or now.isoformat()),
                "hash": payload_hash,
            }
            delivered += 1
            # Persist after each send. A crash can at worst duplicate the
            # just-accepted message; it cannot silently skip a later fill.
            cutoff = (now.date() - timedelta(days=_KEEP_DAYS)).isoformat()
            state["seen"] = {
                key: stamp
                for key, stamp in seen.items()
                if not stamp
                or str(stamp.get("ts", "") if isinstance(stamp, dict) else stamp)[:10]
                >= cutoff
            }
            state["last_delivery_at"] = now.isoformat()
            _atomic_write(state_path, state)
        # Advancing a catch-up cursor after a failed old fill would permanently
        # strand it outside the next date window (Friday failure on Monday is
        # the critical case). Only a fully handled batch may move the cursor.
        if complete:
            state["last_poll_at"] = now.isoformat()
        _atomic_write(state_path, state)
        if delivered:
            logger.info(
                "change_id=broker-fill-slack-notifier delivered %d broker fill "
                "notification(s)",
                delivered,
            )
        return delivered


def poll_fill_notifications_safe() -> int:
    """Fail-soft scheduler wrapper: notification failures never escape.

    The broker fetch has bounded network timeouts. Filesystem or unexpected
    parsing errors are logged for the next scheduled retry.
    """
    try:
        return poll_fill_notifications()
    except Exception as exc:
        logger.error("fill notification poll failed (trading continues): %s", exc)
        return 0
