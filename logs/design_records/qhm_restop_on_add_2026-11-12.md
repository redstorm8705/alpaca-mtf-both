# QHM-RESTOP-1112 — every QHM add recalculates the stop from the new average cost

Approval-ID: QHM-RESTOP-1112
Covers: execution/quarterly_hold_manager.py, tests/test_qhm_restop_1112.py
Not changed: execution/broker.py, execution/ownership_guard.py, config.py, data/fetcher.py
Status: rev 1 (signed: Claude, 2026-11-12), for board + GAI review.

## Owner direction (2026-11-12, verbatim)
"When new shares are added manually or otherwise, a new stop needs to be recalculated automatically."
Context the same morning: "I don't want nvda to sell at the open. I want to average down." and "We now own 5 shares
at a 173.50 average. The stop needs to be updated to reflect that new cost average."

## Problem (measured)
- QHM's add paths only ever RAISE the stop: tranche/dip-adds pass `raise_stop_to=_design_stop_price(new avg)` and
  apply it only when `pos.stop_price < raise_stop_to < live_price * 0.98` (quarterly_hold_manager.py ~4466); the
  owner-buy adoption uses `max(old stop, design stop)` (~7426). Averaging DOWN therefore keeps the old, higher stop.
- NVDA 11-12: average fell to $173.51 after the owner bought 2 at $159.30 (Alpaca fill 85ef4ccc, 13:20 UTC), but QHM's
  stop stayed $163.07 while NVDA traded $159.22 pre-market. Re-placed at 01:41 PT (order QH-NVDA-s-1794474520479), it
  would have triggered at the open and sold 2 shares the owner was averaging into. The owner cancelled it in the app;
  Claude then set QHM's stop price to $147.48 by hand (state edit + restart, owner-directed), which is
  `_design_stop_price(NVDA, 173.506)` = max(173.506 - 2.5 x 16.47, 173.506 x 0.85) = $147.48.
- Shares added outside a QHM path (the owner's 2, the 11-09 orphan) are not covered by QHM's stop until an adoption
  path books them; QHM-ADOPT-1111 (in review) and the owner-buy adoption are those paths.

## Change
After every CONFIRMED increase of a QHM hold's share count — a QHM tranche/dip-add fill, a completed owner-buy
adoption, a proven-share adoption (QHM-ADOPT-1111) — QHM recalculates the stop and resizes its own stop to cover
every share it holds:
1. cost stop = `_design_stop_price(symbol, new average)` (existing derived formula: 14-week ATR x the existing
   multiple, floored at the existing hard-floor percentage; no new number). New average = Alpaca's avg_entry_price for
   the position when QHM holds every broker share, else QHM's own qty-weighted average of its fills.
2. Market guard: a sell stop at or above the live price would sell at once. If cost stop >= live price, use
   price stop = `_design_stop_price(symbol, live price)` instead (same formula on the live price; always below it).
3. Winner guard (owner rule: never a stop below entry on a well-in-profit lot): when the live price is above the new
   average AND the current stop is above the result of 1-2, keep the current stop (no loosening on a winner).
   Otherwise the result of 1-2 replaces the stop, up OR down. Averaging down is the case that lowers it.
4. The resize goes through the existing in-place replace (`replace_stop_order`), confirmed by the replaced order's own
   remaining quantity and stop price, before `pos.stop_price` is saved. Unconfirmed -> the old stop stays, the hold
   goes to the existing coverage-pending path, retry next cycle. ATR unreadable -> `_design_stop_price` falls back to
   the hard floor (existing behaviour); live price unreadable -> no change this cycle.
5. Logged: "[change=qhm-restop-1112] QHM <SYM> stop recalculated after add: avg <old>-><new> stop <old>-><new>
   (cost <c> price <p> rule <cost|price|winner-keep>) qty <n>", plus a trade_events.jsonl row (event qhm_restop).
Replaces the raise-only rule in the tranche path and the max(old, design) rule in the owner-buy path.

## Rev 2 — changes requested in round 1 (board Harris, board risk seat, GAI), folded in
Rules 1-3 above are replaced by this order of steps (the code follows it exactly):
a. Share count = QHM's own count (`_strict_resync_qty`), never the broker net (F6 / Day shares never go under QHM's
   stop; Harris 5). Broker long below QHM's count -> no restop, coverage-pending path (GAI 4).
