# QHM earnings-stop replay (Rule C front-loaded simulation): 2026-10-29 (signed: Claude)

Research only. No bot code was changed and no orders were placed. Owner direction 2026-10-29: "I think stops should be
updated around earnings but not removed." This replay tests 16 ways of updating the QHM stop around a report, all of
which keep a stop on at every moment.

Labels: **[measured]** = computed in this replay from Alpaca SIP daily bars and SEC EDGAR filings. **[inferred]** = my
reasoning, not measured.

## Plain English

- **No stop update beat today's rule.** The pre-stated pick was the variant with the best average return per hold on
  2016-2021. That pick was "C_half+D": lock half the gain before the report, then re-anchor the stop after it. It lost
  to today's rule on 2022-2026 by 0.29 points per hold (95% CI −0.57 to −0.03) [measured].
- **Raising the stop just before the report (B) hurt clearly.** At k = 1.0 the stop sits about one "normal earnings
  move" below the close. Report-day exits rose from 22 to 46 on 2022-2026, and the result fell 0.80 points per hold
  (CI −1.33 to −0.33) [measured]. Many of the extra exits came back within 20 sessions: 11 of 30 gap exits recovered
  to the pre-report close.
- **Why a stop cannot fix a gap.** When the stock opens below the stop, the stop fills at the open, however high or
  low the stop was set. GOOGL is the example. Today's stop was 288.97. A k = 1.0 stop would have been 316.23. Both
  fill at the 286.88 open [measured]. A tighter stop changes results only on smaller moves. In large caps those moves
  did not keep going on average: all report-day gap exits under B1.0 averaged +0.4% over the next 20 sessions (vs −4.1% for the ones A takes)
  [measured].
- **The post-report re-anchor (D) is about neutral.** It changed 3 of 263 holds on 2016-2021 (+0.03 points per hold)
  and 4 of 337 on 2022-2026 (−0.02 points, CI −0.16 to +0.12) [measured]. It seldom fires because the trailing lock
  is usually already above it. It is the one variant that "updates the stop around earnings" without a measurable
  cost. That is not evidence of a gain.
- **Selling before the report** was run as a reference only, since it is not a stop update. It cut the worst hold from
  −32%/−34% to −17%/−19%, but cost 1.1 and 1.4 points per hold in the two periods. Holding through the print earned more
  [measured].
