# Day-tier deep search, round 4 — 2026-10-29 (signed: Claude)

Read-only research: no bot code, no orders. This round follows round 3's finding that the trend-day edge is earned
overnight. Rules as before:
- finalists picked on discovery (2020-10..2024-10) only;
- the sealed 2024-10..2026-10 test was read once per finalist;
- one run on the untouched 2016-01..2020-10 confirmation set;
- fixed-slot weighting;
- sessions after 2026-10-26 were not touched.

Scripts (scratchpad `day_search/`): `fam_r4.py`, `fam_r4a.py`, `finalize_r4.py`, outputs `out/finalists_r4_revealed.csv`
and `out/r4_bench.txt`.

## Plain-English summary

**First candidate in four rounds to pass a multiple-testing-adjusted test on data it was never tuned on.** (Measured.)
It is an overnight trade, so it belongs to the Swing tier, or a Day-tier buy handed to Swing, not a same-day trade.

**The rule.**
- **When:** days when SPY's prior close is above its 200-day average.
- **What:** among 50 large stocks, take the 20% that are strongest from yesterday's close to 15:25.
- **Trade:** buy them at the 16:00 close and sell at the next 09:30 open.
- **Cost assumed:** 5 bps round trip.

| | Discovery 2020-24 | Test 2024-26 (read once) | Confirmation 2016-20 (untouched, 30 other large caps) |
|---|---|---|---|
| Net bps per stock-night | +8.6 | +4.8 | +10.5 |
| t | 2.74 | 0.78 | 3.86 |
| $ per night (2x overnight buying power = $5,000, 10 slots of $500) | $3.24 | $2.16 | $2.57 |
| Sharpe | 1.35 | 0.60 | 1.75 |
| Max drawdown | −$1,351 | −$1,032 | −$409 |
| Worst night | −$247 | −$310 | −$128 |
| Worst single stock-night | −31% | −29% | −21% |
| Beta to SPY's overnight move | 1.15 | 1.49 | 1.14 |
| Alpha t (after removing SPY) | 1.01 | −0.60 | 2.71 |

**Significance.**
- Bonferroni across the 5 finalists: test p 1.0; confirmation p < 0.001.
- Test and confirmation combined (Stouffer z 3.67): p 0.001.
- On discovery alone it does not clear the deflated-Sharpe bar for about 30,000 trials (Sharpe 1.35 vs about 2.1).
  The untouched confirmation set is what carries it.

**Caveats, plainly.**
- **The 2024–26 test years are weak** (t 0.78), and below the open the alpha is not significant in 2 of 3 periods.
- **Part of the result is ordinary overnight market drift with beta above 1.** Plain overnight holding of every stock
  on trend-up days earns +1.5 / +3.5 / +2.3 bps (t 0.7–1.2). Picking the strongest adds roughly 5–8 bps a night.
- **Gap risk is real.**
  - Worst night −$310, about 12% of the $2,500 equity.
  - Max drawdown up to −$1,351, about 54% of equity at full 2x sizing.
  - Live sizing would have to be far smaller than full 2x.
- **By year ($/night):** 2020 +21.3 (one strong quarter), 2021 +4.0, 2022 −2.0, 2023 +1.8, 2024 +6.5, 2025 +3.6,
  2026 −0.5. Confirmation years: 2016 +0.7, 2017 +4.5, 2018 −0.7, 2019 +2.7, 2020 +6.4.
- **Universe difference.** Confirmation used 30 non-Day large caps; the Day names have no 2016–2020 data.

**Total candidates tried, all rounds: 30,235.**

## A. Entry timing into the 5-session trend hold

Setup: 2,439 trend-aligned long Track B signals; discovery n 1,431. Exit at the close of the 5th session, 5 bps.

| Entry | Mean R | bps | t (weekly clusters) | Paired R vs next bar, same signals |
|---|---|---|---|---|
| **Next bar after the signal (design baseline)** | **+0.236** | +84 | 2.81 | — |
| VWAP dip later that day (no dip → 16:00 close) | +0.211 | +78 | 2.61 | −0.025 (t −1.2) |
| 60m RSI dip (no dip → close) | +0.196 | +74 | 2.49 | −0.039 (t −1.3) |
| Last 30 minutes (15:30) | +0.165 | +64 | 2.10 | −0.071 (t −2.4) |
| 16:00 close | +0.198 | +75 | 2.51 | −0.037 (t −1.2) |
| Next morning's open | +0.129 | +46 | 1.89 | −0.108 (t −2.7) |

