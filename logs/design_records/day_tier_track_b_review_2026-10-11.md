# Day tier Track A vs Track B — review and decision (2026-10-11, Claude)

**CEO (2026-10-11):** "I'm not exactly clear on the material differences between track A and track B. Let's revisit this."

## What each track is (verified at source)
- **Track A ("GEX-core"):** 11 names. Trades the options-driven price walls on 5-minute bars all session: in a calm
  options market it fades a failed poke through a wall; in a volatile one it rides a close through it. Budget
  min(buying power x 20%, equity x 100%) x conviction. Stop at the wall. (`strategy/day_tier_entry_trigger.py`)
- **Track B ("movers"):** 15 names, screened for a gap of 1%+, 1.5x relative volume, price $5+. Enters a mover that
  broke and held its first-15-minute range on volume, on the right side of VWAP, not over-extended. Budget equity x
  15% x 35% (about $130) x conviction 0.5-0.6; one shot per stock per day; stop at the opening-range level, 2R
  target, bracket order; 2x bull ETF on longs. (`strategy/day_tier_momentum_trigger.py`, `strategy/day_tier_track_b.py`)
- **Shared:** order code, end-of-day flat, Day kill switch, the watch-day rule, the Day gross cap (equity x 60%).

## Production record (OCI logs; fills 9/15-10/09, Track B funnel 9/24-10/09, 12 sessions)
- 27 Day fills 9/15-10/09: Track A 26 (14 tagged + 12 untagged on 9/15-9/23, before the tag existed), Track B 1
  (PLTU 10/09, take-profit, +$37.89).
- Track B funnel: 911 screen passes -> 7 trigger signals -> 1 fill. Misses: watch-day rule 3 (META 10/07, MU 10/08,
  SMCI 10/09), Day gross cap already used by Track A 2 (MU/MUU, COIN 10/07, while B's own
  budget had room), one-share minimum (TSLA 10/02, before the 10/06 floor) 1.
- Track A realized -$55.86 over 26 fills since 9/15 (tier's own realized_pnl, not Alpaca fills — indicative only).
- The trigger's WAIT reasons were not logged.

## BGG consensus (board Simons+Brandt 2-0, GAI; Gro unavailable — daily cap, 2026-07-07 rule)
1. **KEEP Track B as a separate track.** Not merge (loses per-track measurement); not retire (n=1 judges the gates,
   not the edge).
2. **Ship now (not risk-path):** log why each screened mover did not trigger (`wait_code` on the trigger; one
   `[track-b-wait]` line per runner tick).
3. **Replay first, then the risk-path gate:** exempting Track B's breakdown shorts from the watch-day rule (its
   evidence was measured on counter-trend shorts held to close, a different setup), and reserving Track B's 35%
   slice inside the Day gross cap so Track A borrows it only while B is idle. Both raise entry frequency (Rule E).
4. **Not now:** a bigger Track B budget (size; GAI against, Simons for; no majority).
5. **Separate review owed:** Track A's own record.
