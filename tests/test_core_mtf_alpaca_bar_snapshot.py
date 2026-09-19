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
        {
            "bars": {
                "NVDA": [{"t": "2026-09-01T14:00:00Z"}],
                "MSFT": [{"t": "2026-09-01T14:00:00Z"}],
            }
        },
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
        {
            "NVDA": [{"t": "2026-09-01T14:00:00Z", "o": 100}],
            "MSFT": [{"t": "2026-09-01T14:00:00Z", "o": 200}],
        },
        fetched_at=datetime(2026, 9, 2, tzinfo=UTC),
    )

    saved = json.loads(output.read_text())

    assert saved == artifact
    assert artifact["bar_count"] == 2
    assert len(artifact["bar_data_sha256"]) == 64
    assert artifact["market_data_kind"] == "aggregated_ohlcv_bars"
    assert artifact["execution_evidence"] == "not_bid_ask_or_quote_data"
    assert artifact["fill_claims_permitted"] is False
    assert artifact["missing_symbols"] == []
    assert not output.with_suffix(".json.tmp").exists()


def test_rejects_repeated_pagination_token():
    pages = [
        {"bars": {"NVDA": [{"t": "2026-09-01T14:00:00Z"}]}, "next_page_token": "loop"},
        {"bars": {"NVDA": [{"t": "2026-09-01T14:05:00Z"}]}, "next_page_token": "loop"},
    ]

    def fetch(_request):
        return json.dumps(pages.pop(0)).encode()

    try:
        fetch_stock_bars(_query(), api_key="key", secret_key="secret", request_fn=fetch)
    except ValueError as exc:
        assert "repeated next_page_token" in str(exc)
    else:
        raise AssertionError("expected repeated page token to raise")


def test_rejects_duplicate_or_malformed_bars():
    duplicate = {
        "bars": {"NVDA": [{"t": "2026-09-01T14:00:00Z"}, {"t": "2026-09-01T14:00:00Z"}]}
    }

    def duplicate_fetch(_request):
        return json.dumps(duplicate).encode()

    try:
        fetch_stock_bars(
            _query(), api_key="key", secret_key="secret", request_fn=duplicate_fetch
        )
    except ValueError as exc:
        assert "duplicate bar" in str(exc)
    else:
        raise AssertionError("expected duplicate bar to raise")

    def malformed_fetch(_request):
        return json.dumps({"bars": {"NVDA": ["bad"]}}).encode()

    try:
        fetch_stock_bars(
            _query(), api_key="key", secret_key="secret", request_fn=malformed_fetch
        )
    except ValueError as exc:
        assert "non-object bar" in str(exc)
    else:
        raise AssertionError("expected malformed bar to raise")


def test_rejects_snapshot_with_omitted_requested_symbol(tmp_path):
    try:
        write_snapshot(
            Path(tmp_path) / "bars.json",
            _query(),
            {"NVDA": [{"t": "2026-09-01T14:00:00Z", "o": 100}]},
            fetched_at=datetime(2026, 9, 2, tzinfo=UTC),
        )
    except ValueError as exc:
        assert "omitted requested symbols" in str(exc)
    else:
        raise AssertionError("expected omitted symbol to raise")
