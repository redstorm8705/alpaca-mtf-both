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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from alerts import send_slack_delivery
from execution.state_io import _ET, _SSL_CTX

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_STATE = _ROOT / "data" / "state" / "fill_notifications.json"
_PT = ZoneInfo("America/Los_Angeles")
_KEEP_DAYS = 8
_INITIAL_BACKFILL_DAYS = 4
_MAX_CATCHUP_DAYS = 31
_CLOCK_SKEW = timedelta(minutes=5)
# Realized P/L FIFO window: an older entry shows "unavailable", never an estimate.
_PNL_LOOKBACK_DAYS = 90
_ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"
_QTY_EPS = 1e-6
# Day-tier carry check: Day lots left open on any of the prior 7 calendar days
# make that session's Day-only replay unreliable (the tier should end flat).
_DAY_CARRY_DAYS = 7
_BROKER_TS = re.compile(
    r"^(?P<head>.+?)(?:\.(?P<fraction>[0-9]+))?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})$"
)

_TIER_LABELS = {
    "daytrade": "Day",
    "intraday": "Swing",
    "qhm": "QHM",
    "forever6": "Forever 6",
}


def _parse_broker_timestamp(
    value: str, *, report_normalization: bool = False
) -> datetime:
    """Parse Alpaca timestamps on Python 3.10, including 4/5-digit fractions."""
    match = _BROKER_TS.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid broker timestamp: {value!r}")
    fraction = match.group("fraction")
    if (
        report_normalization
        and fraction is not None
        and len(fraction) not in (3, 6)
    ):
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
        canonicalized_seen = 0
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
            canonical_stamp = acknowledged_at.isoformat()
            if ack.get("ts") != canonical_stamp:
                ack["ts"] = canonical_stamp
                canonicalized_seen += 1
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
        if canonicalized_seen:
            logger.info(
                "change_id=fill-timestamp-state-canonical normalized %d stored "
                "fill timestamp(s)",
                canonicalized_seen,
            )
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


def _fetch_fills_range(start: datetime, end: datetime) -> list:
    """All Alpaca FILL activities between two aware datetimes (T1 broker ledger).

    Paginated by page_token = the last activity id (verified live 2026-10-14:
    Alpaca ignores after_id and repeats page 1). Raises on any failure so the
    caller reports P/L as unavailable instead of guessing."""
    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    rows: list = []
    page_token: Optional[str] = None
    for _page in range(200):  # bounded: 200 x 100 fills
        params = {
            "direction": "asc",
            "page_size": "100",
            "after": start.isoformat(),
            "until": end.isoformat(),
        }
        if page_token:
            params["page_token"] = page_token
        url = (
            f"{_ALPACA_PAPER_BASE}/v2/account/activities/FILL?"
            + urllib.parse.urlencode(params)
        )
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            page = json.loads(resp.read().decode())
        if not isinstance(page, list):
            raise RuntimeError(f"Alpaca FILL API error: {str(page)[:200]}")
        rows.extend(page)
        if len(page) < 100:
            unique: dict = {}
            for row in rows:  # a boundary row returned twice must not count twice
                key = str(row.get("id") or "") if isinstance(row, dict) else ""
                unique[key or f"anon:{len(unique)}"] = row
            return list(unique.values())
        ids = [str(row.get("id") or "") for row in page if isinstance(row, dict)]
        next_token = next((i for i in reversed(ids) if i), "")
        if not next_token or next_token == page_token:
            raise RuntimeError("FILL pagination did not advance")
        page_token = next_token
    raise RuntimeError("FILL history exceeded the pagination bound")


