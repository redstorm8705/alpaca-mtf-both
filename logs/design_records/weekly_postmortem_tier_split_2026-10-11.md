# Weekly post-mortem tier and lifecycle accounting — 2026-10-11

**Author/signature:** OpenAI Codex (OpenAI Codex)

## Defect

The weekly post-mortem rebuilt FIFO from fills by symbol and grouped rows by symbol,
direction, and exit date. With same-direction co-holds live, that could merge Day and
Swing lots. It also counted a realized partial exit as a completed trade and used a
nearest-time, same-symbol event to attach exit metadata. The report therefore could
publish a correct account cash total beside incorrect tier ownership, W/L, and reasons.

## Shipped model

The report consumes one canonical Alpaca fills-plus-orders FIFO snapshot from
`reporting.report_figures`. Entry-order client IDs provide ownership. Historical aliases
are normalized to the only four tiers: Day, Swing, QHM, and F6.

Two accounting layers are explicit:

1. **Weekly realized cash** sums every closing fill whose PT close date is in the week,
   with a tier split that must reconcile exactly to the account total.
2. **Completed lifecycle outcomes** group all partial closes by exact entry order and
   count W/L only when the lifecycle is fully closed and its final close is in the week.
   Prior-week partials remain part of full-lifecycle P&L but not this week's cash.

Open lifecycles with partial exits contribute to weekly cash and are disclosed separately;
they cannot inflate trade count or win rate. Same-symbol co-holds remain separate because
their entry order IDs and tiers are distinct. Score, MRI, and exit reason are enriched only
through an exact broker exit-order ID; missing identity stays unknown.

The report fails closed without figures when broker history, fill-to-order attribution,
close identity, lifecycle identity, or current-week tier attribution is incomplete.

## Cold-second rejection and correction

The first staged revision was rejected. It could omit an earlier leg when that leg's raw
tier conflicted with the final leg, chose metadata from a sorted set of order IDs rather
than the chronological final exit, and accepted truthy strings/non-finite numerics.

The corrected view selects relevant lifecycles by exact entry order ID, then validates
every historical leg has the same symbol, direction, and canonical tier. It requires a
real Boolean completion flag, positive whole-share quantity, positive finite prices,
finite P&L, parseable timestamps, and matching PT exit date. Metadata joins only the
chronologically final broker exit order and requires the event symbol to match.

The second staged revision was rejected because whitespace-padded entry IDs could evade
the second-pass join, the raw `exit_date` could hide a malformed current-week close before
validation, and entry timestamps after exits were accepted. The final view validates exact
untrimmed order identity, timestamps, PT close date, and entry-before-exit chronology for
every canonical row before deciding week membership. It permits distinct entry timestamps
within one entry order because Alpaca can fill one order in several executions; live data
contained this valid pattern.

The third cold review found that ISO timestamps with different UTC offsets were still
sorted as strings. The final implementation carries parsed instants through lifecycle
ordering and chooses the final exit and entry/exit extrema by actual time. A mixed-offset
regression proves metadata follows the true final broker exit.

The fourth cold review found that trimming a padded symbol could merge a malformed prior
leg into a current lifecycle. Exact symbols now fail closed on surrounding whitespace.
The independent claims review then found two proof gaps: account reconciliation was not
part of the publication gate, and distinct exit orders tied at the latest timestamp made
the final metadata identity unknowable. Publication now requires the canonical account
invariant to be exactly true and rejects an ambiguous final-exit tie. Reversed-input tests
prove the result no longer depends on source ordering.

## Live read-only replay

The 2026-10-05 through 2026-10-09 production snapshot contained 453 FIFO close legs,
zero unmatched closes, zero missing order joins, and zero missing close identities. The
new view reconciled weekly realized cash to **+$7.36**:

- Day **+$3.01**
- Swing **−$0.10**
- QHM **+$4.45**
- F6 **$0.00**

The replay also proved why the two layers matter: NFLX's fully closed Swing lifecycle was
**+$2.27**, while only **−$0.10** of that lifecycle was realized during the target week.

## Scope

Reporting only. No signal, entry, exit, sizing, stop, order, allocation, or tier ownership
behavior changes. The first scheduled Saturday/Sunday run remains post-live evidence.
