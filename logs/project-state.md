# Project State — alpaca-mtf-bot (2026-10-08 10:45 PT, Claude)
- OCI 137.131.51.250 at fc307e7; mtf-bot / mtf-writer / mtf-http active. Paper account (~$2.5K), goal $25K.
- Deployed this session: tier-safe stop recoveries (#517); day-tier close timing 3:58 PM ET + after-hours limit exit (#522, #524); preship static-facts mechanism (#525); tier names Day/Swing/QHM/F6 (#526); ledger operator page deferred for the owner tier's own exit (#528).
- NOT shipped: Track A ETF routing (stock at 2+ shares, 2x/inverse ETF at 0-1, never 1-share ETF, 1-share stock fallback, one day lot per stock). Patch: logs/design_records/track_a_etf_route_wip_2026-10-08.patch. Next: fresh cold-2nd → Gro+GAI preship → ship.
- CEO decisions: retries default for exits; all non-promoted day lots closed by EOD; ETFs only when the stock is unaffordable; leveraged cap 5%→10% (Swing, gated); index 3x ETFs for Day+Swing; TQQQ/SQQQ standalone retired.
- Open: rest of item 5 (Swing routing, 10% cap, gap limit, index 3x); items 4, 6, 7, 8 (design record logs/design_records/tier_safety_and_cohold_plan_2026-10-08.md).
