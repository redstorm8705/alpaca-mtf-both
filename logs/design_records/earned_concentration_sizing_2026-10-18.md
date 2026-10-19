# Earned concentration sizing for the Day tier — 2026-10-18 (Claude)

Approval-ID: CONV-1018
Covers: execution/day_trade_manager.py, execution/earned_sizing.py, run_day_tier.py, tests/test_earned_sizing.py

## Why (owner, 2026-10-17)
"It's imperative we add with size when the conviction is highest. It's fine if the position is concentrated. I'd rather
own 3 large positions than 10 smaller ones." Margin counts (intraday buying power about 4x equity).

## What the data says (read-only replay on OCI, 32 completed Day-tier trades, logs/day_tier_events.jsonl)
- Realized: -$12.92. Conviction does not rank outcomes: low tercile mean R -0.42 (2/10 wins, -$42.88), mid +0.19
  (5/10, +$47.82), top -0.27 (3/12, -$17.86); the eight highest-conviction trades went 0 for 8 (mean R -0.74).
- Size is not the binding problem today, edge is: the same trades at full base risk with the fixed caps lifted to
  buying power -> -$94.16 (worst day -3.60% of equity); at 2% risk and 4x -> -$118.52.
- On 10/10 trades the fixed notional caps cut the stop-distance size from 73/45/18/23 shares to 18/5/4/3.
- The Confluence 2.0 family registry (strategy/day_tier_family_admissions.json) already marks gex_wall_fade_v1,
  gex_wall_ride_v1 and orb_break_hold_v1 BLOCKED/FAIL; monday_weekend_dip_v1 is the only admitted family.

## Design (board Thorp + Taleb seats, Gro, GAI — all APPROVE-WITH-CHANGES, aligned)
1. Size multiplier m per SETUP FAMILY (the entry's family_id), never per conviction score. For each family, from its
   own completed Day-tier trades (R = result per share / stop distance, Alpaca fills): empirical-Bayes shrinkage of
   mean R toward the pooled mean (prior strength estimated from between- vs within-family variance), lower bound =
   shrunk mean minus one standard error. m = 1 unless the lower bound is above zero; above that, m = half-Kelly
   (0.5 x lower bound / variance of R), floored at 1. No hand-set per-family number and no "wait for N trades" gate:
   the interval does the waiting. Today no family qualifies, so m = 1 everywhere (replay unchanged: -$12.92).
2. Concentration for EARNED size only: when m > 1, the entry's fixed caps (60% of equity per name, $1,300 per thin
   name, Day-tier gross) are replaced by derived bounds — per-name ceiling = per-position loss budget / the name's
   99th-percentile adverse gap-through-stop measured from a year of Alpaca bars; book bound = the sum of every open
   Day position's stressed gap loss at correlation 1 stays within the 7% kill; thin names capped at a share of
   20-day average daily volume set from measured IEX slippage. Intended stop-distance risk is never exceeded.
   Positions with m = 1 keep today's caps.
3. Every entry logs family, its n, shrunk mean R, standard error, lower bound, m, the gap quantile and which bound
   applied. Missing data -> m = 1 and today's caps. The weekly belief test gains "conviction ranks R" and per-family R.

## Owner changes (Rafael 2026-10-18: "I want more size on the middle tier and we can recalibrate each week. The
## record currently shows an edge, albeit, on small size. Until it shows differently, it's winning so let's go with
## it. Tiers with day that are losing money, should have tighter stops since they aren't proven.")
These REPLACE design items 1-2 above (item 3, logging, stays):
A. Conviction bands are the terciles of all completed Day trades' conviction, recomputed every Monday from
   logs/day_tier_events.jsonl (today: low < 0.47, middle 0.47-0.60, top > 0.60). Each band's mean R and variance are
   recomputed at the same time (the weekly recalibration).
B. A band whose mean R is positive (today: middle, n=13, mean R +0.32, sd 1.24) sizes up: per-trade risk = half-Kelly
   of that band (0.5 x mean R / variance of R, as a fraction of equity), never below base risk and never above half of
   the 7% daily kill (3.5% of equity) so one full stop-out cannot trip the kill alone. Shares stay bounded by buying
   power after the main-book reserve, the maintenance cushion and the per-name gap ceiling (item 2 bounds).
C. A band whose mean R is negative (today: low and top) keeps today's share count (sized off its normal stop) but its
   protective stop is placed at HALF that distance — half the dollar risk — and is not widened back by the room-stop
   logic.
D. When a band's weekly mean R changes sign it moves between B and C automatically; missing data -> today's sizing
   (base risk).