def _fetch_fills_for_day(date_str: str) -> list:
    """One ET trading day of FILL activities via page_token paging.

    Replaces fifo_pnl._fetch_alpaca_fills_for_date here: that helper pages with
    after_id, which Alpaca ignores, so a 100+ fill day would repeat page 1
    forever while this process holds its lock.
    """
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime(day.year, day.month, day.day, tzinfo=_ET)
    rows = _fetch_fills_range(start, start + timedelta(days=1) - timedelta(seconds=1))
    out = []
    for row in rows:
        try:
            stamp = _parse_broker_timestamp(str(row.get("transaction_time") or ""))
        except (AttributeError, ValueError):
            out.append(row)  # malformed rows still reach the malformed-fill page
            continue
        if stamp.astimezone(_ET).date() == day:
            out.append(row)
    return out


class _RealizedPnl:
    """Per-poll realized P/L for one fill, from Alpaca fills only (FIFO).

    A tagged closing fill (Day/Swing/QHM/F6) is matched only against lots its own
    tier bought; a manual/unknown close is matched against the account's oldest
    lots and labeled "account FIFO". Day-tier fills use that session's Day fills
    (the tier is flat every night). Every other path requires the window's net
    position to equal the broker's and a complete order-tag map; otherwise the
    result is ``unavailable`` — never an estimate. Reads are cached per poll.
    """

    def __init__(self, now: datetime):
        self._now = now
        self._history: Optional[list] = None
        self._positions: Optional[dict] = None
        self._tiers: dict = {}
        self._tiers_complete: dict = {}
        self._history_error: Optional[str] = None

    def _load_history(self) -> list:
        if self._history_error is not None:
            raise RuntimeError(self._history_error)  # do not refetch per fill
        if self._history is None:
            start = self._now - timedelta(days=_PNL_LOOKBACK_DAYS)
            try:
                self._history = _fetch_fills_range(start, self._now + _CLOCK_SKEW)
            except Exception as exc:
                self._history_error = f"history read failed: {exc}"
                raise
        return self._history

    def _load_positions(self) -> dict:
        if self._positions is None:
            from execution import broker

            signed: dict = {}
            for pos in broker.get_open_positions() or []:
                qty = float(getattr(pos, "qty", 0) or 0)
                side = str(
                    getattr(getattr(pos, "side", ""), "value", getattr(pos, "side", ""))
                ).lower()
                if "short" in side and qty > 0:
                    qty = -qty
                signed[str(getattr(pos, "symbol", "")).upper()] = qty
            self._positions = signed
        return self._positions

    def _tier_map(self, symbol: str) -> dict:
        if symbol not in self._tiers:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            from execution import broker
            from execution.ownership_guard import tier_of_coid

            mapping: dict = {}
            request = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                # submitted-at filter: reach past the fill window for resting GTC orders
                after=self._now - timedelta(days=_PNL_LOOKBACK_DAYS + 30),
                symbols=[symbol],
                nested=True,
                limit=500,
            )
            orders = broker._get_trading_client().get_orders(filter=request) or []
            # Alpaca returns newest first; a full page may have cut off older tags.
            self._tiers_complete[symbol] = len(orders) < 500
            for order in orders:
                tier = (
                    tier_of_coid(str(getattr(order, "client_order_id", "") or ""))
                    or "untagged"
                )
                mapping[str(getattr(order, "id", ""))] = tier
                for leg in getattr(order, "legs", None) or []:
                    leg_tier = tier_of_coid(
                        str(getattr(leg, "client_order_id", "") or "")
                    )
                    mapping[str(getattr(leg, "id", ""))] = leg_tier or tier
            self._tiers[symbol] = mapping
        return self._tiers[symbol]

    def lookup(self, fill: dict, tier: str) -> dict:
        try:
            return self._compute(fill)
        except Exception as exc:
            logger.warning(
                "fill notification realized P/L unavailable for %s: %s",
                fill.get("id"),
                exc,
            )
            return {"status": "unavailable", "reason": "broker read failed"}

    def _compute(self, fill: dict) -> dict:
        symbol = str(fill.get("symbol") or "").strip().upper()
        target = _fill_id(fill)
        rows = [
            r
            for r in self._load_history()
            if isinstance(r, dict)
            and str(r.get("symbol") or "").strip().upper() == symbol
        ]
        rows.sort(
            key=lambda r: (
                _parse_broker_timestamp(str(r.get("transaction_time"))),
                str(r.get("id")),
            )
        )
        target_row = next((r for r in rows if _fill_id(r) == target), None)
        if target_row is None:
            return {"status": "unavailable", "reason": "fill not in broker history"}
        tiers = self._tier_map(symbol)
        own = tiers.get(str(target_row.get("order_id") or ""), "untagged")
        incomplete = {"status": "unavailable", "reason": "order history incomplete"}
        if own == "daytrade":
            # The Day tier is flat every night, so its own fills since that
            # session's start are a complete ledger (no lookback or account-
            # position dependency; an older buy-and-hold lot cannot block it).
            day = (
                _parse_broker_timestamp(str(target_row.get("transaction_time")))
                .astimezone(_ET)
                .date()
            )
            def _et_day(r: dict):
                stamp = _parse_broker_timestamp(str(r.get("transaction_time")))
                return stamp.astimezone(_ET).date()

            def _signed(r: dict) -> float:
                q = float(r.get("qty") or 0)
                return q if str(r.get("side") or "").lower().startswith("buy") else -q

            recent = [r for r in rows if (day - _et_day(r)).days <= _DAY_CARRY_DAYS]
            if any(str(r.get("order_id") or "") not in tiers for r in recent):
                return incomplete
            carried = sum(
                _signed(r) for r in rows
                if tiers.get(str(r.get("order_id") or "")) == "daytrade"
                and _et_day(r) < day
                and (day - _et_day(r)).days <= _DAY_CARRY_DAYS
            )
            if abs(carried) > _QTY_EPS:
                # The ledger assumes the Day tier ends every session flat.
                return {"status": "unavailable", "reason": "Day lots carried overnight"}
            session = [r for r in rows if _et_day(r) == day]
            target_at = session.index(target_row)
            if any(
                tiers.get(str(r.get("order_id") or "")) not in _TIER_LABELS
                for r in session[:target_at]
            ):
                # A manual trade earlier in the session may have consumed Day lots that
                # the Day-only replay below cannot see -> never a double-counted lot.
                return {"status": "unavailable", "reason": "manual trade in session"}
            rows = [
                r for r in session
                if tiers.get(str(r.get("order_id") or "")) == "daytrade"
            ]
        else:
            if not self._tiers_complete.get(symbol, False):
                return {"status": "unavailable", "reason": "order history too long"}
            if any(str(r.get("order_id") or "") not in tiers for r in rows):
                # A fill whose order is missing from the tag map would be pooled under
                # the wrong owner (e.g. a GTC submitted before the order window).
                return incomplete
            if own in _TIER_LABELS and any(
                tiers.get(str(r.get("order_id") or "")) not in _TIER_LABELS
                for r in rows[: rows.index(target_row)]
            ):
                # Which tier's lots an earlier manual trade drew from is unknowable,
                # so a tagged close after it has no provable cost basis.
                return {"status": "unavailable", "reason": "manual trade in window",
                        "owner": own}
            net = 0.0
            for r in rows:
                q = float(r.get("qty") or 0)
                net += q if str(r.get("side") or "").lower().startswith("buy") else -q
            broker_qty = self._load_positions().get(symbol, 0.0)
            if abs(net - broker_qty) > _QTY_EPS:
                side = str(target_row.get("side") or "").lower()
                if (
                    side.startswith("buy")
                    and broker_qty > _QTY_EPS
                    and own == "forever6"
                ) or side == "sell_short":
                    # Forever-6 has no short path, so its buy is an add; a sell_short
                    # is a short open. Neither realizes anything. Swing and QHM code
                    # can short and a manual buy may be a cover, so those fall
                    # through to "unavailable" — never a hidden loss.
                    return {"status": "opening", "owner": own}
                return {
                    "status": "unavailable",
                    "reason": f"entry older than {_PNL_LOOKBACK_DAYS} days",
                    "owner": own,
                }
        account_fifo = (
            own not in _TIER_LABELS
        )  # a manual/unknown close has no own bucket: match the account's lots
        # {"qty", "price", "tier", "sign"} oldest first; sign +1 long, -1 short
        lots: list = []
        for r in rows:
            qty = float(r.get("qty") or 0)
            price = float(r.get("price") or 0)
            sign = 1 if str(r.get("side") or "").lower().startswith("buy") else -1
            tier = tiers.get(str(r.get("order_id") or ""), "untagged")
            pool_tier = (
                None if tier not in _TIER_LABELS else tier
            )  # untagged/unknown fills draw on the account
            pool = [
                lot for lot in lots if pool_tier is None or lot["tier"] == pool_tier
            ]
            pool_net = sum(lot["qty"] * lot["sign"] for lot in pool)
            closing = min(qty, abs(pool_net)) if pool_net * sign < 0 else 0.0
            pnl = cost = 0.0
            left = closing
            for lot in pool:
                if left <= _QTY_EPS:
                    break
                if lot["sign"] == sign:
                    continue
                take = min(lot["qty"], left)
                pnl += (price - lot["price"]) * take * lot["sign"]
                cost += lot["price"] * take
                lot["qty"] -= take
                left -= take
            lots = [lot for lot in lots if lot["qty"] > _QTY_EPS]
            opening = qty - closing
            if _fill_id(r) == target:
                if (
                    tier in _TIER_LABELS
                    and str(r.get("side") or "").lower() == "sell"
                    and opening > _QTY_EPS
                ):
                    # Alpaca labels a long-reducing fill "sell" (a short open is
                    # "sell_short"): selling more than the tier's own lots means
                    # its ledger is incomplete -> no number, never a wrong one.
                    return {"status": "unavailable", "reason": "tier lots incomplete"}
                if closing <= _QTY_EPS:
                    return {"status": "opening", "owner": own}
                return {
                    "status": "ok",
                    "owner": own,
                    "pnl": pnl,
                    "pct": (pnl / cost * 100.0) if cost > 0 else None,
                    "cost": cost,
                    "closed_qty": closing,
                    "flipped_qty": opening if opening > _QTY_EPS else 0.0,
                    "mixed": account_fifo,
                    "covered": sign > 0,
                }
            if opening > _QTY_EPS:
                lots.append(
                    {"qty": opening, "price": price, "tier": tier, "sign": sign}
                )
        return {"status": "unavailable", "reason": "fill not in broker history"}


