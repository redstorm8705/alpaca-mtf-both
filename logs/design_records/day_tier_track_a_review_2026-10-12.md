# Day tier Track A — record review (2026-10-12, Claude; board seat Harris lens)

Data: day_tier_events.jsonl (OCI) joined to Alpaca fills by order id; working data scratchpad rows.json/fills.json.
- 26 Track A entries 9/15-10/09. Tier log -$55.86; the 24 fill-closed trades total -$58.26 on both sources to the cent;
  the $2.40 gap is the two 10/07 lots handed to the Swing tier (booked at a mark).
- Per-trade R: -8.84R total, -0.37R per trade. FADE 16 trades -$4.86 (-6.18R); RIDE 8 trades -$53.40 (-2.66R).
  Stop-outs 12: -$59.14; targets 3: +$10.39; EOD flats 8: -$9.51.
- Slippage is small: entries net $7.47 better than signal prices; stop exits $0.12 over 11 trades (plus META 9/23,
  the crossed-fill case fixed by 2b8e3e5 on 9/26).
- One trade dominates the dollars: SNXX 10/09 (RIDE, 89 sh at the 10/10 max-size path) -$30.26 = 54% of the loss.

## Findings
1. **FADE: stops inside the noise.** 6 of 9 FADE stop-outs had a stop narrower than one median 5-minute bar; 5 of the
   9 later reached their target the same day (direction right, stop wrong). Proxy measure (5m high-low), not the
   code's exact true-range median.
2. **RIDE: no follow-through.** 0 of 8 reached 2R; 6 of 8 never exceeded +0.4R.
3. **The 10/11 real-time room check** (stop >= 1.5x the real-time 5m range, else skip) would have skipped ~11 of 15
   FADE trades on the same proxy, including the two best (TSLA +$7.45, AAPL 9/25 +$3.10) — a data-collection cost.
   [proxy estimate; needs the exact gate replayed]

## Candidate changes (not yet through the board)
- **C1 (CEO decision, conflicts with the 10/10 order "size by stop distance, no caps when conviction is high"):**
  per-trade risk cap ~0.5% of start-of-day equity (replay: -$58.26 -> -$33.95; R unchanged). Not pursued without
  Rafael.
- **C2:** FADE widens the stop to max(structural, 1.5x real-time 5m range) and SHRINKS shares to keep dollar risk,
  instead of skipping (replay at constant risk, fractional shares: FADE -$4.86 -> -$0.51, n=15). Whole-share floor can
  raise dollar risk on 1-share names. Risk-path (more entries pass) -> board + replay on all logged decisions.
- **C3:** RIDE time stop: exit if not +0.5R within N bars (hypothesis, n=8; sweep N=3/6/12). Exit-only; route as
  risk-path unless cleared.

## Replay of the candidates (research/day_tier_track_a_replay.py, logs/day_tier_track_a_replay.json; 51 of 53 decisions)
- The 10/10-11 room change WIDENS stops (`_room_stop`), it does not skip. A skip gate (what-if) would drop 12 of 15
  FADE fills (-4.9R saved, dollar result a coin flip, loses the two best winners) and 12 unfilled setups that
  simulated +6.0R. Not recommended.
- C2 (widen + shrink, whole shares, 1-share floor): FADE -12.68R / -$12.75 vs -6.82R simulated structural; the
  1-share floor added $14.11 of dollar risk across 7 trades. **The same applies to the shipped `_room_stop` on 1-share
  names: widening raises dollar risk instead of holding it constant** (the 10/06 CEO "must trade" order). Decision for
  Rafael, with the board's view.
- C3 RIDE time stop (exit if not +0.5R within N 5m bars): N=3 +0.95R / -$3.61 vs -2.59R / -$52.62 simulated baseline;
  N3 > N6 > N12 holds under leave-one-out; n=8 — a hypothesis. Next: board + a wider replay (all RIDE triggers).
