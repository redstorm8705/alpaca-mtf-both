# Strategy Edge Report single-source rebuild — 2026-09-27

**Owner/signature:** ChatGPT/Codex
**Scope:** reporting only; no entry, exit, sizing, broker mutation, or risk-path behavior.

## Verified defect

The all-time Strategy Edge card combined three incompatible datasets:

1. headline lifetime P&L/win rate from `reporting.pnl_ledger`;
2. score rows from `logs/fifo_edge.json`, where each partial close was counted as a trade leg;
3. exit reason, hold time, and drawdown from corrupted `trade_log.json`.

The 2026-09-26 audit measured 309 entry-level lifecycles and 34% win rate, while the card body displayed 416 close legs and 41% win rate. The card also assigned stale `overnight` flags to hold-time buckets and reported a tracker-derived $918.93 drawdown instead of broker-grounded evidence.

## Implemented contract

- One `build_report_figures()` call supplies the entire Strategy Edge card.
- `build_ledger()` joins Alpaca fill `order_id` to Alpaca order `client_order_id` and carries the **entry order tier** through the unified FIFO lot.
- Exact tags map to Core MTF (`IN`), Day Tier (`DT`), QHM (`QH`), or Forever-6 (`F6`). Untagged legacy entries remain **Unattributed**; they are never guessed into Core MTF.
- Partial-close P&L remains in realized cash P&L, but an entry with an open residual is excluded from completed-trade count, win rate, payoff, and profit factor.
- Multiple entry fills belonging to one broker order form one lifecycle.
- Max drawdown uses the chronological realized-fill cash path. Hold buckets derive from entry and final-exit timestamps, not a stale `overnight` flag.
- FIFO legs produced by one broker fill are aggregated by Alpaca activity ID before the drawdown path advances; lot ordering cannot fabricate intra-fill drawdown.
- The card withholds every metric when FIFO has unmatched closes, an opening fill cannot join to its broker order, or a realized close has neither an activity ID nor order ID. Fractional quantities fail closed instead of being truncated.
- Malformed or non-finite quantities, and malformed, non-finite, or non-positive fill prices fail closed before FIFO accounting.
- Score/setup and exit-reason panels are withheld until Confluence 2.0/E1 provides exact lifecycle identifiers. The report states this explicitly instead of joining metadata by symbol or nearest time.

## Live read-only evidence before ship

Using the paper account history on 2026-09-27:

- 682 fills; 2,926 orders; 0 unmatched closes.
- Realized P&L: **+$71.05**; invariant drift **$2.04** within the existing $5 tolerance.
- **213 completed entry-order lifecycles + 3 partially exited open lifecycles**. The earlier 309 estimate grouped broker fills by timestamp; the final contract correctly merges multiple fills belonging to one entry order.
- Completed win rate: **36.2%**; profit factor **1.04**.
- Exact-tag tier results: Core MTF 67 completed / -$350.56 / 17.9% WR; Day Tier 15 / -$24.75 / 20.0%; QHM 9 / +$66.09 / 66.7%; Forever-6 0; Unattributed legacy 122 / +$380.27 / 45.9%.

These figures are descriptive accounting evidence, not proof of strategy edge or statistical significance.

## Acceptance checks

- shared-symbol FIFO close attributes each matched lot to its entry tier;
- untagged entries remain Unattributed;
- a realized partial with an open residual contributes P&L but not a completed win;
- two exit legs merge into one completed lifecycle;
- multiple fills for one entry order merge into one lifecycle;
- the rendered card names the Alpaca fills+orders source and discloses withheld metadata.
- incomplete FIFO history, order attribution, and close-fill identity each render an explicit unavailable card with no performance metrics;
- fractional fills fail closed, and multiple FIFO legs from one close fill advance the drawdown path atomically.
