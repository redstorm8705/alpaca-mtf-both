# Young-instrument Swing scoring (DRAM) — design (2026-10-13, Claude)

**Trigger (Rafael 2026-10-12):** "For DRAM, we need to make the logic/bot aware that it only has 6-months worth of
data and update accordingly."

**Facts:**
- DRAM has 133 daily bars (first bar 2026-04-02). It is the only one of the 40 universe names with 252 or fewer.
- `strategy/confluence.py` blocks any name with `len(daily_df) <= SMA_200`, logging "No daily data — entry blocked
  (fail-closed)". That happened 107 times per side on 2026-10-12.
- Without that block, DRAM still scores at most 7 of 12, because daily trend (3 points) needs 150/200 bars and
  12-1 momentum (2 points) needs 273 bars. The live threshold is 8.
- The Day tier already handles young names: `day_tier_side.py`, CEO 2026-10-09 "6+ months = tradable".

## BGG (board LdP + Thorp, Gro, GAI): all four choose A — renormalize, no proxies
- Rejected B, proxy horizons: an untested substitute feature that double-counts short-horizon trend (LdP, Thorp).
- Rejected C, keep closed: it gives up DRAM's data until roughly Jan-May 2027.
- Constraints (consolidated):
  1. **Young detection, fail-closed otherwise:**
     - at least 120 daily bars;
     - the fetch reached back past the first bar, so the history is short because the listing is new;
     - the last bar is fresh;
     - a name once seen with 252+ bars and now short is a failed fetch.
  2. **Score only what the history supports:** daily-trend and momentum points are absent, not proxied.
  3. **Stricter bar:** 6 of the 7 supportable points (LdP and Thorp).
  4. **Size never raised by the young path.** Thorp: 0.5x Kelly risk. LdP: the lowest conviction size tier.
     GAI: history/252, about 0.53 for DRAM. Consensus: about half size.
  5. **At most one young-name Swing position at a time.**
  6. **Log** `young_history`, `history_days` and `max_supportable` on every decision.
  7. **Per-feature kill flag.**
  8. **Graduation:** the trend points return at 200 bars; the normal path applies at 273 bars.
  9. **Swing breakout tier unchanged.** LdP: close > SMA200 *is* the signal. Thorp: a separate diff if ever.
  10. **Reversal:** turn the flag off if the first 10-15 young-path trades show negative mean R (t < -1) or a win
      rate under 35%.
- Rejected from Gro and GAI:
  - Gro: a 48-hour shadow run (no shadows, Rafael 2026-09-26) and an uncomputed "30% of days" figure.
  - GAI: a tighter profit target (no evidence offered).

## Front-loaded replay (LdP's pseudo-young test)
- **Script:** `research/young_history_replay.py`. **Output:** `logs/young_history_replay_2026-10-12.json`.
- **Data:** `delta_shadow` scores from 2026-07-20 to 2026-10-12, one observation per (symbol, day, direction):
  4,592 obs over 47 names.
- **Method:** young score = non-momentum score minus the daily-trend points recomputed from T1 daily bars. Outcome =
  signed close-to-close return.

| Cohort | n | 5-day mean | 5-day win rate |
|---|---|---|---|
| Full scorer, at least 8 | 2,926 | -56.9 bp | 47.5% |
| Young, at least 6 of 7 | 3,338 | -32.4 bp | 48.3% |
| Young, at least 6 and full under 8 | 1,026 | +74.3 bp | 52.6% |
| Long, full at least 8 | 1,677 | +40.6 bp | 51.4% |
| Long, young at least 6 | 1,684 | +79.4 bp | 52.7% |
| Short, full at least 8 | 1,166 | -187 bp | 42.3% |
| Short, young at least 6 | 1,545 | -146 bp | 43.9% |

- **LdP pass criterion (young hit rate no more than 10 points below full): PASS.** Young is equal or better at every
  horizon.
- **Caveats:**
  - Overlapping 5-day windows inflate the t-statistics.
  - It is a single regime (a rising market, Jul-Oct).
  - It is a daily max per symbol, not the exact entry bar.
- **Separate finding (logged for the CO audit, not this diff):** Swing-scorer SHORTS lost money in this window under
  both rules (5-day mean -187 bp, win rate 42%).

## Outcome after tracing the live path (2026-10-13): the scorer change has no trading effect, so it is not built
- **The 12-point Swing tier opens no new trades** (`SWING12_NEW_ENTRIES_ENABLED = False`, config.py:1032; retired
  for PF 0.89 over 154 trades). It also allocates $0 below score 10 (`entry_logic.py:1234`,
  `_LINEAR_SCORE_MIN = 10`). Renormalizing it would not let DRAM trade.
- **The Swing breakout tier is not a DRAM route either.** Its pool is 30 megacaps (`_DEFAULT_POOL`,
  swing_breakout_manager.py:81) ranked top-10 by dollar volume, and DRAM is not in the pool. The board's 2-2 split
  on the breakout tier is moot.
- **DRAM's live route is the Day tier, which is already young-aware** (CEO 2026-10-09). On 2026-10-12 it read
  DRAM SHORT with 100% coverage on the truncated stack and reached `would_consider=True` (conviction 0.66) twice.
  No entry was possible because buying power was $0 (the UBER incident).
- **Remaining:** relabel the scorer's "No daily data — entry blocked" WARNING (214 a day for DRAM) as a young-
  instrument INFO line. This is observability only, with no score or gate change. Optional and low priority.
- **Logged for the CO audit:** Swing-scorer shorts lost money in the 2026-07-20 to 10-12 window (5-day mean
  -187 bp, win rate 42%). That matters if the 12-point tier is ever re-enabled, and for any consumer of the scorer.
