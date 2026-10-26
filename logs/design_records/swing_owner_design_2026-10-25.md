# Swing tier — owner design: top 50, top-10 priority, 10 slots, breakout must hold — 2026-10-25 (signed: Claude)

Approval-ID: SWING-OWNER-1025
Covers: execution/swing_breakout_manager.py, config.py, CLAUDE.md, tests/test_swing_owner_design.py, tests/test_swing_breakout_manager.py, research/swing_owner_prior.py, research/swing_owner_prior_2026-10-25.json, logs/post_live/expect/swing-owner-1025.json
Owner numbers: config.py SWING_BREAKOUT_TOP_N=50; config.py SWING_BREAKOUT_MAX_SLOTS=10; config.py SWING_BREAKOUT_KELLY_FRACTION=0.25

## Why
Owner 2026-10-23: Swing universe top 50 with the top 10 given priority; at least 10 slots; never enter below the breakout
level (the breakout must be recovered). Goal: more trades (data collection) without giving up return.

## Replay (read-only; research/c2_swing_lab.load_panel — point-in-time S&P 500 + NDX membership, split-adjusted daily
bars, closed bars only, entry next open, 10 bps round trip, compounding; 2017-01-03..2026-09-25)
| Variant | Trades/yr | Avg/trade | CAGR | Max DD | Years up |
|---|---|---|---|---|---|
| A today (top 10, 4 x 20%, any open) | 30.6 | +2.56% | 14.65% | -22.2% | 8/10 |
| B owner (top 50 prio 10, 10 x 10%, recover) | 112.5 | +1.32% | 13.48% | -22.6% | 8/10 |
| C top 50 prio 10, 10 x 10%, any open | 104.0 | +1.43% | 13.69% | -21.4% | 8/10 |
| D top 10, 10 x 10%, recover | 42.5 | +2.80% | 11.39% | -13.9% | 8/10 |
| E top 50 prio 10, 4 x 20%, recover | 51.8 | +1.18% | 10.30% | -30.3% | 7/10 |
SPY buy-and-hold over the same window: 13.5%/yr. Honest read (board Taleb): most of the return is market beta; the
owner design's gain is 3.7x the trade data at about the same return and drawdown, not extra profit.
Risk at stop for a 10% slot (11,933 top-50 signals): median 0.49% of equity, p99 1.65%; 0.15% of signals exceed 2%
and are already cut by the tier's 2% risk-at-stop ceiling.

## Design
1. Universe: rank the widened pool by 60-day median dollar volume; trade the top 50; the top 10 enter first
   (priority), then the rest by rank. The fixed pool widens from 30 megacaps to the S&P 500 + NDX names that can reach
   the top 50 (from Alpaca T1 daily bars, refreshed daily).
2. REV 2 (owner 2026-10-25: "larger swing sizing with stops set for each stock based on their own data"; 3-seat board
   vote Thorp / Shaw / Taleb on Invariant #11): up to 10 lots, each sized by its OWN stop:
   shares = r x equity / expected loss per share, where expected loss per share = (entry - stop) x the tier's own mean
   loss multiple on stopped lots (fills; replay prior: gap-throughs make 33% of trades lose more than 1R), stop =
   entry - 2.5 x ATR14 with the stop distance floored at the stock's own rolling low percentile of ATR% (a stale or
   near-zero ATR can never inflate size). r = 0.25 x Kelly from the tier's closed lots (R from Alpaca fills FIFO;
   Kelly = max(0, mean R - SE) / var R); until 30 closed lots exist, the replay's R distribution (1,053 trades, mean
   0.183R, sd 1.517R -> 1.48% of equity). Capped by the existing 2% risk-at-stop ceiling; tighten-only r x (K / 1.9).
   Invariant #11: limb 1 (swing/day <= 100% of equity) no longer binds the Swing tier; limb 2 (total overnight <=
   K x equity, margin-call-derived, ceiling 1.9, block-only) is the Swing ceiling after QHM/F6/other notional.
   Gap check before each entry: the open Swing book plus the new lot, each weighted by its own empirical 99th-
   percentile one-day move, must fit the K-implied margin buffer; the new lot is resized down to a MINIMUM OF ONE
   SHARE (never a skip) and the resize is logged. No stepped or calendar-gated budget (owner rule; board).
   Replay at r ~1.5% / cap ~1.5x: 95 trades/yr, +1.49%/trade, CAGR 16.7%, maxDD -34.3%, worst Swing-book day -10.7%,
   8/10 years up (r 2% / 1.5x: 85/yr, CAGR 20.1%, maxDD -38.6%, worst day -11.0%).
