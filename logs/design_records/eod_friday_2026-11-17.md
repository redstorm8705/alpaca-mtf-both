# EOD-FRIDAY-1117 — Friday's session gets its end-of-day lot pass

Approval-ID: EOD-FRIDAY-1117
Covers: server-ops, scripts/install_nightly_restart_cron.sh (new), scripts/repair_fifo_lots_1117.py (new), tests/test_eod_friday_1117.py (new)
Not changed: execution/portfolio_tracker.py, execution/fifo_pnl.py, execution/swing_overnight.py, main.py
Status: rev 1 (signed: Claude, 2026-11-17). Design only.

## Problem (measured 2026-11-17; issues 977 and 979)
- The tracker's FIFO carries open lots forward only in the end-of-day pass that runs when the bot shuts down. The
  shutdown is the nightly restart: crontab `0 6,7 * * 1-5 ... cron_tz_wrapper.py 02:00 sudo systemctl restart mtf-bot`.
  In UTC days 1-5 that is 02:00 ET Monday to Friday, which processes the Sunday to Thursday PT sessions. Friday's
  session would be processed at 02:00 ET Saturday (UTC day 6), which is excluded.
- 2026-11-13 (Friday): the trend-night QQQM lot was bought at the close (2 shares at 279.91). No pass ran for 11-13
  (log: no shutdown pass at 2026-11-14 07:00 UTC). On 11-16 the sale of those 2 shares found no lot and no tracker
  record and was booked as a "synthetic short" (CRITICAL at 15:28 UTC). Any position opened on a Friday and closed
  the next week hits the same gap; the trend-night tier opens one on most Fridays.
- Authoritative P&L (the Alpaca fills ledger) is unaffected. The tracker's closed trades feed the Kelly stats rebuild,
  so a synthetic short can distort that input.

## Change
1. Restart day list `1-5` -> `1-6` (adds 02:00 ET Saturday = Friday 23:00 PT). Installed by a new idempotent script
   `scripts/install_nightly_restart_cron.sh` that edits only that one line (same pattern as the other install_*_cron.sh
   scripts), so the change is in git, not a hand edit. On a weekend restart the bot finds the market
   closed and places no orders.
2. One-time repair after deploy: run the end-of-day FIFO pass for 2026-11-13 and 2026-11-16 in order (the existing
   pass, invoked for those dates by the install script's `--repair` option only if the pass already accepts a date;
   otherwise the two QQQM synthetic rows are corrected by the existing heal tool), then confirm no QQQM synthetic short
   remains in closed trades.
3. Prevention: a test that reads the restart line and proves every weekday PT session (Mon-Fri) maps to exactly one
   02:00 ET restart, and the post-live check: a new [post-live] error if a session date has no end-of-day pass line.

## Rev 2 (2026-11-17) — from the board seats (Peterffy, Kim; Thorp, Taleb). Supersedes Problem's mechanism and Change 2.
- Mechanism corrected (measured): the end-of-day pass runs on every regular-hours cycle and at shutdown, not only at
  shutdown. The last regular-hours pass on Friday 11-13 saved lots at 20:55:24 UTC, before the market-on-close fill at
  20:59:57; Monday 11-16 likewise at 20:55:52. Monday-Thursday sessions are caught up by the 02:00 ET restart's pass
  (11-17 07:00 UTC saved lots for 11-16); Friday's is not, because the restart excludes Saturday. So the restart is the
  only pass late enough to see the close-auction fill.
- The phantom is still stored: `data/state/open_lots_prior_day.json` on the server carries two QQQM SHORT lots (1 at
  274.69, 1 at 274.79). Unrepaired, Friday 11-20's trend-night buy would be booked as covering them.
