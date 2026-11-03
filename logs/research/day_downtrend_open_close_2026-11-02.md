# Day tier — buy the open on SPY-below-200-day days, tested — 2026-11-02 (signed: Claude)

Read-only research; no bot code changed. Pre-declared single candidate, chosen from a discovery-only observation in
round 3 (`day_search/out/r3_overnight.txt`: on SPY-below-200d days the big universe earned +12.5 bps open→close).

## Rule
- Regime: SPY's prior settled close below the average of its prior 200 settled closes (known before the open).
- Buy every name at the 09:30 open, sell at the 15:55 close. Long only. No stop.
- Cost: stocks 5 bps, ETFs 2 bps round trip.
- Book: causal fixed slot per name.
- Mirror (SPY above 200d, long) reported as a benchmark only.

## Data
- SIP 5-min bars:
  - big (50 stocks + SPY), 2020-10..2026-10;
  - r6etf (26 ETFs);
  - 2016-2020 backfills (bf_stocks, bf_r6etf).
- Discovery before 2024-10-26; test after (one look); 2016-2020 confirmation (one look).
- SPY daily closes come from the cached 16:00 bars; the first 200 sessions of 2016 have no regime and are skipped.

## Result

| | Days | Net bps/trade | Book t | Sharpe |
|---|---|---|---|---|
| Stocks, discovery | 238 | +6.2 | 0.61 | 0.63 |
| Stocks, test | 55 | +24.4 | 0.81 | 1.74 |
| Stocks, 2016-20 | 125 | +16.0 | 1.04 | 1.48 |
| ETFs, discovery | 238 | +3.7 | 0.50 | 0.52 |
| ETFs, test | 55 | +19.1 | 0.84 | 1.80 |
| ETFs, 2016-20 | 125 | +16.9 | 1.10 | 1.57 |

- Positive net in all three periods. Not significant:
  - Stouffer of test + confirmation for stocks: z ≈ 1.31, one-sided p ≈ 0.10;
  - no correction applied for the ~31,000 earlier candidates.
- By year (stocks, net bps): 2018 −7, 2019 +31, 2020 +26, 2022 +1, 2023 +47, 2025 +38, 2026 −19.
  - The longest bear year (2022, 211 days) earned about nothing.
- It is one bet per day on the market's intraday move (all names move together), so per-trade averages overstate it.
- **Benchmark finding:** on SPY-above-200d days, buying the open and selling the close loses after costs in every
  period:
  - stocks −4.3 / −2.9 / −5.0 bps;
  - ETFs −2.7 / −1.7 / −3.7 bps.
  - In an uptrend the intraday session carries about zero drift; the drift is overnight (round 3-4, trend_night).

## Verdict
Not an edge to ship. It also cannot trade today: SPY is above its 200-day average, so the rule is idle.

## Candidates and test-year looks
- 1 candidate (plus the benchmark mirror).
- Test years and 2016-20 each read once.
- Running total across the Day research: about 31,000 candidates, 0 survivors same-day.

Script: session scratchpad `dn/downday.py` on the round-1 harness `day_search/` (core.py, fam_r3.py).