3. Entry: a breakout signal (close > prior 55-day closing high and > SMA200) creates a pending setup at the breakout
   level. It is entered at an open at or above that level; an open below it waits; the setup is dropped when price
   closes below the prior 20-day low (the existing exit line). One entry per setup.
4. Exits unchanged (close below the prior 20-day low, 30 sessions, GTC stop at entry - 2.5 x ATR14). Each exit logs
   its Alpaca fill price and the slippage versus the stop level (board Taleb: a gap must show as the real loss).
5. Aggregate risk (board Taleb): the sum of risk at stop across open Swing lots is bounded by the K-limb room above
   (derived from the book's worst 5-session loss); every entry logs the aggregate.
6. Decision stack per entry/skip in trade_events.jsonl: rank, priority flag, slot %, risk at stop, aggregate risk,
   room, K, recovery state. Log tag `change=swing-owner-1025`.
Forward pass (separate, gated): shrink r when open lots are highly correlated (board Thorp); a per-stock stop multiple
fitted from the stock's own gap/excursion data instead of the fixed 2.5 x ATR (board Taleb).

## Review trail
Gro + GAI round 1: APPROVE-WITH-CHANGES with 5% slots (claim: a 10% slot risks >2% at the stop). Refuted with the
measurement above (median 0.49%, p99 1.65%); round 2 both accept 10% slots with the 2% ceiling. Their stepped-budget
suggestion is not adopted (owner rule; allocator disabled since 2026-10-02, so the binding bound is the room above).
Board Thorp + Taleb: APPROVE-WITH-CHANGES — reject the stepped budget, keep the recovery rule, add the aggregate risk
log and the exit gap-slippage log (adopted).
REV 2 (owner direction 2026-10-25): board vote on Invariant #11 — Thorp, Shaw, Taleb all APPROVE-WITH-CHANGES (limb 1
retired for Swing only; r = 0.25 Kelly from fills with an SE haircut; K-scaled r; stop-distance floor; gap check).
GAI round 3 APPROVE-WITH-CHANGES (size by expected realized loss incl. gaps — adopted as the tier's own loss multiple).
Gro rounds 3-4 REJECT on false premises (a $0.48 micro-cap, a "one-year" replay, a liquidation it read as adopted);
round 5 after counter-evidence APPROVE-WITH-CHANGES (one-share floor on the gap-check resize — adopted; a $10 minimum
is met by every top-50 name; fail-closed on missing data already holds; a regime gate flag would be a shadow).
Not adopted (owner rules / fixed numbers): fixed 0.25 x K reserve, K capped at 1.5x until $10K, soft limb halving at a
fixed 100%, forced Swing liquidation after an 8% day, a 5% kill above 1x, a 5-session read-only run.
CLAUDE.md Architecture Invariant #11 text is updated in the same ship (execution-governing doc -> final pre-ship pass).
Build (2026-10-25): the pool is the 77 current S&P 500 / NDX names that ranked top 50 by 60-day median $ volume on any of
the last 252 sessions (research/swing_owner_prior.py); the replay prior is research/swing_owner_prior_2026-10-25.json (1,053
trades, mean 0.183R, sd 1.517R, stopped-lot loss multiple 1.064). Entry also requires the live ask at or above the level
(never entered below it). Cold review fix: a lot that fills and is closed in the same scan (stop not placeable) uses the
setup's one entry; a close below the prior 20-session low on any bar since the setup was created drops it. Read-only
server run 2026-10-24: 77/77 names with bars, scan 26 s, XOM 3 sh / NOW 2 sh on the real book.

## Required questions (RQ-4)
Q1: More data per year (113 vs 31 trades) at about the same return and drawdown; serves data collection and edge evaluation.
Q2: More trades: about 3.7x today's Swing trade count.
Q3: Simplest form: the existing tier with a wider ranked pool, more smaller slots and one entry condition.
Q4: Builds on c2_swing_breakout_tier_2026-09-29 and swing_exit_unification_2026-10-13; no open PR touches the Swing tier.
Q5: Universe rank, room and K are data-derived; 10 slots and 10% are the owner's design numbers, bounded by the 2% ceiling and K.
Q6: Exposes fixed slot sizing; forward pass: fill-derived fractional Kelly with a correlation-aware slot size.
Q7: Feeds the shared overnight room (K) used by every tier; skips QHM/F6 symbols as today; fails to "skip + log".
Q8: All figures from this session's read-only replay and risk probe on the lab cache.
Q9: 2017-2026 incl. 2018, 2020 crash, 2022 bear; expected ~110 trades/yr at ~+1.3%/trade; reversal: live per-trade mean below zero over its first 100 trades.
Q10: No edge held back; this ships live after the gate.
Q11: Risk-path: concurrency 4 -> 10 lots, stop-based size (~1.5% risk/trade), Swing exposure up to the K limb (~1.5x); 3-seat board incl. the masked-loss seat voted.
Q12: Stops on every lot, the 7% kill, never-mask-a-loss and the K limb unchanged; Invariant #11 limb 1 retired for Swing by board vote.
Q13: Missing bars -> no new entries that cycle (existing fail-closed universe path); stale data never creates a setup.
Q14: Not a shadow: live on approval.
Q15: Full decision stack per entry/skip/exit (item 6).
Q16: `change=swing-owner-1025` lines; post-live audit of the first session's entries vs the replay rules.
Q17: Unit tests with a mocked broker for ranking, priority, slots, recovery and room; a read-only universe run on OCI before live.
Q18: The one-slot limit in the old record was stale (allocator off); re-checked in code, not assumed.
Q19: Changes Swing entry rules and sizing: owner approves this exact design before any commit.
Q20: Reviewer prompts carried facts; the 5%-slot claim was refuted with a measurement, not a re-roll.
Q21: All broker calls mocked in tests; the broker refuses under test.
Q22: Universe/ranking runs read-only on OCI against live bars before go-live.
Q23: Full-path test: signal -> pending -> recovered open -> entry -> stop -> exit, mocked broker.
Q24: No absence claim; every pool symbol is checked against Alpaca daily bars.
Q25: Volatility and stop distances measured from 10 years of daily bars (lab cache).
Q26: T1 Alpaca daily bars (lab cache for the replay; live via data/fetcher); fallback = no new entries that cycle.
Q27: Existing state file data/state/swing_breakout.json (atomic); decisions to trade_events.jsonl.
Q28: Main bot run_cycle, once per ET day from 10:05 ET, as today; exits on the existing ~15:50 ET check.
Q29: Highest-value open owner design with a finished replay.
Q30: Correlation of 10 megacap longs (a gap day hits every lot); addressed by the K room and aggregate-risk logging.
Q31: Five variants tried; walk-forward not run (fixed rules, no fitted parameters); informs the reversal criterion only.
Q32: Point-in-time membership, closed bars, next-open entry — no look-ahead or survivorship.
Q33: 10 bps round trip in the replay; live costs read from Alpaca fills after go-live.
Q34: Mostly beta at flat sizing (CAGR ~ SPY's); stop-based sizing adds return (16.7% vs 13.5%) at a deeper drawdown.
Q35: Worst drawdown -22.6% (2022/2020 regimes); highly correlated with QHM megacaps, which K counts.
Q36: Top-50 names are the most liquid US stocks; a $250-$2,500 slot is far below their volume.
Q37: ~1,100 replay trades; live per-trade mean resolves within its first ~100 trades for the reversal check.
Q38: Weekly per-trade mean from fills; non-working = live mean below zero after 100 trades -> owner review.
Q39: Yes: r = 0.25 x Kelly from the tier's Alpaca fills (replay prior until 30 closed lots).
Q40: Protects trade count and protection; check = weekly report of Swing trades/week, per-trade mean and max open risk from fills.
Q41: Correlation-aware, Kelly-sized slots (forward pass).
Q42: Daily bars and Alpaca fills.
Q43: Rule-based breakout with ranked universe and slot sizing bounded by risk and overnight capacity.
Q44: The first-session post-live audit and the weekly fills-based report.