def _pt_clock(when: datetime) -> str:
    local = when.astimezone(_PT)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'} PT"


def _stamped(text: str, now: datetime) -> str:
    """Incident pages carry the time they were raised (cards carry the fill time)."""
    return f"{text}\n{_pt_clock(now)}"


def _message(fill: dict, tier: str, realized: Optional[dict] = None) -> str:
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
    elif tier == "untagged" or (realized or {}).get("owner") == "untagged":
        # The P/L lookup's order map proves an untagged order (not a bracket leg).
        tier_text = "Manual"
    else:
        tier_text = "Tier unknown"
    if not isinstance(raw_when, str) or not raw_when:
        raise ValueError("missing fill timestamp")
    parsed_when = _parse_broker_timestamp(raw_when, report_normalization=True)
    if parsed_when.tzinfo is None:
        raise ValueError("fill timestamp is not timezone-aware")
    status = (realized or {}).get("status")
    if verb == "BUY":
        action = "Covered" if status == "ok" else "Bought"
    else:
        action = "Shorted" if side == "sell_short" else "Sold"
    lines = [
        f"{emoji} *{action} {symbol}* · {tier_text}",
        f"{qty_text} share{'s' if qty != 1 else ''} @ ${price:,.2f}"
        f" · ${qty * price:,.2f}",
    ]
    if status == "ok":
        pnl = float(realized["pnl"])  # type: ignore[index]
        sign = "+" if pnl >= 0 else "-"
        pct = realized.get("pct")  # type: ignore[union-attr]
        pct_text = f" ({sign}{abs(pct):.1f}%)" if isinstance(pct, (int, float)) else ""
        mixed = " · account FIFO" if realized.get("mixed") else ""  # type: ignore[union-attr]
        closed = float(realized.get("closed_qty") or qty)  # type: ignore[union-attr]
        part = "" if closed >= qty - _QTY_EPS else f" on {closed:g} sh"
        lines.append(f"Realized P/L{part}: *{sign}${abs(pnl):,.2f}*{pct_text}{mixed}")
    elif status == "unavailable":
        lines.append(f"Realized P/L: unavailable ({realized.get('reason', 'unknown')})")  # type: ignore[union-attr]
    order_id = str(fill.get("order_id") or "")
    footer = f" · order {order_id[-8:]}" if order_id else ""
    lines.append(f"{_pt_clock(parsed_when)}{footer}")
    return "\n".join(lines)


