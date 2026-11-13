# QHM-OWNERBUY-1113 — owner "buy more" (market or limit) and quick commands; the stop is recalculated on every fill

Approval-ID: QHM-OWNERBUY-1113
Covers: scripts/qhm_owner.py (new), execution/quarterly_hold_manager.py, tests/test_qhm_owner_buy_1113.py (new)
Not changed: scripts/qhm_owner_trim.py, scripts/qhm_owner_stop.py, execution/broker.py, config.py
Status: rev 2 (signed: Claude, 2026-11-13): reviewer changes folded in; for confirmation.

## Owner direction (2026-11-13, verbatim)
"We should probably write a buy more (market) and buy more at a certain price (limit) as well as quick fire commands. All
existing stops need to be updated once filled."

## Change
1. One command, scripts/qhm_owner.py, with short verbs that write request files QHM executes in its cycle (same
   pattern as qhm_owner_trim.py: atomic, locked, the script never talks to the broker):
   - `buy NVDA 2` — buy 2 shares at market (marketable limit at the ask + the existing QHM add-path slippage cap).
   - `buy NVDA 2 @160 [--until DATE]` — held by QHM until the live ask is at or below $160, then bought through the add path (no order rests at the
     broker, see Rev 2); expires on the date (default: the QHM hold's review
     date, same horizon rule as QHM-OWNERSTOP-1113).
   - `trim NVDA 1` and `stop NVDA 147.48 --until DATE` — aliases for the existing owner trim and owner stop.
   - `cancel NVDA buy`, `show` (owner requests, open QH orders, the stop per name).
2. QHM places the buy through its existing add path (`_stop_safe_add`): the QHM caps (20% per name, 40% aggregate new
   money) and the tier allocator apply; a request that does not fit is reduced to what fits and paged ("bought 1 of 2:
   the 20% per-stock limit"), never silently dropped.
3. Stop on every fill (partial included): the shares are covered at once by resizing the existing QH stop to the new
   share count, then the stop price is recalculated by QHM-RESTOP-1112's rule for the new average (never raised through
   the market; an active owner stop wins).
4. Every request, fill and refusal: one BLUF Slack line and a trade_events row (event qhm_owner_buy).
5. Unknown symbol (not a QHM hold): refused by the script. A new QHM name stays the job of the QHM pick process.

## Rev 2 — changes from the board seats (Thorp, Harris), GAI and Gro (2026-11-13). Supersedes "Change" where they differ.
- Limit buys are held by QHM, not resting at the broker (Harris, Thorp): Alpaca rejects a buy while the QH sell stop
  rests (wash-trade guard, 2026-11-09 NVDA reject), and cancelling the stop to rest a buy would leave shares
  uncovered. So `buy NVDA 2 @160` is stored; each market-hours cycle checks the live ask; when ask <= 160 QHM runs the
  existing `_stop_safe_add` (marketable limit, short fill window, stop resized to the actual filled quantity, restore
  or pending). Expiry or cancel only deletes the request; no order is ever left at the broker.
- Market buys run in market hours only (`_stop_safe_add` defers when the market is closed); a request made after hours
  waits for the open and the page says so.
- Caps checked at execution, one request at a time (Gro C, Thorp): each request goes through the cap gate, which reads
  the live QHM total including any buy just filled, so several requests together cannot pass the 40% aggregate.
- Fresh quote (Gro G): the trigger and the order use a live two-sided quote from the same cycle; a stale or one-sided
  quote skips that cycle.
- Stop coverage (Gro A, B): `_stop_safe_add` already restores the stop or marks it pending, and QHM-RESTOP-1112 never
  places a stop at or above the market. After every fill the cycle checks broker QH stop quantity == QHM shares and
  pages on a mismatch.
- Messages use the actual filled quantity (Gro D): "bought 1 of 2; 1 still waiting".
- Kill switch (Gro F): QHM fills are outside the 7% daily kill switch by design (Architecture Invariant 6,
  KILL-ATTR-1023); each owner-buy page states the dollars at risk at the stop instead.
