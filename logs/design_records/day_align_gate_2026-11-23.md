# DAY-ALIGN-1123 — the Day tier's FADE setup stops under the fade stop rule (it lost in both halves)

Approval-ID: DAY-ALIGN-1123
Covers: strategy/day_tier_alignment.py, run_day_tier.py, tests/test_day_align_gate_1123.py
Not changed: config.py, execution/day_trade_manager.py
Status: rev 4 (signed: Claude, 2026-11-23). Design only. Rev 4: Option B chosen (board Harris B, Brandt A+B;
reviewer #2 B); Option A dropped — with B in place A adds 6 RIDE trades netting -$0.58. Prior: rev 3 (fork). Rev 3: board (Harris +
Brandt) rejected rev 1-2 — `gate_ok` and DAYTRADE_ALIGN_GATE_BLOCKS cover more than alignment (they would also block
owner-approved Track B veto setups and watch-day shorts that were never alignment-tested); the evidence was regrouped.

## Problem (measured 2026-11-23 from the server logs and Alpaca bars)
- 2026-11-23 06:40 PT the Day tier (Track A) shorted 8 NVDA at 158.58; the stop bought them back at 165.97 at 06:58 PT:
  -$59.13 (day_tier_events.jsonl exit_fill). SPY closed Friday 690.68 and traded ~723 today (+4.7%).
- The side came from daily moving averages after Friday's sell-off ("bull 0/10 MAs"); the trigger was a with-trend
  FADE (a dip under the 160 call wall read as a "failed upside sweep"). The 2m/5m alignment check read "not aligned
  (ema2, ema5)" and was recorded only (CEO 2026-10-06 "the day tier must trade").
- The pre-registered fade stop rule (counter_trend_fades_ok in the Day alignment module: cumulative <= -$25 or 3
  consecutive losses stops new fades) applies only to COUNTER-trend fades; this was a with-trend FADE.

## Evidence (all 114 closed Day trades with a decision record, 2026-09-15 to 2026-11-23; P&L from exit fills)
| Group | Trades | Wins | Avg win / avg loss | Total | First half | Second half |
|---|---|---|---|---|---|---|
| FADE setup (all) | 31 | 10 | $6.25 / -$10.38 | -$155.43 | -$45.13 | -$110.30 |
| RIDE | 41 | 26 | $10.07 / -$10.22 | +$161.19 | | |
| PULLBACK | 32 | 12 | $9.51 / -$3.46 | +$0.94 | | |
| DRIVE | 10 | 4 | $21.91 / -$10.50 | +$24.58 | | |
| alignment read, not aligned | 11 | 5 | | -$59.82 | -$35.26 | -$24.56 |
| alignment read, aligned | 80 | 42 | | +$141.45 | +$21.01 | +$120.44 |
- Honest limit: today's NVDA short (-$59.13) is one of the 11 not-aligned trades; the other 10 net -$0.69. The FADE
  result does not depend on today: without it FADE is -$96.30 over 30 trades, negative in both halves.
- Candidates looked at: 3 (gate_ok, alignment-only, FADE). Halves looked at once each.

## Overlap (measured)
- Of the 11 not-aligned trades, 5 were FADE (covered by B) and 6 RIDE (net -$0.58). Option A adds almost nothing once B
  is on, so it is dropped.
- FADE trades by track: 21 Track A, 10 older records with no track field (Track B has no FADE trigger).

## Change (Option B)
- `counter_trend_fades_ok` (the Day alignment module) is extended into the FADE stop rule: it counts every Track A
  entry whose decision record has `trigger.mode == "FADE"` (with-trend and counter-trend), not only
  `trigger.counter_trend`. Track B decisions stay excluded, as today. The criterion is unchanged and pre-registered:
  cumulative realized P&L <= -$25 or 3 consecutive losses stops new FADE entries.
- `run_day_tier.py` Track A: every FADE entry is checked against that rule before sizing (today only counter-trend
  fades are); a blocked FADE is skipped through the existing `_log_direction_skip` with the rule's reason and
  `[change=day-align-1123]`.
