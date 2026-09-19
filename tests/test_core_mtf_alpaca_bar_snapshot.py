import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from research.core_mtf_alpaca_bar_snapshot import (
    BarSnapshotRequest,
    fetch_stock_bars,
    write_snapshot,
)


UTC = timezone.utc


def _query():
    return BarSnapshotRequest(
        symbols=("msft", "NVDA"),
        timeframe="5Min",
        start=datetime(2026, 9, 1, 14, tzinfo=UTC),
        end=datetime(2026, 9, 1, 15, tzinfo=UTC),
        feed="iex",
    )


def test_fetches_pages_and_sorts_bars_without_network():
    pages = [
        {"bars": {"NVDA": [{"t": "2026-09-01T14:05:00Z"}]}, "next_page_token": "next"},
        {"bars": {"NVDA": [{"t": "2026-09-01T14:00:00Z"}], "MSFT": []}},
    ]
    observed = []

    def fetch(request):
        observed.append(parse_qs(urlparse(request.full_url).query))
        return json.dumps(pages.pop(0)).encode()

    bars = fetch_stock_bars(
        _query(), api_key="key", secret_key="secret", request_fn=fetch
    )

    assert [bar["t"] for bar in bars["NVDA"]] == [
        "2026-09-01T14:00:00Z",
        "2026-09-01T14:05:00Z",
    ]
    assert observed[0]["symbols"] == ["MSFT,NVDA"]
    assert observed[1]["page_token"] == ["next"]


def test_snapshot_is_content_hashed_and_written_atomically(tmp_path):
    output = Path(tmp_path) / "bars.json"
    artifact = write_snapshot(
        output,
        _query(),
        {"NVDA": [{"t": "2026-09-01T14:00:00Z", "o": 100}]},
        fetched_at=datetime(2026, 9, 2, tzinfo=UTC),
    )

    saved = json.loads(output.read_text())

    assert saved == artifact
    assert artifact["bar_count"] == 1
    assert len(artifact["bar_data_sha256"]) == 64
    assert not output.with_suffix(".json.tmp").exists()
