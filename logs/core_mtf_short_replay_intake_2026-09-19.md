# Core MTF short replay intake — 2026-09-19

## Terminology

| User-facing name | Meaning | Existing code label |
|---|---|---|
| Day Tier | Same-session day-trading strategy; flat by close | `daytrade` |
| Core MTF | Higher-timeframe, multi-day strategy; may enter during the session and carry overnight | legacy `TradeMode.INTRADAY` / `trade_mode="intraday"` |

Use **Core MTF** in reports and new design records. Do not rename the persisted legacy field without a compatibility migration.

## Reconciled 30-day closed-short cohort

Window: 2026-08-20 through 2026-09-19. P&L source is broker FIFO reconciliation, rather than event-log P&L.

| Scope | Closed FIFO lots | Realized P&L | Win/loss |
|---|---:|---:|---:|
| Core MTF shorts | 9 | -$80.49 | 0 / 9 |
| Day Tier shorts | 4 | -$7.81 | 0 / 4 |
| All shorts | 13 | -$88.30 | 0 / 13 |

The Core MTF 9 lots correspond to five parent entries: MARA (2026-08-06), TSLA (2026-08-20), MARA (2026-09-01), AVGO (2026-09-04), and UBER (2026-09-08). Open UBER (2026-09-15) and NFLX (2026-09-18) short entries are excluded from realized-P&L conclusions. The previously cited “11 shorts” is not supported by this exact broker-FIFO window; it may use another window, include opens, or count fills.

## Verified inputs and defects

- VWAP, 15-minute/hourly confirmation, daily trend, 12–1 month momentum, and GEX are present in the Core MTF path.
- Premarket high/low data exists, but `run_premarket_gate.py` currently uses it only for a long gap-retrace path.
- No production dark-pool feed or decision-stack field has been verified; it is not a valid input until provenance and timestamp semantics exist.
- The decision log keeps long-oriented names for inverted short predicates. A log field such as `daily_above_150sma=true` can mean that a short predicate passed. The record is not self-explanatory enough for reliable short attribution.
- TSLA (2026-08-20) and AVGO (2026-09-04) had positive recorded 12-month momentum at short entry (+5.72 and +6.25). This is a testable candidate hard-admission feature, not proof by itself.

### P0 — no completed-bar boundary

`data/fetcher.py:214-240` requests bars through the current moment and returns the last row unchanged. Core MTF consumes that last row for entry and exit calculations (`strategy/confluence.py`, `strategy/signal_generator.py`) and its weekly-bias gate uses the latest fetched weekly close. During regular trading, a bar can still change. The current source therefore permits repainting/look-ahead in live decision inputs and makes a naïve historical replay optimistic.

The required correction is a single explicit completed-bar adapter: all indicators/regime inputs use only a bar whose close was already known, while execution pricing comes from an explicitly separate live quote. This touches entries and exits, so it is a risk-path change requiring the full BGGN and mechanical gate before shipping.

## Next sequence

1. Export Core MTF short decision stacks, completed-bar features, broker fills, exits, and FIFO outcomes; keep open trades separate.
2. Simulate the completed-bar adapter plus short-specific overnight-range/VWAP admission candidates across bull, bear, and chop samples with next-available pricing, costs, stops, and overnight behavior.
3. Record trial count and size/frequency/concurrency delta, then run BGGN, adversarial, cold-review, statics, and exact-diff preship gates.
4. A gate-cleared paper change ships with a reversible kill flag; it does not sit in passive shadow.
