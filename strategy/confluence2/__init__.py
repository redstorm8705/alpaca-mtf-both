"""Confluence 2.0 feature and scoring code (entry rebuild, 2026-09-27).

CLOSED BARS ONLY: modules in this package read bars exclusively through
data.fetcher.fetch_closed_bars() / ClosedBars. Importing fetch_bars,
fetch_multi_timeframe, fetch_bars_window, get_client or DataFetcher here fails
tests/test_closed_bar_gate.py.
"""
