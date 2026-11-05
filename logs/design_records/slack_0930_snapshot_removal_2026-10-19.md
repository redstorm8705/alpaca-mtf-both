# Remove the 9:30 AM ET P&L Slack snapshot — 2026-10-19 (Claude)

Approval-ID: SLACK-0930
Covers: server-ops

## Why (owner, 2026-10-19)
"The 630 slack performance notification should be removed. It'll always be $0 since the market opens at 630 am pst
(930 est)."

## Facts
- The message is scripts/pnl_snapshot.py run by the OCI crontab line `30 13,14 * * 1-5 ... cron_tz_wrapper.py 09:30
  ... pnl_snapshot.py` (crontab line 99, verified this session).
- pnl_snapshot.py is read-only (its docstring: "fetches Alpaca + reads the ownership ledger + posts one Slack card +
  writes one log line. It submits no orders and mutates no state"). No later snapshot reads the 9:30 one.
- The hourly runs at 10:00-16:00 ET and the 18:45 ET realized card stay.

## Change
Comment out that one crontab line on the server (marked with the date and this ID). No code or repo file changes.

## Required questions
Q1: Removes a message that always reads $0 at the open; no effect on trading or data collection.
Q2: No change to the number of trades: the snapshot only posts a Slack card and submits no orders.
Q3: Yes; the simplest form is removing the one schedule line.
Q4: No other work covers it; the other snapshot schedules are unchanged.
Q5: No threshold or number is introduced; the change removes one schedule line.
Q6: Static assumption exposed: a fixed hourly schedule includes the open minute; the hourly cards remain.
Q7: The snapshot feeds nothing else (read-only, posts Slack only).
Q8: The crontab line and the script's read-only docstring were read this session.
Q9: After the change, tomorrow's 6:30 AM PT card does not post and the 7:00 AM PT card does.
Q10: No trading edge is involved; this is a reporting schedule change.
Q11: Not risk-path: no size, frequency or exposure change.
Q12: The safety envelope is untouched.
Q13: Nothing reads its output, so nothing can fail from its absence.
Q14: Not a shadow; it removes a message.
Q15: The crontab keeps the line, commented with the date and SLACK-0930, so the change is visible.
Q16: Proof: no "pnl_snapshot" log line at 13:30/14:30 UTC on 2026-10-20 and a line at 14:00/15:00 UTC.
Q17: Checked on the server on 2026-10-20 from the snapshot log.
Q18: No anomaly is explained away; the $0 reading at the open is expected.
Q19: Not an entry, exit, sizing or routing change; the owner asked for it.
Q20: Facts only; no reviewer prompt needed for a schedule removal.
Q21: No test reaches the broker; no code change.
Q22: Not a new control: nothing blocks or gates trading, so no read-only probe is needed.
Q23: The crontab is listed after the edit to confirm only that line changed.
Q24: The record makes no claim that anything does not exist or cannot trade.
Q25: The record quotes no stock or market statistics.
Q26: No data-source change.
Q27: No output change other than one fewer Slack card and log line per day.
Q28: OCI crontab, user ubuntu.
Q29: Owner request; minutes of work.
Q30: No execution-framework component is involved; only one reporting schedule line changes.
Q31: No edge claim, so no out-of-sample test applies.
Q32: No replay or backtest is used, so look-ahead and survivorship cannot apply.
Q33: No trading cost is involved.
Q34: The change adds no market exposure and no return to decompose.
Q35: No market behaviour involved.
Q36: Unaffected by account size.
Q37: No edge statistic is involved, so statistical power does not apply here.
Q38: No signal or edge exists here that could decay or need replacing.
Q39: Position sizing and the Kelly inputs are unchanged by removing this message.
Q40: Outcome: no $0 card at the open; check: the snapshot log on 2026-10-20 has no 13:30/14:30 UTC run.
Q41: An expert would also check nothing depends on the run (checked: read-only).
Q42: Rawest source: the crontab itself.
Q43: Professional method: remove the schedule entry, keep it commented for audit.
Q44: Independent evidence: the server's snapshot log the next morning.