- "Dip only, otherwise skip" versions look better on the trades they take. That is mechanical: on signals where
  price later dips, the dip price is lower by construction. Skipping the no-dip signals drops the best trades, and
  overall they lose (vwap-dip-or-skip +0.180R).
- **Conclusion: entering immediately is best.** No Day-tier timing improves the hold.
- Side note: the same hold without the 1×ATR stop scores +0.296R vs +0.236R. That is an exit change for the Swing
  design, not tested further.
- 16 trials.

## B. Buy late, hold overnight (stocks: 51 names; ETFs: 20)

Grid:
- entry 15:30 or the 16:00 close × exit next 09:30 open or next 10:00;
- name rules: all / weakest 20% / strongest 20% / red on the day / green on the day / dipped in the last hour;
- regime: trend-up / trend-down / all days.

That is 72 rows each for stocks and ETFs.
- Weakest-into-close: no edge (+0.4 bps, t 0.16 in discovery).
- ETFs: best t 2.12, not a finalist.
- The 5 finalists were all stocks on trend-up days: strongest 20% held close→open (the winner above); green-on-the-day
  close→10:00; strongest close→10:00; strongest 15:30→open; last-hour dip close→open.

Their test / confirmation t and Bonferroni p (k = 5):

| Finalist | Test t | Confirmation t | Confirmation p | Combined p |
|---|---|---|---|---|
| Strongest close→open | 0.78 | 3.86 | <0.001 | 0.001 |
| Green close→10:00 | 1.53 | 2.95 | 0.008 | 0.002 |
| Strongest close→10:00 | 1.26 | 2.83 | 0.012 | 0.006 |
| Strongest 15:30→open | 0.73 | 2.92 | 0.009 | 0.011 |
| Last-hour dip close→open | 0.69 | 1.31 | 0.48 | 0.35 |

The first four are the same effect: strength into the close continues overnight in up-trend markets.

## C. Inverse 3x ETFs overnight in down-trends (SQQQ, SPXS, SOXS)

- Trend-down nights: +7.9 bps net, t 0.36. Drawdown −43% of the slot book. Not a finalist.
- On up-trend nights they lose (t −2.9), as expected.
- 12 trials.

## Diagnosis

The edge is overnight, positive-trend, momentum-into-the-close. It is a known pattern in the literature: overnight
returns concentrate in the strongest stocks.
[inferred: consistent with published overnight-momentum studies, not verified here]

It is about half market beta. Its alpha is confirmed only on the 2016–2020 data, and it weakened in 2024–26.

## Recommended next test

A Swing-tier design review of this rule, not a Day-tier change:
1. Size so the worst night is at most about 3% of equity. At the measured −6% of book on the worst night, that means
   at most about 0.5x equity overnight notional for this rule.
2. Run it on the post-holdout sessions with `daily.py` (strategy file to be added) for a forward check.
3. Add a beta-hedged variant (short SPY at the close, cover at the open) and confirm its alpha separately.

Owner approval and the full gate are required before anything ships.

## Independent re-derivation (Claude, 2026-10-29, own code, raw Alpaca SIP daily bars, adjustment=all)
Proxy rule: SPY close > its 200-day average; strength = close-to-close return of day d (the tested rule uses prior
close → 15:25); top quintile of 30 large caps (my list: JPM BAC XOM CVX JNJ PFE UNH KO WMT HD DIS INTC CSCO ORCL QCOM
ADBE CRM TXN PEP COST MCD NKE V MA LLY MRK ABT TMO HON CAT); buy close, sell next open; 2016-01-04..2020-10-23.
- Strong quintile: 882 nights, +10.06 bps gross, +5.06 net of 5 bps, t 4.74.
- All 30 names: +6.66 bps gross.
- The overnight drift on trend-up days reproduces; the strong-name premium is smaller under this proxy (+3.4 bps vs
  +5-8 in the harness). Script: scratchpad `verify/ovn.py`.
