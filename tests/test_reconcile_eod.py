"""Tests for reconcile_eod._weighted_avg_exit_price RC-3 fix (skipped-fill sentinels)."""
import logging
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconcile_eod import _weighted_avg_exit_price


def _fill(qty: str, price: str, ts: str = "2026-06-02T10:00:00Z") -> dict:
    return {"qty": qty, "price": price, "transaction_time": ts}


def test_clean_fills_no_warning(caplog):
    fills = [_fill("5", "100.00"), _fill("3", "101.00")]
    price, qty = _weighted_avg_exit_price(fills, qty_remaining=8)
    assert qty == 8
    assert abs(price - (5 * 100 + 3 * 101) / 8) < 0.001
    assert "skipped" not in caplog.text


def test_malformed_qty_in_fifo_loop_logs_warning(caplog):
    """Inner FIFO loop: malformed qty should log WARNING and skip fill."""
    # partial_qty=2 means FIFO skip 2 shares first; one fill has bad qty
    fills = [
        _fill("bad_qty", "100.00"),  # malformed — will be skipped in inner FIFO loop
        _fill("2", "100.00"),        # will satisfy partial_qty=2
        _fill("5", "102.00"),        # final exit fill
    ]
    with caplog.at_level(logging.WARNING, logger="reconcile_eod"):
        price, qty = _weighted_avg_exit_price(fills, qty_remaining=5, partial_qty=2)
    assert "FIFO loop" in caplog.text
    assert "skipped" in caplog.text


def test_malformed_price_in_outer_loop_logs_warning(caplog):
    """Outer weighted-avg loop: fill with malformed price logs WARNING and is skipped.
    The fill must have valid qty (passes inner loop) but malformed price (fails outer)."""
    fills = [
        _fill("3", "bad_price"),  # valid qty, malformed price — skipped in outer loop
        _fill("5", "102.00"),     # valid
    ]
    with caplog.at_level(logging.WARNING, logger="reconcile_eod"):
        price, qty = _weighted_avg_exit_price(fills, qty_remaining=5)
    assert "weighted-avg loop" in caplog.text
    assert "skipped" in caplog.text
    assert qty == 5
    assert abs(price - 102.00) < 0.001


def test_all_malformed_returns_none(caplog):
    """If all fills are malformed, returns (None, 0)."""
    fills = [_fill("x", "y"), _fill("z", "w")]
    with caplog.at_level(logging.WARNING, logger="reconcile_eod"):
        result = _weighted_avg_exit_price(fills, qty_remaining=5)
    assert result == (None, 0)


def test_empty_fills_returns_none():
    result = _weighted_avg_exit_price([], qty_remaining=5)
    assert result == (None, 0)


def test_zero_qty_remaining_returns_none():
    fills = [_fill("5", "100.00")]
    result = _weighted_avg_exit_price(fills, qty_remaining=0)
    assert result == (None, 0)
