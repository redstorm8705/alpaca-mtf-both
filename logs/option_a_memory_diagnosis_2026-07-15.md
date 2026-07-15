# Option A — memory / OOM diagnosis (2026-07-15). DIAGNOSIS COMPLETE; fix = next.

**REFRAME:** NOT a monotonic leak — it's a SMALL true leak + a LARGE transient peak, summing to OOM
on the ~1GB box. RSS oscillates 195↔574MB within a single lifetime (a true leak never drops back).
gc.collect() now ~200ms (was 2.5-4.6s); gc.freeze() active (main.py:946). score_comparison is bounded
(pruned 14d, signal_generator.py:870 — old P5-M3 "unbounded" note is STALE).

## ROOT CAUSES (hunt: general-purpose agent, ranked)
1. **[TRUE LEAK — fix first] `data/fetcher.py:87` `_bar_cache`** — module dict, shared TTL bar cache.
   Written every fetch `_bar_cache[key]=(df,monotonic)` (key=(symbol,tf,n_bars), value=full DataFrame).
   The 180s TTL (`config.ALPACA_BAR_CACHE_TTL_SECS`) is ONLY a read-freshness check in `_cache_get`
   (L110-119). NO eviction/pop/del/clear/maxlen anywhere (only touch sites: L116 read, L127 write).
   `prune_atr()` at main.py:980 prunes a DIFFERENT cache (data/bar_cache.py). Stale DataFrames stay
   resident all session; premarket-mover symbols rotate daily → new keys that never leave → slow floor climb.
   **FIX:** add TTL eviction in the write path (`_cache_put`) — drop entries older than TTL on each write;
   AND/OR prune `_bar_cache` at daily reset like the ATR cache. Lowest-risk, highest-leverage.
2. **[TRANSIENT PEAK — the 230↔575 bounce] `strategy/signal_generator.py:200-226` `_analyze_symbol_full`** —
   per-cycle universe working set: 36 symbols × 3 TF, each `prepare_df` (confluence.py:55) copies+widens
   the frame (MA/MACD/RSI/VWAP cols); returned dict RETAINS `_entry_df`/`_daily_df`. Collected each cycle by
   gc.collect() (main.py:1046) → RSS drops back. **LEVER:** reduce `num_bars=400` (BARS_TO_FETCH) fetches;
   free `_entry_df`/`_daily_df` after scoring; or batch symbols.
3. [MED] `data/fetcher.py:55` `_fetch_bars_warned` — never-cleared symbol→time dict (tiny, floats only). Prune at daily reset.
4. Bounded/NOT leaks (confirmed): ATR `_cache` (pruned, TTL 4h), kelly `_tqi_history` (trim 10/100),
   lifecycle `_feed_age_history` (deque maxlen=3), GateState buffers + caches (cleared at daily reset),
   tracker.open_trades (capped MAX_OPEN_POSITIONS=20).

## NEXT STEP (resume here)
1. **Convene BGG** (board + Gro + GAI) on the FIX design: (a) `_bar_cache` TTL-eviction-on-write + daily
   prune (the true-leak fix), (b) peak reduction (free _entry_df/_daily_df post-score and/or trim num_bars=400).
   Open Q: evict-on-write vs periodic sweep; does trimming num_bars break EMA30/MACD26 lookback (config
   comment: EMA30 needs ~107 bars, MACD26 ~93 — so 150 floor OK, 400 is the 12h/daily/weekly)?
2. Build the fix (data/fetcher.py primarily; RTH-execution → full patch sequence + preship).
3. OPTIONAL FIRST: ship a tracemalloc snapshot-diff instrumentation (observability-only, main.py cycle
   boundary) to CONFIRM the _bar_cache floor climb vs peak, before/after the fix. No tracemalloc wired today.
4. Also fold the Slack-relief secondary follow-up: the */5 "bot DOWN" watchdog grace + consolidate the 3
   RAM watchdogs (the OOM restarts are what make them fire).
