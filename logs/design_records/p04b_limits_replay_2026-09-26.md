# P0-4b — book-wide caps + dynamic daily / drawdown limits: replay evidence (2026-09-26)

**Source:** read-only replay on OCI of Alpaca portfolio history (1D), fills and orders, 2026-07-01 → 2026-09-25 (61 sessions). Raw data is in `/tmp/q2_*.json` on OCI.
**Spec under test:** `bggn_tp_sizing_dynamic_limits_2026-09-25.md` Q2.

## Equity
- Equity went from $2,804.10 to $2,540.68.
- Peak was $2,864.85 (2026-08-04/05).
- **Max drawdown was 17.32% on 2026-08-31.** It was a gradual bleed, not one day.

## (b) Daily entry-halt L = clamp(min(4.2%, 2σ₂₀, CVaR95), 2%, 5.6%)
It trips on 5 days, and every one is a real loss:

| Date | Loss | L |
|---|---|---|
| 07-07 | 2.47% | 2.00% (floor) |
| 07-16 | 2.53% | 2.00% |
| 07-17 | 2.32% | 2.00% |
| 08-06 | 3.26% | 2.50% |
| 08-20 | 4.12% | 3.33% |

None fall in the 09-16/17 window. Those sessions actually moved −0.55% and +1.62%, which confirms that the 7% kill trip then was false.

## (c) Drawdown throttle m = clamp(1 − DD/DD*, 0, 1)

| DD* | Days throttled (DD ≥ DD*/2) | Days halted (DD ≥ DD*) |
|---|---|---|
| 8% | 9 | 30 of 61 |
| 10% | 11 | 26 of 61 |
| 15% | 22 | 8 (08-31 → 09-17) |

Every one of these trips reflects a real drawdown, not a false reading.

## Book-wide gross exposure (end-of-day, all fills, valued at close)
- The book-wide gross ran from 0.20× to 2.98× equity.
- It was above 2.5× on 4 days: 08-26 (2.66×), 08-27 (2.98×), 08-31 (2.60×) and 09-01 (2.62×). All four were in the deep-drawdown stretch.

## Overnight cap
- `MAX_OVERNIGHT_EXPOSURE_PCT=0.40` (config.py:236) has **0 call sites**. It is dead code.
- If it were wired at 0.40, it would exceed the limit on 59 of 61 days (97%).

## Data-integrity finding (new)
Tier attribution by `client_order_id` is unreliable:
- 127 of 371 fills are untagged (pre-convention).
- Tagged entries and exits often disagree. For example, AAPL 09-11 entry was IN- and its 09-15 exit untagged; SPY 08-10 entry IN- and its 08-11 exit untagged.
- As a result, core-only and day-tier-only exposure cannot be measured from fills.
- The QHM book (GEV/LLY/GE) does tie out exactly to `quarterly_holds.json`.

## BGGN alignment (2026-09-26) — 4/4
Voices: board sizing seat (Thorp/Taleb), board portfolio seat (Dalio/López de Prado), Gro, GAI.

- **D1:** count ALL tiers against the 2.5× gross cap. Read it tag-free from live broker positions, reusing `day_trade_manager._account_gross`, instead of the core-only tracker sum. It does not depend on the unreliable tags.
- **D2:** a dynamic overnight cap derived from the held names' realized overnight-gap distribution: Σ notional × 2σ gap ≤ a fraction of equity bounded by the 7% kill. Static 0.40 is rejected because it trips 97% of days. Leaving it unwired is rejected because overnight gaps are the main tail vector. The exact fraction is a parameter for Rafael.
- **D3:** drawdown throttle at DD* = 15%. Size is full until 7.5% drawdown, then shrinks linearly to zero at 15%.
  - 8–10% is rejected: it halts about half of sessions and censors the losing-tail data needed for the entry rebuild.
  - A Monte-Carlo DD* is rejected for now: 61 sessions is about one drawdown episode (overfit risk).
- **D4:** adopt L = clamp(min(4.2%, 2σ₂₀, CVaR95), 2%, 5.6%) as specified. It blocks entries only, and caught 5 of 5 real losses with 0 false trips.

**Routing:** risk-path (Rule E), so this needs Rafael's approval. Build order after approval: D1 → D4 → D3 → D2. Each ships as its own gated diff.

## Rafael decision (2026-09-26)
- **D3 (drawdown throttle): REJECTED.**
- **D4 (early daily entry stop): REJECTED.**
- Rationale: this is a paper account, and data collection is the #1 goal. The 7% daily kill switch stays the only brake, to be revisited at real-money conversion.
- **D2 (overnight cap):** applies to SWING positions only. QHM and Forever-6 are excluded as intentional buy-and-hold positions, averaged on directional conviction.
- **D1 (book-wide gross cap):** awaiting Rafael's confirmation of scope. The open question is whether QHM and F6 are also excluded from it.
- New CLAUDE.md rule: "THIS IS A PAPER TRADING ACCOUNT".

## D1 scope re-aligned under the paper-account frame (2026-09-26) — Gro A, GAI A, board seat A
The swing gross cap (2.5× equity) counts **swing + day-tier positions read live from Alpaca, minus QHM and Forever-6 shares**.

Rejected options:
- **B (swing-only):** blind to the day tier's exposure on the same account.
- **C (everything):** a conviction add to QHM would block unrelated swing entries.

Remaining P0-4b scope for Rafael's approval:
- D1 (A).
- D2: a dynamic overnight cap on swing positions only (QHM/F6 excluded).

## Rafael decision, round 2 (2026-09-26)
- **D1: NO CHANGE.** Each tier keeps its own rules. The swing gross cap stays swing-only, which is today's code (`check_gross_exposure_for_order` sums the swing tracker only). The day tier keeps its own caps. QHM and F6 are excluded.
- **D2:** gap-risk tracking is for SWING positions only. A QHM gap-down should trigger the buy-the-dip evaluation instead.
  - That already exists: the QHM dip-add is LIVE (`_DIP_ADD_ENABLED=True`).
  - It has two rungs: Rung A at ≤ −2% below cost average and Rung B at ≤ −5%.
  - Limits: max 3 per quarter, at least 2 days apart, no adds within 7 days of earnings, stop adding below first-entry −15%.
  - It is evaluated every RTH cycle. It last fired on 2026-07-29 (NVDA rung B).
  - D2 build awaits Rafael's explicit approval.
- **F6:** Rafael wants it turned ON. A readiness audit is in progress. Blockers recorded in `tb_audit_log.md`: same-pass ledger-drift resync, the shared-lot ring-fence question, ETF wind-down detection, `OWNERSHIP_GUARD_ENFORCE=False`, and F6 positions reading NAKED in the audits.
