"""Account-wide, read-only position ownership snapshot.

Alpaca exposes one net position per symbol while this bot runs several independent
strategy tiers.  This module joins the broker net with the durable ownership ledger
and the day tier's faster append-only lifecycle log.  Callers can therefore answer
"does this tier own the *whole* broker position?" without treating mere symbol
membership as proof of exclusive ownership.

The snapshot never places, cancels, or changes an order.  An unreadable source stays
visible in ``errors`` and makes exclusive ownership unprovable (fail closed for new
co-holds).  This is the shared contract for later allocator, reconciliation, stop,
and reporting integrations.

Authored-by: OpenAI Codex (GPT-6), 2026-10-07.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_EPS = 1e-6
_LEDGER_TIERS = ("intraday", "qhm", "forever6", "daytrade")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def _field(obj: object, name: str, default: object = None) -> Any:
    return (
        obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
    )


def _canonical_symbol(raw: object, source: str) -> str:
    if (
        not isinstance(raw, str)
        or raw != raw.strip()
        or raw != raw.upper()
        or not _SYMBOL_RE.fullmatch(raw)
    ):
        raise ValueError(f"{source} symbol is missing or noncanonical: {raw!r}")
    return raw


def _broker_book(positions: Iterable[object]) -> dict[str, float]:
    if positions is None:
        raise ValueError("broker positions payload is None")
    book: dict[str, float] = {}
    for position in positions or ():
        symbol = _canonical_symbol(_field(position, "symbol", None), "broker position")
        if symbol in book:
            raise ValueError(f"duplicate broker position for {symbol}")
        raw_qty = _field(position, "qty", None)
        if raw_qty is None:
            raise ValueError(f"broker position missing qty for {symbol}")
        if isinstance(raw_qty, bool):
            raise ValueError(f"broker position has invalid qty for {symbol}")
        try:
            qty = float(raw_qty)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"broker position has invalid qty for {symbol}") from exc
        if not math.isfinite(qty):
            raise ValueError(f"broker position has non-finite qty for {symbol}")
        if abs(qty) > _EPS:
            book[symbol] = qty
    return book


def _today_daytrade_claims(now_et: datetime | None = None) -> dict[str, float]:
    """Signed open day-tier quantity from the fsync-durable lifecycle log.

    Only today's entries participate.  The day tier force-flattens each session, so
    an older unclosed record is stale and must not claim a current broker position.
    """
    from strategy import day_tier_logger

    now = (now_et or datetime.now(_ET)).astimezone(_ET)
    today = now.date()
    claims: dict[str, float] = {}
    trades, complete = day_tier_logger.open_trades_from_log_checked()
    if not complete:
        raise ValueError("day-tier lifecycle log incomplete")
    for trade in trades.values():
        symbol = _canonical_symbol(trade.get("symbol"), "day-tier lifecycle entry")
        raw_stamp = str(trade.get("entry_ts") or "")
        if "T" not in raw_stamp:
            raise ValueError(f"day-tier lifecycle timestamp invalid for {symbol}")
        stamp = datetime.fromisoformat(raw_stamp)
        if stamp.tzinfo is None:
            # Historical day-tier records use Pacific time when the offset is absent.
            stamp = stamp.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        if stamp.astimezone(_ET) > now:
            raise ValueError(
                f"day-tier lifecycle timestamp is in the future for {symbol}"
            )
        if stamp.astimezone(_ET).date() != today:
            continue
        raw_qty = trade.get("fill_qty")
        if isinstance(raw_qty, bool) or raw_qty is None or str(raw_qty).strip() == "":
            raise ValueError(f"day-tier lifecycle qty invalid for {symbol}")
        qty = abs(float(raw_qty))
        if not math.isfinite(qty):
            raise ValueError(f"day-tier lifecycle qty non-finite for {symbol}")
        side = str(trade.get("side") or "").lower()
        if side not in ("long", "buy", "short", "sell", "sell_short"):
            raise ValueError(f"day-tier lifecycle side invalid for {symbol}: {side!r}")
        signed = -qty if side in ("short", "sell", "sell_short") else qty
        claims[symbol] = claims.get(symbol, 0.0) + signed
    return {symbol: qty for symbol, qty in claims.items() if abs(qty) > _EPS}


@dataclass(frozen=True)
class OwnershipSnapshot:
    """Immutable broker net plus signed per-tier claims."""

    broker_qty: Mapping[str, float]
    claims: Mapping[str, Mapping[str, float]]
    errors: tuple[str, ...] = ()

    def claimed_qty(self, symbol: str, tier: str) -> float:
        return float(self.claims.get(str(symbol).upper(), {}).get(tier, 0.0) or 0.0)

    def claimants(self, symbol: str) -> frozenset[str]:
        return frozenset(
            tier
            for tier, qty in self.claims.get(str(symbol).upper(), {}).items()
            if abs(float(qty or 0.0)) > _EPS
        )

    def exclusively_owned(self, symbol: str, tier: str) -> bool:
        """True only when this tier's signed claim equals the complete broker net.

        Exact signed equality prevents a long claim from "owning" a broker short and
        detects same-symbol co-holds where the tier owns only part of the net.  Any
        unreadable ownership source makes exclusivity unprovable for every symbol;
        callers fail closed to foreign-held until the next clean snapshot.
        """
        symbol = str(symbol).upper()
        if self.errors or symbol not in self.broker_qty:
            return False
        own = self.claimed_qty(symbol, tier)
        net = float(self.broker_qty[symbol])
        no_other_claim = all(
            abs(float(qty or 0.0)) <= _EPS
            for name, qty in self.claims.get(symbol, {}).items()
            if name != tier
        )
        return abs(own - net) <= _EPS and no_other_claim

    def foreign_symbols(self, tier: str) -> frozenset[str]:
        """Every live broker symbol not proven to be exclusively owned by ``tier``."""
        return frozenset(
            symbol
            for symbol in self.broker_qty
            if not self.exclusively_owned(symbol, tier)
        )

    def residual_qty(self, symbol: str) -> float:
        """Broker quantity not explained by any tier claim (signed)."""
        symbol = str(symbol).upper()
        return float(self.broker_qty.get(symbol, 0.0)) - sum(
            float(qty or 0.0) for qty in self.claims.get(symbol, {}).values()
        )


def build_ownership_snapshot(
    positions: Iterable[object] | None = None,
    ledger: dict | None = None,
    *,
    now_et: datetime | None = None,
) -> OwnershipSnapshot:
    """Build the shared ownership view from broker truth and durable tier claims.

    ``positions`` and ``ledger`` are injectable for deterministic tests.  When they
    are omitted, live broker positions and the persisted ownership ledger are read.
    The day-tier lifecycle log overlays its ledger claim because it is written at fill
    time while the ledger maintainer runs periodically.  The overlay replaces, rather
    than adds to, the ledger daytrade quantity to avoid double counting.
    """
    errors: list[str] = []
    if positions is None:
        try:
            from execution import broker

            positions = broker.get_open_positions()
        except Exception as exc:  # broker uncertainty must remain explicit
            raise RuntimeError(f"broker positions unreadable: {exc}") from exc
    try:
        broker_qty = _broker_book(positions)
    except Exception as exc:
        raise RuntimeError(f"broker positions invalid: {exc}") from exc

    if ledger is None:
        try:
            from execution.ownership_guard import load_ledger

            ledger = load_ledger()
        except Exception as exc:  # noqa: BLE001 — corrupt/unreadable ledger is a recorded source error
            ledger = {"positions": {}}
            errors.append(f"ownership_ledger:{type(exc).__name__}")

    mutable: dict[str, dict[str, float]] = {}
    if not isinstance(ledger, dict) or not isinstance(ledger.get("positions"), dict):
        errors.append("ownership_ledger:invalid_schema")
        ledger_positions: dict = {}
    else:
        ledger_positions = ledger["positions"]
    normalized_symbols: set[str] = set()
    for raw_symbol, entry in ledger_positions.items():
        try:
            symbol = _canonical_symbol(raw_symbol, "ledger")
        except ValueError:
            errors.append(f"ledger_symbol:{raw_symbol!r}")
            continue
        if symbol in normalized_symbols:
            errors.append(f"ledger_symbol:{symbol}")
            continue
        normalized_symbols.add(symbol)
        if not isinstance(entry, dict) or not isinstance(entry.get("tiers", {}), dict):
            errors.append(f"ledger_entry:{symbol}")
            continue
        tiers = entry.get("tiers", {}) if isinstance(entry, dict) else {}
        unknown_tiers = set(tiers) - set(_LEDGER_TIERS)
        if unknown_tiers:
            errors.append(
                f"ledger_unknown_tier:{symbol}:{','.join(sorted(unknown_tiers))}"
            )
        for tier in _LEDGER_TIERS:
            if tier not in tiers:
                continue  # absent tier is a valid backward-compatible zero claim
            try:
                tier_claim = tiers[tier]
                if not isinstance(tier_claim, dict) or "qty" not in tier_claim:
                    raise ValueError("claim mapping/qty missing")
                raw_qty = tier_claim["qty"]
                if (
                    isinstance(raw_qty, bool)
                    or raw_qty is None
                    or str(raw_qty).strip() == ""
                ):
                    raise ValueError("claim qty invalid")
                qty = float(raw_qty)
            except (AttributeError, TypeError, ValueError):
                errors.append(f"ledger_claim:{symbol}:{tier}")
                continue
            if not math.isfinite(qty):
                errors.append(f"ledger_claim_nonfinite:{symbol}:{tier}")
                continue
            if abs(qty) > _EPS:
                mutable.setdefault(symbol, {})[tier] = qty

    try:
        live_daytrade = _today_daytrade_claims(now_et)
    except Exception as exc:  # noqa: BLE001 — durable-log failure is a recorded source error
        live_daytrade = {}
        errors.append(f"daytrade_log:{type(exc).__name__}")
    # A complete current-session log is more current than the periodic ledger in
    # both directions: clear every ledger daytrade slice, then overlay open claims.
    if not any(error.startswith("daytrade_log:") for error in errors):
        for tiers in mutable.values():
            tiers.pop("daytrade", None)
    for symbol, qty in live_daytrade.items():
        mutable.setdefault(symbol, {})["daytrade"] = qty

    frozen_claims = MappingProxyType(
        {symbol: MappingProxyType(dict(tiers)) for symbol, tiers in mutable.items()}
    )
    return OwnershipSnapshot(
        broker_qty=MappingProxyType(dict(broker_qty)),
        claims=frozen_claims,
        errors=tuple(errors),
    )