def _merge_fills(items: list) -> tuple:
    """Merge one order's partial fills (same order and tier) into one card input.

    Quantity is summed, price is the quantity-weighted average, the time is the
    first piece's. Realized P/L sums only when every piece has a number; any
    unavailable piece makes the whole card unavailable (never a partial total).
    """
    first = items[0]
    fills = [item[2] for item in items]
    tier = first[3]
    if len(items) == 1:
        return first[2], tier, first[4]
    qty = sum(float(f.get("qty") or 0) for f in fills)
    vwap = sum(float(f.get("qty") or 0) * float(f.get("price") or 0) for f in fills)
    merged = {**fills[0], "qty": f"{qty:g}", "price": f"{vwap / qty:.6f}"}
    results = [item[4] or {} for item in items]
    unavailable = next((r for r in results if r.get("status") == "unavailable"), None)
    closes = [r for r in results if r.get("status") == "ok"]
    if unavailable is not None:
        realized = unavailable
    elif not closes:
        realized = results[0] or {"status": "opening"}
    else:
        pnl = sum(float(r["pnl"]) for r in closes)
        cost = sum(float(r.get("cost") or 0) for r in closes)
        realized = {
            "status": "ok",
            "pnl": pnl,
            "pct": (pnl / cost * 100.0) if cost > 0 else None,
            "cost": cost,
            "closed_qty": sum(float(r.get("closed_qty") or 0) for r in closes),
            "mixed": any(r.get("mixed") for r in closes),
            "covered": closes[0].get("covered"),
            "owner": closes[0].get("owner"),
        }
    return merged, tier, realized