b. Average = QHM's own QH-tagged buy fills plus the fill prices of owner-buy orders proven by the owner-buy adoption
   and shares proven by QHM-ADOPT, weighted by quantity; Alpaca's position average only when no other tier holds the
   symbol (Harris 4). Tranche path: the actual average after the fill, not the projected one (Harris 6).
c. Live price: in regular hours the position's current price; outside them the latest T1 trade, taking the lower of
   that and the last regular-hours price; never `lastday_price`. No fresh price -> no restop this cycle (Harris 2).
d. Margin m = the symbol's median absolute overnight gap over the last year of T1 daily bars (open vs prior close),
   times the live price. A stop is "valid" only when it is below live - m (Harris 3; replaces the static 0.98).
e. cost stop = `_design_stop_price(avg)`. If valid -> candidate = cost stop.
   Else price stop = `_design_stop_price(live)`, and candidate = max(price stop, old stop if the old stop is valid):
   the market guard never lowers a stop that is still valid (risk seat 1, GAI 3).
e2. The price stop must itself be valid (below live - m); if it is not, no change and a CRITICAL page (GAI round 2).
   With e2 the fallback can only come out BELOW the cost stop: it is used only when the cost stop is at or above
   live - m, and it is required to be below live - m. So the guard never tightens the stop beyond the cost-based
   level; it can only move it further from the market.
   On the owner's direction ("a new stop needs to be recalculated automatically" / "reflect that new cost average"),
   a recompute after an add may move the stop UP as well as down (e.g. old stop $120, new cost stop $147.48 with
   live $150 and m about $1.50): the result is always below live - m, so it never sells the shares at once. GAI round
   2 asked for "never higher on an average-down"; that conflicts with the owner's stated rule and is not adopted.
f. Winner: when live - avg >= one 14-week ATR (a data-defined "well in profit", Harris 1) and the old stop is valid
   and above the candidate, keep the old stop (no loosening on a winner). An old stop that is not valid is never kept.
g. No-op when the candidate equals the old stop to the cent (GAI 1; no tolerance number).
h. Apply: tranche path through the existing `_restore_or_pending` resubmit (the stop was cancelled before the buy);
   otherwise the in-place `replace_stop_order`. Confirm the NEW order's id, remaining quantity and stop price before
   saving `pos.stop_price` (Harris 7). Unconfirmed -> old stop stays, coverage-pending path, which pages (risk seat 5).
i. Visible widening: the log line and the qhm_restop row carry the dollar risk at the stop, qty x (avg - stop), before
   and after, and every downward move sends one HEADS-UP Slack line (risk seat 2).
j. Cannot compute (ATR, price or average unreadable) while the old stop is NOT valid -> CRITICAL page to the owner
   (the 11-12 near-sale case), no change (risk seat 3).
k. Bound test: three bot dip-adds leave the stop at or above first entry x 0.85 x 0.85 (risk seat 4).
Tests: the Harris inputs ($190 avg, add 10 at $150, live $160 pre-market -> no stop above live), a pre-market price
below the prior close with only lastday_price available, a partial tranche fill, NVDA with an F6 claim in the symbol,
the risk-seat input (avg $173.51, old stop $135, live $140 -> stop stays $135), and the bound test.

## Build (rev 3, after two cold-review rounds; owner-approved rev 2)
- Implemented as one per-cycle pass (`_maybe_restop_after_add`, before the stop reconcile in the weekly check): when
  QHM's share count rises above `restop_qty` (the count the stop was last recalculated at), the stop is recalculated
  once by steps a-k; a first sight or a decrease only re-baselines, so nothing changes for existing holds on deploy.
  The tranche path's raise-only rule and the owner-buy max(old, design) still run first; the pass follows next cycle.
- Step f (round-2 review, owner rule "never a stop below entry on a well-in-profit lot"): a lot in profit (live above
  the average) keeps a valid higher stop; the one-ATR "well in profit" test was dropped because it let a modest winner
  be lowered.
- Failed save after a confirmed resize: memory keeps the broker's new stop price and id (the next reconcile matches it),
  only the recalculation re-arms; the lowering Slack line is sent either way.
- Never above the market (round-2 review, lost replace reply): the per-cycle stop reconcile never moves a QHM sell stop
  to a price at or above the live price; if the recorded price is there and the broker's own QH stop rests below the
  market, the record adopts the broker's price and the owner gets one HEADS-UP line.
