# KILL-EXIT-JOIN-1117 — the loss limit's exact measure stays exact when a QHM/F6 lot is fully sold

Approval-ID: KILL-EXIT-JOIN-1117
Covers: execution/risk_manager.py, execution/quarterly_hold_manager.py, tests/test_kill_exit_join_1117.py (new)
Not changed: run_ledger_sync.py, execution/ownership_guard.py, execution/forever_hold_manager.py, config.py
Status: rev 1 (signed: Claude, 2026-11-17). Design only.

## Problem (measured 2026-11-17, server log + broker orders; issues 961 and 936)
- QHM's last GOOGL share was sold by its GTC stop (QH-GOOGL-s-1794582132824, submitted 11-13, filled at the 11-16 open).
  After the ledger healed GOOGL to flat, every kill check that day hit `execution/risk_manager.py:1921-1927`: a fill
  tagged qhm today on a symbol the ledger no longer lists -> "protected_fill_on_ledger_flat_symbol" -> exact join
  unresolved -> ledger rebuild requested. A rebuild cannot change that outcome (the fill and the flat ledger are both
  correct), so the join stayed unresolved until the PT date rolled over (resolved 2026-11-17 08:04 UTC).
- Effect on 11-16: 222 checks ran on the KILL-BACKUP measure instead of the exact one (entries were not blocked), and
  ledger_sync ran 226 times (about 40 an hour, 18 s each) instead of about 35 a day. Same reason for GE on 11-12/11-13.

## Change
1. `risk_manager.py` exact join: a fill tagged qhm or forever6 on a symbol the ledger has no protected row for is the
   protected tier's own exit, and is excluded like every other protected fill, when ALL hold: the fill is a sell, the
   broker shows no position in that symbol now, and the sum of today's protected-tagged sells in that symbol is not
   more than the sum of today's protected-tagged buys plus the protected quantity the ledger held at the start of the
   day (read from the same ledger checkpoint the kill check already uses; if that start-of-day figure is unreadable,
   the join stays unresolved as today). Any other protected fill on a ledger-flat symbol (a buy, or a symbol the
   broker still holds) stays unresolved exactly as today.
2. `run_ledger_sync.py` rebuild runner: a rebuild request whose fills/positions fingerprint equals the fingerprint of
   the last COMPLETED rebuild is skipped (logged once per fingerprint), the same back-off it already applies after a
   refused rebuild. The scheduled pass every 20 minutes still runs.
3. Each excluded exit fill is logged once per symbol per day with `[change=kill-exit-join-1117]`.

## What it does not change
The 7% limit, which tiers count (Day + Swing; QHM/F6 excluded per Architecture Invariant 6 / KILL-ATTR-1023), the
backup measure, the fail-closed paths. A Day/Swing fill is never excluded: the tier comes from the exact order lookup.

## Tests
GOOGL replay (QHM stop-out at the open, ledger healed flat, Day round trip later): exact before and after the heal;
the Day loss is still counted. Refused cases: protected buy on a flat symbol; protected sell while the broker still
holds the symbol; protected sells exceeding start-of-day plus today's buys; start-of-day unreadable. Runner: identical
fingerprint after a completed rebuild is skipped; a new fill triggers a rebuild.

## Rev 2 (2026-11-17) — from the GAI and reviewer #2 (Gro; Codex at its usage limit) design reviews
- The own-exit test is EQUALITY: today's protected-tagged sells in the symbol == the ledger's start-of-day protected
  quantity + today's protected-tagged buys (Gro). Fewer sells means some protected shares left another way; more
  means an over-sell; both stay unresolved as today.
- A fill whose tier lookup fails or is ambiguous is never excluded (already the unresolved sentinel; now tested).
- A distinct log line when the start-of-day protected quantity is unreadable (GAI).
- Added tests: a Day and a Swing fill in the same symbol on the same day as a QHM full exit are both counted (GAI);
  over-sell (start 10 + buys 2, sells 13) and under-sell stay unresolved (Gro); a refused rebuild followed by a
  completed rebuild with the same fingerprint, then a new fill, behaves as specified (Gro); exactly one change-tag log
  line per symbol per day (Gro).

