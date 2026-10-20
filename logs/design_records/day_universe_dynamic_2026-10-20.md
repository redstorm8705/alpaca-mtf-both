# Dynamic Day-tier universe — design brief for the board, Gro and GAI — 2026-10-20 (Claude)

Status: DESIGN (no code). Owner request 2026-10-20: "I want to expand the day trade universe but will defer to the
bggn on which stocks to include, I think we need more than just a static number."

## Frame
PAPER account, $2.5K → $25K. #1 goal: data collection and edge evaluation; the bot trades every day; the only daily
brake is the 7% kill switch. More trades with conviction, never fewer.

## Facts today (verified 2026-10-20)
- Track A (GEX setups) trades a fixed 11-name list: config.DAYTRADE_UNIVERSE = AAPL, AMZN, GOOGL, META, MSFT, NVDA,
  TSLA, MU, SNDK, DRAM, EWY. It must match data/gex.py `_DAYTRADE_UNDERLYINGS`, or a name reads GEX=UNKNOWN and
  never trades.
- Track B (morning movers) uses a fixed 15-name list, config.DAYTRADE_TRACK_B_UNIVERSE: NVDA, TSLA, META, AMD, AMZN,
  AAPL, MSFT, GOOGL, AVGO, NFLX, MU, COIN, PLTR, SMCI, UBER. The config comment says the fixed list doubles as the
  float/liquidity guard ("no market-wide gainers feed").
- Track M trades QQQ only, on a Monday weekend gap-down.
- Capacity limits:
  - GEX refresh measures ~2 s per symbol: 13 symbols in ~27 s at 17:20 UTC on 10/20, every 15 minutes, on the writer
    thread.
  - The Day runner allows DAYTRADE_MAX_API_CALLS_PER_RUN = 60 per 2-minute tick, and DAYTRADE_CALLS_PER_ENTRY_EST = 15
    calls per entry.
  - Track B has a 75 s tick time budget.
- On 10/20 no Day-tier trade by 09:10 PT. 10 of 11 Track-A names qualified, but none showed a failed sweep or a fresh
  wall break.

## Questions for the board
1. Candidate pool: the SPY + QQQ constituents, the S&P 500 + Nasdaq-100, or another liquid pool? Leveraged/inverse
   ETFs are excluded from the pool and stay in the existing pivot route.
2. Daily selection rule, so the list is derived and not fixed. Inputs considered:
   - liquidity: dollar ADV, IEX share of volume, quoted spread;
   - range: ATR% / realized volatility;
   - option-chain depth (open interest near the money), which GEX needs to be reliable;
   - price vs buying power (whole-share fit);
   - catalysts: earnings date, gap;
   - each name's own measured Day-tier result so far (from Alpaca fills).
3. Size: derive the count from the measured capacity (GEX refresh time, the API-call budget per tick) and from how
   many names pass the selection rule. Never a fixed N.
4. Rotation: rebuild the list pre-market each day? Allow an intraday add (a stock gapping on news)?
5. Track B: should it draw from the same dynamic pool, with the screen still requiring gap + RVOL?
6. Fail-safe: a failed selection run keeps yesterday's list. A name without a reliable GEX reading stands down
   (unchanged).
7. Risk-path routing: more names means more trade frequency, so this is risk-path (Rule E). Per-trade size stays
   bounded by the existing Kelly, per-trade-risk, gross, single-name, thin-name and correlation caps.

## Required questions
See the board/Gro/GAI answers once aligned; Claude fills Q1–Q44 in the final design before any owner approval.

## Alignment (2026-10-20): board Kyle/Harris + LdP/Asness, Gro and GAI round 2, all AGREE-WITH-CHANGES on the skeleton
- **Pool:** a point-in-time dollar-volume pool of tradable Alpaca US stocks and ETFs, ranked from bars, which avoids
  index-membership survivorship. Leveraged/inverse ETFs stay out of the pool and keep their pivot route.
- **Eligibility** (no new numbers):
  - Track A only: the existing GEX quality gate.
  - Measured cost/ATR and IEX print rate no worse than the incumbents'. Cost comes from fills or bar-based spread
    estimators, never a single IEX quote (TSLA quoted 128 bp against about 2 bp real).
  - Whole-share fit under the live budget.
  - Open split: the incumbent "worst" (Kyle) or the incumbent "median" (Gro) as the ceiling. The board leans
    "worst" because it admits more names, which fits the more-trades frame.
