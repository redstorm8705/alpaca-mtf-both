# DAY-VETO-SIZE-1109 — Day tier trend vetoes change size, not count (design for review)

Approval-ID: DAY-VETO-SIZE-1109
Covers: run_day_tier.py, execution/day_trade_manager.py, execution/day_veto_sizing.py, execution/earned_sizing.py, config.py, data/state/day_veto_prior.json, research/day_veto_prior.py, tests/test_day_veto_size_1109.py
Not changed: research/day_tier_trackb_watchday_replay.py, research/day_tier_trackb_oos_replay.py, strategy/day_tier_side.py, execution/broker.py
Reviewer #2 waiver: the approval includes waiving reviewer #2 (chunked Gro) on execution/day_veto_sizing.py and execution/day_trade_manager.py after six refuted rejects; GAI still required.
Status: rev 8. Design review: board Thorp, Asness, Taleb APPROVE-WITH-CHANGES (folded in); reviewer #2 (Gro) APPROVE-WITH-CHANGES (folded in, two declined with reasons); GAI APPROVED rev 8 after one counter-prompt round (2026-11-10). Built 2026-11-10 in claude/day-veto-size-1109, staged, not committed (signed: Claude).

FRAME: PAPER account, goal $2.5K -> $25K. Data collection and edge evaluation are the #1 objective; the bot trades every
day; the only daily brake is the 7% kill switch (new entries only). Safety envelope unchanged: a stop on every position,
never mask a loss, code correctness.

## What happened (2026-11-09, verified from OCI logs/day_tier_events.jsonl and Alpaca bars)
SPY +2.36% (open 741.94, close 752.72). The Day tier evaluated 18 ENTER signals (8 distinct setups) and placed none.
Skip reasons (run_day_tier._direction_block, called for Track A at ~L1313 and Track B at ~L1501):
- rule 1 "no clear trend direction (side=TWO_SIDED)": META FADE long 06:40 PT, PLTR DRIVE long 06:56, TSLA PULLBACK long
  08:00, AMD FADE long 12:20.
- rule 3 "long against the SHORT daily side (only a failed-trend fade is allowed)": DRAM DRIVE long 06:56, AVGO PULLBACK
  long 07:08 (and DRAM RIDE 07:08, same symbol).
- SMCI FADE long 12:00: counter-trend fade not confirmed failed.
"Side" = strategy/day_tier_side.compute_side_bias: a weighted vote of daily EMAs/SMAs plus 10-week and 10-month SMAs;
LONG/SHORT only past a threshold, else TWO_SIDED.
Same-day replay with the live stop/target geometry (signal price entry, SIP 1m path): PLTR +1.9R, DRAM +1.3R,
AVGO +1.9R, TSLA -0.4R (Track B); META -1.1R / ~0R noise-stop, AMD +1.7R / -0.3R noise-stop, SMCI -0.4..-0.7R (Track A).

## Owner rule this conflicts with (owner 2026-10-19, CONV-1018)
"Trades in the losing ranges still should be taken, they shouldn't be 5% of overall budget though until strategy shows
edge and/or increased win rate. Skipping all together lowers overall trade amount and data collection." Conviction/edge
gates change SIZE, never count, unless the owner explicitly approves a cut. Also binding (owner 2026-10-30): no
"small fixed size until N trades" ramp — size from replay-derived values on day one, updated by live fills.

## Evidence (Track B replay, research/day_tier_trackb_watchday_replay.py, room-ok signals, 5 bps cost, 2R target)
| window | group | n | mean R | total R | t |
|---|---|---|---|---|---|
| 2023-10..2025-09 (older) | taken today | 918 | -0.026 | -24.2 | -0.67 |
| 2023-10..2025-09 | blocked by rules 1+3 | 360 | -0.073 | -26.4 | -1.17 |
| 2023-10..2025-09, next-bar entry | blocked by rules 1+3 | 360 | -0.084 | -30.3 | -1.36 |
| 2025-10..2026-11-06 (recent) | taken today | 520 | +0.002 | +1.0 | +0.04 |
| 2025-10..2026-11-06 | blocked by rules 1+3 | 322 | +0.113 | +36.4 | +1.73 (1.54 session-clustered) |
| 2026-10-10..2026-11-06 (4 weeks, never consulted before) | all room-ok | 66 | +0.422 | +27.9 | +2.70 |
| 3 years pooled (board recompute) | blocked by rules 1+3 | 682 | ~+0.015 | +10.0 | ~+0.3 |
Nothing clears a multiple-testing bar (~34 tries; Bonferroni t >= 2.81). The only supported claim is the negative one:
the daily-side vote does not separate winners from losers in either the taken or the blocked group.
Blocked setups: roughly flat across 3 years (older negative, recent positive). Not a proven edge, not a proven loss.
Alternatives tried this session and REJECTED (none positive in both windows): remove rules (variant A/B); remove only
when SPY > 50-day SMA; replace daily side with SPY's same-day direction (M1, and M2 with a derived clear-day floor:
discovery -0.093R t -2.09); a separate SPY/QQQ trend-day setup at 10:00 and 10:30 (SPY test -0.197R t -2.08; QQQ
discovery +0.132R but test -0.072R); last-half-hour index momentum (Gao-Han-Li-Zhou 2018; negative both windows after
2 bps). About 14 candidates tried this session; the 2025-10..2026-10 window was consulted in the 2026-10-11 review.
Track A (FADE/RIDE) rule-1 skips were NOT replayed over history (only today).

