# Broker-authoritative fill notifications — 2026-10-13

**Author:** OpenAI Codex
**Decision:** Approved by the execution-design board, Groq, and Google AI Studio.

## Problem and evidence

The live broker filled an EWY sale on 2026-10-13 at $164.28. Startup
reconciliation recovered the fill and recorded the Swing exit, but that path did
not call the strategy-owned Slack exit alert. The same gap can affect broker-held
stops, manual orders, and any reconciliation path outside a strategy's normal
entry/exit caller.

## Decision

Alpaca's FILL activity ledger is the authoritative source for execution
notifications. A standalone process polls that ledger once per minute and sends
one broker-fill fact for every unseen activity. It is kept outside all trading
loops so Alpaca history, order attribution, Slack latency, and filesystem I/O
cannot delay entries, exits, or protection.

Delivery is at least once. An activity ID is acknowledged only after Slack
accepts the message. State is written atomically under a process lock. A failed
delivery preserves the cursor for retry. Corrupt or future-dated state is paged,
quarantined, and replayed over a bounded 31-day recovery window. Malformed or
conflicting activities produce integrity alerts and are never presented as
normal buys or sells.

Tier attribution uses the order's client ID and inherits a proved bracket/OCO
parent tag. If attribution cannot be proved, the notification says
`Attribution unavailable`; it never invents a tier. User-facing tier names are
limited to Day, Swing, QHM, and Forever 6.

## Rejected alternatives

- Adding another `alert_exit` call to startup reconciliation would repair only
  one caller and leave the other broker-fill paths silent.
- Polling inside Swing or Day loops could delay trading during network or Slack
  failure.
- Silently baselining on first run would preserve the existing missed fills.
- Acknowledging before Slack success could permanently lose a notification.

## Validation required

- Focused delivery, retry, corruption, mixed-row, schema, attribution, and
  deduplication tests.
- Ruff E/W/F/B, mypy with unreachable checks, `py_compile`, shell syntax, and
  diff checks.
- Independent board and cold mechanical review of the exact staged bytes.
- Groq and Google AI Studio preship approval.
- Read-only live replay against Alpaca before activation, then a production
  first run followed by a zero-new-fill deduplication run.
