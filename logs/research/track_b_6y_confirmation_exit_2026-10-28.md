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
