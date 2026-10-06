# Day tier not trading + secondary price confirmation — BGG alignment (2026-10-06, Claude)

## Findings (verified: OCI logs, Alpaca orders API)
- Day-tier filled entries: 9/21 1, 9/23 6, 9/24 1, 9/25 3, 9/28 3, 9/29 2; then 9/30-10/2 none, 10/5 one unfilled (stale-bar
  pricing, fixed 10/5), 10/6 none (to 12:16 ET).
- 9/29-10/1: 14 entries skipped "allocator: broker snapshot unreadable: ValueError" (none after 10/1).
- 10/5 and 10/6: Track A fired 69 and 20 ENTER triggers; the 2026-10-04 alignment / counter-trend gate blocked all of 10/6's.
  10/6 was an up day and Track A produced only SHORT triggers.
- Counterfactual, 10/6's 8 distinct blocked setups (IEX 1m, signal -> 12:20 ET, bps): MSFT short FADE MAE -116 / MFE +2;
  NVDA FADE -74/+4, -48/+30, -54/+24; TSLA FADE -36/+42; EWY FADE -24/+61, -43/+42; SNDK RIDE short +21/+154 (blocked as
  "short against the LONG trend side"). n=8, one day, stops not modeled — neither supports nor refutes the gate.
- DEFECT: strategy/day_tier_entry_trigger.py FADE branches set target = GEX centroid with no profit-side check. Live 10/6:
  NVDA short entry 241.25 target 241.74; EWY short entry 189.56 target 192.39.
- Track B (movers) is live but sized to zero: 10/2 "track B: budget $130.31 x conviction 0.60 = $78.19 < 1 share @ $371.72".

## Alignment (board Harris + Thorp seats, Gro, GAI)
Q1 day tier:
1. Fix the FADE target side: target must be on the profit side, reward:risk >= 1.0 vs the planned stop. Harris, Thorp, Gro
   yes; GAI called it low priority. Rule E: size 0, frequency down, concurrency 0 (selectivity).
2. Keep the alignment gate until its own pre-registered test (fewer than ~1 trade per 5 sessions -> loosen the 5m check
   first, via the board). 4/4.
3. GEX families (Track A): MINIMUM-SIZE LIVE (1 share) for measurement. 4/4 after counter-prompts (Gro and GAI first said
   stand down; round 1 Gro premise inverted; round 2 my false claim that Track A is the only non-Monday source was corrected
   — Track B exists but sizes to 0; round 3 scope + paper-frame justification -> both MINIMUM-SIZE LIVE). Rule E: size down.
4. Open: SNDK RIDE short labelled counter-trend (Thorp: possible logic mismatch — RIDE is with-break); Track B budget below
   one share of its universe (a size change -> risk-path board item); budget re-route to Track M / swing (risk-path board item);
   wire the evidence registry into the runner.
Q2 secondary price confirmation (exit side only; Rule E all zero):
- Hard stops: unchanged (3-scan confirm + broker stop). Harris, Thorp, GAI; Gro wanted a quote check with fail-open (minority).
- Trail and breakeven (protective): act when the print AND the IEX bid (long) / ask (short) both cross; if only the print
  crosses, re-check once (at most one scan); quote unreadable/stale/crossed -> act on the print as today. Never delays a
  protective exit by more than one scan; broker stop stays in place.
- Profit-tranche targets: print + bid/ask + a volatility band (k x recent 1m realized vol, floor = the symbol's p99 IEX/SIP
  gap); unconfirmed -> wait for the next scan.
- Caveat: Alpaca's IEX quote is IEX's own BBO, not the NBBO; wide on thin names (hence the stale/crossed fallbacks).
