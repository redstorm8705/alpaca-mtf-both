# Day tier — momentum replay + pre-registered pattern sweep (2026-10-04, Claude-signed) — RESEARCH, nothing shipped

**CORRECTION (same day, board seat López de Prado/Harris/Taleb-Thorp, verified by re-execution):** the k=0 stop in
patterns.py used bars 1-3 (after the bar-1 entry) for the pivot -> look-ahead that engineered away early stop-outs for
P5/P11 (and random-null entries at slots 1-2). Fixed: structural_D(…, max(k, 1), …). CAUSAL results for P11 (hold to
close): Y1 +0.06R (p 0.23), Y2 +0.02R (p 0.40) at 0.1 slippage; -0.03 / -0.08 at 0.25; best subset |gap| z>=2 vs own
60-day: +0.17/+0.09 (0.1) and +0.12/+0.02 (0.25), p 0.15-0.47. **Conclusion: no intraday entry pattern on these 15
names passes after costs in either year.** Item 5 below is SUPERSEDED. Live entry slippage measured on the 19 live
day-tier fills: median 0.04 x 5m bar range (~0.7 bp) -> the 0.1 assumption is conservative.


Scripts (research only, run on OCI): /tmp/momo_replay.py, /tmp/early_trend.py, /tmp/patterns.py, /tmp/overnight.py
(copies in the session scratchpad). Data: IEX 5m RTH bars, 15 Track-B names + SPY/QQQ + SMH XLK XLY XLC XLI XLF;
Year 2 = 2025-09-18..2026-10-02 (data/cache/lab/bars/5Min_iex), Year 1 = 2024-07-05..2025-09-17 (…/5Min_iex_y1).
Method: walk-forward (thresholds from the symbol's own prior 20 sessions only), last 60 sessions held out, random-slot
null on the same symbol-day/direction, common exit (structural stop, ratchet trail at entry risk, flat at close),
fills = next bar open + 0.1 (or 0.25) x bar range adverse; 15 patterns -> pass needs held-out p < 0.0033 and beat null.
Stated approximations: daily side = prior-session 20/50-SMA stack (proxy for Layer A); no 2m bars.

## Results
1. Momentum continuation (design record day_tier_momentum_continuation_2026-10-04.md): FAILS. Held-out vs random null,
   q=0.7: -0.13R (zero slippage) / -0.22R (0.25) vs random +0.25 / +0.12. Does not ship.
2. Unconditional early trend entry (09:45/10:00, trend side): ~0 to -0.14R held-out; the 5m EMA+VWAP gate moves it to
   ~0 (the shipped alignment gate filters losers; it does not create edge by itself).
3. 15-pattern sweep, Year 2, 5 exits (trail, TP1R, TP2R, hold-to-close, 2x stop): NO pattern passes. Everything sits
   near 0R (win ~35-45%). Held-out window (Jul-Oct 2026) worse for all, incl. random.
4. Overnight vs intraday (Year 2, 15 names): sum of log returns overnight +222% vs intraday +10%; "short-side" days
   (SMA proxy) still rose intraday (+6bp) and overnight (+16bp). Implication: flat-by-close forfeits most of the drift;
   intraday shorts of these names fought the drift. (Verify with the real Layer A before acting.)
5. CANDIDATE — failed-gap fade (P11): gap >= own p80 of prior-60-day |gap|, first 5m bar closes AGAINST the gap ->
   fade at the next bar open, ignores daily side. Selected from Year 2 (+0.03..+0.19R held-out across exits), then
   validated on INDEPENDENT Year 1: n=357 (~1.4/day), meanR +0.11..+0.30 across all 5 exits, bootstrap p 0.003-0.014,
   both directions positive (fade gap-down long +0.12..+0.30, fade gap-up short +0.09..+0.37), 12/15 tickers and
   8/12 months positive. Gap-continuation (P5) failed Year-1 validation -> dropped.
## Open (before any build)
- With-trend vs against-trend split of P11 (running); exit selection; conflict with the live direction rules (P11 fires
  ~09:35 and ignores the daily side; current rules allow counter-trend fades only after 11:00 with the 15m/30m test) ->
  board + Gro + GAI, then a Rafael approval package. Earnings-day drift not yet tested (needs FMP history).
