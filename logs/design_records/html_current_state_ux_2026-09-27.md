# HTML current-state UX — 2026-09-27

**Owner/signature:** ChatGPT/Codex (`feat/html-ux-current-state`)

## Problem
The five generated operator pages used fragmented navigation and mixed account-equity figures with legacy trade counts. Open positions had no strategy-tier ownership. Options preserved useful weekly and 0DTE recommendations but gave every candidate equal hierarchy, making the page hard to act on.

## Decision
- Use one shared five-page navigation and tier vocabulary: Core MTF, Day Tier, QHM, Forever-6, Unattributed.
- Show exact current tier ownership only when `ownership_ledger.json` reconciles to the broker position; otherwise label it Unattributed.
- Separate account P&L (equity minus deposits) from completed broker-lifecycle edge metrics.
- Cache the canonical Strategy Edge summary for the fast dashboard with integrity, schema, type, finite-number, and 36-hour freshness gates. Fail closed when invalid.
- Preserve all weekly and 0DTE options rows. Promote the first already-ranked qualified setup per horizon, explicitly state that horizons are separate decisions, and reject malformed directions rather than defaulting to PUT.
- Keep all existing content and recommendations; this change removes no workflow.

## Risk and boundaries
Reporting/UI only. No signal, sizing, entry, exit, broker, or position-state behavior changes. The cache is written atomically with a unique same-directory temporary file. Metrics are withheld when FIFO/order identity integrity fails.

## Validation
Targeted reporting and UX regression suite; Python compile; ruff E/W/F/B; exact-diff cold UX and adversarial reviews; Groq and Google AI Studio preship review; repository mechanical gate and CI before deployment.