- **Ranking:** equal-weight percentile of opening RVOL and gap, each measured against the name's own distribution.
  No fitted weights. A name's own P&L never removes it.
- **Size:** min(measured GEX capacity, data-API capacity, pass count). The 60-call budget limits entries per tick,
  not names watched (round-1 Gro/GAI error corrected). GEX measured about 31 s per 13 names on a 900 s cycle.
- **Lifecycle:**
  - Floor: yesterday's list. Open positions are always kept.
  - Rebuilt pre-market with rank hysteresis.
  - Intraday adds for Track B only.
  - One atomic artifact read by `config.DAYTRADE_UNIVERSE` consumers, `data/gex.py` and Track B.
  - Fallback: yesterday's list, then the static lists.
- **Replay before ship:** walk-forward V0 (static) / V1 (dynamic pool, same N) / V2 (capacity N), with costs and a
  DSR over 20 trials. Honest limit: Track A on new names cannot be replayed on its GEX component (no historical OI).
- **Risk-path:** frequency rises, so this goes to the board plus the masked-loss seat. Per-trade size stays under
  the existing caps.

## Direction-gate finding (owner rule; measured 2026-10-20, read-only replay of every "ENTER skipped" line)
- 53 unique skipped setups between 10/05 and 10/20.
- Signed move from the trigger price to the close: mean +0.23%, median +0.05%, 29/53 up. No stops/targets and no
  costs, IEX bars.
- By reason:
  - "no clear trend (TWO_SIDED)": 16 skips, mean −0.02%. The gate is doing no harm here.
  - "watch day not confirmed": 16 skips, +0.33%.
  - "against the daily side": 4 skips (3 + 1), all up to the close, mean about +2%. That sample is too small to act on.
- 10/20: COIN +0.68%, TSLA +0.23%, MU −0.70% to the close.
- Recommendation: leave the gate as the owner set it. Track the against-the-daily-side subset, and include the gate
  in the V0/V1/V2 replay so the universe change is measured with it in place.

## Pool measurement (2026-10-20, read-only, scratchpad pool_measure.py on OCI)
- Pool: 5,139 active, tradable, shortable, easy-to-borrow Alpaca US equities/ETFs, with leveraged/inverse ETFs
  excluded via `instrument_policy.is_leveraged_etf`.
- Ranking: 20-session median SIP dollar volume.
- Measures, for the top 300 by dollar volume plus the 21 incumbents:
  - Corwin–Schultz high-low spread (bps);
  - ATR% (median true range);
  - cost/ATR;
  - median IEX prints per 5-minute RTH bar over the last 5 sessions.
- Incumbent envelope:
  - cost/ATR: worst 0.372 (COIN), median 0.028;
  - IEX prints per 5-minute bar: worst 36 (COIN), median 145.
- The "median incumbent" rule fails 12 of the 21 incumbents themselves (AAPL, AMD, AMZN, COIN, DRAM, EWY, GOOGL,
  META, PLTR, SMCI, SNDK, UBER), so it would shrink the list. Rejected. The rule is "no worse than the worst incumbent".
- That rule admits 184 of the top 300.
- New names, the first 40 by dollar volume: SPY, QQQ, SPCX, INTC, IWM, GOOG, HYG, MRVL, TSM, LITE, TLT, SOXX, SMH, ORCL,
  BE, NBIS, LQD, MRNA, DELL, STX, WDC, SKHY, MSTR, AMAT, MDT, LRCX, JPM, ASML, WMT, CRWV, CRWD, XLF, V, CRM, XOM, HOOD,
  PANW, BAC, UNH, GEV.
- Caveats:
  - The admitted set includes low-range bond ETFs (HYG, TLT, LQD). The in-play ranking, not eligibility, keeps them
    down the list.
  - This measurement uses current data. The replay below is NOT point-in-time on the pool (survivorship bias).

