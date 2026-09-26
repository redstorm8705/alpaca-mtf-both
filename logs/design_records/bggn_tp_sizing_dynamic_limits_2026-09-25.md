# BGGN — 1R take-profit + size-up, dynamic risk limits, stop-replacement wait (2026-09-25)

**Asked by:** Rafael (CEO), after the diagnosis-only bot audit (`bot_audit_2026-09-24.md`).
**Voices:**
- Board, as 3 cold seats: Thorp/Taleb (sizing), Harris/Brandt (execution), López de Prado/Simons (validation).
- Gro (`openai/gpt-oss-120b`) and GAI (Gemini ladder), both given the same prompt.
- Prompt: `scratchpad/bggn_q.txt`, passed through the bias gate clean.
**Status:** ALIGNED (5/5 on all three questions). Nothing is implemented yet.

## Evidence (fills + 5-min bars, `/tmp/tp.py` on OCI)
The sample is 79 core swing trades since July that have a logged stop. The counterfactual uses only bars after the fill.

| Exit rule | Result |
|---|---|
| Actual (baseline) | avg −0.17R, PF 0.47 |
| Take profit at 0.25R | PF 0.51 |
| Take profit at 0.5R | PF 0.63 |
| Take profit at 0.75R | PF 0.74 |
| Take profit at 1.0R | hit 10/79, avg −0.10R, PF 0.64 |
| Take profit at 1.5R | PF 0.51 |
| 0.75R + breakeven after +0.5R | avg −0.07R, PF 0.74 |

Median best price reached after entry is about +0.26R. **No exit rule turns these entries positive.**

## Q1 — Take profit at 1R + bigger size
- **Consensus:**
  - A 1R target, or better a DYNAMIC target, is fine as a harm-reducer. It cuts the loss per trade by about 40% but does not make the strategy profitable.
  - **Size-up is REJECTED for now, 5/5.** The Kelly-optimal size for a negative-edge strategy is zero, and doubling size doubles the loss. At −0.10R over 79 trades that goes from about −7.9R to about −15.8R.
- **Dynamic target (all 5):** set the target from the measured favourable-excursion distribution of similar setups, by ATR/VIX regime bucket. Take about the 60th percentile, with a fallback to the pooled data when a bucket has fewer than 20 trades. Move the stop to breakeven at about the bucket median.
- **Gate before any size-up:**
  - entry fixes are live (completed bars; the always-true conditions removed);
  - ≥ 30–40 new out-of-sample trades with mean R > 0;
  - the lower confidence bound is > 0, or PF ≥ 1.2 (Thorp accepts the point estimate plus PF ≥ 1.2 under the growth mandate);
  - after that, size by quarter-Kelly computed from those trades, never by a fixed multiple.
- **Honest limits:**
  - The spread between variants (about 0.09R standard error) is inside the noise.
  - Picking the best of 9 variants on 79 trades is overfit, so any adopted rule must be validated going forward.
  - Gro's dollar figures (for example "−$254/trade") were wrong and are discarded. −0.10R on about $25 of risk is about −$2.50 per trade.

## Q1b — Score rebuild (12/12 ≈ 10/12)
López de Prado seat, with the other seats agreeing on the diagnosis:
- Score on closed bars only.
- Drop the always-true conditions and move to continuous features.
- Label thousands of historical signal events with triple barriers across the universe, rather than fitting to 79 trades.
- Add a small regularized meta-label model that outputs P(win). The score levels become calibrated probability deciles, and they must rise monotonically in win rate and mean R.
- Validate with purged cross-validation and a deflated Sharpe. Every configuration tried gets logged.

## Q2 — Dynamic risk limits (replacing the static values in audit section J)
All three approve the dynamic form (5/5). Each limit can only REDUCE exposure, never raise it past today's caps. The **7% kill switch stays the fixed outer bound**, and every limit blocks new ENTRIES only, never exits.

- **(a) Per-symbol cap:** cap_i = (b·equity) / (σ_i · √(1+(n−1)ρ̄)).
  - σ_i = max(20-day realized vol, ATR/price, overnight gap₉₉/2.33).
  - ρ̄ = average correlation to the open book.
  - b = ¼-Kelly risk budget, clipped to [0.5%, 1.5–2%].
  - Ceiling: cap_i·gap₉₉ ≤ ⅓ of the 7% kill.
  - Stale σ: use 1.5× the last good σ, or the universe 90th percentile.
- **(b) Daily entry-halt:** L = clamp(min(0.6×7%, 2σ of daily P&L over 20–40 days, CVaR95), 2%, 5.6%) of equity.
  - Under 10–20 sessions of data: use the floor.
- **(c) Drawdown throttle and halt:** size multiplier m = clamp(1 − DD/DD*, 0, 1).
  - DD* = the 95th-percentile maximum drawdown from a Monte Carlo of the rolling trade-R distribution, or Thorp's x^(2/c−1) bound at fractional Kelly c.
  - Throttle from DD*/2; halt at DD*.
  - Bounds [8–10%, 15–35%]. Whether the growth mandate should allow the wider Thorp bound is **open**. Today's negative edge pins it to the floor either way.
  - Missing history or Kelly ≤ 0: use the floor.
- **Required before build:** replay July–September equity to confirm there are no false trips.
- **Routing:** risk-path (it can change size), so it goes through the full board + masked-loss seat.

## Q3 — 1–2 s wait between cancel and resubmit
**The fixed wait is rejected, 5/5.** Alpaca documents no time bound for `pending_cancel`, and a `sleep()` blocks every other position's exit checks. The replacement mechanism:
1. Move stops with `PATCH /v2/orders/{id}` (replace). Per the docs, replace can't apply while the order is pending_*, and if the old order fills first the replace is rejected.
2. Otherwise run a per-position state machine: cancel, then poll or watch `trade_updates` until the order reaches a terminal state.

   | Old order ends as | Action |
   |---|---|
   | canceled | Submit the new stop. |
   | filled | Do NOT submit. The position is closed, and a new stop could open an accidental short. |
   | still pending | Keep it; the old stop still protects. Retry next cycle. |

3. **Every cycle, check the invariant:** each open position has a working broker stop covering its quantity. If one is missing, resubmit it and send a CRITICAL page. Flatten after 2 failures. This is what closes the UBER 204-minute class.
- **Rafael's intent (never let the race happen again) is kept.** The mechanism confirms the cancel instead of guessing a duration.
- **Needs measuring:** the paper cancel→terminal latency distribution, and how often replace is rejected.

## Next actions (build order; each is its own gated diff)
1. **Ship first (ready items):** midday ground-truth fix; "Swing" labels on the hourly card.
2. **P0:** stop replacement via replace + state machine, plus the per-cycle stop-coverage invariant. Risk-path.
3. **P0:** the kill switch keeps managing exits.
4. **P1:** dynamic exit target (MFE quantile by regime). No size change.
5. **P1:** score rebuild (closed bars, meta-labeling).
6. **P1:** dynamic limits (a)–(c) after the equity replay.
7. **Size-up:** only after the Q1 gate is met.

## Decision (Rafael, 2026-09-25)
Core swing strategy: **keep taking new entries and collect data** while entries are rebuilt (not paused).
Exits and stop protection unchanged. Size-up stays gated per Q1.
Process directive (same message): run a full self-audit of my own diff before sending it to any reviewer;
reviewers should not be the ones finding my defects.