## Rev 3 (2026-11-17) — from the board seats (Thorp, Taleb masked-loss; Peterffy, Kim). Supersedes Change 1-2 and Rev 2's quantity rule.
- No start-of-day figure exists in the code (the checkpoint holds claims as of the last rebuild, which read 0 for
  GOOGL after the heal), so the quantity proof moves to the protected tier's own record. A protected-tagged sell fill
  on a symbol the ledger has no protected row for is the tier's own exit, and is excluded, only when ALL hold:
  (a) its order id is the exit or stop order id recorded on that tier's CLOSED position for the symbol in the tier's
  own state file (QHM: data/state quarterly-hold state, which keeps `stop_order_id` on a CLOSED position; F6: its
  state file); (b) the total filled quantity of that order today equals the quantity that closed position held
  (`qty_filled`); (c) the broker shows no position in the symbol. Anything else stays unresolved as today.
- Taleb's masked-loss case is refused by (b): QHM held 1, a stale stop sold 2 (one was a Swing share) -> 2 != 1 ->
  unresolved -> the backup measure counts the Swing share's loss.
- Change 2 (skip a rebuild whose fingerprint equals the last completed one) is dropped (Kim, Peterffy: the fingerprint
  does not cover every ledger input, so the skip could starve a needed rebuild). Rebuild requests keep their existing
  write-rate limit; removing the false "unresolved" removes the loop on 11-16.
- Covers is unchanged in practice: run_ledger_sync.py is no longer edited (listed under Not changed).
- Tests: the GOOGL 11-16 replay (exact all day after the heal); the stale-stop case above stays unresolved; a Day and a
  Swing fill in the same symbol are counted; order id not on the closed record -> unresolved; state file unreadable ->
  unresolved; one change-tag line per symbol per day. A read-only run on the server against the 11-16 fills before
  the change is called live.

## Rev 4 (2026-11-17) — from the board's round-2 review (both pairs: the proof did not hold on QHM's registered-stop
close path). Supersedes Rev 3 where they differ.
- Measured on the server (2026-11-17): GOOGL's CLOSED record in data/state/quarterly_holds.json is
  {state CLOSED, qty_filled 1, stop_order_id c6873dcc-989f-4d8f-b145-8319aabc3f4b, closed_reason qhm_stop_out}, and
  that order is QH-GOOGL-s-1794582132824-111eb63c, filled 1 at 2026-11-16 14:31:58 UTC. So the 11-16 case closed on
  the external-close path (quarterly_hold_manager.py:3240-3319) and the Rev 3 proof matches it.
- The registered-stop close path (quarterly_hold_manager.py:2588-2597) clears stop_order_id and sets qty_filled to 0
  before marking CLOSED, so the proof would not match there (safe: stays unresolved, but no fix). Change: in the same
  save that marks a position CLOSED, on BOTH close paths, QHM records `exit_order_id` and `exit_qty` (the order that
  closed it and the shares that order closed). The kill check reads those two fields; it no longer reads
  stop_order_id/qty_filled. Same rollback rules as the existing close save.
- F6 is not covered: it has no CLOSED record (its sells are trims in the trim state). A forever6-tagged fill on a
  ledger-flat symbol stays unresolved exactly as today. (F6 is currently off.)
- Tests drive each QHM close path through its real code to produce the CLOSED record (no hand-written fixture), then
  run the kill join on it; plus the 11-16 GOOGL record as found on the server.
