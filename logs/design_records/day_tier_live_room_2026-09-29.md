# Day tier — min-stop room measured from the live touch (2026-09-29, Claude-signed)

Evidence (server logs/day_tier_runner_cron.log, fills): 2026-09-29 06:56 PT NVDA short FADE — reference 229.955, stop 230.29,
limit 229.50, fill 230.30 (market 230.2984) -> "fill INVALIDATED the setup" page + flatten ($0.00). Same class before the
post-fill guard: META 2026-09-23 (stop 750.13, fill 756.71, -$0.64), AAPL 2026-09-23 (fill crossed the target, -$0.16).
Root cause: `_min_stop_room_ok` measures entry->stop from the marketable LIMIT price (slippage on the favorable side of a stale
5-min reference), so NVDA showed $0.79 of room when the live bid was ~$0.01 from the stop.
Fix: also measure from the live touch the order trades into (short: bid, long: ask); use the smaller distance; skip if the
stop is already reached. Rule B screen: pure selectivity (only removes entries whose stop is already inside/through the live
market); size, frequency ceiling and concurrency unchanged -> not risk-path. Tests: tests/test_day_tier_live_room.py.
