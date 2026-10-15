# Dynamic capacity — replace the fixed 1.9x / 2-slot / 3% numbers (design record, 2026-10-14, Claude-signed)

## Why
CEO 2026-10-14: the Swing capacity change shipped three fixed numbers (overnight cap K = 1.9x, Swing reserve
= 2 slots, one-share risk = 3%). Standing rule from the same day: no static thresholds, no tag exemption
(CLAUDE.md "NO STATIC-THRESHOLD EXEMPTIONS"). Each number must be derived from data.

## BGGN (Gro gpt-oss-120b, GAI gemini-3.1-flash-lite, board Thorp + Taleb masked-loss seat) — aligned
1. **Overnight cap K (build first, tighten-only).** K_raw = 1 / (m + (1 - m) * d_book), with m = account
   maintenance ratio read from Alpaca (maintenance_margin / long_market_value) and
   d_book = 2 x stress / book leverage. stress = max(99th-pct worst 5-session loss of TODAY's book weights
   over the trailing 504 sessions x VIX scale, max over held names of weight x that name's worst single day
   on full split-adjusted history). VIX scale = max(1, VIX / its trailing-252 median) — a tightener only.
   K_dyn = min(K_raw, current ceiling 1.9) (raising above 1.9 needs a board vote); tighten at once, loosen
   only after 5 sessions (rolling minimum). Split-adjusted T1 daily bars only (raw bars show NVDA -89.9% on
   its 2024-06-10 split). Short history -> fall back to the tighter value; any stale/unreadable input ->
   the tighter of last good and the current ceiling, never looser. GAI's "shadow mode first" rejected (no-shadow rule).
2. **Swing reserve (board).** occupancy = lambda x H (Little's law), sized at a high quantile of
   occupancy (Taleb: breakouts cluster), lambda shrunk toward the replay rate (Bayesian), counting only signals
   that pass entry filters; risk-path (concurrency) -> board.
3. **One-share risk (board + masked-loss seat).** cap = min(0.25 x Kelly f* with lower-confidence p,
   normal per-lot cap, 7% / (worst-to-average loss ratio)); tier-level p/b, replay prior blended with live by
   n/(n + n0). GAI half-Kelly rejected (doubles size on in-sample estimates). Proposed 0.25 x f* alone
   rejected (6.0% of equity, 2x today's 3%).

## Front-loaded replay — item 1 (OCI, 2026-10-14, today's book, split-adjusted bars, 501 sessions)
K_dyn = 1.90 on 486 of 501 days; tightened on 15 days, all in the April 2025 sell-off, lowest **1.45x on
2025-04-08**. Expected effect: no change on calm days; fewer new overnight entries during market stress.
Size/frequency/concurrency delta: tighten-only (can only reduce) -> not risk-path by Rule E.
Reversal criterion: K_dyn below 1.9 on a calm day (VIX near its median, no book drawdown), or a margin call
while K_dyn allowed the exposure.

## Item 1 revised under the no-static rule (2026-10-14, BGGN aligned 4/4: Thorp + Taleb seats, Gro, GAI)
The fixed `M = 2` and fixed 504/252-session windows are static numbers, so they were replaced:
- stress_hist = WORST-EVER 5-session loss of today's book weights (equity-denominated, sum = book leverage) over each
  name's FULL split-adjusted history x R, where R = MEDIAN record-jump ratio (each new worst-5-session record / the
  previous record, counting only records above the median |5-session move|). Today R = 1.197 (12 records since 2016).
- VIX scale = max(1, VIX / median VIX over full history). d_book = max(stress_hist x VIX scale, concentration) /
  book leverage (weights are equity-denominated, so the division is correct — verified at source).
- K_dyn = min(1.9, K_raw), rolling 5-session minimum; short-history names take the book's largest worst-day.
Replay (502 sessions): A original 27 days < 1.9, low 1.47; B worst-ever only 10 / 1.62; C x max jump 1.607
94 / 1.26 (Gro's first pick); D x median jump 22 / 1.484 (chosen; Gro moved to D after counter-prompt).
Correction: margin-call drop at K = 1.9 with live m = 0.327 is 29.6% of long value, not ~32% (that used m = 0.30).

## Status
Item 1 (variant D) now building through the full gate. Items 2-3 follow through the board.
