# Project State — alpaca-mtf-bot (2026-09-30 08:40 PT, Claude-signed)
- P0: the tier-capital allocator (#442, live 2026-09-29) denies ALL new entries (enum-string bug on py3.10 + ledger
  crossed rows from untagged day-tier OCO legs). Exits/stops unaffected. Recommended fix (Gro+GAI): flip
  TIER_CAPITAL_ALLOCATOR_ENABLED=False, ship the fixes, re-enable after an OCI probe. Awaiting Rafael.
- Shipped: day-tier live-price stop room (#443); QHM tranche adds + stop-safe add; earnings blackout removed.
- Built, not shipped: C2 swing breakout tier (branch claude/c2-swing-breakout-build; patch in logs/pending_patches).
- OCI mtf-bot 137.131.51.250 at 7cac5d9; services active. Account ~$2,520 equity, paper.
