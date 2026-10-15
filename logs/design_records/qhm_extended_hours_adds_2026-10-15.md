# QHM extended-hours dip-adds — design record (Claude, 2026-10-15)

**Owner request (2026-10-15):** "Waiting until the open doesn't make sense if we're already in the QHM buy zone" —
let QHM (and Swing-and-higher) act pre-market / after-hours. Owner confirmed extended-hours limit orders work on the
account (no paper test needed).

## Front-loaded replay (Rule C)
1 year, real Alpaca SIP 5-minute bars incl. extended hours, 10 names (NVDA GOOGL GE GEV LLY MSFT AAPL META AMZN AMD).
Out-of-sample Rung A level per day; trigger = first extended-hours 5-min close at/below the level; buy = close +0.1%.
- 40 triggers (14 after-hours, 26 pre-market).
- Where the regular session would also have bought at the open (25): the open was cheaper in 76% (median 0.47%).
- **After-hours (14):** median 0.97% worse than the next open; 20-session forward median −0.34%, mean +1.24%.
- **Pre-market (26):** median +0.05% vs the open (better 50%); 20-session forward median +1.36%, mean +3.55%.
- Adds the regular session would never have made: 5 (3 after-hours, 2 pre-market: AMD 2025-11-04 −8.8%,
  GOOGL 2026-01-16 −4.9% 20-session forward).
- Spreads were not replayable (no historical quotes); the live spread check prunes further.

## Decision — Option A, pre-market only (4/4)
Board Harris + Thorp seats, Gro, GAI all voted A (pre-market only, after-hours off). Thorp: Kelly bets only a
non-negative edge; pre-market is price-neutral data at no measured cost, after-hours is a measured ~1% worse entry.
Harris: after-hours adverse selection is measured, not noise.

## Build
- `broker.extended_session_now(now=None, premarket_only=False)`: session edges from the Alpaca calendar row
  (session_open/open/close/session_close; SDK drops them, so the raw /calendar response is read). None on failure.
- `dip_zones` stores each name's `median_move` (median |1-session return|, past year); `typical_move_for()`.
- `QuarterlyHoldManager.run_extended_hours_check()`: pre-market only; per ACTIVE hold the same ownership proof
  as regular hours (stop-coverage reconcile from one order-book snapshot, external-close detection, strict qty
  resync), then `_maybe_dip_add(..., extended=True)`: fresh IEX print (data.live_price) instead of the position
  mark; `_extended_quote_ok`: spread ≤ own median daily move and the print inside the quote; `_stop_safe_add`
  with `extended_hours=True`. Stop cancelled around the add is inert pre-market anyway; re-placed for the held qty.
- `run_cycle` closed-market branch calls it, blocked by the kill switch / control fault.
- All QHM caps unchanged (20%/name, 40% aggregate, 1.375× ceiling, 3/quarter, 2 days apart, earnings blackout,
  −15% first-entry floor, overnight room, RegT BP).
- Gap-risk sizing check (BGGN item): measured 1-year overnight-gap 95th percentile across the 36-name universe
  (median 2.73%, max DRAM 8.04%). At the 20% per-name cap a 95th-pct gap costs ≤ 1.6% of equity, inside the 7%
  budget, so no extra cap was added.

**Size / frequency / concurrency delta:** size 0; concurrency 0; frequency up (adds can fill pre-market) →
risk-path, board gate run above.
**Reversal criterion:** if after the first live month the pre-market adds' fills are worse than that day's
open by more than the replay's median, or their 20-session forward return is below the regular-hours adds', turn
it off.

## Not in this increment
Extended-hours profit-taking (QHM / Swing), an extended-hours bot-watched stop, Swing extended-hours adds.
