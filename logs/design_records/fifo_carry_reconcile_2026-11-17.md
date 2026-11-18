# FIFO-CARRY-1117 — the tracker's carried lots are checked against Alpaca at the start of each day

Approval-ID: FIFO-CARRY-1117
Covers: execution/portfolio_tracker.py, execution/fifo_pnl.py, tests/test_fifo_carry_1117.py
Not changed: execution/broker.py, execution/risk_manager.py, execution/quarterly_hold_manager.py,
scripts/repair_fifo_lots_1117.py
Status: rev 4 (signed: Claude, 2026-11-17). Rev 4: board seat (McKinney) — positions read twice around the fill fetch;
only symbols where both reads agree are reconciled; edge cases defined.
Prior: rev 3 (signed: Claude, 2026-11-17). Design only. Rev 3: reviewer #2 round 2 — a symbol whose carried lots mix
long and short is dropped whole.
Prior: rev 2 (signed: Claude, 2026-11-17). Rev 2: reviewer #2 — an opposite-sign carry is dropped, not
left in place.

## Problem (measured 2026-11-17/18 from Alpaca fills and the server's state file)
- GitHub issue #984: at 03:00 UTC 11-17 the tracker logged "PLTR: buy_to_cover with no open short lots (net_qty=-6,
  qty=8)". Alpaca fills: the Day tier shorted PLTR 6 + 2 = 8 at 159.44 (order 1a9e5b9d, 20:26:33-34 UTC 11-16) and
  covered 8 at 160.04 (order 971153dc, 20:58:10 UTC). The account had no other PLTR fill since at least 2026-10-20.
- The tracker log shows only one short-open line, for the 2-share piece, with net_qty=-4 before it. So the tracker
  started 11-16 carrying a 2-share PLTR LONG lot that did not exist at the broker. The 6-share short was matched
  against that phantom long (a fake closed trade), leaving 4 short; 4 + 2 = 6 short against an 8-share cover, so 2
  shares of the real Day-tier trade were dropped from the report.
- The same file today: 29 of its 33 symbols carry lots Alpaca does not hold (checked 2026-11-18 05:3x UTC against
  /v2/positions): MARA 29 long, RIVN 9 long, SOFI 4 short, TSLA 3 short, UBER 3 short, HOOD 3 long, AAPL 3 long vs 1
  held, and 22 more. The next Day or Swing trade in any of these symbols will be mis-reported the same way.
- Why: `_load_prior_day_lots` (fifo_pnl.py:358) loads yesterday's lots as-is and only warns when the file is more than
  a day old; nothing ever compares them to the broker. Lots go stale whenever a fill is missed (restart, outage,
  manual trade, an earlier bug) and then live forever.

