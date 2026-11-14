# QHM-OVERWEIGHT-1113 — let a top-conviction QHM name go above its normal weight, funded by unused QHM budget, with profit targets before earnings

Approval-ID: QHM-OVERWEIGHT-1113
Covers: execution/quarterly_hold_manager.py, tests/test_qhm_overweight_1113.py (new)
Not changed: execution/tier_capital_allocator.py, execution/broker.py, config.py
Status: rev 2 (signed: Claude, 2026-11-13): reviewer changes folded in; for confirmation.

## Owner direction (2026-11-13, verbatim)
"We also need to figure out when and how to overweight qhm holds above the 40% allocation per stock. My solution is to
redistribute the available capital from another stock (that hasn't been purchased yet) and count it towards the overall
qhm allocation. That, of course, means targets need to be identified to clearly take profit at certain levels ahead of
earnings."

## Today (code, main e5b6b291, execution/quarterly_hold_manager.py:92-93)
- `_QHM_AGG_CAP_PCT = 0.40`: all QHM new money <= 40% of equity (aggregate).
- `_QHM_PER_NAME_CAP_PCT = 0.20`: any one QHM name <= 20% of equity.
- Each hold has `target_equity_pct`; dip-adds may reach target x `_DIP_ADD_CEILING_MULT` (:3830-3842).
So the 40% is the whole tier; the per-stock limit is 20%. Room left by a pick that has not been bought yet sits unused.

## Change
1. Unused budget is lendable: room = 40% x equity - QHM market value - reserved room for picks approved but not yet
   bought (reserved only while their entry is live; released when it expires). Overweight draws only from that room.
2. Who may overweight: a hold whose conviction is the tier's highest AND whose trend is intact (close above its 50-day
   average, from Alpaca daily bars). Its ceiling rises from target to min(Q-OWNER ceiling, target + lendable room).
3. Every overweight share carries a profit target set before the next earnings report:
   - target 1 = the name's 75th-percentile 20-session return from its own 2 years of daily bars, applied to the average;
   - target 2 = the 90th percentile; the overweight shares (not the core) are sold in halves at T1 and T2;
   - and in the session before earnings, any overweight shares still held are trimmed back to target weight (the
     existing `_maybe_earnings_trim` path), so the extra size never rides an earnings gap.
4. When a reserved pick is bought, the overweight is not force-sold: new adds to the overweight name stop, and the
   targets bring it back down.
5. Stops: overweight shares share the hold's stop (QHM-RESTOP-1112 recalculates it on each add).
6. Every overweight add, target fill and trim-back: BLUF Slack line + trade_events row (event qhm_overweight).

## Q-OWNER (needs the owner's choice; board recommendation to follow)
How high may one name go? (a) 30% of equity, (b) 40% (the whole tier in one name), (c) derived: the per-name ceiling
that keeps that name's 99th-percentile one-day loss within the account's 7% kill-switch amount.

## Rev 2 — changes from the board seats (Thorp, Harris), GAI and Gro (2026-11-13). Supersedes "Change" where they differ.
- Reservations reconciled every cycle (GAI): before lendable room is computed, reservations for picks whose entry has
  expired or been cancelled are released, so no ghost reservation can over-allocate.
- Ceiling (Thorp, Harris and GAI all recommend a derived ceiling): ceiling = the smallest of
  (1) target + lendable room (the 40% tier cap still binds);
  (2) 0.25 x Kelly for the name from the tier's own fills (the replay prior until enough fills exist);
  (3) the weight at which the loss at the stop plus the name's measured 99th-percentile gap past the stop equals
      config.MAX_DAILY_LOSS_PCT x equity.
  Note (Thorp): the 7% kill switch does not count QHM losses (Invariant 6), so limb (3) is the protection, not the
  switch. Thorp also proposes a first ceiling of 30% that the limbs can only lower; that is a fixed number, so it is
  the owner's choice (Q-OWNER).
- The per-name cap (GAI rev-2 confirmation): for the one name that qualifies to overweight, the derived ceiling
  replaces `_QHM_PER_NAME_CAP_PCT` (20%) in the cap gate (`quarterly_hold_manager.py:8578`) for that name only;
  every other QHM name keeps the 20% cap unchanged. The trade_events row names which limb (1, 2, 3, or the owner's
  start ceiling) set the ceiling that cycle.
