# Track B — 6-year confirmation + exit study (2026-10-28, Claude)

Read-only research, owner request 2026-10-27 ("focus on the past 6 years"; size up only on airtight confirmation).

**Method.** Frozen live Track B rules (`screen_mover` + `compute_momentum_trigger`, stop at the structural level ±0.1%,
5 bps cost, EOD exit) replayed through `research/day_tier_trackb_watchday_replay` helpers on the 18 `DAY_TIER_UNIVERSE`
names, 2020-10-26..2026-10-26, all day to ~15:35 ET, re-entry after the prior trade's exit. 7,624 signals, 1,431 sessions.
Confirmations picked on 2020-10-26..2024-10-25 (discovery), checked on 2024-10-28..2026-10-26 (test).
Scripts: `conf6y.py`, `conf6y_an.py`, `exit6y.py`, `exit6y_an.py` (session scratchpad; raw rows on OCI `/tmp/conf6y_rows.json`,
`/tmp/exit6y_rows.json`).

**Baseline (2R target).** Discovery n=4,891, −0.061R, t −3.48. Test n=2,733, −0.022R, t −0.96.

**Confirmations (test years, with / without).** daily trend agrees −0.052 / +0.005; SPY agrees −0.032 / +0.006;
room ≥1.5 ATR −0.011 / −0.050 (discovery +0.003 / −0.212); RVOL ≥2 +0.002 / −0.049; first shot −0.038 / +0.027;
before 11 ET −0.035 / −0.001; long −0.040 / short −0.004. VWAP agreement untestable (99%+ of signals agree).
Gap size (fraction): <2% −0.089 / −0.058; 2–3% −0.038 / −0.040; 3–5% −0.084 / −0.028; ≥5% +0.002 (n 788) / +0.041 (n 668, t 0.85).
120 two/three-feature combos (discovery n≥150): best discovery t = 0.81 (+0.030R); same combo test −0.039R.
Per-symbol results flip sign between halves (e.g. SMCI −0.155 → +0.210, GOOGL −0.025 → −0.334).

**Exits (same signals; disc / test mean R, t).** 1R −0.067 / −0.051 (t −2.85); 1.5R −0.066 / −0.035; 2R −0.061 / −0.022;
3R −0.057 / −0.021; hold to close −0.053 / −0.037; 2R with stop to entry at +1R −0.065 / −0.027. Check: 2R column equals
the original R on all 7,624 rows.

**Finding.** No tested confirmation or exit gives Track B an edge that holds out of sample. Not a basis for larger
size (stock or 2x ETF). Track B keeps its current size (never-skip rule). Next: search new entry signals with the
same discovery/test split.

## Part 2 — math check, why it lost, and the version that wins (2026-10-28, Claude)

**Rule applied (Rafael, 2026-10-28):** a losing test is never the deliverable. Verify the math, find why it lost, and keep
testing until a version wins on the untouched 2024–26 years.

**Math check.** All 7,624 signals regenerated from the bot's own `screen_mover` + `compute_momentum_trigger`: 7,624 match the
study, 0 R differences. Every trade re-priced by an independent simulator (numpy, written from scratch): max |R difference|
0.0, 0 exit-type differences. No look-ahead: the prior close is strictly before the session; the price path starts at the bar
after the signal bar. Entry is the signal bar's IEX close; vs the SIP close it differs by a median 1.06 bps (mean −0.05 bps).
6 of 1,409 checked trades had a bar touching both stop and target (counted as the stop, the worse case).
Scope: the study replays raw signals; live also applies the trend-direction gate, the watch-day rule and the min-stop skip.

**Why it lost.** Before costs the signal is slightly positive: discovery +0.021R (t 1.2), test +0.057R (t 2.44). The 5 bps
round-trip cost averages 0.08R because the stop sits 0.1% beyond the opening-range level (median risk 81–86 bps; trades
with under 25 bps of risk pay over 0.2R each in cost). Holding to the close with that stop: −0.053R / −0.038R.

**What was tried (614 rule sets, all picked on 2020–24 only).**
- 454 opening-range rules on the 18 names (first 5/15/30-min direction, follow or fade, ATR or range stop, volume filter,
  top-k) and 4 last-half-hour momentum rules: none above t 2 in both halves; the last-half-hour rules lose 3–6 bps a trade.
- 144 Track B re-pricings (structural / 0.1–1.0× daily-ATR / minimum-width stops; same day, 1, 2, 5-session holds;
  long/short). Long-only multi-day holds win but follow the market: 5-session longs lose −0.36R in 2022 (SPY −20%);
  shorts win only in 2022.
- 16 market-trend versions (SPY above/below its 50- or 200-day average decides long-only or short-only).

**The winning version (discovery pick = highest discovery t).** Same Track B entry signal, three changes:
1. take it only in the direction of SPY's 200-day trend (SPY's prior close above its 200-day average → longs only;
   below → shorts only);
2. stop = 1.0 × the stock's 14-day daily ATR from entry (not 0.1% beyond the opening-range level);
3. hold up to 5 sessions; out at the 5th session's close if the stop is not hit.

| | Trades | Mean R | Net bps/trade | Win % | Sharpe | Month-block 90% CI (R) |
|---|---|---|---|---|---|---|
| 2020–24 (discovery) | 2,541 | +0.183 | +68 | 41.5% | 1.85 | +0.019 … +0.346 |
| 2024–26 (test) | 1,422 | +0.217 | +98 | 41.6% | 2.14 | +0.039 … +0.402 |

- Beats the same rule entered on every session of the same names in the same market trend (no signal): +0.102R (p 0.035)
  in discovery, +0.151R (p 0.016) in test — the signal adds value beyond market drift.
- By year: 2020 +0.565, 2021 +0.374, 2022 −0.039 (long-only was −0.360), 2023 +0.185, 2024 +0.225, 2025 +0.127, 2026 +0.243.
- At 10 bps cost: +0.168R / +0.203R. First signal of each day only: +0.224R / +0.122R (test t 1.41).
- The same rule exited at the same day's close is about breakeven (+0.009 to +0.017R): the edge needs the multi-day hold.

**Limits.** A multi-day hold belongs with the Swing tier's overnight rules (K cap, Swing sizing), not the intraday Day tier.
Taking every signal means a median 15 open positions (90th percentile 27), more than a $2.5K account holds, so a live
version needs a ranking rule (first signal of the day was tested; weaker in test). After the entry day the stop is checked
on daily bars; whether Alpaca daily highs/lows include extended hours is unverified (if they do, stops trigger more often —
the conservative direction). 614 rule sets were tried; the 2024–26 result is for the single discovery pick. Short borrow
fees are not modelled.
Test-year exposure: the 2024–26 results of the earlier batches (454 + 144 rule sets) were printed before the 16 market-trend
versions were designed, so the test years are not fully untouched for this pick. The trend idea came from the 2022 loss
(a discovery year). Independent confirmation is still owed: data never consulted (sessions after 2026-10-26) or its first
live sessions.

**Next:** design record, then Board + Codex + GAI review of this rule as a Swing-tier entry source.
Scripts (session scratchpad): `verify.py`, `gen.py`, `lab.py`, `tbvar.py`, `control.py`, `regime.py`, `regime_ctrl.py`,
`conc.py`, `peryear.py`.