## Change
1. `portfolio_tracker.write_eod_summary`, on the first pass of a new PT day only (the lots file's stored date parses
   and is before today; a missing file or an unparseable date skips the check, as today): read Alpaca positions (P1,
   `execution/broker.py`'s existing position call, short timeout), fetch today's fills (the pass already does), then
   read positions again (P2). A symbol is reconciled only when (a) its P1 and P2 quantities are
   equal and (b) the fetched fills hold no fill for it today. Then no fill landed between the reads, and start-of-day
   quantity = int(P2 quantity) with the FIFO's `int(float(qty))` truncation; no fill arithmetic is involved. Residual
   case [hypothesis — unverified]: if Alpaca listed a fill in positions before listing it in fill activities, a fill in
   that gap would be counted twice. To close it, the check also reads today's orders for the symbol (the existing
   orders call, status all, after today's PT midnight) and skips the symbol if any order has a filled quantity. Any other symbol is skipped for the day
   (a symbol traded today is reconciled at the next day's first pass) with one WARNING naming both reads. QHM symbols
   are skipped entirely. The PLTR case passes (a): PLTR had no fill on 11-16 before 20:26 UTC, long after the first
   pass at the open.
2. First, a symbol whose carried lots mix long and short lots (the FIFO never leaves both sides open, so this is
   always corrupt state) has every carried lot dropped. Then, carried net equals start-of-day quantity: unchanged. Start-of-day is 0: the symbol's carried lots are dropped. Same
   sign and smaller: the oldest lots are removed first until the carried net equals it (the remaining lots are the
   most recent buys, which is what FIFO assumes is still open). Opposite sign (carried long while Alpaca's
   start-of-day is short, or the reverse): every carried lot for the symbol is dropped. Same sign and larger at Alpaca:
   unchanged, and the existing orphan-seeding step handles the missing shares as today. After the step, no symbol's
   carried lots are mixed-sign, or have a net of the opposite sign of, or a larger size than, its Alpaca start-of-day
   quantity.
3. Every change logs one WARNING `[change=fifo-carry-1117] SYM carried N, P1 a, P2 b, start-of-day M:
   dropped/trimmed K`, so each decision replays from the log alone,
   and the day's count of changed symbols is written into the state file's existing structure.
4. If either positions read fails or times out, or returns no positions while the account's long or short market value
   is not zero, nothing is changed and one WARNING says so; the pass continues as today.
5. One-time cleanup of today's 29 phantom symbols happens through this same code on the next new-day pass; no manual
   edit. `scripts/repair_fifo_lots_1117.py` (EOD-FRIDAY-1117) is not needed for it.
Tests (`test_fifo_carry_1117.py`, broker mocked): phantom long dropped; long trimmed oldest-first; carried +5 long vs Alpaca -3 short drops all five;
mixed +5 long / -5 short with Alpaca 0 drops both; a symbol with
any fill today is skipped;  QHM symbol untouched;
positions read failure, timeout, or an empty read with non-zero market value changes nothing; a fill landing
between the two reads skips that symbol (AAPL 1 @ 180 + 2 @ 185 carried, a sell of 1 between reads: no trim); 
fractional Alpaca quantities truncate like the FIFO; missing or unparseable stored date skips the check; same-day second pass does not run the check; the PLTR 11-16 fill sequence
replayed from the real fills gives one 8-share short trade (159.44 -> 160.04, -$4.80) and no CRITICAL line.

## Required questions
Q1: Every Day and Swing trade in a stale symbol is mis-reported; post-mortems and the tracker's trade list read it.
Q2: No trade-count change.
Q3: Simplest form: one comparison against the broker's own positions once per day.
Q4: Same family as EOD-FRIDAY-1117 (PR 983, merged) and #977; nothing open touches this block.
Q5: Not applicable because no threshold is introduced; quantities come from Alpaca.
Q6: Forward: the same start-of-day check can feed the anomaly rules (a phantom lot becomes a detected event).
Q7: Interconnect: the result feeds only the tracker report; kill switch and sizing are not changed.
Q8: Measured: Alpaca fills for 11-16, the server log lines at 20:28:01 and 03:00:02, and the 29-symbol comparison.
Q9: Unit tests with the real 11-16 PLTR fills replayed; a dry run on a copy of the server's state file.
Q10: Not applicable because it is accounting, not a trading edge.
Q11: No size, frequency or exposure change.
Q12: No safety-envelope change; the kill switch uses its own fills measure, not these lots.
Q13: Positions read fails: no change, one WARNING, today's behaviour.
Q14: Not a shadow: it changes the live report state.
Q15: Each change is one log line naming the symbol, P1, P2 and start-of-day; each skip logs its reason.
Q16: Proof: the next new-day pass logs `[change=fifo-carry-1117]` for each symbol it reconciles; untraded phantom symbols match Alpaca after it.
Q17: The first pass after the next PT midnight.
Q18: The anomaly closed: tracker lots that never get compared to the broker.
Q19: Hotspot file: owner approval; board, GAI and reviewer #2 on the exact diff.
Q20: Neutral prompts; tests included.
Q21: Tests mock every broker call.
Q22: Not applicable because it is not a trading control.
Q23: Tests plus the dry run on the copied state file, listing reconciled and skipped symbols separately.
Q24: Not applicable because it makes no availability claim.
Q25: Not applicable because it uses no market statistic.
Q26: T1: Alpaca positions and fills only.
Q27: Writes the existing lots file through its existing atomic write.
Q28: Once per PT day, on the first tracker pass.
Q29: Small; it stops every future phantom-lot report error.
Q30: Blind spot: a carried state with no reconciliation to its source of truth.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because there is no market replay; the fill replay uses real Alpaca fills.
Q33: Not applicable because it places no trades.
Q34: Not applicable because it has no return.
Q35: Not applicable because it is not market-dependent.
Q36: Not applicable because it trades nothing.
Q37: Not applicable because it is not a trading edge.
Q38: Not applicable because it is not a trading edge.
Q39: Not applicable because nothing is sized here.
Q40: Protects the accuracy of the daily trade report; the check: after the pass, for every reconciled symbol the carried
lots are single-sign, the same sign as Alpaca start-of-day and no larger.
Q41: Reconcile to the broker at a fixed point each day, as a back office does.
Q42: Alpaca positions and fills, the rawest data available.
Q43: Daily position reconciliation against the custodian, the standard back-office method.
Q44: The next day's log and a fresh comparison of the lots file against /v2/positions.

## Review record (rev 4)
Board (McKinney, cold): REJECT rev 3 (race between the positions read and the fill fetch at the open), APPROVE rev 4.
GAI: APPROVE rev 1-4. Reviewer #2 (Codex at its usage limit; Gro): REJECT rev 1 (opposite-sign carry kept) and rev 2
(mixed-sign lots kept), APPROVE rev 3 and rev 4.
Build notes from the board (implementation detail, no change to the rule): the orders read is taken after P2, uses
status closed filtered on filled_at after today's PT midnight (so a GTC stop submitted yesterday and filled today is
seen), pages through every result, and on failure changes nothing with one WARNING; every skip logs its reason; a
short test uses an Alpaca quantity string "-3" against 5 carried short shares (trims to 3); the dry run reports
reconciled and skipped symbols separately (only symbols that pass the check are cleaned on the first day).
Follow-up noted by the board, not in this change: a symbol the bot trades in its first cycle every day is skipped
every day; a later change can run the check before the first order or verify a complete fill set.
