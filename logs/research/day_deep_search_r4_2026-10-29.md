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

## Live-sizing replay (follow-up, 2026-10-29)

Same rule, `strong_q|up close->open`. No new search, and no new test-year reads beyond these replays of the already
revealed finalist. Script: `day_search/live_replay_r4.py`, output `out/live_replay_r4.txt`. All numbers are measured.

**Setup.**
- Each period starts at **$2,555** and compounds nightly.
- Total overnight notional is L × equity, split equally across the night's names.
- Whole shares only, 1-share floor. A name whose single share costs more than its slot is skipped.
- 5 bps round trip.
- Confirmation (2016–20) uses 30 non-Day large caps, as before.

**Trend-up nights per year (the regime):**

| Period | Nights per year |
|---|---|
| Discovery | 2020 (from Oct 26): 45; 2021: 252; 2022: 39; 2023: 221; 2024: 206 |
| Test | 2024 (from Oct 26): 43; 2025: 208; 2026: 192 |
| Confirmation | 2016: 51; 2017: 249; 2018: 207; 2019: 220; 2020 (to Oct 23): 147 |

| Variant | Period | Names skipped | Return / night (bps of equity) | Sharpe | t | Max drawdown | Worst night (% equity) | End equity |
|---|---|---|---|---|---|---|---|---|
| 0.5x | discovery | 37% | +1.62 | 1.09 | 2.17 | −9.0% | −1.87% | $2,996 |
| 0.5x | test | 57% | +0.51 | 0.37 | 0.52 | −3.4% | −1.21% | $2,618 |
| 0.5x | confirmation | 8% | +2.98 | 1.81 | 3.95 | −5.6% | −1.54% | $3,636 |
| 1.0x | discovery | 9% | +5.00 | 1.26 | 2.51 | −21.3% | −4.02% | $4,129 |
| 1.0x | test | 26% | +1.32 | 0.31 | 0.44 | −10.2% | −3.28% | $2,699 |
| 1.0x | confirmation | 0% | +7.10 | 1.84 | 4.00 | −10.4% | −3.33% | $5,843 |
| 0.5x, no earnings nights | discovery / test / confirmation | — | +1.54 / +0.35 / +2.69 | 1.11 / 0.25 / 1.80 | 2.21 / 0.36 / 3.92 | −9.2 / −4.5 / −4.3% | −2.04 / −1.71 / −1.55% | |
| 0.5x, SPY-hedged (β 1.15 from discovery) | discovery / test / confirmation | — | +0.04 / −0.87 / +0.59 | 0.04 / −0.70 / 0.55 | 0.07 / −0.98 / 1.20 | −7.5 / −5.1 / −9.3% | −1.78 / −1.18 / −1.25% | |
| 0.5x, no earnings + hedged | discovery / test / confirmation | — | −0.18 / −1.09 / +0.36 | −0.19 / −0.92 / 0.40 | −0.38 / −1.29 / 0.86 | −7.9 / −7.8 / −6.2% | −1.12 / −1.70 / −0.77% | |
| 1.0x, no earnings nights | discovery / test / confirmation | — | +4.41 / +0.82 / +5.87 | 1.19 / 0.19 / 1.70 | 2.38 / 0.27 / 3.71 | −22.7 / −15.1 / −10.3% | −4.39 / −3.80 / −3.36% | |

**Whole-share skips.** At 0.5x of ~$2,555, slots are about $128. 37% (discovery) and 57% (test) of the selected
names cost more than a slot and are skipped, so the live version holds the cheaper half of the picks. 1.0x halves the
skips and roughly triples the return per night. 2016–20 skips are low because those names were cheaper.

**Protective stop** at entry − k × the name's own point-in-time 99th-percentile overnight loss:
- It would have been gapped through 41 / 26 / 81 times at k = 1.0 and 15 / 18 / 29 times at k = 1.5
  (discovery / test / confirmation).
- **The P&L change is zero by construction.** Alpaca stops do not trigger outside regular hours, so the stop fills
  at the 09:30 open, which is the planned exit anyway. A stop cannot reduce this trade's gap risk; only size can.

**Earnings exclusion.**
- Dates: SEC EDGAR 8-K item 2.02 acceptance timestamps. A release between 15:25 ET and the next 09:30 ET skips the
  name for that night.
- XOM, BABA and SHOP have no usable 2.02 history (other items / 6-K / 40-F), so FMP report dates were used and both
  adjacent nights skipped.
- It removed 145 / 75 / 132 stock-nights. Return per night fell slightly in all three periods. The worst night did
  not improve: the worst nights are market-wide gaps, not earnings.

**Beta hedge.**
- Short SPY at the close for 1.15 × the long notional, cover at the open, 2 bps on the SPY leg.
- It removes essentially all of the return: +0.04 / −0.87 / +0.59 bps per night, t 0.07 / −0.98 / 1.20.
- At live sizing the edge is the market's overnight drift, levered by the selection's beta. The stock-selection part
  is not significant in any period once hedged.

