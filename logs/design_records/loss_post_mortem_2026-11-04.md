# Losing-trade post-mortem, every loss, every tier — LOSS-PM-1104 (signed: Claude, 2026-11-04; rev 2) — DESIGN

Approval-ID: LOSS-PM-1104
Covers: scripts/loss_post_mortem.py (new), run_loss_post_mortem.py (new), tests/test_loss_post_mortem.py (new), scripts/install_loss_pm_cron.sh (new)

Owner direction 2026-11-04 (verbatim): "Every losing trade needs a formal evaluation, remediation, and optimization plan
for future trades. Why did we enter when we did, was the target realistic given the prior moves this quarter, what was
the conviction behind the trade, why was the stop set where it was, was it dynamic relative to how the stock normally
moves, is it a concentrated, sector-specific buy or does it diversify the portfolio, what could've been done better to
make this a winning trade, did we need to enter earlier? … Do not ever just report what happened without investigating
how to make losing trades profitable in the future."

## 1. What it does
After the close each trading day (cron, ET-anchored like the other after-close jobs), for every lot CLOSED at a loss
that day in any tier (Swing breakout, trend night, Day, QHM, F6 — F6 never sells, so it only appears if a sale was
external), read-only:
1. **Facts** from the rawest sources: Alpaca fills (entry/exit price and time, from the fills API), the tier's own
   `entry`/`exit` decision stack in trade_events.jsonl (signal, level, sizing, Kelly inputs, stop), SIP daily and 5-min
   bars, FMP/Finnhub earnings dates, sector ETF bars (a fixed symbol→sector-ETF map, unknown → "none").
2. **The eight answers** (each a computed field, never free text alone):
   - entry timing: bars since the setup first qualified; earlier qualifying signals in the prior 20 sessions; entry
     price vs the trigger level in ATR units; would the first signal's lot still be open, and its P&L;
   - target realism: the strategy's exit rules, the trade's best move (MFE) and worst (MAE) in R, vs the distribution
     of the same name's moves this quarter (20/60-session ranges);
   - conviction: the decision stack's score/rank and Kelly inputs (source, n, mean R) and the size it produced;
   - stop: distance in ATR and %, where it sits in the name's own adverse-move distribution, whether the exit was a
     gap through the stop, an intraday break, or a rule exit; price now vs the stop (was it noise?);
   - concentration: the book at entry by sector (share of notional), correlation of the name with the book's
     largest holdings (60-session returns);
   - market and sector: SPY, the sector ETF and (for energy) USO over the hold, vs the stock;
   - earnings: report dates inside or right after the hold;
   - what would have won: every candidate rule in the tier's remedy library replayed on THIS trade (did it block the
     entry, change the exit, and the P&L under it).
3. **Remedy library and weekly roll-up.** Each tier keeps a list of candidate rules (for Swing today: earnings skip,
   late-breakout skip, sector-trend confirm, extension cap, break-even and chandelier stops — `logs/research/
   swing_pm/breakout_remedies.py`). Every Saturday the roll-up re-runs the tier's full history replay for each
   candidate (discovery/test split, candidate count, per-trade R AND total R, capacity-aware where slots bind) and
   reports which candidates the week's losses point to. A candidate that wins on both periods and on total R becomes
   a proposal through the normal gate and owner approval — the report never changes trading by itself.
4. **Output:** `logs/post_mortems/<date>_<tier>_<symbol>.md` (atomic write), one `loss_post_mortem` event per loss in
   trade_events.jsonl, and a Slack post in plain English: what happened, the finding that matters, the remedy evidence.

## 1b. Changes from board seat López de Prado — adopted
1. **One persistent registry** (`logs/post_mortems/remedy_registry.jsonl`) of every candidate ever tested and every read
   of the test years, across all runs; selection uses the cumulative count, not "in this run".
2. **A proposal must clear a multiple-testing-adjusted test** on paired per-trade differences against the live rule
   (Romano-Wolf / Hansen SPA over the registered candidates), OR confirm on a frozen holdout never consulted: bars after
   2026-11-04 plus live fills.
3. **Decision measure = capacity-constrained replay:** live slots, overnight-limit room and the dollar equity path under
   live Kelly sizing. Total R with unlimited slots is secondary.
4. **No "would have won" on one trade.** Each remedy that would have changed the trade shows its full-history ledger:
   losers blocked vs winners blocked, and the mean R of the trades it removes per period. (XOM: the earnings, late-
   breakout and sector filters removed trades averaging +0.85, +0.26 and +0.71 R in 2024-26 — they cut winners.)
5. **Base rates from winners:** the same facts are computed on every closed lot, winners included, so a loss feature is
   shown against how common it is among winners.
6. **Point-in-time universe membership** in the replays; a missing earnings date is excluded, never "no earnings".
7. **Every candidate resolves to propose or retire** at each evaluation; none sits in an observe state.

## 1c. Changes from reviewer #2 (Gro; Codex at its usage limit) — adopted
1. **Missing earnings dates** are excluded from any earnings-based remedy replay and shown as "unknown" in the report
   (same as 1b item 6), never treated as "no earnings".
