# REPORT-STATS-1109 — the midday and after-close cards show each tier's real trade record from Alpaca fills (design record)

Approval-ID: REPORT-STATS-1109
Covers: reporting/pnl_ledger.py, scripts/audit_slack.py, tests/test_report_stats_1109.py
Not changed: midday_audit.py, nightly_audit.py, execution/portfolio_tracker.py, tests/test_audit_card_pnl.py

Status: rev 1 for owner approval (signed: Claude, 2026-11-09). Report-only: no trading behaviour changes.

## The problem (measured 2026-11-09)
- The owner: "the win rate has been stuck at 34.6% for the past several weeks in the mid day and post market reports."
- The cards' "Lifetime" block reads `all_time_stats` from the EOD file, built by `portfolio_tracker.get_stats()` from the old
  tracker (`trade_log.json`): 186 closed trades, last exit 2026-10-13, `win_rate 34.6`, `profit_factor 0.83`. That tracker
  records only the core intraday tier; Day, Swing and QHM trades never reach it. Every EOD file from 2026-10-23 to
  2026-11-09 shows win_rate 34.6 and 186 trades.

## The fix
- `reporting/pnl_ledger.trade_stats(round_trips, since=None)`: from the FIFO round trips `compute_realized` already builds
  (each carries its entry tier from the order's client id), one trade = one closing order per tier; per tier and in total:
  trades, wins, losses, win rate, profit factor (n/a with no losing trade), average win/loss, net. Per-trade P&L is rounded
  to cents before counting, so float noise never makes a $0.00 trade a win.
- `scripts/audit_slack._trade_record_fields`: the card's block becomes "Trade record · Alpaca fills (lifetime, month to
  date)", one field per tier with trades (Day, Swing + core, QHM, Forever 6, Untagged, All), e.g.
  "Day — 45% win · 85 trades · PF 1.75 · net $177.26 · Nov: 71% of 21 · $156.74". It reuses the nightly card's own fills
  read; the midday card reads fills and orders once more (strict). If the full history cannot be read, ONE field says
  "unavailable — Alpaca fills unreadable this run"; the frozen tracker numbers are never shown again.
- Dry run on the server with live fills (read-only, 2026-11-09): Day 45% of 85 (PF 1.75, +$177), Swing + core 33% of 94
  (PF 0.28, −$461), QHM 50% of 14 (PF 0.63, −$82), Untagged 56% of 162 (PF 1.72, +$533; pre-tagging and manual orders),
  All 47% of 355 (PF 1.09, +$167).

## Tests
`tests/test_report_stats_1109.py` (7): one trade per closing order and tier; the since filter; empty input; float noise is
a zero trade; card fields come from the ledger; unreadable fills → one unavailable field; the card ignores the EOD
`all_time_stats`. With `tests/test_audit_card_pnl.py`, 32 pass on Python 3.10. Cold review PASS (two rounds).

## Required questions

Q1: Edge evaluation: the owner sees each tier's real record (Swing + core is the drag; Day the earner).
Q2: No change to the number of trades; only the Slack card text changes.
Q3: Reuses compute_realized; one pure function plus the card.
Q4: Replaces the tracker stats on these cards only; the weekly review and tracker are unchanged.
Q5: No thresholds; month-to-date is the calendar.
Q6: Forward pass: per-tier edge tracking (rolling expectancy by tier) for the weekly post-mortem.
Q7: The same numbers can feed the per-tier Kelly inputs later (separate change).
Q8: §1 measured from trade_log.json and the EOD files.
Q9: Replayed on live fills (dry run above); wrong if a tier's count disagrees with its closing orders.
Q10: Not applicable — no trading signal is involved; this is a report of past fills.
Q11: No size/frequency/exposure change.
Q12: The safety envelope is untouched: no stop, kill switch, paper flag or data-tier code changes.
Q13: Fills unreadable → one "unavailable" field.
Q14: Not a shadow: the card shows real past fills, and no decision is computed and left unused.
Q15: The card makes no trading decision; each number is recomputed from Alpaca fills every run.
Q16: The first midday card after deploy shows the "Trade record" block.
Q17: Read that Slack card.
Q18: The frozen number is a defect.
Q19: No entry, exit, sizing or routing change; the change is report text only.
Q20: Review prompts carried facts only; two cold reviews passed; the numbers come from a live read-only run.
Q21: Tests mock the fills read.
Q22: Dry run above.
Q23: Dry run above.
Q24: Not applicable — the change makes no claim about whether an instrument exists or trades.
Q25: Not applicable — the change quotes no market statistics; every number is computed from the account's own fills.
Q26: T1 Alpaca fills/orders (strict pagination); fallback: the unavailable field.
Q27: Slack card only; no file writes.
Q28: Cron processes midday_audit.py / nightly_audit.py, not the trading thread.
Q29: Owner-raised; the cards are how he judges each tier.
Q30: Untagged history should be attributed later.
Q31: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q32: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q33: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q34: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q35: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q36: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q37: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q38: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q39: Not applicable — this is a report of realized fills per tier, with no edge, replay or sizing claim to test.
Q40: Protects "the card's win rate is current"; check: the card's All count equals the closing orders in the fills.
Q41: Per-tier expectancy trend.
Q42: The rawest data: Alpaca fills and orders, matched FIFO, with no summary file in between.
Q43: FIFO round trips from fills.
Q44: Alpaca fills vs the card.
