"""Extract auditable Core MTF candidates from the append-only event ledger.

The historical production label ``intraday`` means Core MTF (multi-day), not
the same-session Day Tier.  This research-only adapter makes that mapping
explicit and preserves the source line for every replay candidate.  It never
imports the live trading path, fetches market data, or submits orders.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


_CORE_MTF_LEGACY_MODE = "intraday"


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
    return parsed if parsed.tzinfo is not None else None


def iter_core_mtf_short_entries(
    ledger_path: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Iterator[CoreMTFShortEvent]:
    """Yield valid historical Core MTF short entries with provenance.

    Rows with malformed JSON, missing fields, a non-Core-MTF mode, invalid
    prices, or inverted short protection are intentionally excluded.  Consumers
    can compare the source line against the immutable event ledger.
    """
    if start is not None and start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    if end is not None and end.tzinfo is None:
        raise ValueError("end must be timezone-aware")
    if start is not None and end is not None and start > end:
        raise ValueError("start must not be after end")

    with ledger_path.open("r", encoding="utf-8") as source:
        for line_number, raw in enumerate(source, start=1):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("event") != "entry" or row.get("direction") != "short":
                continue
            if row.get("trade_mode") != _CORE_MTF_LEGACY_MODE:
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
                continue
            if start is not None and decision_time < start:
                continue
            if end is not None and decision_time > end:
                continue
            if stop <= entry or target >= entry:
                continue

            score = _finite_positive(row.get("score"))
            mri_level = row.get("mri_level")
            yield CoreMTFShortEvent(
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