def poll_fill_notifications(
    *,
    now_et: Optional[datetime] = None,
    state_path: Path = _STATE,
    fetch_fills: Callable[[str], list] = _fetch_fills_for_day,
    deliver: Callable[[str], bool] = send_slack_delivery,
    tier_lookup: Callable[[dict], str] = _tier_for_fill,
    realized_lookup: Optional[Callable[[dict, str], dict]] = None,
) -> int:
    """Notify today's unseen fills and return the number delivered.

    Only fills whose ET trading date is today are posted (CEO 2026-10-14); an
    unseen fill from an earlier date is acknowledged without a card and counted
    in a WARNING log. The fetch window, cursor and at-least-once rules are
    unchanged: process oldest first, acknowledge each ID only after confirmed
    Slack delivery, and keep the prior cursor after a failed delivery.
    """
    now = now_et or datetime.now(_ET)
    if realized_lookup is None:
        realized_lookup = _RealizedPnl(now).lookup
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
            if not deliver(_stamped(incident, now)):
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
            if not deliver(_stamped(gap_msg, now)):
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
        skipped_old = 0
        ready: list = []
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
            if not deliver(_stamped(bad, now)):
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
                    if not deliver(_stamped(bad, now)):
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
                    if not deliver(_stamped(conflict, now)):
                        complete = False
                        break
                    malformed_seen[conflict_key] = now.isoformat()
                    _atomic_write(state_path, state)
                continue
            try:
                fill_day = (
                    _parse_broker_timestamp(str(fill.get("transaction_time") or ""))
                    .astimezone(_ET)
                    .date()
                )
            except (TypeError, ValueError):
                fill_day = (
                    now.date()
                )  # unparseable -> _message reports it as malformed below
            if fill_day < now.date():
                seen[fid] = {
                    "ts": _parse_broker_timestamp(
                        str(fill.get("transaction_time"))
                    ).isoformat(),
                    "hash": payload_hash,
                }
                skipped_old += 1
                _atomic_write(state_path, state)
                continue
            try:
                tier = tier_lookup(fill)
                realized = realized_lookup(fill, tier)
                _message(fill, tier, realized)  # validate now; a bad row pages below
            except Exception as exc:
                fingerprint = payload_hash
                if fingerprint not in malformed_seen:
                    bad = (
                        ":rotating_light: *MALFORMED BROKER FILL ACTIVITY*\n"
                        f"Fill `{fid}` was not labeled BUY/SELL: {exc}\n"
                        f"Fingerprint: `{fingerprint[:12]}`"
                    )
                    if not deliver(_stamped(bad, now)):
                        complete = False
                        break
                    malformed_seen[fingerprint] = now.isoformat()
                    _atomic_write(state_path, state)
                continue
            ready.append((fid, payload_hash, fill, tier, realized))
        # One card per order: the partial fills of one order seen in this poll are
        # merged (Day-tier orders fill in many small pieces). Each piece's ID is
        # acknowledged only after its card is accepted, as before.
        groups: dict = {}
        for item in ready if complete else []:
            key = (str(item[2].get("order_id") or "") or f"fill:{item[0]}", item[3])
            groups.setdefault(key, []).append(item)
        for items in groups.values():
            fill, tier, realized = _merge_fills(items)
            card = _message(fill, tier, realized)
            if not deliver(card):
                # Preserve this and every later unseen fill for the next poll.
                logger.warning(
                    "fill notification delivery deferred for %s", items[0][0]
                )
                complete = False
                break
            for fid, payload_hash, raw, _tier, _realized in items:
                seen[fid] = {
                    "ts": _parse_broker_timestamp(
                        str(raw.get("transaction_time") or now.isoformat())
                    ).isoformat(),
                    "hash": payload_hash,
                }
                delivered += 1
            # Persist after each send. A crash can at worst duplicate the
            # just-accepted message; it cannot silently skip a later fill.
            cutoff = (now.date() - timedelta(days=_KEEP_DAYS)).isoformat()
            for key in [
                key
                for key, stamp in seen.items()
                if stamp
                and str(stamp.get("ts", "") if isinstance(stamp, dict) else stamp)[:10]
                < cutoff
            ]:
                del seen[key]  # prune in place: `seen` stays state["seen"]
            state["last_delivery_at"] = now.isoformat()
            _atomic_write(state_path, state)
            logger.info(
                "change_id=fill-notifier-ux-pnl card symbol=%s tier=%s fills=%d "
                "pnl_status=%s",
                str(fill.get("symbol") or ""),
                tier,
                len(items),
                (realized or {}).get("status", "none"),
            )
        # Advancing a catch-up cursor after a failed old fill would permanently
        # strand it outside the next date window (Friday failure on Monday is
        # the critical case). Only a fully handled batch may move the cursor.
        if complete:
            state["last_poll_at"] = now.isoformat()
        _atomic_write(state_path, state)
        if skipped_old:
            logger.warning(
                "change_id=fill-notifier-today-only acknowledged %d fill(s) from "
                "earlier trading dates without a Slack card",
                skipped_old,
            )
            notice = (
                f":information_source: {skipped_old} broker fill(s) from an earlier "
                "trading date were not posted (cards show today's fills only)."
            )
            if not deliver(_stamped(notice, now)):
                logger.error("fill notification skipped-fill notice not delivered")
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