2. **Partial and split fills:** entry and exit prices are the volume-weighted average of every fill of that lot from
   the Alpaca fills API; the report shows each fill, and stop analysis uses the stop price and the actual average exit.
3. **Small samples:** a tier with too few closed trades for a stable paired-difference estimate (judged by the width of
   the bootstrap interval, reported) can only propose a remedy through the frozen-holdout confirmation path (1b item 2),
   never on an adjusted p-value alone.
Not adopted: a daily sector-map refresh service — the map changes rarely; a name missing from it shows "sector: none".

## 2. Not in scope
No order, stop, size or entry change. Model text (GAI) may summarize, but every number comes from the computed fields.

## 3. Failure
Any missing source → that field says "unavailable (<reason>)"; the report still posts. A crash in one loss never stops
the others (each wrapped, logged). The job never runs on the trading thread.

## Required questions (RQ-4)
Q1: Data collection and edge evaluation: every loss becomes a structured record and a tested remedy candidate — the loop the owner asked for.
Q2: No trade-count change; it only reports and proposes.
Q3: Simpler: the existing weekly post-mortem (weekly_postmortem.py) is weekly and narrative; this is per loss, same day, computed answers and counterfactual replays; it reuses its bar and earnings helpers where they fit.
Q4: weekly_postmortem.py (WTP, Saturday), scripts/belief_tests.py, post-live audits; XOM manual post-mortem 2026-11-04 (logs/research/swing_pm/) is the template; no open PR on this.
Q5: All thresholds are measured from the name's own history or the tier's replay; the sector map is a fixed lookup (flagged).
Q6: Static assumption exposed: strategies run fixed rules regardless of their own loss patterns; forward pass: the weekly roll-up proposes rule changes from accumulated losses.
Q7: Feeds the board/owner proposal queue and each tier's remedy library; reads every tier's decision stack; fails safe (report only).
Q8: Every field is computed from fills, bars and logged decision stacks; model text is labelled as summary.
Q9: Validated on the XOM 11-04 loss (manual post-mortem reproduced by the job) and the AAPL 11-03 and NOW/GOOGL 10-29 losses before ship.
Q10: No validated edge is held back; this only reports and proposes.
Q11: No — read-only; no size, frequency or exposure path.
Q12: The safety envelope is untouched: no stop, kill switch, sizing or paper flag changes.
Q13: Missing data → field marked unavailable; never blocks the report or trading.
Q14: No shadow: it is a report that drives proposals; it does not compute an unused trading signal.
Q15: Each report is itself the decision explanation; it logs its inputs.
Q16: `loss_post_mortem` events and the files in logs/post_mortems/; first full session after deploy audited.
Q17: Run on the server against 11-03 and 11-04 losses before ship.
Q18: This exists because a loss was reported as "by design".
Q19: No entry/exit/sizing change; owner approves the design as a new feature.
Q20: Neutral prompts; numbers computed, not generated.
Q21: No broker calls; read-only Alpaca data and fills endpoints are mocked in tests.
Q22: Read-only run on the server before calling it live.
Q23: Full-path run on the last two weeks' losses before ready.
Q24: N/A — no existence claims made.
Q25: All stock statistics measured from SIP bars.
Q26: T1 Alpaca bars and fills; T2 FMP/Finnhub earnings; sector ETFs via T1.
Q27: logs/post_mortems/*.md (atomic), trade_events.jsonl, Slack.
Q28: After-close cron (ET-anchored), separate process; no trading thread.
Q29: High: the owner's direct mandate; turns every loss into an optimization input.
Q30: Standard practice: per-trade attribution and counterfactual analysis; the bot had only a weekly narrative.
Q31: Cumulative registry of candidates and test-year reads; proposals need a multiple-testing-adjusted paired test or a frozen-holdout confirmation (section 1b).
Q32: Remedy replays use only data available at each historical decision; universe survivorship stated.
Q33: Replays apply the tier's measured costs; live fills are the source for the loss itself.
Q34: Each report shows the stock vs SPY and its sector over the hold (how much of the loss was market or sector).
Q35: Market and sector context is a field; weekly roll-up splits by regime when enough losses accumulate.
Q36: Decision measure is the capacity-constrained dollar replay (section 1b item 3).
Q37: Intervals are on the paired difference against the live rule, with the registry's cumulative count.
Q38: A candidate that stops winning in the roll-up is dropped from the proposal list.
Q39: Reports the Kelly inputs used; no sizing change.
Q40: Protects learning from losses; check = every losing exit has a report within one session (the job asserts its count against the day's losing fills).
Q41: Add per-tier attribution (market, sector, stock-specific) and track remedy hit-rates over time.
Q42: Fills, SIP bars, logged decision stacks.
Q43: Trade-level attribution and counterfactual replay; the design uses both.
Q44: The owner reads each report; the weekly roll-up's proposals go through board + GAI + Codex.
