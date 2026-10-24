# Track A edge sizing + stop-beyond-level replay — 2026-10-23 (signed: Claude)

Approval-ID: TRACK-A-EDGE-1023
Covers: execution/day_trade_manager.py, execution/earned_sizing.py, tests/test_earned_sizing.py, tests/golden/day_entry_grid.json

## Why
Owner request 2026-10-23, after the weekly Day-tier audit: run the Track A sizing fix and the stop-placement fix
through the board, Gro and GAI. Owner rule 2026-10-19 (CONV-1018): "Trades in the losing ranges still should be taken,
they shouldn't be 5% of overall budget though until strategy shows edge and/or increased win rate."

## Facts (read-only, OCI, logs/day_tier_events.jsonl + Alpaca fills + 1-minute IEX bars)
- Every closed Day trade 2026-09-15 .. 2026-10-23 reconciles to the cent with Alpaca fills (this week: -$17.16 both).
- Track A (GEX wall FADE/RIDE): 27 replayable trades (3 handed to Swing excluded), mean R -0.447, sd 0.829,
  t = -2.8, 95% interval of mean R [-0.76, -0.13]; -$99.37; 5 wins. Worst trade SNXX 10/09 89 sh -$30.26.
  Weaker readings (López de Prado seat): grouped by day (14 days) t = -1.63; the 21 trades after week 1, t = -1.59;
  median R -0.97 (most trades are full stop-outs); bootstrap P(mean R > 0) = 0.004; Welch t (B - A) = 2.73.
- Track B (movers PULLBACK/DRIVE): 12 trades, mean R +0.753, sd 1.418, t = 1.84, interval [-0.05, +1.56]; +$66.89.
- CONV-1018 bands are terciles of ALL Day trades' conviction. Track B entries carry conviction 0.50-0.60 (the middle band,
  "proven"); Track A entries carry 0.19-0.83 (low/top bands, mean R -0.43 / -0.58, not proven). A non-proven band
  trades at TODAY'S sizing, which for Track A is the 1.5% risk basis sized to the stop distance (DRAM 10/21: 26 sh,
  ~$1,260; AMZN 10/23: requested 2 sh at conviction 0.47, wired 6 sh). So the losing setup family takes the largest
  positions in the Day tier.
- The Confluence 2.0 registry (strategy/day_tier_family_admissions.json) already marks gex_wall_fade_v1 and
  gex_wall_ride_v1 BLOCKED / audit FAIL, but live Track A entries are logged as family "unclassified_v1", so the
  registry does not reach them. Blocking them would cut trades (owner rule: never fewer); this design sizes instead.

