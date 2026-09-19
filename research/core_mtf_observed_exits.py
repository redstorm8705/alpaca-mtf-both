"""Conservatively reconcile Core MTF short parents to observed ledger exits.

This research-only module reports a full exit as verified only when its
terminal ledger event explicitly embeds the parent entry price and closes the
logged quantity. Same-symbol proximity alone is deliberately insufficient.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from research.core_mtf_replay_events import CoreMTFShortEvent


_ENTRY_IN_REASON = re.compile(r"\bentry=\$?(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_TERMINAL_EVENTS = frozenset({"exit", "stop_hit"})


@dataclass(frozen=True)
class ObservedExitLink:
    """One candidate's ledger outcome, with explicit evidence strength."""

    entry_source_line: int
    symbol: str
    entry_time_utc: str
    status: str
    reason: str
    exit_source_line: int | None
    exit_time_utc: str | None
    exit_price: float | None
    exit_quantity: float | None
    exit_event: str | None
    exit_reason: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def reconcile_observed_exits(
    ledger_path: Path, candidates: Iterable[CoreMTFShortEvent]
) -> tuple[ObservedExitLink, ...]:
    """Return explicit full/partial links; label all other candidates unmatched."""
    raw_input = ledger_path.read_bytes()
    rows = _parse_rows(raw_input)
    ordered_candidates = sorted(candidates, key=lambda item: item.decision_time)
    links: list[ObservedExitLink] = []
    for index, candidate in enumerate(ordered_candidates):
        entry_row = rows.get(candidate.source_line)
        entry_qty = _positive(entry_row.get("size")) if entry_row else None
        next_entry_time = _next_same_symbol_time(ordered_candidates, index)
        matching_exit = _observed_exit(
            rows,
            candidate,
            entry_qty,
            next_entry_time,
        )
        if matching_exit is None:
            links.append(
                ObservedExitLink(
                    entry_source_line=candidate.source_line,
                    symbol=candidate.symbol,
                    entry_time_utc=candidate.decision_time.isoformat(),
                    status="unmatched",
                    reason="no_explicit_quantity_complete_terminal_exit",
                    exit_source_line=None,
                    exit_time_utc=None,
                    exit_price=None,
                    exit_quantity=None,
                    exit_event=None,
                    exit_reason=None,
                )
            )
            continue
        line_number, event, event_time, price, quantity = matching_exit
        status = "verified" if quantity == entry_qty else "partial_verified"
        reason = (
            "terminal_reason_entry_and_quantity_match"
            if status == "verified"
            else "terminal_reason_entry_partial_quantity_match"
        )
        links.append(
            ObservedExitLink(
                entry_source_line=candidate.source_line,
                symbol=candidate.symbol,
                entry_time_utc=candidate.decision_time.isoformat(),
                status=status,
                reason=reason,
                exit_source_line=line_number,
                exit_time_utc=event_time.isoformat(),
                exit_price=price,
                exit_quantity=quantity,
                exit_event=str(event.get("event")),
                exit_reason=str(event.get("reason") or ""),
            )
        )
    return tuple(links)


def source_sha256(ledger_path: Path) -> str:
    """Expose an immutable binding for a reconciliation artifact."""
    return hashlib.sha256(ledger_path.read_bytes()).hexdigest()


def _parse_rows(raw_input: bytes) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for line_number, raw in enumerate(raw_input.decode("utf-8").splitlines(), start=1):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows[line_number] = row
    return rows


def _next_same_symbol_time(
    candidates: list[CoreMTFShortEvent], index: int
) -> datetime | None:
    candidate = candidates[index]
    for later in candidates[index + 1 :]:
        if later.symbol == candidate.symbol:
            return later.decision_time
    return None


def _observed_exit(
    rows: dict[int, dict[str, Any]],
    candidate: CoreMTFShortEvent,
    entry_qty: float | None,
    next_entry_time: datetime | None,
) -> tuple[int, dict[str, Any], datetime, float, float] | None:
    if entry_qty is None:
        return None
    for line_number, event in rows.items():
        if event.get("symbol") != candidate.symbol:
            continue
        if event.get("event") not in _TERMINAL_EVENTS:
            continue
        event_time = _time(event.get("ts"))
        price = _positive(event.get("price"))
        quantity = _positive(event.get("size"))
        if event_time is None or price is None or quantity is None:
            continue
        if event_time <= candidate.decision_time:
            continue
        if next_entry_time is not None and event_time >= next_entry_time:
            continue
        if quantity > entry_qty:
            continue
        if not _reason_matches_entry(event.get("reason"), candidate.logged_entry_price):
            continue
        return line_number, event, event_time, price, quantity
    return None


def _reason_matches_entry(value: Any, entry_price: float) -> bool:
    if not isinstance(value, str):
        return False
    match = _ENTRY_IN_REASON.search(value)
    if match is None:
        return False
    return round(float(match.group(1)), 2) == round(entry_price, 2)


def _positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
