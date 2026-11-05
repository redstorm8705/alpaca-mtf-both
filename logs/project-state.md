# alpaca-mtf-bot — project state (2026-11-04 PT)

**Bot:** paper account, OCI services mtf-bot/mtf-writer/mtf-http/nginx; main @ 8fcd50f (GitHub). Account equity and the
full position list were NOT re-read on 11-04 (cloud session, no Alpaca/OCI access); last confirmed equity ~$2,696 (11-01).
Position changes since 11-01 known from the handoff: AAPL Swing lot stopped out at the 11-03 open ($327.34); XOM Swing
stopped out 11-04 (−$34.98); GOOGL QHM tranche 1 filled 11-02 (ACTIVE, 1 sh, GTC stop).

**Shipped 11-02 to 11-04:** QHM saves state the moment a tranche fill lands (#817); trend_night QQQM sizing ignores
Day-tier lots closed before the close (#822); Swing breakout entry needs a fresh last trade at or above the level (#823);
news HALT keywords match whole phrases, false halts removed (#825); earnings-document reader, EDGAR + transcripts, inert
(#836); Schwab NBBO quote client, inert until login (#837, #838); dashboard one row per tier with targets and today's
trades (#839). All deployed-unexercised unless a post-live audit says otherwise.

**Approved, build queued:** GAP-EXIT-1029 (non-earnings gap-downs wait); EARN-GAP-REVIEW-1104 (no sale on an earnings
gap-down until a document review decides QHM promotion or a bounce target; owner to add ALPHAVANTAGE_API_KEY on OCI);
LOSS-PM-1104 (same-day post-mortem for every losing trade + weekly remedy roll-up).

**In design:** CATALYST-DYNAMIC-1104 — replace the keyword bad-news entry block with a whole-article reading (two AI
readers, verbatim quotes, entry-block only). Board 5/5 approve-with-changes; GAI + Codex review owed. Found live defects in
today's gate: newer news cancels a block, 72h fetch vs a 30-day window, 25-article cap, watch set misses RIVN/MU/SNDK.
Record: logs/design_records/catalyst_dynamic_2026-11-04.md. ALPHA-ENGINE-1104 design merged (#835).

**Open bugs:** tracker FIFO orphan on trend-night fills; QHM overnight-room check fails closed on the unpriced trend-night
buy; Swing breakout has no earnings check and sets up QHM-reserved names; drift detector flags QQQM.
**Overdue post-live audits:** #823, #822, #817, #813, #809, #808, #801.
**Owner decisions pending:** Day ORB data plan (prior-day SIP proxy vs paid real-time SIP); F6 enablement (F6 OFF).
Full list: logs/research/open_queue_2026-11-04.md.

**Process:** reviewers are BGC — board, GAI, Codex; Gro is the backup only when Codex is unavailable (REVIEWER-SWAP-1025).
Master Brain login restored 11-04.

**Owner preferences:** paper account, $2.5K -> $25K, data collection first, trade every day; no shadow periods; problem /
fix / real stock example in plain English; CEO-level updates only; buy the dip first on QHM names; dynamic rules, never
static keyword lists or fixed thresholds.