## Design (Fix 1 — TRACK-A-EDGE)
1. Track A's own record: R of every completed Track A trade (same R as earned_sizing.completed_trades: realized $/share
   from broker-confirmed fills / initial stop distance) whose last exit is before this week's Monday 00:00 ET
   (earned_sizing.week_start_et — the same weekly walk-forward cutoff CONV-1018 uses). Track = the entry fill's
   "track" field; a missing field is Track A (_entry_track's existing rule). Track M keeps its own sizing.
2. If that record has at least two trades (the CONV-1018 per-band minimum) and its mean R <= 0, a Track A entry's share
   count is cut to the Track B probe size: floor(start-of-day equity x _DAYTIER_ALLOC_PCT x _TRACK_B_SHARE x the
   entry's conviction / limit price), never below the symbol's minimum entry quantity (one share) and never above the
   count today's sizing chose. min() only: it can shrink an entry, never grow one, never skip one.
3. Mean R > 0, or fewer than two Track A trades: today's sizing, unchanged.
4. The stop, target, every account room, the daily dollar budget, the Day-tier 5% force-flat and the 7% kill are
   unchanged and still apply after the cut.
5. Logged on the entry's size record: track_record {n, mean_r, week_start, applied, probe_qty, qty_before}. Log line
   `change=track-a-edge-1023`.
6. Fail safe: an unreadable journal (read_events_checked not readable) -> the record is unknown -> the cut APPLIES
   (Track A gets the small size, never the large one; trade still taken). Rationale: the unknown state must never
   restore the large size of a family whose last known record is negative.
No new number: _DAYTIER_ALLOC_PCT (0.15) and _TRACK_B_SHARE (0.35) are the existing Track B budget settings; the
two-trade minimum and the weekly cutoff are CONV-1018's.

## Replay (Fix 1) — logs/replays/track_a_edge_stop_level_replay_2026-10-23.py + _analyze_.py
Walk-forward (record from trades closed before each trade's week): 27 Track A trades, 21 probe-sized (the first 6, in
the week of 9/15, had no prior record and keep today's size). Track A P&L -$99.37 -> -$22.61 (+$76.76). Trade count
27 -> 27. Biggest changes: SNXX 89 -> 3 sh (-$30.26 -> -$1.02), DRAM 10/21 26 -> 2 sh (-$19.50 -> -$1.50), AMZN
10/13 6 -> 1 (-$13.14 -> -$2.19). Winners shrink too: TSLA 10/07 +$7.45 -> +$2.48, GOOGL 9/23 +$2.53 -> +$0.63.
All Day trades: -$32.48 -> +$44.28 (Change 1 alone; the analysis script's +$40.98 line is both changes together). Method: P&L scaled by qty_new / qty_old on the same fills (exits unchanged).

## Fix 2 — stop beyond the level (replayed; NOT recommended)
Variant: the stop may not sit between the entry and the level that defines the trade (wall_ref); stop = the more
protective of today's stop and level x (1 +/- DAYTRADE_STOP_BUFFER_PCT); same dollar risk -> fewer shares (min one);
FADE target unchanged, other modes target = DAYTRADE_RIDE_TARGET_R x the new distance.
- 12 trades would change: 10 Track A FADE trades (stop placed between entry and the faded wall by the 1:1 mirror rule,
  last one 10/13) and 2 Track B trades whose CONV-1018 tight stop (d' = 1.5 x ATR) sat inside the broken level
  (SMCI and AVGO, 10/22).
- Simulated P&L on those 12: -$6.28 -> -$12.60 (worse). Stop-outs 8 -> 6, but dollar risk rose $29.61 -> $33.62
  because one-share trades cannot shrink (META 9/18 risk $2.28 -> $5.56) and the winners had fewer shares (TSLA 10/07
  +$7.32 -> +$2.44, AVGO 10/22 +$3.26 -> +$1.63). Only AMZN 10/09 flipped from a stop-out to a target (-$1.80 -> +$0.59).
- Simulator fidelity: re-running every trade with its ACTUAL stop/target/qty matches the real P&L within $1 on 34 of
  39 trades (sum -$35.71 simulated vs -$32.48 real); the 5 misses are EOD/partial-fill timing.
Recommendation: do not ship Fix 2. The stop location is not what is losing money; Track A's size is.

## Forward pass (separate diff, after this one)
Generalize item 1 to every setup family (Track B PULLBACK vs DRIVE, Track A FADE vs RIDE, Track M): size follows the
family's own walk-forward record instead of conviction terciles pooled across families, which today conflate "track"
with "conviction".

## Required questions (RQ-4)
Q1: Moves the goal by removing the Day tier's largest loss source (-$99 over 27 trades) while keeping every Track A
    trade for data; R-based edge measurement is unaffected by share count.
Q2: Trade count unchanged: 27 of 27 Track A replay trades still trade (min one share; never a skip).
Q3: Simplest form: a min() on the share count using the existing Track B budget formula. The registry block is the
    restrictive alternative and is not used.
Q4: Builds on CONV-1018 (same R, same weekly cutoff, same two-trade minimum) and the Track B budget; the C2 registry
    already rates Track A FAIL. No open PR or Codex session touches Day sizing (git log origin/main since 10/20).
Q5: No new number: existing settings plus Track A's measured mean R.
Q6: Exposes the static assumption that conviction bands pooled across tracks measure edge; forward pass above.
Q7: Feeds sizing only; reads the same journal CONV-1018 reads; fails to the small size.
Q8: Every figure above is from this session's read-only replay and the journal; mean R and interval computed from fills.
Q9: Walk-forward on 27 Track A trades 9/15-10/23 (VIX regimes of late Sept and Oct). Expected: Track A losses cut by
    roughly the size ratio (~75%). Reversal: Track A's walk-forward mean R turns positive -> today's sizing returns
    automatically; or a probe-sized Track A loss exceeds its logged risk.
Q10: No validated edge is held back: Track A's interval is entirely below zero; Track B is untouched.
Q11: Size only, shrink-only (size delta <= 0, frequency 0, concurrency <= 0). Not an amplifier; reviewed by the board
     with the masked-loss seat anyway because it is in the sizing path.
Q12: Stops, the 7% kill, never-mask-a-loss and paper=True unchanged.
Q13: Unreadable journal -> the cut applies (small size, trade still taken).
Q14: Not a shadow: live on approval.
Q15: track_record block on every Track A entry's size record.
Q16: `change=track-a-edge-1023` log line on each Track A entry; post-live replay against the first session.
Q17: Read-only run on OCI against the live journal before live (prints today's record and the probe size).
Q18: The DRAM/AMZN losses are not "by design": the design gave a losing family full size.
Q19: Changes sizing: owner approves this exact design before any commit.
Q20: Prompts carry facts only (bias gate); Fix 2 reported as measured although it was the owner's request.
Q21: Pure function in _wired_entry; tests use the golden-master grid, no broker.
Q22: Read-only OCI run before live.
Q23: Golden-master grid + the read-only OCI run on the live journal.
Q24: N/A.
Q25: All statistics measured from the journal and IEX bars.
Q26: T1 Alpaca fills (via the journal) and IEX 1-minute bars; fallback = the small size.
Q27: No new state file; the record is computed from logs/day_tier_events.jsonl.
Q28: Inside _wired_entry, after the CONV/_bounded_entry_qty share count and before the daily dollar budget fit;
     the record is read in place_entry's input snapshot (the REPLAY-1019 pure/impure split).
Q29: Highest-value open Day item: the largest measured loss source this week and all history.
Q30: A desk sizes a strategy by its own measured edge, not a pooled score; this does that for Track A.
Q31: Walk-forward; two fixes replayed, one variant each; Track A's t = -2.8 survives the two tries.
Q32: Walk-forward cutoff uses only trades closed before the entry's week; same symbols as traded.
Q33: Real fills include spread and slippage; smaller size lowers impact.
Q34: Not measured against SPY here; Track A is long and short, so beta is mixed (hypothesis — unverified).
Q35: Worst Track A day 10/09 -$29.12; after the cut about -$0.93 on the same trades.
Q36: Probe size scales with equity (5.25% x equity x conviction), so it grows toward $25K.
Q37: At sd 0.83, ~70 trades resolve a 0.2R mean; Track A has 27 and is already significant below zero.
Q38: Decay: weekly walk-forward mean R; "non-working" for zero trades stays the owner's call (registry says FAIL).
Q39: Size follows the measured record; P&L from Alpaca fills.
Q40: Outcome: Track A's dollar losses scale with the probe size; check = weekly report of Track A loss per trade
     against its logged probe risk.
Q41: Per-family sizing (forward pass).
Q42: Raw fills and the decision journal.
Q43: Size by the strategy's own out-of-sample record (fractional Kelly at <= 0 edge is zero; the probe is the
     data-collection minimum).
Q44: The first-session post-live replay and the weekly belief test, separate from the sizing code.

## Board + Gro + GAI review (2026-10-23) — aligned 5/5
Voices: Thorp seat, Taleb seat, López de Prado seat, Gro, GAI. Change 1 APPROVE-WITH-CHANGES (all five); Change 2 REJECT
(all five). Number corrections adopted above (Track A 5 wins; conviction ranges; combined vs Change-1-alone labels).

### Change 1 — FINAL design (supersedes items 2, 3 and 6 above)
qty = min(today's qty, max(min_qty, floor(KELLY_FRACTION x max(0, LB) / varR x start-of-day equity / stop distance)))
- LB = meanR - sd / sqrt(n): one standard error below Track A's mean R (Taleb + López de Prado seats: size returns
  only when the record is positive beyond its own noise; Thorp seat's continuous rule kept, so no cliff).
- meanR, varR: Track A's walk-forward record (trades whose last exit is before this week's Monday 00:00 ET).
- min_qty: the existing _min_entry_qty(symbol, ...) (1 share; 2 for a leveraged ETF such as SNXX/SNDQ — Taleb seat: the raw
  1 would push a 2x ETF below its own minimum and skip the trade). KELLY_FRACTION: the existing paper-profile setting.
- Kelly at mean R <= 0 is zero, so a losing Track A takes the minimum size; a positive record grows the size continuously
  with the measured edge, capped by today's count (no cliff from 1 share back to full size — Thorp seat).
- Fewer than two Track A trades on record, or an unreadable journal: the minimum size (Thorp seat, Gro: no record means
  zero estimated edge; owner rule "not the full budget until it shows edge").
- Rows with no track field are left OUT of Track A's record and logged (López de Prado seat: a misread Track B trade
  must not decide when Track A's size returns); the entry itself still takes the cut.
- Shrink-only, never a skip. Replay: Track A -$99.37 -> -$21.32 (n>=2 rule) / -$19.26 (minimum also when n<2); trade
  count 27 -> 27.
Not adopted: the Track B probe formula (no edge basis; a dollar cap, not a risk cap — Thorp, López de Prado); GAI's
10-day rolling window (at today's trade rate it holds about 5 trades); Gro's posterior-probability guard (forward pass).

### Change 2 — rejected
All five voices: no measured benefit, higher dollar risk on one-share trades. The stop location is recorded on every entry
(wall_ref and stop_price), so the question can be revisited from the journal.
- The cut is applied last, after either sizing path (CONV-1018 or _bounded_entry_qty), including a band that later
  becomes "proven" and a 10/10 max-size entry (Taleb seat). No Track A entry has ever been 10/10 (ten_of_ten is set by
  Track B only), so today this changes nothing for 10/10; the owner decides whether a future 10/10 Track A keeps full size.
- Reporting (Taleb seat, follow-up diff, weekly_postmortem.py): per-track result in R as well as dollars, so a smaller
  dollar loss cannot hide a family that still loses per trade.