E. Book bound (Gro, GAI, Taleb seat): the sum of every open Day-tier position's stop risk plus the new entry's risk may
   not exceed the remaining daily kill budget (7% of equity plus today's realized Day-tier P&L); a new entry is sized
   down to fit, never forced out. Several middle-band losers therefore cannot together breach the kill (the three worst
   middle-band losses at once would be about -$217, 8.7% of equity — prevented by this bound).
F. A losing band's tighter stop = max(half the normal stop distance, the existing volatility floor of 1.5 x 5-minute
   ATR or 2 x spread), share count unchanged — dollar risk falls, but a stop is never placed inside normal noise.
G. Taleb seat follow-up (adopted): bound E counts each open position as stop risk PLUS its per-name gap allowance (the
   99th-percentile adverse gap-through-stop from item 2); the tighter stop must also clear the live quoted spread (the
   wider of IEX spread and the last-trade gap on IEX-only names), and a rejected stop placement is logged; band R is
   computed from actual fill prices (the real stop-out, slippage included); the remaining kill budget is reduced by
   today's realized Day LOSSES only — realized gains do not enlarge it.
H. Not adopted, owner decision: the seat asked to size up only once the band's lower confidence bound is above zero
   (at n=13 the middle band's +0.32 mean R has an error near 0.3). The owner decided to size up on the positive record
   now ("until it shows differently, it's winning"); the weekly recalibration (D) is the reversal mechanism.
Replay (read-only, 32 trades, 1-minute IEX paths): middle band walk-forward +$239.25 vs +$57.97 actual (worst trade
-$87.22, 3.5% cap respected); losing bands at half-distance stops with the same shares -$126.97 vs -$350.44 at today's
stops (13 of 19 stopped earlier). In-sample on a small record: the weekly recalibration is the reversal mechanism.

## Owner direction 2026-10-18 (later, OPEN — supersedes the 3.5% per-trade cap in B)
"This size should be based on conviction levels that currently exist. We shouldn't be entering a trade unless there's
already high conviction. ... If we have a high conviction trade, we need to size up to 6% of the daily loss limit. The
more size, the tighter the stops and targets (scaling out on the upside to reduce risk). ... Capping size limits means
we don't believe in the conviction level."
Claude's response (pending the owner's answer, then replay + board/Gro/GAI on stops/scale-out, then owner approval):
- "High conviction" = the band with the proven record (recalibrated weekly; today 0.47-0.60) because the raw top
  scores went 0 for 8; entries only in that band; size follows conviction with no per-trade cap.
- OWNER ANSWER 2026-10-18: "6% total" — total open Day-tier risk (stop + gap allowance) <= 6% of equity; one top
  trade may use all of it, several share it; the 7% kill holds.
- Bigger size -> tighter stop (never inside noise/spread) + closer targets with scale-out partials.
NOT APPROVED YET — no code written.

## 6% replay + mechanics review (2026-10-19, Claude) — aligned 4/4, awaiting the owner's decision
Replay: `logs/replays/conv1018_6pct_replay.py` (read-only on OCI; 32 completed Day trades, Alpaca fills from
logs/day_tier_events.jsonl, 1-minute IEX paths, stop checked before targets inside a bar; 9+ variants tried).
Final mechanics (exact formulas; Thorp seat, Taleb seat, Gro and GAI aligned after 3 counter-prompt rounds):
1. Entries only in the band with positive mean R (conviction terciles of completed Day trades, recomputed weekly).
2. S = today's stop distance; d' = min(max(0.5 x S, 1.5 x ATR5, 2 x median IEX spread over 5 min), S).
3. Gap allowance per share A = max(G x S, 1.5 x ATR5); G = pooled worst observed overshoot/stop ratio over all
   Day stop-outs (0.77 now, ratchets up only, never a quantile). Measured: overshoot does not scale with stop
   width (corr -0.01), so A is tied to S, not d'.