- Step c (extended-hours price): not needed in this build because the pass runs only inside the weekly check, which
  runs only in regular hours (strategy/run_cycle.py:568); recorded so a future move outside RTH adds it.

## NVDA after deploy (expected)
Already 5 shares at $147.48 (set by hand 11-12 at the owner's direction): the pass baselines at 5 and changes nothing.

## Required questions
Q1: Lets the owner average down QHM names without the bot selling the shares he is adding to; more held exposure at a
deliberate cost basis on a paper account (growth, not preservation).
Q2: No change in the number of trades.
Q3: Should exist (owner direction). Simplest: reuse the existing design-stop formula at the add points that already
compute it, and drop the raise-only restriction.
Q4: Extends QHM-COVER-1109 (stop-safe add), the owner-buy adoption, QHM-ADOPT-1111 (PR 952). Conflicts with the
raise-only comment at ~4464 ("Never loosen the prior stop"), which this replaces by owner direction.
Q5: No new number: the stop comes from the 14-week ATR (rolling data) and the existing multiple and floor constants.
Q6: Static assumption exposed: "a QHM stop only moves up". Forward build: an owner stop override (owner sets a stop for
a symbol until a date) and an average-down mode that buys QHM dips within the cap.
Q7: Feeds the stop only; the unmanaged-position alarm goes quiet when the stop covers all shares.
Q8: NVDA 11-12 orders and fills above (Alpaca), mtf_bot.log 13:23 "NVDA stop coverage unresolved".
Q9: Replay NVDA 11-06..11-12: each add recomputes; 11-12 avg $173.51 -> $147.48 (computed on the server this morning).
Wrong if a recompute ever places a stop at or above the live price, or lowers a stop on a lot in profit.
Q10: Not applicable because this is an exit-protection rule, not a held-back signal.
Q11: No size or frequency change; the stop level of an existing hold moves (risk per share changes), so risk-path:
board + masked-loss seat.
Q12: Keeps a stop on every QHM share (it now covers adopted shares too); lowering a stop on averaging down widens the
per-share risk by owner direction, bounded by the existing ATR/floor formula; kill switch untouched (QHM excluded).
Q13: ATR unreadable -> hard floor; price unreadable -> no change; resize unconfirmed -> old stop stays + pending.
Q14: No: it acts on the first add after deploy.
Q15: The [change=qhm-restop-1112] log line and the trade_events row carry every input and the rule chosen.
Q16: Proof line: "[change=qhm-restop-1112] QHM NVDA stop recalculated after add".
Q17: NVDA's next adoption on OCI; read the Alpaca stop after it.
Q18: The 11-12 near-sale is the anomaly; investigated from orders, fills and logs.
Q19: Changes exit logic: owner approves this exact design before any commit.
Q20: Reviewer prompts carry the code and facts only.
Q21: Tests mock every broker call.
Q22: Read-only server run of the recompute for NVDA before live.
Q23: Unit tests plus the read-only server run.
Q24: Not applicable because no availability claim is made.
Q25: ATR measured from T1 weekly bars (16.47 for NVDA on 11-12).
Q26: T1 Alpaca weekly bars via data/fetcher.py; Alpaca position for the average; fallback as in Q13.
Q27: data/state/quarterly_holds.json via QHM's existing atomic save.
Q28: Inside the existing add/adoption completion paths on the trading thread; one weekly-bar fetch (already made by
those paths).
Q29: P0 by owner direction; the old rule nearly sold an averaged-down position at the open.
Q30: Blind spot: exit rules that ignore the position's cost basis after an add.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because no replay of returns is involved.
Q33: Not applicable because no trades are added.
Q34: Not applicable because no new exposure.
Q35: High-VIX weeks widen the 14-week ATR and so the stop distance (existing formula behaviour).
Q36: Not applicable because no sizing change.
Q37: Not applicable because no statistic is estimated.
Q38: Reversal: a recompute that sells shares at once, or loosens a winner's stop.
Q39: Not applicable because it sizes nothing.
Q40: Protects "a stop on every QHM share at the cost-based level": machine check = after each add, the Alpaca QHM stop
covers QHM's share count at a price below the live price (logged).
Q41: Tie the stop to the position's real cost and volatility at every change in size, as a portfolio manager would.
Q42: Alpaca fills, positions and orders; T1 weekly bars.
Q43: Volatility-scaled stops recomputed on each change in position.
Q44: The Alpaca order record after the first live add, read independently.