## Proposed change (revision 4 — board Asness, Thorp and reviewer #2 changes applied)
SCOPE: Track B only. Track A's rule-1 skips first get their own historical replay; Track A joins only if that replay
supports it (separate decision).

1. Rules relaxed (Track B call of the direction-rule check only):
   - rule 1 relaxed ONLY for side == TWO_SIDED. side UNKNOWN (unreadable side) stays blocked (fail closed).
   - rule 3 (long against a SHORT daily side) relaxed.
   - no early return: a relaxed setup falls through to the direction-validity check and rule 2 (every 2m/5m check
     opposes the trade -> block), exactly as a with-trend setup does.
   The setup proceeds tagged `veto_downsized` with the relaxed rule's reason.
2. Size: the Day tier sizes by stop risk (DAYTRADE_PER_TRADE_RISK_BASIS_PCT x equity / stop distance; there is no
   Kelly in the Day tier). A veto_downsized setup passes risk_mult = m into that path (not conviction, which is a
   validity precondition only), where
     f* = p - (1 - p) / b  (p = win rate, b = mean win R / mean loss R, net of cost, Track B vetoed group),
     m = clamp(c x f*_LCB / RISK_BASIS_PCT, m_floor, 1), c = the existing fractional-Kelly constant
     (SWING_BREAKOUT_KELLY_FRACTION), f*_LCB = f* at the posterior mean minus one standard error,
     m_floor = the smallest m at which min_entry_qty survives the bounded-quantity step for that setup's stop.
   p and b come from a posterior: replay trades weighted by recency (half-life fitted to the measured decay between
   the older and recent windows), the prior's effective count shrunk for ~34 variants tried, then updated with every
   closed veto_downsized trade from Alpaca fills (continuous, no sample-count switch). Missing or unreadable inputs
   -> m_floor. No division by a Kelly that can be zero or negative.
   The floor is taken only when min_entry_qty x |entry - stop| is within the existing per-trade risk cap
   (DAYTRADE_PER_TRADE_RISK_EQUITY_PCT of start-of-day equity); otherwise skipped and logged "veto setup skipped: one
   share risks more than the per-trade cap". (Notional is not risk: SPY $750 with a 0.7% stop risks ~$5.25/share.)
2b. Revision 5 (board Taleb): CONV-1018 conviction sizing (earned_sizing.conv_size via _conv_compute) can size a lot
   to the remaining tier budget and ignore risk_mult. Therefore:
   - veto_downsized setups are EXCLUDED from CONV-1018 sizing and always sized by the bounded stop-risk path with
     risk_mult = m;
   - veto_downsized trades are EXCLUDED from the CONV-1018 band record (they can neither prove nor pollute a band) and
     get their own R ledger from Alpaca fills (the posterior in item 2 reads it);
   - per-trade risk check on every veto lot: min_entry_qty x |entry - stop| x DAYTRADE_BUDGET_SLIPPAGE_MULT must be
     within DAYTRADE_PER_TRADE_RISK_EQUITY_PCT of start-of-day equity, else skip and log;
   - same-direction cluster bound: the combined stop-risk of open veto_downsized lots in one direction may not exceed
     DAYTRADE_PER_TRADE_RISK_EQUITY_PCT of start-of-day equity (one normal trade's risk — an existing number, no new
     constant); a veto setup that would exceed it is skipped and logged with the cluster total.
   - log also: the qty the setup would get without the veto, the sizing path that ran, dollar risk as % of equity,
     and the count and combined stop-risk of open same-direction veto lots. Proof line tag: [change=day-veto-size-1109].