- Unknown symbol stays refused (Gro E): a new QHM name goes through the QHM pick process, not a buy command.

## Required questions
Q1: Lets the owner add to conviction holds quickly with the bot's own protection applied; growth-aligned.
Q2: Adds owner-directed trades; no bot trades removed.
Q3: Should exist (owner). Simplest form: request files + the existing add path, which already sizes, caps and stops.
Q4: Builds on qhm_owner_trim.py, QHM-OWNERSTOP-1113 (approved 11-13), QHM-RESTOP-1112, QHM-ADOPT-1111; no conflict.
Q5: Prices and quantities are the owner's; caps and the stop rule are the existing derived ones.
Q6: Static assumption: "only the bot initiates QHM buys". Forward: the same commands for F6.
Q7: Feeds QHM size, the allocator and the stop; no other path.
Q8: 11-12: the owner bought NVDA by hand in the Alpaca app; the stop had to be fixed by hand (QHM-ADOPT/RESTOP).
Q9: Replay 11-12: a `buy NVDA 2` request at 06:30 PT would have run through `_stop_safe_add` in the first market-hours
cycle (~$151) and resized the stop to 5 shares in the same step. Wrong if shares are ever uncovered after a fill, or
if an expired request leaves an order at the broker (tested).
Q10: Not applicable because no signal.
Q11: Changes size by owner direction inside existing caps -> risk-path: board + masked-loss seat.
Q12: A stop covers every share after every fill; caps unchanged.
Q13: Request file unreadable -> no buy, CRITICAL + page; quote stale or one-sided -> that cycle skipped (market and
held-limit requests alike); a partial fill leaves the remainder as a request, re-checked against the caps each attempt.
Q14: No shadow: owner buys execute live through the existing add path from the first request.
Q15: trade_events row with request, size after caps, fill, stop before/after.
Q16: "[change=qhm-ownerbuy-1113] QHM <SYM> owner buy <n> filled @ $x; stop $a -> $b for <m> sh".
Q17: First owner buy after deploy, checked against the broker's order record.
Q18: The 11-12 hand buy that left shares uncovered is the anomaly.
Q19: Changes QHM entries/sizing: owner approves this exact design.
Q20: Prompts neutral; board + GAI next.
Q21: Tests mock every broker call.
Q22: Read-only `show` on the server first.
Q23: Unit tests of market, limit, partial fill, cap reduction, expiry, cancel.
Q24: Not applicable because no availability claim.
Q25: Not applicable because no statistic.
Q26: T1 Alpaca quotes/orders through existing QHM calls.
Q27: data/state/qhm_owner_buys.json (atomic, locked).
Q28: QHM per-cycle pass on the trading thread, beside the owner-trim pass.
Q29: P1: removes the hand-buy / uncovered-shares step used on 11-12.
Q30: Blind spot: owner actions in the Alpaca app bypass the bot's stop logic.
Q31: Not applicable because no rule is fitted; quantities and prices are the owner's.
Q32: Not applicable because there is no replay of returns; execution uses the live quote of the acting cycle.
Q33: Costs are the existing add path's marketable-limit slippage cap; live fills are logged per buy.
Q34: Not applicable because no new return stream is claimed.
Q35: Not applicable because owner-directed buys are bounded in every regime by the existing stop and caps.
Q36: QHM names are large caps; a 1-5 share order is far below 1% of daily volume.
Q37: Not applicable because no statistical edge is claimed.
Q38: Reversal: any fill leaving shares without a stop, or a buy past a cap.
Q39: Size is the owner's, clamped by the caps.
Q40: Outcome: after every fill, broker QH stop qty == QHM shares; checked each cycle, paged on mismatch.
Q41: Extend to F6; a one-tap Slack command later.
Q42: Broker fills and live two-sided quotes, the rawest data for an order path.
Q43: Order-entry with automatic protective re-stop, as broker bracket logic does.
Q44: The broker order record after the first live owner buy.