- Change 2 replaced: the pass takes no date (`write_eod_summary` uses today's PT date), so a past-date re-run is not
  possible. New owner-run script `scripts/repair_fifo_lots_1117.py` (dry-run by default; `--apply` writes): removes the
  two QQQM short lots from the open-lots file (atomic tmp + replace, backup kept), rewrites the QQQM closed-trade rows
  for 11-16 from the Alpaca fills (buy 2 @ 279.91 on 11-13, sell 2 @ 274.74 on 11-16), and triggers the existing Kelly
  rebuild. It prints before/after and refuses if the file no longer matches what it expects. Run before 2026-11-20.
- Forward (logged, not in this change): make the end-of-day pass event-driven (latch "books closed" only after every
  filled order of the session, including close-auction fills, is visible), so it no longer depends on the restart.
- Covers adds `scripts/repair_fifo_lots_1117.py` (new) and its test.
- Board round 2 (Peterffy, Kim): `--apply` runs only while mtf-bot is stopped (the script checks `systemctl
  is-active mtf-bot` and refuses otherwise), because the bot rewrites the open-lots file on every pass. Sequence on the
  server: stop mtf-bot, run the script with `--apply`, start mtf-bot; before 2026-11-20 13:00 PT.
- Tests added from reviewer #2 (Gro, round 3): each PT weekday session maps to exactly one 02:00 ET restart and no
  weekend session gets one; the repair script refuses on a file whose QQQM entry differs from the expected two short
  lots, refuses while mtf-bot is active, refuses on a missing file, and writes through tmp + os.replace with a backup.
  Forward (logged): give write_eod_summary an optional date so a missed day can be replayed.
- Open question carried to the forward item, not this change: why the once-a-day after-hours pass
  (run_cycle.py:667-681) did not catch the 20:59:57 fill on 11-13 (no "AH EOD" log line that evening).

## Required questions
Q1: Keeps the tracker's trade records and Kelly input true; data collection quality, no capital-preservation change.
Q2: No trade-count change.
Q3: Should exist: one weekday is silently skipped. Simplest form: include that day.
Q4: The 02:00 ET restart line in the server crontab; scripts/install_*_cron.sh pattern; no open PR touches it.
Q5: Not applicable because no numeric threshold is introduced; it is a calendar day list.
Q6: Forward: the end-of-day pass could be its own job rather than a side effect of the restart.
Q7: Not applicable because no trading signal changes.
Q8: Cases: 11-13 -> 11-16 QQQM (synthetic short) vs 11-10 -> 11-11 QQQM (no error, Tuesday pass ran).
Q9: The test maps each PT session to its restart; the repair is checked on the real 11-13/11-16 fills.
Q10: Not applicable because no signal is involved.
Q11: No size, frequency or exposure change.
Q12: No safety-envelope change.
Q13: If the Saturday restart fails, the next run behaves as today; nothing new can block trading.
Q14: Not a shadow: the restart line and the repair act on live state the day they ship.
Q15: Not applicable because no trade decision is made.
Q16: Proof: a shutdown pass line at Saturday 07:00 UTC, and no FIFO orphan for the next Friday trend-night lot.
Q17: The first Saturday after deploy (2026-11-21).
Q18: The anomaly closed: the 11-16 CRITICAL synthetic short.
Q19: Server-ops change: owner approval; GAI and Codex review the script and the exact cron line.
Q20: Prompts were neutral (bias filter passed); the mechanism was corrected after the board found it overstated.
Q21: No broker contact in the test; the repair reads fills only.
Q22: Not applicable because it is not a trading control.
Q23: Unit test of the day mapping; repair checked on real data.
Q24: Not applicable because it makes no availability claim.
Q25: Not applicable because it uses no market statistic.
Q26: Alpaca fills (T1) for the repair, as the pass already uses.
Q27: The crontab line; the tracker's existing files, written by the existing pass.
Q28: Server crontab; the bot's shutdown path.
Q29: Fixes a weekly data error.
Q30: Blind spot: a calendar gap between UTC cron days and PT sessions.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because there is no market replay.
Q33: Not applicable because there is no trading cost.
Q34: Not applicable because there is no return stream.
Q35: Not applicable because no market regime is involved.
Q36: Not applicable because there is no sizing or liquidity.
Q37: Not applicable because no edge is claimed.
Q38: Reversal: a Friday lot sold the next week still produces a synthetic short.
Q39: Not applicable because it sizes nothing.
Q40: Outcome: every PT session gets one end-of-day pass; machine check = the test plus the post-live line.
Q41: Next: decouple the end-of-day pass from the restart.
Q42: The server crontab and the bot log.
Q43: Batch-job calendar practice: schedule by the business calendar, not the server's UTC weekday.
Q44: The Saturday 2026-11-21 log line and the next Monday trend-night sale.