2c. Revision 6 (confirmation round — Thorp, Asness, Taleb, reviewer #2):
   - Cluster room: a veto lot that would breach the same-direction bound is SHRUNK to the remaining room; skipped only
     when even min_entry_qty does not fit (Thorp).
   - m_floor > 1 (one share risks more than the risk basis allows): skip and log "one share risks more than the risk
     basis allows" — never a silent fail-closed risk_mult (Thorp).
   - Recency weighting: equal weights (no fitted half-life). A fitted half-life is allowed later only if the two
     windows differ significantly after the multiple-testing adjustment (Asness). An adaptive half-life is a
     forward-pass idea only, not part of this change (reviewer #2 nit, recorded).
   - The standard error in f*_LCB is clustered by session, as in the reversal interval (Asness).
   - Cluster direction = exposure sign (lev.exposure_sign): a short signal routed to an inverse fund BUY counts as
     short exposure (Taleb).
   - Cluster stop-risk total is scaled by DAYTRADE_BUDGET_SLIPPAGE_MULT, measured from each lot's current stop (Taleb).
   - Zero or negative stop distance -> skip and log before any risk division (reviewer #2).
   Declined with reasons (reviewer #2): (a) a direction-agnostic aggregate cap at "1.5 x per-trade cap" — a new fixed
   number; opposite-direction veto lots offset market exposure, and the Day daily dollar budget already bounds the
   total across directions.
2d. Revision 7 — AUTOMATIC PAUSE ON A PROVEN LOSING STREAK (owner direction 2026-11-09: "I want that to ship";
   replaces reviewer #2's "after N consecutive negative LCBs", which used a fixed count):
   - Trigger (derived, no fixed count): after each closed veto_downsized trade, from Alpaca fills (own R ledger), when
     the UPPER end of the 90% session-clustered confidence interval of live mean R is below 0 — the losses are too
     consistent to be chance — the veto path PAUSES: vetoed setups go back to being skipped, logged
     "veto path paused: live mean R CI upper < 0 (n=.., mean=.., upper=..)".
   - The pause is computed per direction (long / short exposure sign), so a losing long stream does not stop the
     short side and vice versa.
   - On the pause: one Slack page to the owner with the numbers. The pause lasts for the REST OF THAT TRADING DAY only
     and resets automatically at the next trading day's open (owner direction 2026-11-09: "It just needs to reset by
     the next trading day"). Next day the path trades again; the trigger is re-checked after each new closed
     veto_downsized trade, so it pauses again that day only if the new results still put the upper bound below 0.
     Pause state is keyed by trading date (atomic write in data/state), so a restart cannot lose or extend a pause.
   - This is an owner-approved trade-count cut (CONV-1018 reserves cuts to the owner); it applies only to
     veto_downsized trades, never to with-trend trades.
   - Expected: with the 3-year replay's flat edge, a pause needs a run of losses clearly worse than the replay; it is
     a backstop, not a routine event.
2g. Revision 8 (GAI round 1):
   - Order of checks: the minimum-order risk check (min_entry_qty x |entry - stop| x DAYTRADE_BUDGET_SLIPPAGE_MULT
     within the per-trade cap, stop distance > 0) runs FIRST; a setup failing it is skipped and logged before any
     Kelly/m math or sizing call.
   - Pause statistic: the pause uses the SAME posterior as sizing (shrunk replay prior + live veto fills), not live
     fills alone, so two or three similar losses cannot collapse the interval and pause the path on noise (GAI's
     concern). It also needs live fills from at least two distinct sessions before it can trigger.
     Prior strength (made concrete, derived): the replay prior's effective trade count = replay trades / number of
     variants tried (682 / ~34 = ~20), so live veto fills outweigh the prior after about 20 trades; the replay never
     counts as 682 trades of evidence.
     A fixed "max consecutive losses = 4" (GAI's proposed fix) is declined: a fixed count is banned by the
     no-static-threshold rule, and a break-even strategy produces 4-loss streaks by chance often.
   - Cluster bound freshness: the same-direction total counts open veto lots from the broker plus every veto entry
     already submitted in the current tick (in-memory, same thread), so a burst in one tick cannot exceed it.
   - Each veto_downsized order carries a client order id marker, so the R ledger is rebuilt from Alpaca orders/fills,
     not only from state files.
2e. Both directions (owner direction 2026-11-09: "it also needs to include downside like last Thursday or Friday"):
   rule 1 (TWO_SIDED) is relaxed for longs AND shorts; rule 3 covers longs against a SHORT side; Track B shorts against
   a LONG side are already allowed when the stop has room (DAYTRADE_TRACK_B_WATCHDAY_EXEMPT). Measured: Thu 11-05
   (SPY down) the Day tier filled 10 trades, mostly shorts and inverse funds; blocked were 1 TWO_SIDED short fade and
   2 watch-day short fades (Track A). Fri 11-06: 2 shorts filled; 5 longs blocked by rule 3 (2 Track B PULLBACK,
   3 Track A RIDE). Track A setups (FADE/RIDE) are not covered until Track A's own replay.
2f. Track A replay (2026-11-09, scratchpad/ta_replay.py; every logged Track A ENTER skipped by rule 1 or rule 3 since
   2026-10-05 — Track A needs live GEX walls, so logged decisions are its only history): n=34, win 29%, mean -0.296R,
   total -10.1R, t -1.11 (FADE n=18 -0.464R; RIDE n=16 -0.107R; shorts n=14 -0.529R, t -2.04). Base structural stop
   (the live noise-stop widening is not modelled). Decision: Track A keeps its current skips (no change); revisit
   when more Track A skips accumulate in the log.
3. Unchanged: every downstream cap (Track B budget, Day daily dollar budget, Day gross, overnight K, the per-trade
   risk cap), every stop and target, the room gate, the watch-day rule and its Track B exemption, the Track A path,
   the 7% kill switch. There is no Day position-count limit (removed 2026-10-04, per the Thorp seat's source check).
   A cap refusal is logged with its own reason. Kill flag: config.DAYTRADE_VETO_DOWNSIZE_ENABLED (False restores
   blocking).
4. Decision explainability: each veto_downsized decision logs the relaxed rule, side, side_score, p, b, n_prior,
   n_live, f*_LCB, m, the final risk_mult and qty.
5. Test (full path): a vetoed TWO_SIDED Track B signal reaches the entry call with qty >= min_entry_qty and the tag;
   UNKNOWN stays blocked; a rule-2 opposition still blocks; a floor whose one-share risk exceeds the cap is skipped.
   All broker calls mocked.

## Rule B/E routing (author's statement — reviewers must verify independently)
Size: per-trade risk can only go DOWN versus an un-vetoed setup (m <= 1). Frequency: UP (about 1.2 extra Track B
trades per session in the 13-month replay: 322 over 266 sessions). Concurrency/exposure: UP within the Day dollar
budget and gross caps. The multiplier sits upstream of the bounded-quantity step. => RISK-PATH: board + owner approval.
Correlation: vetoed setups cluster on one-direction market days (11-09: all longs on a +2.36% SPY day); each is
floor-sized and the Day daily dollar budget bounds the total.

## Expected effect / reversal
Expected: on days like 11-09, 3-5 extra Track B entries at minimum size; P&L impact near zero either way until live
fills raise f*_LCB. Reversal (no fixed number): when the upper end of the 90% session-clustered confidence interval of
live mean R (Alpaca fills, veto_downsized trades) is below 0, it is reported to the owner as non-working for a
stop/keep decision; m already sits at m_floor whenever f*_LCB <= 0. Front-loaded test data:
research/day_tier_trackb_watchday_replay.py and day_tier_trackb_oos_replay.py.

## Required questions
Q1: Moves data collection: about 1.2 extra Track B trades per session in the 13-month replay (322 over 266 sessions), at minimum size until live fills show edge. Not conservative.
Q2: Trade count goes UP. The only cut is the owner-requested per-direction pause, which lasts the rest of one trading day.
Q3: Simplest form of the owner's 10-19 rule (change size, not count); the safety pieces (risk check first, cluster, broker confirmation, pause) make it safe instead of skipping.
Q4: Extends CONV-1018 (owner 10-19), the 10-30 no-ramp rule and the 10-11 Track B review; no conflict. DAY-VETO tag excluded from CONV-1018 bands. No open PR touches these files (main checked 11-10).
Q5: Every number is derived or an existing config value: m from the posterior Kelly, prior strength = replay trades / variants tried, pause from the 90% interval, cap = DAYTRADE_PER_TRADE_RISK_EQUITY_PCT, slippage = DAYTRADE_BUDGET_SLIPPAGE_MULT, retention = _STATE_TTL_DAYS.
Q6: Exposes the fixed daily-side vote threshold; forward pass: a per-group edge estimate that updates live and feeds size for every gate (Asness/Taleb).
Q7: Wired into sizing (risk_mult), the CONV-1018 band and jump records (excluded), the R ledger, and Slack pages; each connection fails closed and is tested.
Q8: Measured: 11-09 skips in OCI day_tier_runner_cron.log (15 matching lines); replays in research/ with 682 trades over two windows. Edge claims are labelled flat, not proven.
Q9: Tested on 2023-10..2026-11 (two windows, 682 vetoed setups): older -0.073R, recent +0.113R, pooled ~+0.015R. Expected effect small either way at minimum size. Wrong if live veto mean R's 90% upper bound is below 0 (the pause reports it).
Q10: No proven edge held back; the change takes the setups small instead of skipping them.
Q11: Yes, risk-path (frequency, concurrency): board Thorp + Asness + Taleb (masked-loss seat) reviewed.
Q12: Envelope unchanged: every stop, the 7% kill switch, never-mask-a-loss (own R ledger, exclusions paged), paper=True.
Q13: Missing prior or inputs -> minimum size; unreadable broker book, side or stop -> skip new veto setups; unreadable pause file -> paused; each paged once.
Q14: Not a shadow: live trades from day one, sized from the replay.
Q15: Each veto decision logs the relaxed rule, side, side_score, p, b, n_prior, n_live, f_LCB, m, risk_mult, qty, dollar risk and cluster state to day_tier_events.jsonl.
Q16: Proof line "[change=day-veto-size-1109]" in logs/day_tier_runner_cron.log; post-live audit replays the first session's veto decisions against Alpaca fills.
Q17: First live veto decision line plus its Alpaca order with the veto client order id marker.
Q18: The 11-09 zero-trade day was investigated against the logs and bars, not explained away.
Q19: Changes entry and sizing: owner approves the exact design (this record).
Q20: Prompts were neutral facts; corrected overclaims (window labels, t-stat, cluster size, pause count) after review.
Q21: All broker calls mocked in the 62 tests; MTF_TEST_MODE set.
Q22: Fail-closed controls read live data at runtime; a read-only server dry run is part of the post-live check on the first session.
Q23: Full-path tests run a vetoed Track B signal through place_entry with the broker mocked.
Q24: Not applicable: no existence claims.
Q25: Statistics are measured from Alpaca SIP bars and the replay files, not from memory.
Q26: T1 Alpaca (bars, orders, fills, positions); no fallback source; unreadable data fails closed.
Q27: data/state/day_veto_*.json and the journal, all atomic writes; prior file regenerated by research/day_veto_prior.py.
Q28: run_day_tier Track B loop -> _direction_block_b -> _veto_prepare -> place_entry -> _veto_sizing -> broker; runs in the Day runner process, not the main bot's run_cycle thread.
Q29: High: the owner asked for it after 11-09; low build cost relative to the trades it unblocks.
Q30: Missing factor-correlation limits across tiers; the same-direction cluster covers veto lots only (daily kill budget covers the total).
Q31: Two-window replay; about 34 variants tried; nothing significant after the deflated threshold, so size starts at minimum.
Q32: Signal-bar entry is slightly optimistic (next-bar entry -0.084R vs -0.073R); today's universe (survivorship); the recent window was looked at before.
Q33: 5 bps cost in the replay; live 1-share slippage unmeasured until fills arrive.
Q34: 11-09 gains were mostly market beta (SPY +2.36%); not separated.
Q35: Worst single replay trade about -1.4R; correlated same-day longs bounded by the cluster cap.
Q36: Minimum-size orders are trivial versus daily volume.
Q37: About 400-800 trades needed to tell +/-0.1R from zero; 682 replay, 0 live.
Q38: Decay: the posterior updates on every live fill; non-working = the 90% upper bound below 0 -> per-direction pause and a page; turning it off is the owner's call.
Q39: Size from the Kelly fraction of the posterior (replay + Alpaca fills).
Q40: Protects trade count on strong market days; check: daily count of veto decisions on sessions with Track B side vetoes.
Q41: Pool all gates into one live edge model (Asness).
Q42: Raw Alpaca bars, orders, fills and the logged decision stacks.
Q43: Fractional Kelly on a shrunk posterior with a lower confidence bound, plus a correlated-exposure cap.
Q44: Independent proof: the live Alpaca fill record of veto_downsized trades and the post-live audit.
