# Day-tier deep search, round 2 — 2026-10-29 (signed: Claude)

Read-only research: no bot code changed, no orders. Follows `day_deep_search_r1_2026-10-29.md`. The harness is the
session scratchpad `day_search/`.

## Plain-English summary

**Round 1's best candidate failed confirmation.** The rule buys ETFs after an hourly oversold reading (60-minute
RSI(14) below 30) and holds for the next hour. On 2016–2020 data that had never been loaded, it lost money after
costs. The diagnosis explains why: it is almost entirely a leveraged bet on the market bouncing within the hour,
not a separate edge. (Measured.)

**No new candidate survived either.** About 3,000 new rule variants were tried across the five families the owner
named or implied. The only family that looked promising in discovery was end-of-day leveraged-ETF rebalancing flow
(t up to 2.7). It lost money in the 2024–26 test years.

**Running total: 29,947 candidates tried, 0 survivors.** The current Day-tier logic also has no edge. Same-day
trading on liquid US stocks and ETFs, after realistic costs, keeps coming up empty at this scale.

## 1. Confirmation on never-loaded data (2016-01-04 .. 2020-10-23, 1,198 sessions, run once)

Setup:
- Data: SIP 5-min bars for the 20 ETFs plus TQQQ.
- Candidate 1 was run exactly as tested in round 1, at 15 fixed slots of $667 ($2,500 × 4 buying power).
- The single pre-declared variant: the same signal computed on SPY and QQQ, traded long in SPXL and TQQQ, 2 slots
  of $5,000.

| | $/day | Sharpe | t | Max drawdown | Positions opened/day |
|---|---|---|---|---|---|
| Candidate 1, 0 bps | +0.65 | 0.32 | 0.69 | −$623 | 2.25 |
| Candidate 1, 2 bps | +0.35 | 0.17 | 0.38 | −$810 | |
| **Candidate 1, 5 bps** | **−0.10** | **−0.05** | **−0.10** | **−$1,262** | |
| Candidate 1, 10 bps | −0.85 | −0.41 | −0.90 | −$2,036 | |
| Variant, 0 bps | +0.42 | 0.09 | 0.19 | −$2,417 | 0.20 |
| **Variant, 5 bps** | **−0.07** | **−0.01** | **−0.03** | **−$2,814** | |
| Variant, 10 bps | −0.56 | −0.12 | −0.25 | −$3,237 | |

By year at 5 bps ($/day):

| | 2016 | 2017 | 2018 | 2019 | 2020 |
|---|---|---|---|---|---|
| Candidate 1 | −0.79 | +0.42 | −2.61 | +0.28 | +2.76 |
| Variant | +0.08 | +0.96 | −6.76 | +0.38 | +6.17 |

Both made money only in rebound years (2020) and lost in 2018.

**Combined evidence.** Discovery was the selection sample, so only the test years and the confirmation years count:
- Test t: 1.55 (498 sessions).
- Confirmation t: −0.10 (1,198 sessions). One-sided p 0.54; 1.0 after a Bonferroni ×2 for the two pre-declared runs.
- Stouffer combination of test and confirmation: z 0.75, p 0.23. After ×5 for the round-1 finalist look: 1.0.

**Verdict: not an edge.**

## 2. Diagnosis of candidate 1

**Market beta.** Each day's P&L was regressed on what the same dollar exposure would have earned holding SPY over the
same 5-minute bars:

| Period | Beta | Alpha $/day (t) | R² |
|---|---|---|---|
| Discovery 2020–24 | 1.47 | −0.61 (−1.98) | 0.84 |
| Test 2024–26 | 1.14 | +0.75 (+1.28) | 0.75 |
| Confirmation 2016–20 | 1.23 | −0.30 (−1.29) | 0.94 |

75–94% of the daily P&L is SPY's move during the held hour. The beta is above 1 because the basket leans on
leveraged and high-beta ETFs. There is no positive alpha in 2 of the 3 periods.

**Is it only the 3x ETFs?** No.

| Period | 3x ETFs (SOXL, SPXL) | Other 18 ETFs |
|---|---|---|
| Discovery | $0.96/day | $1.07/day |
| Test | $0.93/day | $0.88/day |
| Confirmation | $0.22/day | −$0.32/day |

The variant that trades only 3x vehicles has beta 3.3 and also fails. Its small alpha t (2.55) is just the leverage
of a market drift that does not survive costs.

## 3. New families (discovery 2020-10..2024-10 only; test read once for the finalists)

The universe added the 3x and inverse ETFs (TQQQ, SQQQ, SOXS, SPXS) plus TLT, GLD, KRE and ARKK. All regime cut-offs
are trailing (known at the open).