- The rule has no window and no reset (it sums all history): on today's record (-$155.43) FADE stays off until a
  replacement FADE setup is designed and approved (a separate record, per the losing-test rule). Kill flag:
  `DAYTRADE_COUNTER_TREND_FADES_ENABLED` already gates the rule; no new flag.
- Unreadable event log: the rule already fails closed (no new FADE), unchanged.
- Tests: a with-trend FADE (the 11-23 NVDA decision) is blocked once FADE history fails the rule; a non-FADE trade
  (RIDE/PULLBACK/DRIVE) is unaffected; a Track B decision is not counted; the counter-trend path behaves as before.

## Required questions
Q1: Stops the one Day setup that lost money in both halves (FADE, -$155.43 over 31 trades) and keeps the setups that
make money (RIDE +$161.19, DRIVE +$24.58, PULLBACK +$0.94).
Q2: About 27% fewer Day entries (31 of 114 were FADE) until a replacement setup ships. They lost money; the owner's
2026-10-06 "must trade" order is partly reversed for this setup, so this needs the owner's approval.
Q3: Reuses the existing pre-registered fade stop rule; no new mechanism.
Q4: Reverses part of the 2026-10-06 CEO order; FADE redesign is separate; no open PR touches this block.
Q5: No new number; the rule's -$25 / 3-loss limits are the existing pre-registered values (fixed, not derived).
Q6: Forward: the side itself should adapt intraday (a gap-reversal day after a sell-off); a separate design.
Q7: Day tier entries only.
Q8: Measured: the 11-23 log lines and the 114-trade split above.
Q9: Replay: FADE (31 trades, -$155.43, both halves negative) is stopped, today's NVDA short included.
Reversal: each skipped FADE is logged with its trigger, stop and target; if the skipped FADEs over the next 20 would
have made money at the same size, the rule is revisited.
Q10: No: FADE is a measured losing setup, not a validated edge held back.
Q11: Trade frequency down, not up; size unchanged.
Q12: No safety-envelope change.
Q13: Event log unreadable: the rule fails closed (no new FADE), as today.
Q14: Not a shadow: it blocks live entries.
Q15: Each skip logs the gate reason with the decision record.
Q16: Proof: `[change=day-align-1123]` FADE-skip records on the first day a failed-check entry would have fired.
Q17: The first live session after deploy.
Q18: The anomaly closed: a short taken while the bot's own check said the tape was rising.
Q19: Entry change: owner approval of this record.
Q20: Neutral prompts; tests included.
Q21: Tests mock the broker.
Q22: Not applicable because it is not a new fail-closed control.
Q23: Replay of the 11-23 NVDA decision plus unit tests.
Q24: Not applicable because no availability claim is made.
Q25: SPY and NVDA moves measured from Alpaca daily bars.
Q26: T1 Alpaca bars (unchanged).
Q27: Writes the existing skip record.
Q28: run_day_tier.py tick, `_direction_block`, after the trigger and before sizing (unchanged code).
Q29: P0 from the owner today.
Q30: Blind spot: a check computed and logged but not acted on.
Q31: Split-half check only; small sample, stated.
Q32: P&L from real exit fills; no look-ahead (the gate result was recorded at entry time).
Q33: Real fills include costs.
Q34: Not measured; FADE trades both long and short, so it is not a simple market-direction bet.
Q35: Today was a +4.7% SPY rebound after a -3.5% day; the gate failed on it as designed.
Q36: Not applicable because the change only removes entries.
Q37: FADE 30 trades without today, about -$3.21 per trade, t about -2.6 (board estimate from averages); small but
negative in both halves.
Q38: The pre-registered fade stop rule is the owner-approved 'non-working' definition; FADE is now at zero trades
and needs a replacement design.
Q39: No sizing change; P&L from Alpaca exit fills.
Q40: Protects Day-tier P&L; check: the skip log's would-have P&L vs the taken trades' P&L.
Q41: Make the side intraday-aware (Q6).
Q42: Exit fills and the logged decision stacks.
Q43: Trend-alignment filter on intraday entries.
Q44: The next 20 entries vs the skip log.
