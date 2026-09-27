# Day Tier post-fill geometry and weekly-report truth fix — 2026-09-26

**Author:** ChatGPT/Codex (`fix/day-tier-postfill-geometry`)

## Proven defects

1. A Day Tier entry's stop and target were calculated from the signal reference before order submission, but the OCO was submitted after the broker fill without revalidating either leg against the actual fill. On 2026-09-23, AAPL short filled at 338.29 with target 338.76 (target above the short entry) and META short filled at 756.71 with stop 750.13 (stop below the short entry). Both violate the intended `target < fill < stop` short invariant.
2. `weekly_postmortem.py` described and summed `directional exit-to-Friday difference × closed quantity` as dollars “left on the table.” That mixes same-day and multi-day horizons, partial exits, and positions with retained runners, so it is not an attributable opportunity-cost measure.

## Fix

- After a fill is confirmed and durably logged, derive the intended target and require `stop < fill < target` for longs or `target < fill < stop` for shorts. Invalid geometry triggers a scoped close of only the Day Tier-owned quantity with reason `fill_invalidated_setup`; no exit bracket is submitted. If that close fails, the state remains `filled` so the next reconciliation tick retries it.
- Keep the exit-to-Friday value only as a clearly labeled per-share diagnostic. Remove its aggregate dollar total from Markdown, Slack, and the Gemini prompt. Tell Gemini that missing metadata is unknown rather than zero and forbid invented dollar opportunity cost.

## Alternatives rejected

- **Silently fall back to a plain stop:** rejected because it keeps a filled position after its entry thesis or risk boundary was already crossed.
- **Recompute a new wider stop/target after the fill:** rejected for this bug fix because it changes strategy and risk rather than enforcing the submitted setup.
- **Keep the aggregate with a disclaimer:** rejected because the aggregation is dimensionally and causally invalid.

## Safety and reversibility

The entry order has already filled when this gate runs, so a geometry failure must reduce exposure. The existing scoped `flatten_position` owns only the recorded Day Tier quantity and verifies the exit fill. A failed flatten remains in the nonterminal `filled` state and is visible to `reconcile_open_state`. The change does not alter sizing, signal admission, stops for valid fills, account kill limits, or paper-only routing.

## Forward-improvement pass (separate build)

The static assumption exposed by the report is that every exit can be judged against Friday close. The correct dynamic build is a lifecycle-keyed, tier-aware benchmark: same-session close/VWAP for Day Tier, declared horizon and retained-runner mark for Core MTF, and strategy-specific horizons for QHM/F6. It needs exact parent/order lifecycle attribution already being built in the canonical trade-record work. It is deliberately separate from this factual correction so a new evaluation model does not delay the P0 execution fix or contaminate its attribution.

## Validation contract

- Unit reproductions for the real AAPL and META shapes.
- Integration proof: invalid fill calls scoped flatten with `fill_invalidated_setup`; failed flatten remains reconcilable.
- Weekly-report proof: a 4-share, $9/share Friday diagnostic is never rendered as $36 “left on table.”
- Python 3.10 compile, targeted suites, repository preship, cold exact-diff review, Board + Groq + Google AI approval before shipping.