| Family | Trials | Best discovery t | Rules with t > 2 | Read |
|---|---|---|---|---|
| Volatility-regime switching: VIX / SPY realized-vol terciles or median, VIX/VIX3M; mean-reversion rule in one regime, trend rule in the other, both assignments | 270 | 1.56 | 0 | small gross (≤13 bps), dies on cost |
| Opening range on 3x/inverse ETFs × regime filters (VIX, SPY trend, backwardation, gap) | 504 | 1.94 | 0 | largest gross on rare filters (backwardation, n 103) |
| Intraday spread trades: 14 sectors vs SPY (beta-hedged); TQQQ/SPXL/SOXL vs 3× their 1x | 824 | 0.19 | 0 | ~10 bps gross, two legs cost 10 bps |
| End-of-day leveraged rebalancing: the day's move to 14:30 / 15:00 / 15:30 predicts the last 30–60 min, exit 15:55 or the 16:00 close | 864 | 2.74 | 8 | **finalists** |
| OPEX week, OPEX Friday, Monday after; month first / last / last two days (month-end also split by month-to-date move); 5 windows, SPY/QQQ/IWM/TQQQ | 560 | 2.04 | 2 | 47–49 events each; too few |

**Finalists.** Selected by the same pre-registered rule as round 1: at least 200 trades, positive at both 5 and 10
bps, ranked by t. All five came from the end-of-day flow family. Deflated Sharpe uses N = 29,945 trials (round-2
selection, before the two confirmation runs); it is 0.02–0.05 for all five.

| Rule | Discovery bps/trade (t) | Test bps/trade (t) | Bonferroni p |
|---|---|---|---|
| QQQ day move ≥0.5σ at 15:00 → follow in TQQQ to 15:55 | +13.4 (2.57) | −2.3 (−0.41) | 1.0 |
| SMH move at 15:00 → follow in SOXL, short side, to 15:55 | +19.4 (2.33) | −30.6 (−2.09) | 1.0 |
| QQQ move at 15:00 → follow in TQQQ, short side | +14.5 (2.32) | −3.0 (−0.42) | 1.0 |
| QQQ move ≥0.5σ at 14:30 → follow in TQQQ, long side | +17.0 (2.31) | −7.4 (−1.26) | 1.0 |
| SMH move at 15:00 → follow in SOXL to the 16:00 close | +12.9 (2.23) | −20.3 (−2.18) | 1.0 |

The end-of-day flow effect reversed after 2024. This is consistent with leveraged-ETF rebalancing becoming widely
known and traded (inferred, not measured).

## 4. Daily command

From the scratchpad `day_search/`:

    ../evenv/bin/python daily.py strategies/<name>.py

What it does:
1. Downloads the new settled SIP bars (`update.py`, using the bot's read-only data key; run it after the close).
2. Replays the current Track B on every session since 2026-10-27. It calls the live pure functions
   (`screen_mover`, `compute_momentum_trigger`) read-only and prices trades like the live rule: structural stop
   ±0.1%, 2R target, flat at 15:55, 5 bps.
3. Runs the named strategy on the same sessions: $/day at 5 bps on $2,500 × 4.
4. Writes one line per session to `out/daily_log.csv`. A date's line is replaced on rerun, so it is safe to run
   again.

Options: `--since` / `--until` / `--no-update`.

It was smoke-tested only on already-used test sessions (2026-10-19..23). The holdout sessions after 2026-10-26 have
not been evaluated by anything.

## 5. What this means and the next step

**The pattern across 29,947 candidates:**
- Frequent intraday signals on liquid names predict 0–4 bps per trade, while trading costs 2–5 bps.
- Event effects (FOMC, earnings, end-of-day flow, the hourly-dip bounce) look strong in one period and fade or flip
  in the next.

**What would change the answer (inferred, not measured):**
- An information source the bot does not have yet, e.g. live order-flow or option-flow imbalance, or news timing.
- A cost structure near 1 bp all-in.
- Moving the Day tier's capital to the one edge measured on these names, the multi-day SPY-trend hold.

Recommended round 3:
- Test the multi-day hold's intraday entry timing as a Day-tier role, i.e. use the Day tier to enter the Swing
  hold well.
- Test option-flow features from `gex_history.jsonl` once enough history exists.
- The 32-stock 2016–2020 backfill is being downloaded for a stock-level confirmation set.

## Limits

- The confirmation set is the 2016–2020 market (including 2018 and 2020), not the bot's live conditions.
- The 3x/inverse ETF bars are SIP; real fills for SOXS/SPXS may cost more than 5 bps.
- The beta regression uses SPY over the same bars, so it attributes leveraged-sector moves partly to alpha.
- Pairs assume simultaneous fills on both legs.

Scripts: `fetch_bf.py`, `conf_r2.py` (output `out/conf_r2.txt`), `fam_r2.py`, `finalize_r2.py`
(`out/finalists_r2_revealed.csv`), `daily.py`, `update.py`.