## Replay result and revised alignment (2026-10-20, rounds 3-5)
- Replay: 263 sessions (2025-10-01 to 2026-10-16), 184 admitted names plus the current 15. Engine: the bot's own
  screen_mover and compute_momentum_trigger, stop at the opening-range level, 2R target, EOD flat, 5 bp cost.
  - As-live (min-stop room gate plus owner direction rules): current 15 n=326, −0.051R (t −0.81); new names
    n=2,642, 10/session, −0.060R (t −2.64, 90% CI [−0.112, −0.011]).
  - Before costs the trigger has no edge in either set (+0.004R / +0.002R). The in-play rank does not separate
    winners. A name's own past R does not persist (half-on-half correlation 0.054).
  - Stop width as a share of price is known before the order. It separates winners monotonically: new-name
    quintiles −0.156 / −0.152 / −0.073 / +0.003 / +0.080R.
  - Walk-forward, with the cutoff set from prior months only: above the cutoff n=976, +0.043R (t +1.19), about
    3.75/session; below it n=1,317, −0.134R. Holdout on the current 15: above +0.009R, below −0.090R.
- Aligned (selection seat + GAI AGREE-WITH-CHANGES; Gro AGREE-WITH-CHANGES after a counter-prompt that corrected two
  false premises; the microstructure seat's cost and feed rules are carried in):
  - Track B expands to a point-in-time dollar-volume pool.
  - A NEW-name signal is admitted only when its stop width clears the width at which rolling after-cost expectancy
    crosses zero, recomputed monthly from prior trades.
  - The current 15 names are unchanged, every trade at today's size. Track A stays on its current names.
  - Cost: live measured entry+exit cost from fills, or 5 bp until exit cost is logged.
  - Before the first live trade: re-run on a strictly point-in-time pool. If the above-cutoff cohort is below
    zero R there, the expansion does not ship.
  - Reversal: revert to the static list if admitted new-name trades over the trailing 20 sessions fall more than
    2 SE below the incumbents' mean R, or if live cost turns the above-cutoff group negative.
  - Next build: a meta-label on ENTER (purged walk-forward CV on the 8,302 signals) to drive size and admission.
- Disregarded: Gro round-3 figures from analyses that were never run (12-fold walk-forward, 10,000-day Monte Carlo,
  VIX>30 split) and Gro's proposed 200-session "audit-only" mode (a prohibited shadow period).
- Open anomaly to investigate: the admission registry reportedly lists gex_wall_ride/fade as BLOCKED, yet Track A
  trades them live.

## Point-in-time replay — the ship condition FAILED (2026-10-20, scratchpad pit_replay.py on OCI, /tmp/pit_replay.json)
- Each month's pool, envelope and admitted set came from the 20 sessions before that month: 97 to 228 names
  admitted per month. Each month's cutoff came from earlier months only, with the pre-registered rule "the smallest
  stop width whose prior trades at or above it average >= 0R". Cutoffs ran 16.6 to 225.6 bp. Residual bias: the
  asset list was fetched today, so names delisted earlier are absent.
- Current 15, as-live: n=326, −0.051R (t −0.81).
- New names, PIT-admitted, as-live, no cutoff: n=2,139, 8.1/session, −0.064R (t −2.57, CI90 [−0.117, −0.011]).
- New names, PIT-admitted, as-live, WITH cutoff: n=1,500, 5.7/session, −0.061R (t −2.06, CI90 [−0.120, −0.001]).
  Monthly results were mixed: Jan/Feb/Sep positive, Jul/Aug strongly negative (Aug −0.345R).
- Verdict under the aligned condition: the expansion does NOT ship. The selection seat's earlier walk-forward
  (+0.043R) used a percentile cutoff chosen after the stop-width finding. The pre-registered "crosses zero" rule does
  not reproduce it, and testing more cutoff variants now would be data snooping.
- Root cause: universe size is not the binding constraint. The Track-B mover trigger has zero edge before costs on
  every list tested, so more names add cost, not edge.
- Forward pass (aligned in rounds 3-5): build the edge first.
  - A meta-label on ENTER trained with purged walk-forward CV on the replay signal set (8,302 replayed signals, which
    includes losers), or a different trigger that shows edge before costs.
  - Re-test the universe expansion once such a trigger exists.
  - Investigate why Track A trades gex_wall_ride/fade live while the admission registry reportedly lists them BLOCKED.
