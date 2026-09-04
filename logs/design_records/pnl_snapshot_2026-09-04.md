# Design record — hourly per-tier P&L Slack snapshot

**Date:** 2026-09-04 · **Requested by:** Rafael (interactive) · **Author:** Claude
**Covers:** `scripts/pnl_snapshot.py` (new) · **Status:** design locked, building through the code gate

## Request (verbatim intent)
"An hourly Slack notification that gives me a snapshot of the P/L for the day, similar to what
Alpaca is showing — but a P/L snapshot of how each tier is performing, so just their daily P/L
at that moment." Cadence answer: **market hours only, hourly.** Detail answer: **headline +
per-position breakdown.** **Realized-vs-unrealized (2026-09-04):** "I actually want the snapshot
so it can be UNREALIZED. At market close and after the official reconcile, we can have it be
realized P/L." → the hourly card is **unrealized** (open positions marked to market now); a
**realized per-tier** version is a separate **post-close** job (fast-follow).

**Why unrealized sidesteps a real wrinkle:** per-tier *realized-today* via full FIFO round-trip
(exit−entry) does NOT reconcile to Alpaca's day number (equity−last_equity counts only *today's*
price move; a round-trip closed today includes prior-day appreciation). Unrealized has no such
wrinkle — it is an exact ownership-split of each open position's live `unrealized_intraday_pl`.

## Feature Design Protocol — the 5 gates
1. **Data source / tier.** Alpaca REST (T1, authoritative) via `reporting.pnl_ledger`:
   `fetch_account` (equity/last_equity), `fetch_positions` (current_price/lastday_price/
   `unrealized_intraday_pl`/qty), `fetch_all_fills` (FILL activities, carry `order_id`),
   `fetch_all_orders` + `build_coid_map`. Per-tier share qty from the ownership ledger
   (`execution.ownership_guard.load_ledger()` → `positions[sym].tiers[tier].qty`). **Per the
   P&L SOURCING RULE: Alpaca-authoritative, never tracker math.** Fallback: any missing piece
   degrades to 0 and the reconciliation self-check flags it (see gate 4).
2. **Output.** One Slack Block Kit card per invocation via `scripts.audit_slack.post_to_slack`
   (structured blocks, not a text wall). PT timestamps. One `logs/` INFO line per run. No other
   file writes; no state mutation.
3. **Integration point.** New standalone `scripts/pnl_snapshot.py`, run by an **RTH-hourly cron**
   via the existing `scripts/cron_tz_wrapper.py` (ET-anchored). It is NOT imported by the live
   trading path (`run_cycle`/broker/exit) — it only reads Alpaca + the ledger and posts Slack, so
   it cannot affect execution, sizing, scoring, or exits.
4. **Failure mode (fail-safe, never a wrong number).**
   - Snapshot computation raises → **do not post** (log error, exit 1). A missing/late number is
     never shown as $0.
   - `SLACK_WEBHOOK_URL` unset → compute + log, don't post.
   - Ownership ledger unreadable/corrupt → MTM left unattributed; the self-check catches the
     shortfall and the card says **"attribution incomplete"** with the (authoritative) account
     headline still shown.
   - **Reconciliation self-check:** Σ(tier today P&L) vs account `equity − last_equity`; if the
     gap exceeds max($2, 2%) the card flags it instead of showing a split that doesn't reconcile
     (never-mask-a-loss, applied to display).
5. **Board vote required?** **No.** Read-only reporting, zero execution/sizing/scoring/exit
   impact (Feature Design Q5). It still passes the standard **code** gate: full read of the
   sources it reuses, statics (py_compile/ruff/mypy), cold-2nd, and the Gro/GAI preship on the
   exact diff.

## Attribution design (the substantive part) — UNREALIZED hourly card
Per tier = today's **unrealized** P&L on its open positions = the tier's ownership-split of each
position's `unrealized_intraday_pl` (`tier.qty / position.qty × position.unrealized_intraday_pl`).
**Exact** — the ownership ledger tracks per-tier qty; all shares of a symbol share the same
lastday→now move, so a qty-share split is exact, not proportional-approximate.
- **Reference headline:** account day P&L = Alpaca `equity − last_equity` (authoritative, "what
  Alpaca shows"). It includes today's *realized*, so it is shown as a reference, not as the sum of
  the per-tier unrealized lines.
- **"Other / untracked" residual:** `total_unrealized_today − Σ tiers` = unrealized on any shares
  not tier-tagged in the ledger. The per-tier lines + Other sum EXACTLY to the total open
  unrealized today (never a wrong split). Shown only when ≥ $0.50 (suppress rounding residue).
- **NO per-tier realized on the hourly card** (deferred to the post-close realized version — see
  fast-follows). This removes the FIFO-round-trip-vs-today's-move wrinkle entirely.

## Tiers
`intraday`, `qhm`, `forever6`, `daytrade` (from `execution.ownership_guard._TIERS`).

## Anti-silo note
Surfaces a NEW cross-cut (per-tier day P&L) of existing signals for operator visibility. Fails
safe (never posts a wrong number). No coupling into any decision path — pure read-only reporting.

## Realized mode — SHIPPED 2026-09-04 (same file, `--realized`)
Post-close per-tier REALIZED P&L (Rafael: "at market close and after the official reconcile, we
can have it be realized P/L"). Implemented as `compute_realized_snapshot()` + `build_realized_card()`
in the same file, selected by `--realized`. Sourcing: `reporting.pnl_ledger.fetch_all_fills` +
`fetch_all_orders` → `build_coid_map`; each fill is attributed by `ownership_guard.tier_of_coid`
(untagged → intraday); `compute_realized` is run per tier and `per_day[today]` is that tier's
realized today. Every fill partitions to exactly one tier, so Σ tiers == total realized (no
residual). Per-tier FIFO is exact — tiers never cross lots. Realized = the closed round-trip P&L,
which is the correct definition here (unlike the intraday card, "round-trip" is what realized MEANS,
so there is no round-trip-vs-today's-move wrinkle). Cron: one post-close line at 18:45 ET (after the
`pnl_ledger --heal-apply` reconcile that runs 16:30–18:30 ET).

## Fast-follows (tracked)
- Optional per-tier TOTAL (open, since-entry) unrealized alongside today's — deferred.
- Strict in-script market-open check (the RTH cron schedule already bounds when it runs).

## v1 build note
Gate-1 data source is now Alpaca account + positions only (via `reporting.pnl_ledger.fetch_account`
/`fetch_positions`) + the ownership ledger; the hourly path does NOT fetch fills/orders or run
`compute_realized` (that machinery moves to the post-close realized fast-follow).
