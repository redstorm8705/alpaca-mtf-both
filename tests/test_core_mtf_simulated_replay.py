import hashlib
import json
from datetime import timedelta

import pytest

from research.core_mtf_simulated_replay import run_simulation


def _ledger(rows):
    return "".join(json.dumps(row) + "\n" for row in rows)


def _entry(symbol, timestamp):
    return {
        "event": "entry",
        "direction": "short",
        "trade_mode": "intraday",
        "ts": timestamp,
        "symbol": symbol,
        "price": 100,
        "stop": 105,
        "target": 95,
    }


def _snapshot(bars, *, permits_fills=False):
    raw = json.dumps(bars, sort_keys=True, separators=(",", ":")).encode()
    return {
        "kind": "alpaca_stock_bars_snapshot",
        "market_data_kind": "aggregated_ohlcv_bars",
        "execution_evidence": "not_bid_ask_or_quote_data",
        "fill_claims_permitted": permits_fills,
        "bar_data_sha256": hashlib.sha256(raw).hexdigest(),
        "bars": bars,
    }


def _bars():
    return {
        "TEST": [
            {"t": "2026-09-01T15:05:00Z", "o": 100, "h": 106, "l": 94, "c": 100}
        ]
    }


def test_overlap_quarantines_both_same_symbol_parents(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        _ledger(
            [
                _entry("TEST", "2026-09-01T15:00:00Z"),
                _entry("TEST", "2026-09-01T16:00:00Z"),
            ]
        )
    )
    snapshot = tmp_path / "bars.json"
    snapshot.write_text(json.dumps(_snapshot(_bars())))

    result = run_simulation(
        ledger, snapshot, max_hold=timedelta(hours=2), round_trip_cost_bps=8
    )

    assert [row["reason"] for row in result["results"]] == [
        "same_symbol_parent_within_simulation_horizon",
        "same_symbol_parent_within_simulation_horizon",
    ]


def test_missing_symbol_bars_is_explicitly_unevaluable(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(_ledger([_entry("MISSING", "2026-09-01T15:00:00Z")]))
    snapshot = tmp_path / "bars.json"
    snapshot.write_text(json.dumps(_snapshot(_bars())))

    result = run_simulation(
        ledger, snapshot, max_hold=timedelta(hours=1), round_trip_cost_bps=8
    )

    assert result["results"][0]["reason"] == "missing_symbol_bars"


def test_rejects_tampered_or_fill_capable_snapshot(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(_ledger([_entry("TEST", "2026-09-01T15:00:00Z")]))
    snapshot = tmp_path / "bars.json"
    tampered = _snapshot(_bars())
    tampered["bar_data_sha256"] = "bad"
    snapshot.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="hash mismatch"):
        run_simulation(
            ledger, snapshot, max_hold=timedelta(hours=1), round_trip_cost_bps=8
        )
    snapshot.write_text(json.dumps(_snapshot(_bars(), permits_fills=True)))
    with pytest.raises(ValueError, match="fill claims"):
        run_simulation(
            ledger, snapshot, max_hold=timedelta(hours=1), round_trip_cost_bps=8
        )


def test_preserves_explicit_simulation_method_metadata(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(_ledger([_entry("TEST", "2026-09-01T15:00:00Z")]))
    snapshot = tmp_path / "bars.json"
    snapshot.write_text(json.dumps(_snapshot(_bars())))

    result = run_simulation(
        ledger, snapshot, max_hold=timedelta(hours=1), round_trip_cost_bps=8
    )

    assert result["execution_claims_permitted"] is False
    assert result["method"]["entry"] == "next_bar_open_simulation"
    assert result["method"]["intrabar"] == "adverse_stop_first"
    assert result["method"]["round_trip_cost_bps"] == 8
