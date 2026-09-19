"""Create immutable, read-only Alpaca bar snapshots for Core MTF replay research.

This module is deliberately outside the live trading path. It only reads the
Alpaca historical-data API and writes an explicit local artifact supplied by a
CLI caller. It has no broker trading, signal, sizing, or execution imports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_BAR_ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
_SCHEMA_V = 1


@dataclass(frozen=True)
class BarSnapshotRequest:
    """An explicit historical-bar query with UTC start and end boundaries."""

    symbols: tuple[str, ...]
    timeframe: str
    start: datetime
    end: datetime
    feed: str

    def __post_init__(self) -> None:
        if not self.symbols or any(not symbol.strip() for symbol in self.symbols):
            raise ValueError("symbols must contain non-empty names")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if not self.timeframe.strip() or not self.feed.strip():
            raise ValueError("timeframe and feed must be non-empty")

    def as_query(self) -> dict[str, str]:
        return {
            "symbols": ",".join(
                sorted(symbol.strip().upper() for symbol in self.symbols)
            ),
            "timeframe": self.timeframe,
            "start": self.start.astimezone(timezone.utc).isoformat(),
            "end": self.end.astimezone(timezone.utc).isoformat(),
            "feed": self.feed,
        }


def fetch_stock_bars(
    query: BarSnapshotRequest,
    *,
    api_key: str,
    secret_key: str,
    request_fn: Callable[[Request], bytes] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read every page for a historical-bar query without modifying account state."""
    if not api_key or not secret_key:
        raise ValueError("Alpaca historical-data credentials are required")
    fetch = request_fn or _read_response
    page_token: str | None = None
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    while True:
        params = {**query.as_query(), "limit": "10000"}
        if page_token:
            params["page_token"] = page_token
        request = Request(
            f"{_BAR_ENDPOINT}?{urlencode(params)}",
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
                "Accept": "application/json",
            },
            method="GET",
        )
        payload = json.loads(fetch(request).decode("utf-8"))
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, dict):
            raise ValueError("Alpaca response has no bars object")
        for symbol, bars in raw_bars.items():
            if not isinstance(symbol, str) or not isinstance(bars, list):
                raise ValueError("Alpaca response has malformed symbol bars")
            bars_by_symbol.setdefault(symbol.upper(), []).extend(bars)
        next_token = payload.get("next_page_token")
        if next_token is None:
            break
        if not isinstance(next_token, str) or not next_token:
            raise ValueError("Alpaca response has invalid next_page_token")
        page_token = next_token
    return {
        symbol: sorted(bars, key=lambda bar: str(bar.get("t", "")))
        for symbol, bars in sorted(bars_by_symbol.items())
    }


def write_snapshot(
    output_path: Path,
    query: BarSnapshotRequest,
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    fetched_at: datetime,
) -> dict[str, Any]:
    """Atomically write a content-hashed replay input artifact."""
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    raw_bars = json.dumps(
        bars_by_symbol, sort_keys=True, separators=(",", ":")
    ).encode()
    artifact = {
        "schema_v": _SCHEMA_V,
        "kind": "alpaca_stock_bars_snapshot",
        "query": query.as_query(),
        "fetched_at_utc": fetched_at.astimezone(timezone.utc).isoformat(),
        "bar_data_sha256": hashlib.sha256(raw_bars).hexdigest(),
        "bar_count": sum(len(bars) for bars in bars_by_symbol.values()),
        "bars": bars_by_symbol,
    }
    encoded = (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(output_path)
    return artifact


def _read_response(request: Request) -> bytes:
    with urlopen(request, timeout=45) as response:  # noqa: S310 -- fixed HTTPS host
        return response.read()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include an offset")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols")
    parser.add_argument("--start", required=True, type=_parse_timestamp)
    parser.add_argument("--end", required=True, type=_parse_timestamp)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--feed", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    query = BarSnapshotRequest(
        symbols=tuple(args.symbols.split(",")),
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
        feed=args.feed,
    )
    bars = fetch_stock_bars(
        query,
        api_key=os.environ.get("ALPACA_API_KEY", ""),
        secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
    )
    artifact = write_snapshot(
        args.output,
        query,
        bars,
        fetched_at=datetime.now(timezone.utc),
    )
    print(
        f"wrote {args.output}: {artifact['bar_count']} bars "
        f"sha256={artifact['bar_data_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