4. shares = floor(remaining budget / (d' + A)), capped at 4 x equity notional; remaining budget =
   min(6% x equity - open Day risk, 7% x equity - today's realized Day losses - open Day risk). Open risk of a
   position = shares x (d' + A) until closed, even after a stop move; a partial reduces it only by shares sold.
5. Scale-out (owner's request): 1/3 at +2R', 1/3 at +3R' (R' = d'), rest by the normal exit rules; no breakeven
   move, no trail. No scale-out scored higher in the replay; the owner may drop it.
6. Missing/stale ATR or spread -> d' = S. A position whose stop is not confirmed resting is flattened.
Replay of the final mechanics: in-sample n 12 +$255.53 (no scale-out +$326.08); walk-forward n 5 +$160.62
(no scale-out +$167.04); same band at today's sizing +$57.97 / +$16.45; all 32 trades as traded -$12.92.
Worst day -2.93% of equity (TSLA 10-15); no day reached the 7% kill. Thorp seat: 1R = 6%/1.77 = 3.4% of equity,
about 0.18 of full Kelly on the band's R (mean +0.32, sd 1.29, t about 0.9) — not significant after 9 variants;
reversal = rolling 20-trade band mean R <= 0 (weekly recalibration) or any loss above 1.2 x its logged budget.
Not adopted (cut trades or add restrictions): GAI's spread > 10%-of-stop entry filter, sector halving, mean-R < 0.1
halt. Dissent noted: the Taleb seat wanted all tiers' realized losses in the 7% term and preferred no scale-out;
the majority (Thorp seat, Gro, GAI, prior item G) kept Day-tier losses.
Forward pass (separate diff): budget = min(6%, 0.25 x rolling band Kelly); targets from rolling quantiles of
the band's best move in R. Per-trade log: S, d', binding term, A, G, budget used, shares, band, band mean R.

## Gate-compliant revision (2026-10-19, Claude) — aligned 4/4, awaiting the owner's re-approval
The owner approved the section above on 2026-10-19 ("Approve conv 1018"). It cannot ship as written: the standing
no-static gate (.claude/preship/no_static_scan.py, owner mandate 2026-10-14) rejects every new numeric literal.
Probed: `DAYTRADE_CONV_OPEN_RISK_PCT = 0.06`, `S / 2` and `np.quantile(x, [0.5, 0.75])` are rejected. Revised so
that every number is an existing (grandfathered) setting or measured from data:
1. Entries only in the conviction band with positive mean R (terciles of completed Day trades, recomputed weekly).
2. Budget = DAYTRADE_TIER_KILL_EQUITY_PCT (5%, existing) x equity - today's realized Day losses - open Day risk.
   A 6% budget above the Day tier's own 5% force-flat was incoherent (all four voices).
3. S = today's stop distance. d' = min(max(DAYTRADE_MIN_STOP_ATR_MULT x ATR5, DAYTRADE_MIN_STOP_SPREAD_MULT x 5-min
   median IEX spread), S): tighten to the noise floor the bot already measures, never wider than S. Missing,
   stale or non-positive ATR5 or spread -> d' = S (Thorp seat: an all-zero floor would otherwise divide by zero).
4. Gap allowance A = max(G x S, DAYTRADE_MIN_STOP_ATR_MULT x ATR5); G = pooled worst-ever overshoot/stop ratio over
   all Day stop-outs (0.77 now), ratchets up only (GAI's rolling-20 window rejected: it loosens after a tail).
5. Jump charge (Taleb seat): per-share risk = max(d' + A, price x J), J = pooled worst adverse 1-minute move in the
   Day tier's own IEX paths (measured 0.93%), ratchets up only.
6. shares = floor(remaining budget / per-share risk); then the existing account gross cap (MAX_GROSS_EXPOSURE_RATIO
   2.5x) and buying power cut SHARES, never the stop. Open risk per lot = shares still open x per-share risk, kept
   until the lot is closed (a stop move or partial reduces it only by shares sold).
7. Scale-out: 1/(number of targets + 1) = 1/2 at DAYTRADE_RIDE_TARGET_R (2R'); the rest exits by the normal rules.
Replay (read-only, 32 completed Day trades, 1-minute IEX paths, 5% budget, 2.5x gross; >12 variants tried):
   half at 2R: in-sample n 12 +$231.85 (log-growth +0.086), walk-forward n 5 +$117.94 (+0.042);
   no scale-out: +$269.23 (+0.101) / +$116.98 (+0.042); band at today's sizing +$57.97 / +$16.45;
   worst day -1.85% (TSLA 10-15); no kill day; the J charge did not bind on these trades.
Voices: Thorp seat, Taleb seat, Gro, GAI all APPROVE-WITH-CHANGES; every change is in items 1-7. Not adopted:
Gro's 95%-of-budget watchdog (an unrequested process), GAI's rolling G. Gro's "max loss <= budget" proof was
wrong (it omitted the gap allowance and misread S) and is not relied on.
Forward pass (separate diff): budget = min(Day-tier kill, fractional Kelly on the band's rolling R); target from the
band's maximum-favourable-excursion distribution; J regime-aware (VIX-scaled).
Q2 (revised): the band filter takes 12 of the 32 replayed trades (owner-directed: entries only in the proven band).
Q5 (revised): no new number; every value is an existing setting or measured (G, J, ATR5, spread, band R).

## Owner decision beyond this design (not in CONV-1018)
Letting EVERY trade reach full intended risk now (lifting the fixed caps for m = 1 too) would have turned -$12.92
into -$94.16 on the same 32 trades (worst day -3.60%). Not recommended until a family earns it; available as a
separate approval if the owner wants more size now on this paper account.

## Required questions
Q1: Moves the $25K goal by putting size where the bot's own record proves edge; costs nothing where it does not.
Q2: No change to the number of trades (m is floored at 1; no new gate).
Q3: Simplest safe form of the owner's concentration request; restriction is not the answer — earned size is.
Q4: Builds on the Confluence 2.0 family registry/router (family_id already logged on entries) and the stop-distance sizer in day_trade_manager; no duplicate.
Q5: m, the shrinkage strength, the gap ceiling and the ADV share are derived from fills and bars; only half-Kelly (0.5) is a standard fraction.
Q6: Exposes the static assumption "conviction means size"; the forward build is per-family edge-earned size.
Q7: Feeds sizing from realized family edge; feeds the weekly belief report; falls back to m = 1 on missing data.
Q8: All figures above come from the read-only replay this session; gap-through behaviour is measured per name before use.
Q9: Walk-forward replay on the 32 trades: m = 1 throughout today (-$12.92); reversal: a family upsized whose realized R turns negative.
Q10: No validated edge is being held back; none qualifies yet.
Q11: Risk-path (size); board incl. the masked-loss seat reviewed it.
Q12: Stops, the 7% kill, never-mask-a-loss and paper=True unchanged; the book bound keeps stressed gap loss inside the kill.
Q13: Missing or stale data -> m = 1 and today's caps (fail safe).
Q14: Not a shadow: live from day one, with m = 1 until a family earns more.
Q15: Every entry logs the full sizing stack listed in design item 3.
Q16: Log line "earned_size family=... m=..." on each Day entry; audited against the first full session.
Q17: Run read-only on OCI against real fills and bars before calling it live.
Q18: The 0-for-8 top-conviction result is treated as a finding (score mis-ranks), not noise explained away.
Q19: Changes sizing: the owner approves this exact design before any commit.
Q20: Prompts carried facts only (bias gate enforced); reviewers' false claims (a non-existent back-test) were discarded.
Q21: Tests mock every broker call; no test reaches the broker.
Q22: The new bounds run read-only on OCI against the real ledger before live.
Q23: Full-path simulation on the 32 trades before calling it ready.
Q24: N/A — no instrument-availability claims.
Q25: Gap quantiles and ADV measured from a year of Alpaca bars, never quoted.
Q26: T1 Alpaca fills and bars; fallback is m = 1 with today's caps.
Q27: Per-family stats written atomically to data/state/earned_sizing.json; decisions to logs/day_tier_events.jsonl.
Q28: Inside the Day-tier wire-time sizer in execution/day_trade_manager.py, before the broker call; stats refreshed by run_day_tier.py at startup.
Q29: Owner's top-ranked open item after the article rules.
Q30: Adds what a quant desk requires before concentrating: edge-earned sizing and gap/correlation stress bounds.
Q31: Walk-forward only; one design variant; the lower-bound gate is the significance control.
Q32: Uses only trades completed before each entry; no look-ahead.
Q33: R uses actual fill prices, so spread and stop slippage are included.
Q34: Per-family R is market-neutral in intent; beta is tracked by the weekly belief tests.
Q35: Book stress bound at correlation 1; worst replay day reported.
Q36: ADV share cap keeps thin names small at any account size.
Q37: With R spread near 1, about 100 trades per family are needed to resolve a 0.2R edge; the interval shows this live.
Q38: A family's lower bound below zero over its history means no extra size; "non-working" for trading stays the owner's call.
Q39: Yes — half-Kelly on the family's measured R from Alpaca fills.
Q40: Outcome: no Day position loses more than its stop-implied budget plus its measured gap allowance; machine check: weekly comparison of each realized loss to its logged bound.
Q41: An expert would add the gap/correlation stress bounds and size by family edge — both included.
Q42: Rawest data: Day-tier fills and stops (day_tier_events.jsonl) and Alpaca bars.
Q43: Professional method: fractional Kelly on shrunk estimates with stress limits.
Q44: Independent evidence: the weekly belief test and the first-session replay, separate from the sizing code.

## Prior work
- strategy/day_tier_family_router.py and strategy/day_tier_family_admissions.json (Confluence 2.0)
- logs/design_records/day_tier_bp_sizing_2026-09-08.md (buying-power sizing)
- logs/design_records/day_tier_mechanism_foundation_build_2026-10-05.md
- logs/design_records/article_rules_2026-10-18.md (belief tests)
