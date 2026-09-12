# Score-model shadow lifecycle and DSR report — design record

## Purpose

The 12-point/16-point score ledger records decisions but not comparable strategy returns. This increment evaluates score-model shadows under one forward-only, intraday protocol. It does **not** recreate the production entry and exit stack, and it cannot authorize a promotion, entry, sizing change, or order.

## Protocol

For each `(RTH session, score-model variant, symbol)`, select the first score-comparison scan whose own long or short threshold passes. If both directions pass, select the higher recorded score; an exact tie is skipped. Entry is the open of the first 5-minute RTH bar starting strictly after the scan timestamp. Exit is the close of the final 5-minute RTH bar that session. Missing bars make the candidate unevaluable. Long and short gross returns are direction-adjusted; net return is gross return minus a scalar, configurable round-trip cost in basis points.

Each variant contributes one equal-weighted daily return across its completed candidates. The CLI emits per-session candidate, completed, and unevaluable counts, and labels a session with no candidate as `no_observation`, never as 0%. DSR uses only the intersection of completed sessions shared by every measured variant.

## Boundaries and failure handling

`research/shadow_lifecycle.py` is an explicit, offline CLI. It is not imported by `main.py`, `strategy/run_cycle.py`, or `execution/`; no order, broker, or configuration-mutating API is called. Its only optional external call is a historical market-data request made after a session closes. Inputs and results are JSONL. Torn or malformed rows are skipped; results are append-only and keyed deterministically to make reruns idempotent.

The score ledger presently has no rows because it was deployed after market hours on 2026-09-11. This feature must report unavailable rather than synthesize history.

## DSR controls

The report requires at least two measurable variants, 30 common sessions, a positive finite cross-variant Sharpe dispersion, and an attested trial registry. The registry inventories the known 12-point, 16-point, delta, volume, and TSMOM learning-loop trials, but remains `complete: false` until the full historical inventory is checked. That keeps DSR unavailable rather than under-counting multiple tests. PSR/DSR results remain research evidence only.

## Independent design validation

Groq and Google AI Studio reviewed the exact protocol on 2026-09-11 and both returned `VERDICT: APPROVE`. Google AI Studio specifically confirmed the scalar cost deduction (`net = gross - cost`) and the strict-after-scan rule. Raw responses are retained locally in `work/dsr_brick2_design_reviews.txt`.
