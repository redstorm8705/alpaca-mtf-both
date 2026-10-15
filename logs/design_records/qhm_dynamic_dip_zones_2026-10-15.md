# QHM dynamic dip zones — design record (Claude, 2026-10-15)

**Owner request (2026-10-15):** the fixed -2% / -5% QHM dip-add levels are arbitrary; make them data-driven.
Measured on one year of Alpaca daily bars (split-adjusted SIP), never quoted from memory.

## Rule
- Horizon = QHM `_DIP_ADD_MIN_DAYS_BETWEEN` sessions (2-session returns).
- **Rung A** (small add) = min(stock's own past-year 10th-pct 2-session return, pooled-universe 10th pct).
- **Rung B** (larger add) = min(own 2.5th pct, pooled 2.5th pct, Rung A − own median |1-day return|).
- Short history (first bar after the window starts): both rungs also ≤ pooled 5th pct; no own data → pooled only.
- Universe = QHM picks + held QHM + F6 universe + Swing pool (36 names on 2026-10-15).
- `run_dip_zones.py` cron 12:20 + 21:20 UTC weekdays → `data/state/dip_zones.json` (atomic).
- `zones_for()` fails closed: missing/unreadable/invalid → no dip-add. A refresh failure keeps the last good file
  and pages Slack; a name with no bars keeps its last good zone.
- Every size cap unchanged (20% target, 1.375× ceiling, 40% QHM aggregate, 3 adds/quarter, 2 days apart,
  earnings blackout, 0.85× first-entry floor, dynamic overnight cap).

## Board / Gro / GAI
- Design: Thorp + Taleb seats, Gro, GAI aligned on own-distribution zones.
- Adversarial FAIL (2026-10-15): "fires less often than -2/-5" not guaranteed for a calm stock → Rule E fork.
  Options A accept / B fixed bound / C data-derived bound. Vote: Thorp A; Taleb, Gro, GAI C (3-1).
  Implemented Taleb's C: Rung A floored at the pooled 10th percentile (mirrors Rung B's pooled floor).
- Residual disclosed: if the whole universe calms, the pooled floors get shallower too.

## Replay (out-of-sample, 1 year, real bars)
| Group | Rule | Small-add quarters | Fwd return on added $ |
|---|---|---|---|
| QHM | old fixed | 74% | +4.91% |
| QHM | own-only | 62% | +5.93% |
| QHM | **shipped (pooled floor)** | **61%** | **+6.41%** |
| F6 | old / shipped | 76% / 64% | +9.73% / +9.10% |
| Swing | old / shipped | 84% / 73% | +2.45% / +3.61% |

Live zones 2026-10-15: NVDA, LLY, GE, GOOGL, COST, AAPL −4.61 / −8.51; GEV −5.52 / −8.51; SNDK −10.12 / −18.22;
DRAM −8.10 / −14.13 (short history).

**Reversal criterion:** if over the next two quarters live dip-adds' 20-session forward return falls below the
old fixed rule's replayed figure for the same dates, revisit.
**Size / frequency / concurrency delta:** size 0; concurrency 0; frequency measured lower (not guaranteed —
Rule E board gate run, above).

## Follow-ups
- Freshness monitor for `dip_zones.json` and `overnight_k.json` (page if older than one session).