**Reading.**
- At 0.5x the rule makes about +1.6 bps of equity per night in discovery, +0.5 in test and +3.0 in confirmation.
  That is roughly 4% / 1% / 7.5% a year.
- The worst night costs about 1.2–2.0% of equity.
- At 1.0x the return roughly triples, the worst night is about 3.3–4.4% of equity, and the drawdown is up to −23%.
- Because the hedged version is flat, this is a sized long-overnight-beta trade on trend-up days with a modest
  selection tilt, not a market-neutral alpha.

## Index overnight variant (pre-declared, 4 candidates; every period read once)

Script: `day_search/idx_overnight.py`, output `out/idx_overnight.csv`. All numbers are measured.

**The rule.**
- On nights when SPY's prior close is above its 200-day average, buy at the 16:00 close and sell at the next 09:30 open.
- Notional: 0.5x equity for SPY and QQQ; 0.5x/3 for SPXL and TQQQ, which gives the same underlying exposure.
- Start at $2,555, compound nightly, whole shares only.
- Costs: 2 bps round trip for SPY/QQQ, 4 bps for SPXL/TQQQ.
- Reference: the same trade every night, with no filter.

| Period | Vehicle | Bps of equity / night (filtered / every night) | t | Sharpe | Annual % | Max drawdown % | Worst night % equity | Trend nights per year |
|---|---|---|---|---|---|---|---|---|
| Discovery 2020-24 | SPY | 0.93 / 0.55 | 1.49 / 0.62 | 0.75 / 0.31 | 2.3 / 1.3 | −7.9 / −13.6 | −1.57 / −1.64 | 192 |
| | QQQ | 1.53 / 0.69 | 1.78 / 0.62 | 0.89 / 0.31 | 3.8 / 1.6 | −9.2 / −15.4 | −2.50 / −2.68 | 192 |
| | SPXL | 1.14 / 0.83 | 1.78 / 0.93 | 0.89 / 0.47 | 2.9 / 2.0 | −7.4 / −11.7 | −1.73 / −1.78 | 192 |
| | TQQQ | 1.51 / 0.94 | 1.59 / 0.74 | 0.80 / 0.37 | 3.8 / 2.2 | −10.4 / −16.7 | −2.59 / −2.58 | 192 |
| Test 2024-26 | SPY | 0.90 / 0.87 | 1.17 / 0.78 | 0.83 / 0.55 | 2.3 / 2.1 | −4.8 / −7.7 | −1.00 / −1.55 | 224 |
| | QQQ | 1.91 / 2.15 | 1.63 / 1.45 | 1.16 / 1.03 | 4.8 / 5.4 | −4.7 / −7.4 | −1.41 / −1.54 | 224 |
| | SPXL | 0.87 / 0.93 | 1.07 / 0.78 | 0.76 / 0.55 | 2.2 / 2.3 | −4.1 / −7.2 | −0.91 / −1.60 | 222 |
| | TQQQ | 1.67 / 1.72 | 1.14 / 0.92 | 0.81 / 0.65 | 4.2 / 4.2 | −5.9 / −9.8 | −1.70 / −2.04 | 224 |
| Confirmation 2016-20 | SPY | 0.93 / 0.50 | 1.64 / 0.51 | 0.75 / 0.23 | 2.3 / 1.1 | −4.7 / −16.4 | −1.48 / −4.69 | 184 |
| | QQQ | 1.44 / 1.33 | 1.95 / 1.21 | 0.90 / 0.56 | 3.6 / 3.2 | −6.3 / −15.4 | −1.84 / −4.69 | 184 |
| | SPXL | 1.26 / 0.95 | 2.07 / 0.90 | 0.95 / 0.41 | 3.2 / 2.3 | −4.6 / −17.6 | −1.49 / −5.02 | 183 |
| | TQQQ | 1.70 / 1.66 | 2.16 / 1.41 | 0.99 / 0.65 | 4.3 / 4.1 | −6.6 / −17.1 | −1.93 / −5.12 | 184 |

**Adjusted significance** (k = 4 candidates). The trend rule was chosen in 2020–24 studies, so only the test and
confirmation periods count; they are combined with a Stouffer z:

| Vehicle | Combined z | Adjusted p (both periods) | Adjusted p (confirmation alone) |
|---|---|---|---|
| SPY | 2.01 | 0.089 | 0.20 |
| QQQ | 2.52 | 0.023 | 0.10 |
| SPXL | 2.32 | 0.041 | 0.078 |
| TQQQ | 2.43 | 0.030 | 0.061 |

**Reading.**
- The plain index overnight hold on trend-up nights earns about 0.9–1.9 bps of equity per night in every period:
  roughly 2–5% a year at 0.5x underlying exposure.
- The worst night is about −0.9% to −2.6% of equity. Max drawdown is −4% to −10%.
- The trend filter mainly cuts drawdown and the worst nights; the every-night versions reach −4.7% to −5.1% in
  2016–20. It adds little return in 2024–26.
- QQQ/TQQQ do best. It is about as good as the stock-picking version once that is sized live.
- **Not significant on any single period; significant only when test and confirmation are pooled (p 0.02–0.04)**,
  except SPY.
