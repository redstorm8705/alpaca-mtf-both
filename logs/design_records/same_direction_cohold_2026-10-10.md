# Same-direction co-hold — Day tier may trade a stock another tier holds (design, 2026-10-10, Claude)

**CEO rule (repeated, latest 2026-10-10):** the Day tier may trade a stock another tier holds as long as it is the
SAME direction. Opposite direction stays blocked.

**Today:** an interim blanket no-co-hold guard (PR #503, 2026-10-07): `run_day_tier._held_by_other_tiers()` (ownership
snapshot) -> a co-held stock routes to a leveraged/inverse ETF, else skip. 10/09: 19 skips (EWY 14 same-direction short,
MSFT 5 opposite-direction).

## Why the guard exists (facts)
1. Alpaca keeps ONE net position per symbol.
2. Swing exits close the whole symbol: `broker.close_position(symbol)` (exit_logic.py L1398/1561/1660/1743/2001,
   gtc_manager.py L190 cover-on-breach, stop_protection.py L679, entry_logic.py L311, events/handlers.py L97 news halt).
   `close_position` is a chokepoint (broker.py L1940) that already bounds closes for QHM/F6 shares when
   `OWNERSHIP_GUARD_ENFORCE=True` (False today -> raw full close).
3. Day exits are already quantity-bounded: `partial_close_position(qty, tier="daytrade")`, OCO legs sized to the lot.
4. Some Swing stops are sized to the whole netted position (tier-safety design record 2026-10-08, stage-2 list).
5. Day OCO child legs carry Alpaca-generated (untagged) client order ids; Swing and Swing-breakout share the IN- tag.
6. Alpaca wash-trade rule (docs.alpaca.markets/us/docs/user-protection, read 2026-10-10): an existing STOP sell + a new
   LIMIT buy is "always rejected"; existing limit sell + new limit buy rejected if buy price >= sell price. "Bracket or
   OCO ... and trailing stop orders are exceptions to our wash trade protection."

## Failure modes to close
- F1 Swing exit sells the Day lot too -> the Day OCO stop/target later sells shares that are gone -> accidental SHORT.
- F2 Day long entry rejected (wash) because a Swing/QHM/F6 stop sell rests on the symbol.
- F3 A Day OCO leg and a Swing order treated as the other tier's (untagged legs) -> wrong cancel / wrong cover math.
- F4 Day after-hours exit / reconcile acting on shares another tier owns.

## Proposed design (for BGG)
- D1 Chokepoint bound: `broker.close_position(symbol, tier)` for a NON-day tier subtracts the Day tier's same-day
  claim (ownership snapshot = today's Day log, real-time) and closes only its own shares via the tier-scoped partial
  close. Unreadable Day claim -> ??? (fork: refuse the close and page, vs full close). Independent of
  OWNERSHIP_GUARD_ENFORCE (Day is not a protected tier; this is an oversell guard, not a never-sell floor).
- D2 Swing stops sized to the Swing tier's own quantity (tracker qty), never the netted position.
- D3 Day entry on a co-held same-direction stock submits a BRACKET order (entry + stop + target) so the wash-trade
  exception applies [hypothesis — unverified: the docs name bracket/OCO as exceptions but do not state whether a NEW
  bracket entry passes against an EXISTING plain stop of the opposite side]. Verification path without Claude placing
  orders: (a) search prod logs for any bracket entry against a resting stop; (b) Alpaca support/docs; (c) operator test.
- D4 Same-direction only: the snapshot's other-tier signed claim must have the same sign as the Day trade; opposite ->
  block (no ETF pivot change).
- D5 Order ownership: Day OCO child legs inherit the parent's DT- ownership via the parent link (the ledger already does
  this in run_ledger_sync._inherit_leg_tiers); Swing vs breakout disambiguated by the breakout state file.
- D6 Day after-hours exit and reconcile act only on the Day lot quantity (already lot-sized; verify).

## Open forks for BGG
1. D1 unreadable-claim behaviour (refuse+page vs full close).
2. D3 entry mechanics if the bracket exception does not cover the case (cancel Swing stop -> buy -> restore stop, as the
   QHM add already does, vs skip co-hold entries on stocks with a resting opposite-side stop).
3. Sizing: does a co-held Day lot count toward per-name / single-name caps against the combined position?
4. Order of rollout: D1+D2 first (they protect existing positions regardless), then D3-D5, then lift the guard.

## BGG consensus (2026-10-10; board Harris+Taleb, Gro, GAI — Gro/GAI moved to the board position on forks 1-2 after one counter-prompt)
- Fork 1: a non-day close with an unreadable Day claim closes ONLY that tier's own tracker quantity (capped at the
  broker net) and pages. Never refuse a stop exit; never sell the Day lot.
- Fork 2: Day co-hold entry = BRACKET order; on a wash-trade reject fall back to the leveraged/inverse ETF pivot. No
  stop is ever cancelled to make room. Operator test of "new bracket vs existing opposite-side plain stop" is a
  prerequisite to lifting the guard (Claude cannot place orders).
- Fork 3: the Day lot counts toward per-name / single-name caps against the COMBINED position.
- Fork 4: ship the oversell guards first; lift the no-co-hold guard last.

## Additional required fixes found by the board (ship before the guard lifts)
- M1 (live today for QHM/F6 co-holds): `_raw_close_position`'s 40310000 retry calls cancel_open_orders_for_symbol
  with only_tier=None -> cancels another tier's stop. Must pass only_tier=tier.
- M2: while a Day OCO sell stop rests, Swing/QHM/F6 limit-buy adds are wash-rejected and the tier-scoped retry cannot
  clear it -> explicit rule (defer other tiers' adds while a Day lot is open on the symbol).
- T1: the Day claim is logged after the fill -> D1 must also count the Day tier's open DT- orders and fills not yet
  logged.
- T2: partial_close_position takes the side from the live net -> pass the lot direction; refuse if the net sign flipped.
- T3: D2 stop qty = min(tracker qty, broker net minus other tiers' claims); <= 0 -> page, never the netted size.
- F5 (Gro): stale snapshot at Day-entry time -> place_entry re-reads ownership immediately before submit (it already
  re-reads positions/orders; extend to the same-direction check).
