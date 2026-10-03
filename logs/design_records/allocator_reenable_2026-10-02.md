# Tier-capital allocator — re-enable design record (2026-10-02, Claude-signed)

## Status
`TIER_CAPITAL_ALLOCATOR_ENABLED=False` since PR #449 (Rafael approved Option B). The bot trades under the
pre-2026-09-29 per-tier caps plus the 7% kill switch. Fixed so far: enum parsing (PR #451, dormant) and ledger
OCO-leg attribution (PR #453, live; crossed AMZN/META/MSFT rows healed).

## Remaining re-enable blockers (prod evidence 2026-10-02)
1. **QHM/F6 holdings count against the swing/day account-gross and overnight caps.** This conflicts with the CLAUDE.md
   rule "buy-and-hold tiers are excluded from SWING exposure limits". Live numbers: gross $3,430 (QHM $3,175)
   vs account cap $3,052 (1.25 × $2,441) → every swing/day entry would be denied on "account gross cap".
2. **The ledger-freshness lockout.** `_ledger_tier_gross` (tier_capital_allocator.py:673-686) denies every tier
   unless the ledger's per-symbol tier sums equal live broker net EXACTLY. The ledger is rebuilt only by the
   `run_ledger_sync.py` cron (`*/20 13-21 * * 1-5` UTC); grep finds no other caller of `sync_once`/`sync_ledger`.
   So after ANY fill, every admission is denied until the next sync (≤20 min). After the last sync of the day
   (~2:40 PM PT), it is denied until the next morning. Inference from code plus crontab; not yet observed live,
   because the allocator has never admitted an entry.

## Design review — blocker 1 (exclude ledger-attributed QHM+F6 gross for daytrade/swing requests)
Same prompt to Gro + GAI (`design_prompt` in the session scratchpad; summarized here).
- **Gro (gpt-oss-120b): APPROVE-WITH-CHANGES.** Valid points: clamp ≥0, make the overnight factor a config constant,
  and log the effective gross on a deny.
  False premises (to be refuted by counter-prompt):
  - a 30 s ledger-freshness rule. The ledger syncs every 20 min, and the exact-quantity match above is the
    real freshness test.
  - per-day aggregation against the kill switch. The kill switch is a daily LOSS limit
    (risk_manager.check_kill_switch), and reservations already serialize admissions under a flock.
- **GAI (gemini-3.1-flash-lite): APPROVE-WITH-CHANGES.** The worst case is bounded by the maintenance cushion:
  ($2,441 − $1,029) − $650 = **$762** of new swing/day notional. Survivable.
  Valid points: clamp ≥0, and an audit line showing the effective gross.
  A "60 s ledger heartbeat" has the same false premise as above.
  On pending QHM: keep pending QHM counted (conservative; it never under-counts).
- **Board risk seat (Thorp/Taleb):** see the addendum below once returned.

## Recommendation
Keep the allocator OFF. Re-enabling it now would lower entry frequency (blocker 2), which works against the
data-collection objective. It adds risk control the per-tier caps + 7% kill + maintenance cushion already provide
on a paper account. The two blockers become one redesign of the allocator, owned with Codex (allocator author):
(a) the QHM/F6 exclusion as reviewed above; (b) an on-fill ledger update, or allocator tolerance for unsynced
fills that the reservation ledger already accounts for. Gate both through the full board + Gro + GAI before any
re-enable. Re-enable only after a read-only OCI probe of `_evaluate` approves a sample swing and day entry.

## Addendum — board risk seat (Thorp/Taleb lens, cold, Sonnet): APPROVE-WITH-CHANGES on blocker 1
- Gro's changes 2, 4, 5 and 6 are FALSE-PREMISE when checked against the code:
  - #2 would re-create the block; the cushion check already bounds margin.
  - #4: a 30 s staleness rule would always deny; exact reconciliation in `_ledger_tier_gross` is the real
    freshness test.
  - #5: `snap.gross` holds positions only; pending orders sit in `pending_gross`.
  - #6: reservations are cumulative under the flock, and the kill switch is a loss limit.
- Gro's change 7 (tests) is VALID. Changes 1, 3 and 8 are nice-to-have.
- Gro's $506 worst case double-counted swing inside `maint`. The correct cushion bound at admission is $762.
- Structural worst case after fills: about $1,575 of new non-hold notional (tier caps), 0.75× equity intraday.
  Overnight total about $4,150 = 1.7× equity (< Reg-T 2×). Survivable on paper with stops.
- Required for the build:
  1. protected gross = qhm + forever6 `_ledger_tier_gross` on the same snapshot; deny if either is None;
  2. apply only to daytrade/swing requests; QHM/F6 paths unchanged;
  3. `max(gross − protected, 0)`, and deny if protected > gross (drift);
  4. BP, maint and maintenance_reserve stay on full broker figures;
  5. a config kill flag (Rule E);
  6. log the protected gross excluded and the effective gross (Rule D);
  7. tests plus a read-only OCI probe before calling it live;
  8. move the 0.40 literal into the policy and config.
- **Owner flag (not blocking):** the 0.40 × equity overnight cap is a new limit from 9/29. It is stricter than
  invariant #11 (100%) and caps overnight swing at about $976.

**Alignment on blocker 1:** board + Gro + GAI all APPROVE-WITH-CHANGES on the same core design. Gro's
false-premise items are rebutted by the board seat at source; a counter-prompt round is owed before the build ships.
**Blocker 2 (ledger-freshness lockout)** has not yet been reviewed. It must be designed and reviewed before any re-enable.
