# Day audit setup labels — verified scope

**Author:** OpenAI Codex (GPT-6)
**Date:** 2026-10-09

## Evidence and decision

The proposed October 8 META FIFO defect is absent. Alpaca fill/order history shows a tagged Day short at $756.71 on September 23 followed by its $757.35 cover; the October 8 Day long entered at $721.27 and its tagged sell at $720.67 pair correctly for −$0.60. The canonical FIFO result for October 8 is META −$0.60, NVDA +$4.45, and NFLX −$0.10. No accounting code will be changed on the refuted premise.

The separate NVDA attribution gap is present: QHM's tagged three-share August 28 sell consumed one tagged QHM August 18 lot and two untagged August 24 after-hours buys from order/client ID `7ee261e7-12e0-45b1-9b2b-b5615f865a1b`. That belongs to the later QHM ledger-interconnect build and must not be hidden by changing account FIFO.

Day entries are not 12-point Swing confluence entries, yet the meta-audit renders `score=0` on each Day row and aggregates it into the score distribution. Prose disclaimers have not prevented false “entry bypass” findings.

## Build

Change only the report layer in `auto_ai_audit.py`:

1. Read `logs/day_tier_events.jsonl` read-only and build exact `trade_id` evidence from `decision` plus `entry_fill` records.
2. For a Day entry with an exact ID join, render `setup=<family/track/mode>` and `conviction=<value>` and omit the inapplicable 12-point score.
3. For a Day entry without exact evidence, render `setup=UNKNOWN` and `conviction=UNKNOWN`; never infer by symbol/time.
4. Exclude Day entries from the 12-point score distribution. Keep Swing score handling unchanged.
5. Preserve raw events and all trading behavior. No Day strategy/logger/execution file changes.

Malformed, duplicate-conflicting, future, or unreadable Day evidence stays unknown. Exit rows may carry the same exact setup tag when the lifecycle ID joins; they never invent conviction. Tests cover exact join, missing join, conflicting evidence, malformed lines, and Swing parity.

## Risk delta

Size 0, frequency 0, concurrency 0. Reporting-only. Full cold-second, adversarial, Groq, Google AI Studio, CI, deployment, and next-report verification still apply.
