# Meta-audit context isolation and Slack readability

## Problem and evidence

The September 10 meta-audit output discussed August trades even though
`_load_week_trade_events()` correctly filters `trade_events.jsonl` to seven days.
The source was `audit_directives.jsonl`: its `context_only` archive records store raw
provider output, including old trade-by-trade tables. `_format_meta_audit_body()` put
that prose under prior directives, allowing a provider to treat it as fresh evidence.

The same report reached Slack as long pipe-derived rows. The old renderer also clipped
the whole Block Kit payload at 50 blocks, which could omit the latter part of a full
provider report.

## Decision

Compliance context consumes only structured directive rows with both `file` and
`finding`. Archive rows remain durable evidence on disk but cannot become current
trade evidence.

Slack keeps both complete provider responses. Markdown tables become vertical
header/value records; ordinary prose is wrapped for a phone screen; section text is
split below Slack's 3,000-character limit. `alerts.send_slack_blocks()` receives the
complete block list and delivers ordered groups of at most 45 blocks, rather than a
local 50-block truncation.

## Boundaries and failure behavior

This design changes only audit inputs, JSON artifacts, and Slack payloads. It has no
broker, market-data, order, sizing, stop, position, or ledger call. A provider failure
is still visible as one concise error section. Extra markdown-table cells receive an
`Additional detail` label so they are retained rather than discarded.

## Verification and forward pass

The regression test supplies a raw archived August trade table and a structured
directive, then proves only the structured finding reaches the current prompt. It
also proves table cells render vertically and a report with more than 50 blocks reaches
the shared chunking sender intact. Focused OCI validation runs compilation, Ruff,
mypy, and the regression test.

The static assumption exposed here was that a retained audit archive is safe to reuse
as prompt context. The durable guard is the structured-record filter plus a regression
case with an old trade date; future archive fields cannot reintroduce raw narrative
without changing that tested boundary.