- Overweight shares are tracked as their own lot (GAI, Thorp): their cost, stop, targets and P&L are logged separately
  from the core, so a losing overweight lot is never hidden inside a winning core.
- Targets (Thorp, Harris): T1/T2 are held by QHM and executed through the trim path (no resting sell next to the stop),
  set off the overweight lot's own cost, from non-overlapping 20-session returns of the name's own 2 years of bars.
  The stop is resized to the remaining shares in the same step.
- Earnings (Thorp, Harris): the trim-back runs in the last market-hours session before the report, including a
  pre-market report the next day; an unconfirmed or missing earnings date is treated as imminent (trim).
- A name with a negative HOLD-NEWS-REVIEW-1113 decision this quarter is not eligible.
- Q12 corrected: the kill switch excludes QHM; the per-name loss bound is limb (3).

## Required questions
Q1: Puts idle QHM budget to work on the highest-conviction name; growth-aligned.
Q2: More QHM buys on the top name; none removed.
Q3: Should exist (owner). Simplest: lend the unused aggregate room; keep the 40% tier cap.
Q4: QHM caps (2026-09-06/27), dip-add ceiling, earnings trim, tier capital allocator; no conflict (allocator unchanged).
Q5: Targets from the name's own return distribution; ceiling Q-OWNER (c) is derived.
Q6: Static assumption: "every QHM name gets the same weight band". Forward: conviction-weighted QHM sizing (Kelly per name).
Q7: Feeds QHM size and exits; conviction score is its input.
Q8: Today's room: measured at build from live state; [hypothesis] QHM has unused budget (GOOGL and NVDA held).
Q9: Replay: QHM history 2026-08..11 with the rule; expected more P&L on winners, trimmed before each earnings date.
Q10: Not applicable because no validated signal is being held back; this adds sizing room.
Q11: Raises per-name size -> risk-path: board + masked-loss seat.
Q12: Stops on every share; 40% tier cap and kill switch unchanged; per-name ceiling rises (owner choice).
Q13: Bars missing -> no overweight; conviction unknown -> no overweight.
Q14: No shadow: overweight adds, targets and trim-backs execute live from the first eligible cycle.
Q15: trade_events row with room, ceiling, targets, conviction.
Q16: "[change=qhm-overweight-1113] <SYM> overweight to <x>% (room $y); targets $t1/$t2; trim-back before <date>".
Q17: First overweight add, checked against the broker and the ledger.
Q18: Not applicable because no anomaly is being explained away; the idle budget is the observed gap.
Q19: Changes QHM sizing and exits: owner approves this exact design.
Q20: Prompts neutral; board + GAI next.
Q21: Tests mock the broker.
Q22: Read-only room calculation on the server first.
Q23: Replay + unit tests before ready.
Q24: Not applicable because the design makes no claim that an instrument does not exist or is untradable.
Q25: Return percentiles measured from 2 years of Alpaca daily bars.
Q26: T1 Alpaca bars and positions.
Q27: QHM state (quarterly_holds.json), atomic as today.
Q28: QHM add path in its cycle.
Q29: P1 after HOLD-NEWS-REVIEW-1113.
Q30: Blind spot: unused tier budget earns nothing.
Q31: Percentile targets are not fitted; replay is small (one quarter of QHM history) — disclosed.
Q32: Percentiles use bars before each decision only.
Q33: Existing order paths.
Q34: Concentration raises single-name beta; disclosed in the page.
Q35: Worst case: an overweight name gaps on news; HOLD-NEWS-REVIEW-1113 and the pre-earnings trim-back bound it.
Q36: Large caps only (QHM universe).
Q37: Too few QHM trades to estimate an edge; sized by conviction and capped.
Q38: Reversal: overweight trades losing to the core over two quarters.
Q39: Conviction-led; Kelly per name is the forward build.
Q40: Outcome: overweight shares never held into an earnings report; machine check each session before earnings.
Q41: Conviction-weighted Kelly across all QHM names.
Q42: Bars, fills, QHM state.
Q43: Conviction-weighted concentration with event-risk trims, as concentrated long-only managers run.
Q44: Broker records after the first overweight round trip.