- **GOOGL 2026-10-28 under every variant: −15.6%, exit 10-29 at the 286.88 open.** The only variant that avoids it is
  the sell-before-report reference (−4.4%) [measured; the live fill was 285.31, 0.5% below the replay's open fill].

## Method

- **Universe (27 → 26 names).** AAPL MSFT GOOGL AMZN META NVDA AVGO LLY JPM GE XOM V MA COST NFLX AMD ORCL CRM NOW ADBE,
  plus QHM picks/research names GS GEV ANET CEG FCX MU. TSM was dropped because it files 6-K, not 8-K, so EDGAR gives
  no report dates.
- **Bars.** Alpaca SIP daily bars, split-adjusted, 2016-01-04 → 2026-10-29. The 10-29 bar is intraday-partial.
  - GE: bars before the GEHC (2023-01-04) and GEV (2024-04-02) spin-offs were scaled by ex-day open ÷ prior close.
    Without this, the spin-offs show up as fake −20% gaps.
  - Returns exclude dividends. That applies equally to every variant.
- **Report dates.** SEC EDGAR 8-K and 8-K/A Item 2.02 filings, using the acceptance time converted to ET.
  - A filing accepted before 16:00 ET reacts the same session. One accepted at or after 16:00 reacts the next session.
  - Filings within 3 sessions of each other count as one event.
  - Check: the median absolute close-to-close move is 3.59% on reaction sessions, 1.18% the session before, and 1.04%
    on other sessions [measured].
  - Limit: Item 2.02 also includes non-earnings items, such as XOM "earnings considerations" and FCX updates.
- **Entry (identical across all variants).** Buy at the close on the first session of each calendar quarter in which
  the close is ≥ 15% below its 252-session high close. One hold per name per quarter, single tranche. No dip-adds and
  no pre-earnings profit-trim; both are left out of every variant.
  - Result: 600 holds, 263 in discovery (entered before 2022-01-01) and 337 in test. 237 holds contain at least one
    report.
- **Stop rules, as live.**
  - Initial stop: max(entry − 2.5 × 14-week ATR, entry × 0.85). The weekly bars are built from daily bars and include
    the current partial week.
  - Trailing lock: at the close, if the close is ≥ +5% over entry, stop = entry + 0.5 × gain, raise-only, kept ≥ 2%
    below the close. It takes effect the next session.
  - Each session: if the open is ≤ stop, exit at the open (gap fill). Else if the low is ≤ stop, exit at the stop. Else
    hold up to 63 sessions and exit at the close.
- **Variants (16 tried; none ever removes a stop).**
  - A: today (stop unchanged through the report).
  - B k ∈ {1.0, 1.5, 2.0}: at the close of the pre-report session, stop = max(stop, close × (1 − k × proxy)). The proxy
    is the median absolute reaction-session move over the prior 8 reports, point-in-time. It needs ≥ 4 prior reports;
    with fewer, B does nothing.
  - C_be / C_half: at the pre-report close, if in profit, raise the stop to entry, or to entry + 0.5 × gain.
  - D: at the reaction-session close, stop = max(stop, close − 2.5 × weekly ATR, close × 0.85).
  - Combinations: B+D (×3), C_be+D, C_half+D.
  - Added after the first look, still chosen on discovery only:
    - E_low: raise to the report-day low.
    - E_gapup: raise to the pre-report close after an up reaction.
    - E_neg_low: raise to the report-day low after a down reaction.
  - REF_exit_pre: sell at the pre-report close. This is a reference, not a candidate.
- **Selection.** The criterion was fixed before running: the best mean return per hold on discovery. The test period
  was printed twice: once with the first 13 variants, and once more when the 3 E variants were added. Paired bootstrap
  (5,000 resamples) of the per-hold difference vs A.
- **Metrics.**
  - Book: an equal-weight daily book of the open holds.
  - "Recovered": some close within the 20 sessions after a gap exit is ≥ the close before the gap.

## Results [measured]

**Discovery** (entries 2016-01..2021-12, n=263 holds)

| Variant | Book total | Mean/hold | Hit rate | Worst hold | Book max DD | Report-day exits (gap) | Avg ret of report-day exits | Gap exits recovered in 20 sessions | Avg 20-session move after report-day gap exit |
|---|---|---|---|---|---|---|---|---|---|
| A | +298% | +5.55% | 84.8% | -31.7% | -29.8% | 16 (9) | +2.3% | 3 | -4.2% |
| B1.0 | +437% | +5.26% | 81.0% | -31.7% | -29.8% | 36 (22) | +2.5% | 13 | +0.1% |
| B1.5 | +394% | +5.23% | 81.7% | -31.7% | -29.8% | 30 (17) | +2.1% | 9 | -1.0% |
| B2.0 | +335% | +5.25% | 82.5% | -31.7% | -29.8% | 27 (15) | +1.5% | 7 | -2.7% |
| C_be | +323% | +5.56% | 81.7% | -31.7% | -29.8% | 24 (13) | +1.2% | 7 | -2.0% |
| C_half | +329% | +5.58% | 84.8% | -31.7% | -29.8% | 28 (14) | +1.5% | 7 | -3.3% |
| D | +303% | +5.59% | 84.8% | -31.7% | -29.8% | 16 (9) | +2.3% | 3 | -4.2% |
| B1.0+D | +437% | +5.26% | 81.0% | -31.7% | -29.8% | 36 (22) | +2.5% | 13 | +0.1% |
| B1.5+D | +394% | +5.23% | 81.7% | -31.7% | -29.8% | 30 (17) | +2.1% | 9 | -1.0% |
| B2.0+D | +338% | +5.26% | 82.5% | -31.7% | -29.8% | 27 (15) | +1.5% | 7 | -2.7% |
| C_be+D | +325% | +5.58% | 81.7% | -31.7% | -29.8% | 24 (13) | +1.2% | 7 | -2.0% |
| C_half+D | +332% | +5.59% | 84.8% | -31.7% | -29.8% | 28 (14) | +1.5% | 7 | -3.3% |
| REF_exit_pre | +406% | +4.47% | 82.9% | -17.2% | -26.8% | 0 (0) | n/a | 0 | n/a |
| E_low | +476% | +5.31% | 82.9% | -31.7% | -28.2% | 15 (8) | +2.0% | 3 | -1.2% |
| E_gapup | +388% | +5.55% | 84.0% | -31.7% | -29.8% | 16 (9) | +2.3% | 3 | -4.2% |
| E_neg_low | +313% | +5.25% | 82.9% | -31.7% | -28.2% | 15 (8) | +2.0% | 3 | -1.2% |

**Test** (entries 2022-01..2026-10, n=337 holds)

| Variant | Book total | Mean/hold | Hit rate | Worst hold | Book max DD | Report-day exits (gap) | Avg ret of report-day exits | Gap exits recovered in 20 sessions | Avg 20-session move after report-day gap exit |
|---|---|---|---|---|---|---|---|---|---|
| A | +422% | +3.09% | 72.7% | -34.3% | -34.2% | 22 (13) | -5.3% | 1 | -4.1% |
| B1.0 | +272% | +2.29% | 67.1% | -34.3% | -36.5% | 46 (30) | -2.4% | 11 | +0.4% |
| B1.5 | +280% | +2.57% | 68.5% | -34.3% | -36.2% | 38 (22) | -3.3% | 6 | -1.1% |
| B2.0 | +341% | +2.87% | 70.6% | -34.3% | -36.5% | 31 (17) | -4.0% | 2 | -3.9% |
| C_be | +392% | +2.81% | 68.2% | -34.3% | -34.2% | 34 (20) | -4.3% | 3 | -2.1% |
| C_half | +384% | +2.81% | 71.5% | -34.3% | -34.2% | 40 (23) | -3.4% | 6 | -1.2% |
| D | +433% | +3.07% | 72.4% | -34.3% | -35.1% | 22 (13) | -5.3% | 1 | -4.1% |
| B1.0+D | +270% | +2.29% | 66.8% | -34.3% | -37.4% | 46 (30) | -2.4% | 11 | +0.4% |
| B1.5+D | +277% | +2.57% | 68.2% | -34.3% | -37.1% | 38 (22) | -3.3% | 6 | -1.1% |
| B2.0+D | +339% | +2.86% | 70.3% | -34.3% | -37.4% | 31 (17) | -4.0% | 2 | -3.9% |
| C_be+D | +403% | +2.79% | 68.0% | -34.3% | -35.1% | 34 (20) | -4.3% | 3 | -2.1% |
| C_half+D | +394% | +2.80% | 71.2% | -34.3% | -35.1% | 40 (23) | -3.4% | 6 | -1.2% |
| REF_exit_pre | +164% | +1.67% | 65.6% | -19.4% | -30.0% | 0 (0) | n/a | 0 | n/a |
| E_low | +300% | +2.47% | 70.3% | -34.3% | -34.6% | 22 (13) | -5.3% | 1 | -4.1% |
| E_gapup | +402% | +2.60% | 71.5% | -34.3% | -34.9% | 22 (13) | -5.3% | 1 | -4.1% |
| E_neg_low | +346% | +2.82% | 70.0% | -34.3% | -35.7% | 22 (13) | -5.3% | 1 | -4.1% |

**Paired difference vs A (mean return per hold, percentage points, bootstrap 95% CI)**

| Variant | Discovery | Test | Holds changed (disc/test) |
|---|---|---|---|
| C_half+D (discovery pick) | +0.037 [−0.14, +0.26] | **−0.294 [−0.57, −0.03]** | 15 / 24 |
| D | +0.031 [0.00, +0.07] | −0.018 [−0.16, +0.12] | 3 / 4 |
| C_half | +0.025 [−0.15, +0.24] | −0.276 [−0.52, −0.05] | 14 / 20 |
| B1.0 | −0.298 [−0.86, +0.18] | −0.797 [−1.33, −0.33] | 42 / 56 |
| B2.0 | −0.307 [−0.82, +0.08] | −0.221 [−0.55, +0.07] | 25 / 22 |
| REF_exit_pre | −1.086 [−2.16, −0.12] | −1.423 [−2.51, −0.40] | 104 / 127 |

The discovery means are within 0.04 points of each other (C_half+D 5.592%, D 5.586%, C_half 5.579%, A 5.555%). The
"winner" is noise, and it reversed in test. The book-total column ranks the variants differently from mean per hold
(B1.0 best in discovery, worst in test). With few holds open at a time, the book compounds a handful of positions, so
that column is not the selection metric.

## Named cases [measured]

**GOOGL actual QHM hold.** Entry 339.97 on 2026-09-29, stop 288.97. Report accepted 2026-10-28 16:01 ET. Pre-report
close 324.89; 10-29 open 286.875, low 280.36.
- A, C_be, C_half, D, E*: no change to the stop (not in profit, so C and the lock do not apply). Exit at the 286.88
  open, −15.6%.
- B1.0 / 1.5 / 2.0: stop raised to 316.23 / 311.89 / 307.56 (proxy 2.67%). Still exit at the 286.88 open, −15.6%.
- REF_exit_pre: 324.89, −4.4%. The replay's own GOOGL dip entry (2026-10-01 at 338.24, stop 287.50) also gapped out
  at 286.88.

**NOW**: a hypothetical QHM-style hold at the Swing tier's 2026-10-26 entry of 157.59.
- Stop 133.95 (the 15% floor). Pre-report close 150.13; 10-29 open 120.905.
- Every variant exits at the 120.91 open, −23.3%. B1.0 raised the stop to 138.62 (proxy 7.67%) with no effect;
  B1.5 and B2.0 sat below the existing stop.
- REF_exit_pre: −4.7%.
- In the replay's own dip entries, NOW's 2025-2026 holds lost through stops and gaps that were not on report days
  (for example a −15.2% gap on 2025-04-07, not a report day).

## Hand check of 5 trades from raw bars [measured]

The initial stops were recomputed with an independent ISO-week ATR. All matched the replay.
1. **GOOGL A, entry 2026-10-01 at 338.24.** ATR 23.674 → stop 287.50. 10-29 open 286.875 ≤ 287.50, so exit at the
   open: −15.19%. ✓
2. **MSFT A, entry 2020-04-01 at 152.11** (0.806 of the 52-week high). Stop 129.29, trailing-locked up to 177.80. Lows
   stayed ≥ 193.55, so a time exit on 2020-07-01 at 204.70: +34.57%. ✓
3. **JPM A, entry 2018-12-11 at 100.37.** Stop 85.86 → locked to 103.24. On 2019-03-07 the open was 103.25 (above the
   stop) and the low 102.16, so exit at 103.24: +2.86%. ✓
4. **AAPL B1.0, entry 2025-07-01 at 207.82.** Pre-report close 207.57, proxy 1.01% → stop 205.48. 8-01 open 210.865,
   low 201.50, so exit at 205.48: −1.13%. Under A the same hold ran to +22.52% at 63 sessions. This is B's typical
   cost. ✓
5. **AMZN D, entry 2022-01-19 at 156.30.** Report-day close 2022-02-04 157.64 → D stop 133.99 (0.85 × close). On
   3-08 the low was 133.57, so exit at 133.99: −14.27%. Under A (stop 132.86) the hold survived and later
   trail-locked out at +2.75%. This is D's rare cost. ✓

## Why it lost, and what would have been profitable

- **[measured]** Holding through the print with the existing wide stop was the best of everything tested in both
  periods. Report-day gap losses under A cost about 22 × −5.25% ÷ 337 ≈ −0.34 points per hold in test. Avoiding them
  by selling first gave up more upside (−1.4 points per hold).
- **[inferred]** A stop is the wrong tool for gap risk. Its only lever is exits on moves that are NOT gaps through it,
  and on large caps those exits sell noise.
- **[inferred]** If the owner wants protection against a GOOGL-type print, the lever is exposure before the report
  (size), not the stop level. That is a risk-path/sizing question.
  - The Swing-tier version of it was already replayed and withdrawn on 10-29: holding at full size won
    (`logs/design_records/earnings_risk_1029.md`).
- **[inferred]** If the owner still wants stops "updated around earnings", D (a raise-only re-anchor after the report)
  is the only variant that does so without a measurable cost. Its effect is about zero, so it should be described as
  hygiene, not edge.

## Limits

- Daily bars only. Within a day, the order of the open, low and stop is approximated. Gap fills are at the SIP open;
  the live GOOGL fill was 0.5% worse.
- The trailing lock is evaluated at the close; live runs intraday, at most once a day.
- No tranches, dip-adds or earnings trim. Dividends are excluded. GE spin-off adjustment is approximate.
- The proxy is the stock's own past reaction moves, not options-implied, and needs ≥ 4 prior reports. The B result
  could differ with a true implied move [inferred].
- Live use of B or C needs the report date in advance. EDGAR gives it only after the fact. NOW is absent from FMP
  (see the trade audit).
- Item 2.02 includes some non-earnings filings.
- 16 variants tried, and the test period was looked at twice. No variant is claimed as a winner, so no
  multiple-testing adjustment is needed for a claim. Any later pick from this table would need a fresh holdout.
- Scripts and data (scratchpad, not committed): `qhm_earn/fetch_bars.py`, `fetch_edgar.py`, `sim.py`, `analyze.py`,
  `cases.py`, `verify.py`.
