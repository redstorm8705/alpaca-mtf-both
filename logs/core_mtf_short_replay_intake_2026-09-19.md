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

## Parent-entry replay evidence

The “11 shorts” refers to eleven Core MTF parent short entries in `trade_events.jsonl`, including trades outside the 30-day realized-FIFO window and two positions that remained open. The table reconstructs each entry with raw Alpaca bars fully complete before the recorded event timestamp. `VWAP Δ` is the reconstructed session VWAP difference.

| Symbol | Entry date | Completed 15m price | VWAP Δ | Premarket high | 12–1 momentum |
|---|---|---:|---:|---:|---:|
| SMCI | Jul 29 | $26.00 | -3.720% | $28.64 | -38.36% |
| SMCI | Jul 31 | $27.82 | -0.920% | $28.84 | -42.33% |
| MARA | Aug 6 | $10.90 | -0.897% | $11.35 | -24.54% |
| RBLX | Aug 11 | $36.77 | +0.014% | $37.36 | -48.46% |
| COIN | Aug 14 | $149.44 | -0.303% | $153.49 | -60.54% |
| TSLA | Aug 20 | $343.42 | +0.056% | $351.78 | -3.74% |
| MARA | Sep 1 | $10.18 | -1.416% | $10.81 | -24.19% |
| AVGO | Sep 4 | $355.73 | -0.201% | $362.75 | **+41.80%** |
| UBER | Sep 8 | $72.97 | -0.718% | $76.76 | -15.80% |
| UBER | Sep 15 | $71.86 | +0.113% | $72.49 | -17.95% |
| NFLX | Sep 18 | $71.73 | +0.363% | $75.73 | -93.40% |

AVGO is the clear first exclusion candidate: it entered with strongly positive 12–1 momentum, while the existing additive score still admitted it. This does not justify an untested static threshold. The candidate is a dynamic, completed-bar admission rule that requires coherent higher-timeframe downside structure, followed by a short-specific trigger around VWAP and the premarket range.

## Next sequence

1. Export Core MTF short decision stacks, completed-bar features, broker fills, exits, and FIFO outcomes; keep open trades separate.
2. Simulate the completed-bar adapter plus short-specific overnight-range/VWAP admission candidates across bull, bear, and chop samples with next-available pricing, costs, stops, and overnight behavior.
3. Record trial count and size/frequency/concurrency delta, then run BGGN, adversarial, cold-review, statics, and exact-diff preship gates.
4. A gate-cleared paper change ships with a reversible kill flag; it does not sit in passive shadow.