- From reviewer #2 (Gro, round 3): the read of QHM's state in the kill check is guarded (missing file, unreadable
  JSON, I/O error -> unresolved, one log line `[change=kill-exit-join-1117] closed-record unreadable`); the two new
  fields are documented in QHM's position docstring; added tests for a stop order that fills across two days (the
  first day's partial fill is not excluded) and for a read error while QHM is writing the state file.
- Covers adds execution/quarterly_hold_manager.py (two close paths only). Because QHM-OWNERBUY-1113,
  HOLD-NEWS-REVIEW-1113 and QHM-OVERWEIGHT-1113 also change that file, this ships after QHM-OWNERBUY-1113, one change
  per commit.

## Required questions
Q1: Keeps the kill measure exact so Day/Swing keep trading on a measured number; no capital-preservation change.
Q2: No trade-count change; entries were not blocked on 11-16 because the backup carried.
Q3: Should exist: the check flags the protected tier's own exit as unknown. Simplest form: recognise that one case.
Q4: Builds on KILL-JOIN-1107, KILL-ATTR-1023, KILL-BACKUP-1111 (PR 951); no open PR touches these functions.
Q5: Not applicable because no number is introduced; quantities come from fills, the ledger and the broker.
Q6: Forward: the same "own exit" rule for any future protected tier, by tier tag, not by name.
Q7: Not applicable because no trading signal changes.
Q8: Cases: GOOGL 11-16, GE 11-12 and 11-13, the 11-11 QQQM and 11-13 MUD/AMD/NVD rows (those stay as today).
Q9: Replay of the 11-16 GOOGL fills and positions through the new join in the tests.
Q10: Not applicable because no signal threshold is involved.
Q11: No size, frequency or exposure change.
Q12: Safety envelope unchanged: the 7% limit and its tiers are the same; only a false "unresolved" is removed.
Q13: Unreadable start-of-day figure, unreadable fills or positions: unresolved / backup, exactly as today.
Q14: Not a shadow: it changes the live measure's resolution.
Q15: Each excluded exit fill logs one line with the change tag.
Q16: Proof: on the next QHM/F6 full exit, the kill check stays exact (no "unresolved" line for that symbol).
Q17: The next day a QHM or F6 lot is fully sold.
Q18: The anomaly closed: 222 backup checks and 226 ledger syncs on 11-16.
Q19: Kill-switch path: board seats (Thorp, Taleb masked-loss), GAI and Codex review, owner approval.
Q20: Neutral to positive on API load (about 190 fewer ledger syncs on such a day).
Q21: Tests mock all broker and data calls.
Q22: The masked-loss question: a Day/Swing loss cannot be excluded because exclusion needs a protected order tag.
Q23: Unit tests plus the 11-16 replay.
Q24: Not applicable because it makes no availability claim.
Q25: Not applicable because no market statistic is used.
Q26: T1 Alpaca fills and positions as today; the ownership ledger file.
Q27: No new state file; one log line per excluded exit.
Q28: Inside the existing kill check (every cycle) and the existing ledger_sync runner.
Q29: It removes a daily loop seen on 11-12, 11-13 and 11-16.
Q30: Blind spot: the join had no notion of a protected tier closing its own lot.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because there is no market replay; the replay is of fills.
Q33: Not applicable because there is no trading cost.
Q34: Not applicable because there is no return stream.
Q35: Not applicable because no market regime is involved.
Q36: Not applicable because there is no sizing or liquidity.
Q37: Not applicable because no statistical edge is claimed.
Q38: Reversal: a Day/Swing loss is ever excluded, or an exit fill still produces "unresolved".
Q39: Not applicable because it sizes nothing.
Q40: Outcome: zero "protected_fill_on_ledger_flat_symbol" lines for a protected tier's own full exit.
Q41: Next: the 18 s per ledger_sync run (about 3.4k orders paged each run).
Q42: The fills API and the ownership ledger.
Q43: Reconciliation practice: an explained break (own exit) is matched, not escalated.
Q44: The first QHM/F6 full exit after deploy, checked in the log.
