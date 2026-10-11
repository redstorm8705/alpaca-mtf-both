# Buys blocked by another tier's resting stop (Alpaca wash rule) — design (2026-10-11, Claude)

**CEO question (2026-10-11):** "If we're in NVDA on the Day tier with a stop, we can't add more NVDA in the same
direction? Double-check the logic and Alpaca capabilities." Verified answer: Alpaca's wash-trade check is mechanical.
Its table (docs.alpaca.markets/us/docs/user-protection) lists an existing **stop sell** plus a new **limit buy** (or
market buy) as "always rejected", and an existing **stop buy** plus a new **limit sell** as "always rejected". So adding
in the SAME direction is rejected while any opposite-side stop rests on the symbol, whichever tier owns that stop.
"Bracket or OCO ... and trailing stop orders are exceptions." Production has no instance of this case yet. Both real
wash rejects (SMCI 2026-07-29, RBLX 2026-08-11) were a stop against a resting market order.

## Paths that can hit it (verified at source)
| Path | Order | Its own protection |
|------|-------|--------------------|
| Swing intraday entry, `execution/entry_logic.py` ~L1581 | market buy/sell | stop placed after the fill |
| Swing overnight entry, `execution/entry_logic.py` ~L2074 | limit (extended hours) | stop placed after the fill |
| Swing breakout entry, `execution/swing_breakout_manager.py` ~L591 | limit buy | GTC stop after the fill |
| F6 accumulation, `execution/forever_hold_manager.py` ~L281 | 1-share market buy | none (never-sell tier) |
| QHM tranche/dip add, `execution/quarterly_hold_manager.py` `_stop_safe_add` | limit add, after cancelling its OWN stop | re-places its stop |

- **Why it matters now:** the same-direction co-hold went live 2026-10-11 (#590). A Day lot's bracket/OCO sell stop
  can now rest on a stock Swing, breakout, F6 or QHM wants to buy. The same conflict already existed between the
  other tiers (e.g. an F6 buy on a stock Swing holds with a stop).
- **Effect when it happens:** the buy is rejected, so the trade or add is missed. That is a data-collection loss, not
  a safety failure. QHM's add path cancels only its own stop, so another tier's stop still blocks it, and QHM then
  re-places its stop.
- **QHM overlap:** `quarterly_hold_manager.py` is also being changed by ChatGPT's in-flight QHM stop-quantity work.

## Proposed design
- **Only when needed.** Before a buy (or a sell-to-open), read the open orders on the symbol. If an
  OPPOSITE-side stop owned by ANOTHER tier is resting, submit the entry as an Alpaca complex order. Otherwise keep
  today's path unchanged.
- **Swing and breakout:** a bracket entry (entry + protective stop + take-profit). The stop leg becomes the position's
  broker stop (Alpaca supports PATCH on bracket legs, which the Swing stop machinery uses to move stops). The
  take-profit leg is a problem for Swing, which takes profit in software tranches. Options: OTO (entry + stop only), or
  a bracket whose take-profit sits far beyond the last tranche target.
- **F6:** it carries no stop by design, so it cannot use a bracket. Defer the buy to a later cycle while a foreign
  opposite-side stop rests (F6 buys 1 share and is not time-critical).
- **QHM:** route through ChatGPT's in-flight QHM change. Either an OTO add whose stop leg covers the added shares, or
  defer while a foreign stop rests.
- **On a wash reject anyway:** log the reject, skip the entry this cycle, and leave existing positions and stops
  untouched.

## Open forks for BGG
1. Swing/breakout complex order: OTO (stop leg only; Alpaca's docs name only bracket/OCO/trailing stops as
   exceptions, so the OTO exemption is unverified) or a bracket with a far take-profit (exempt per the docs, but the
   take-profit leg holds shares that the software tranche exits then need)?
2. F6: defer while a foreign opposite-side stop rests (recommended), or skip F6 buys on co-held symbols entirely?
3. QHM: hand to ChatGPT inside its in-flight QHM change, or build after it lands?
4. Is this worth building now, given zero production instances and the cost being missed entries only?

## BGG consensus (2026-10-11; board Harris+Brandt 2-0, GAI after one counter-prompt; Gro unavailable — daily token cap,
## 2026-07-07 rule: board majority + GAI)
1. **Swing and breakout entries:** when a FOREIGN opposite-side stop rests on the symbol, submit an OTO entry (entry +
   protective stop leg, no take-profit). The stop leg is recorded under the existing stop keys, so `_move_stops` and
   the software tranches work unchanged. A bracket take-profit leg would hold the full lot and block every tranche
   sell (40310000), and it cannot be removed without cancelling the stop. So: no bracket.
   - A wash reject on the OTO (its exemption is unverified in the docs) -> defer the entry that cycle, logged. The first
     live occurrence is the test, tracked by a post-live expectation.
   - The overnight extended-hours entry always defers (complex orders do not support extended hours).
2. **F6:** defer while a foreign opposite-side stop rests, log each deferral, and page after N consecutive deferred
   days (an anomaly rule). Never skip co-held names entirely.
3. **QHM:** build after ChatGPT's in-flight QHM stop-quantity change merges.
4. **Build now:** the pre-check, a reason logged for each deferral, and the OTO route.
Not risk-path on size or frequency (only the order type changes); it touches stop placement, so the full gate applies,
including a cold-2nd against the tranche path.

## REVISED consensus (2026-10-11, after a new fact; board Harris+Brandt 2-0, GAI; Gro unavailable — daily cap)
New fact: Alpaca's docs say OTO "order replacement is not supported yet". The Swing tier moves its broker stop ONLY by
replace (_move_stops -> replace_stop_order; never cancel-then-resubmit since the 2026-09-18 UBER incident, which sat
204 min with no broker stop). An unreplaceable OTO stop leg would freeze the lot's stop: no fill-shift, no trail
ratchet, every tranche deferred.
**Decision: Option A — DEFER.** When a FOREIGN (another tier's, or untagged) opposite-side stop rests on the symbol,
the Swing intraday entry, the Swing overnight entry, the breakout entry and the F6 buy each defer that cycle, with the
reason logged. The entry fires on a later scan if its signal still holds once the stop clears; a Day lot always exits
the same session.
- **Alert:** a deferral keyed to the same blocking tier that lasts beyond one session pages once per day (it then
  comes back to the board rather than silently blocking the symbol).
- **Revisit:** after the first live data (deferral counts by blocking tier; how often the deferred signal did not
  re-fire), and if a one-lot probe ever shows Alpaca accepts PATCH on an OTO stop leg.
- **Superseded:** the OTO route above.
