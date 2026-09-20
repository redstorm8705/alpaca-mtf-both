"""Extract auditable Core MTF candidates from the append-only event ledger.

The historical production label ``intraday`` means Core MTF (multi-day), not
the same-session Day Tier. This research-only adapter makes that mapping
explicit and preserves source provenance for every replay candidate. It never
imports the live trading path, fetches market data, or submits orders.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


_CORE_MTF_LEGACY_MODE = "intraday"
_EXTRACTOR_VERSION = "1"


@dataclass(frozen=True)
class CoreMTFShortEvent:
    """A structurally complete, logged Core MTF short-entry event."""

    decision_time: datetime
    symbol: str
    logged_entry_price: float
    stop: float
    target: float
    source_file: str
    source_line: int
    score: float | None
    mri_level: str | None

    def as_dict(self) -> dict:
        result = asdict(self)
        result["decision_time"] = self.decision_time.isoformat()
        return result


@dataclass(frozen=True)
class DiscardedCandidate:
    """A Core MTF short-shaped row excluded from a replay cohort."""

    source_line: int
    reason: str


@dataclass(frozen=True)
class ExtractionManifest:
    """Immutable-input accounting for one event-ledger extraction."""

    schema_v: int
    extractor_version: str
    source_file: str
    source_sha256: str
    source_bytes: int
    total_source_lines: int
    accepted_count: int
    discarded_counts: dict[str, int]
    discarded_candidates: tuple[DiscardedCandidate, ...]
    start_utc: str | None
    end_utc: str | None
    extracted_at_utc: str

    def as_dict(self) -> dict:
        result = asdict(self)
        result["discarded_candidates"] = [
            asdict(candidate) for candidate in self.discarded_candidates
        ]
        return result


@dataclass(frozen=True)
class ExtractionResult:
    """The accepted cohort and enough evidence to reconcile every exclusion."""

    events: tuple[CoreMTFShortEvent, ...]
    manifest: ExtractionManifest


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _utc_bound(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def iter_core_mtf_short_entries(
    ledger_path: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Iterator[CoreMTFShortEvent]:
    """Yield accepted historical Core MTF short entries with provenance."""
    yield from extract_core_mtf_short_entries(ledger_path, start=start, end=end).events


def extract_core_mtf_short_entries(
    ledger_path: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    extracted_at: datetime | None = None,
) -> ExtractionResult:
    """Extract a UTC-normalized cohort and deterministic input accounting.

    The manifest SHA-256 and byte count bind all source-line references to the
    exact input snapshot. Each Core-MTF-shaped row that is excluded carries its
    source line and reason, while all rows contribute to discard accounting.
    """
    start_utc = _utc_bound(start, "start")
    end_utc = _utc_bound(end, "end")
    extracted_at_utc = _utc_bound(extracted_at, "extracted_at")
    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise ValueError("start must not be after end")
    if extracted_at_utc is None:
        extracted_at_utc = datetime.now(timezone.utc)

    raw_input = ledger_path.read_bytes()
    source_sha256 = hashlib.sha256(raw_input).hexdigest()
    lines = raw_input.decode("utf-8").splitlines()
    events: list[CoreMTFShortEvent] = []
    discarded: Counter[str] = Counter()
    discarded_candidates: list[DiscardedCandidate] = []

    for line_number, raw in enumerate(lines, start=1):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            discarded["malformed_json"] += 1
            continue
        if not isinstance(row, dict):
            discarded["non_object_row"] += 1
            continue
        if row.get("event") != "entry" or row.get("direction") != "short":
            discarded["not_short_entry"] += 1
            continue
        if row.get("trade_mode") != _CORE_MTF_LEGACY_MODE:
            discarded["not_core_mtf_legacy_mode"] += 1
            continue

        decision_time = _parse_time(row.get("ts"))
        symbol = row.get("symbol")
        entry = _finite_positive(row.get("price"))
        stop = _finite_positive(row.get("stop"))
        target = _finite_positive(row.get("target"))
        if (
            decision_time is None
            or not isinstance(symbol, str)
            or not symbol.strip()
            or entry is None
            or stop is None
            or target is None
        ):
            reason = "invalid_required_fields"
            discarded[reason] += 1
            discarded_candidates.append(DiscardedCandidate(line_number, reason))
            continue
        if start_utc is not None and decision_time < start_utc:
            reason = "before_window"
            discarded[reason] += 1
            discarded_candidates.append(DiscardedCandidate(line_number, reason))
            continue
        if end_utc is not None and decision_time > end_utc:
            reason = "after_window"
            discarded[reason] += 1
            discarded_candidates.append(DiscardedCandidate(line_number, reason))
            continue
        if stop <= entry or target >= entry:
            reason = "invalid_short_geometry"
            discarded[reason] += 1
            discarded_candidates.append(DiscardedCandidate(line_number, reason))
            continue

        score = _finite_positive(row.get("score"))
        mri_level = row.get("mri_level")
        events.append(
            CoreMTFShortEvent(
                decision_time=decision_time,
                symbol=symbol.strip().upper(),
                logged_entry_price=entry,
                stop=stop,
                target=target,
                source_file=str(ledger_path),
                source_line=line_number,
                score=score,
                mri_level=mri_level if isinstance(mri_level, str) else None,
            )
        )

    manifest = ExtractionManifest(
        schema_v=1,
        extractor_version=_EXTRACTOR_VERSION,
        source_file=str(ledger_path),
        source_sha256=source_sha256,
        source_bytes=len(raw_input),
        total_source_lines=len(lines),
        accepted_count=len(events),
        discarded_counts=dict(sorted(discarded.items())),
        discarded_candidates=tuple(discarded_candidates),
        start_utc=start_utc.isoformat() if start_utc is not None else None,
        end_utc=end_utc.isoformat() if end_utc is not None else None,
        extracted_at_utc=extracted_at_utc.isoformat(),
    )
    return ExtractionResult(events=tuple(events), manifest=manifest)
