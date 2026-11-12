# KILL-BACKUP-1111 — the kill switch never blocks on a measurement glitch; it measures from Alpaca instead

Approval-ID: KILL-BACKUP-1111
Covers: execution/risk_manager.py, execution/ownership_guard.py, tests/test_kill_backup_1111.py, tests/test_ownership_ledger.py, tests/test_kill_switch_join.py, tests/test_run_cycle_kill_switch_exits.py
Not changed: strategy/run_cycle.py, config.py, run_ledger_sync.py, data/fetcher.py, execution/ownership_ledger_codec.py
Status: rev 4 (signed: Claude, 2026-11-11). Rev 3 was owner-approved; its build review (verified on OCI) found the
production ledger never stamps `last_reconciled_utc` and every `last_fill_id` is None, so rev 3's checkpoint did not
exist. Rev 4 changes:
1. The ledger rebuild (`sync_ledger`) stamps `fills_through_utc` = the newest `transaction_time` among the fills it
   replayed, compared as parsed UTC times (board Harris: string order breaks on mixed fraction lengths; one tolerant
   parser accepts any fraction length on Python 3.10, used by both the writer and the backup), in the same save as the claims (a refused rebuild writes nothing, so claims and stamp always match). The
   ledger codec keeps extra top-level keys.
2. The backup's checkpoint is `fills_through_utc`; a ledger written before rev 4 (no stamp) means claims with an empty
   journal, so shares the claims do not explain count only when losing (never as a gain).
3. A same-day round trip (net zero today) needs no prior close: its fill terms reduce to realized cash.
4. The escalation page fires once per episode (ended when the exact measure works again); the counter is "backup
   checks today" (the kill check runs more than once per cycle).
5. Inherited from the exact measure and unchanged: an owner's untagged sale of QHM shares counts like a Day/Swing fill.
6. GAI round on rev 4 (APPROVE-WITH-CHANGES), folded in: a warning is logged whenever the backup runs on a ledger with no
   stamp (claims only, no journal). Fill times are truncated to microseconds; a fill in the same microsecond as the
   stamp is then "not after" it and, if the replay missed it, lands in the unexplained shares (counted only when
   losing): the safe direction. The load/save round trip of the stamp is tested (tests/test_ownership_ledger.py).
7. Read-only server run with the real ledger and today's real data: exact -0.66, backup -0.66 (126 tests pass).
8. Build review fixes (rev 4 build, cold second agent + adversarial review, 2026-11-11 night; owner-approved rev 4,
   every change below makes the backup stricter or removes a double count):
   a. No stamped checkpoint (no ledger read yet, a ledger rebuilt before rev 4, a stamp from before today's PT
      midnight, or a claim changed after the stamp with no fill id) -> the backup does not measure and the old
      entries-only block stays. Reproduced inputs: QHM 10 long + Day short 4 read 0 against a true -$120; an
      unstamped ledger after a QHM sale + Day buy read 0 against a true -$100. Item 2 above is replaced by this.
   b. Incremental claim writers (QHM/F6 exact fill credit and reduction) record their source in
      `credited_since_stamp`; the fill journal skips those order ids, so a QHM dip-add credited at once is not counted
      twice (it read -$10 on a flat QHM book). Writers with no fill id (owner-buy assignment, tier reduction) set
      `unkeyed_edit_since_stamp` (-> rule a). Each rebuild clears both in the same save as the stamp.
   c. The backup reuses the positions the exact measure already read this check (no second fetch on the trading
      thread). A zero or unreadable `lastday_price` falls through to the T1 daily close.
   d. The escalation page fires when the ledger FILE was rewritten after the episode began (file time, not fill
      time); the episode resets each PT day. The missing-stamp warning logs once per PT day.
   e. Round 2 of the build review: an untagged fill (an owner app sale or buy) after the stamp in a symbol with
      QHM/F6 shares -> the backup does not measure (an owner sale of 5 QHM NVDA at $210 read 0 against a true Day
      loss of -$100); an untagged fill in a symbol with no protected shares still counts as Day/Swing. An unreadable
      ledger keeps the last good claims but no checkpoint time (it could be from before midnight) -> no measure.
   f. Round 3 (PASS) noted that a stamp from before midnight blocks the backup each morning until the day's first
      fill is rebuilt. A rebuild-time stamp was tried and rejected in round 4 (a rebuild saves minutes after its fill
      read, so its run time does not prove the read covered midnight; the exact fix needs run_ledger_sync, outside
      this approval). Effect: after the day's first fill, if the exact measure fails, entries wait for the rebuild the
      kill check requests (the ledger sync's every-minute --if-requested run), then the backup measures.
   Honest limit: with the production ledger unstamped at deploy, the backup starts measuring after the first ledger
   rebuild that runs on rev 4 (the ledger sync runs every 20 minutes); a ledger rebuild refused for a whole day still
   blocks entries (and pages), as before.
Rev 1-3 were reviewed by two board seats and GAI (rev 1 counted QHM/F6 losses; rev 2 trusted the drift row).

## Owner direction (2026-11-11, verbatim)
"that should not be a reason to block all trades. That doesn't make any sense. The kill switch should only trigger when
losses reach -7%, not some technical glitch. If it can't be priced, the auto heal or whatever back up protocol needs to
audit alpacas full buy/sell list from the app. Today was a wasted opportunity because of a technicality."

## Problem (measured)
With OWNERSHIP_GUARD_ENFORCE on, any failure of the exact per-tier measure raises, and run_cycle blocks new entries for
that cycle (exits continue). Blocked cycles in mtf_bot.log: 11-05 6, 11-06 20 (QHM NVDA fill before the ledger caught
up), 11-10 1 (NVD ledger 90 vs broker 52), 11-11 70 (fixed by KILL-PRIOR-CLOSE-1111). The remaining causes are ledger
lag after a fill (ledger quantity differs from the broker's), a ledger row for a symbol the broker no longer holds, and
an unreadable ledger file. None is a loss.

## Change (rev 2)
When the exact measure fails (any `_kill_join_unresolved` reason, `_protected_held_qty` None, or ledger unreadable),
`check_kill_switch` computes a BACKUP measure B from Alpaca instead of raising. Inputs: Alpaca positions (net qty,
current price, lastday_price), today's fills from `_today_fills` (each tagged with its tier from its own order, not
from the ledger), the prior close (lastday_price, else `_prior_session_close`, split-adjusted), and the ledger FILE's
per-tier claims when it can be read.

Per symbol s, with move m = current - prior_close (rev 3: checkpoint + fill journal, board round 2):
- Checkpoint: the ledger FILE's tier claims as of its `last_reconciled_utc` (claims only; `alpaca_net_qty` and `drift`
  are not used, so a drift row can never put a protected share into the active count).
- Journal: today's tagged fills after `last_reconciled_utc` (fill timestamps from the same Alpaca activity rows
  `_today_fills` reads). Active now a_s = day + swing claims + signed active fills since the checkpoint; protected now
  p_s = qhm + forever_6 claims + signed protected fills since the checkpoint.
- Unexplained shares u_s = broker qty - (a_s + p_s): fills the journal could not place. Counted one-sided.
- Open leg: m x a_s + min(0, m x u_s); protected shares never enter (GAI). A symbol the broker no longer holds has an
  open leg of exactly 0 (its fills carry the P&L); a held symbol with no current price fails that cycle closed.
- Fill terms: fills tagged day / swing / unknown count in full (the same repricing to prior close as the exact measure);
  fills tagged qhm / forever6 (the code's own tag spellings, pinned by a test — Thorp) are excluded, as in the exact
  measure.
- B = sum over symbols. Kill trips when B <= -7% of the start-of-day value, exactly like the exact measure.
- The ledger `drift` field is ignored (it is a reconciliation flag, not P&L). Each backup cycle logs
  "[change=kill-backup-1111] kill-switch backup measure B=<value> attribution_exact=False reason=<check>".
  GAI round 2 asked that unbooked shares never add a gain: min(0, m x u_s) already guarantees it for both signs of u_s
  (an unbooked buy, u_s > 0, counts only a fall; an unbooked sale, u_s < 0, counts only a rise).
- Ledger file unreadable (logged CRITICAL_LEDGER_UNREADABLE, carried into the morning report): the checkpoint is the
  last ledger the bot read successfully (kept in memory with its `last_reconciled_utc`, GAI round 3), and the journal
  replays the tagged fills after it, so QHM/F6 shares stay excluded. Only with no checkpoint at all since the process
  started does every broker share count one-sided (min(0, m x broker qty)) plus the active fill terms; that last
  resort can count a QHM/F6 loss but never hides an active loss.
- A symbol with a quantity to price and no prior close from either source: B cannot be priced -> that cycle keeps the
  entries-only block (Thorp: never treat it as 0).
- Alpaca positions or fills unreadable: the existing entries-only block remains.
- A trip from B is recorded with cause CONTROL_FAULT and attribution_exact=False (never GENUINE_TRADING_LOSS), so it can
  never unlock the F6 starter bypass (risk seat D).
- Every backup cycle logs, per symbol, p_s, a_s, u_s, m and the term used, so a trip can be re-derived. A trip from B
  latches killed=True for the day exactly like a genuine trip (tested).
- Before live: a read-only run on the server against the live ledger and broker (Q22); after live, a nightly machine
  check compares every backup cycle's B with the fills-based Day+Swing P&L (Q40).
- Escalation (risk seat E): every backup cycle logs its reason, B and each symbol's terms; the existing ledger rebuild
  request still fires; one page per PT day when a cycle is still on the backup after the ledger file was rebuilt
  (`last_reconciled_utc` newer than the episode's first backup cycle), naming the symbol and the check that still fails.
  The kill snapshot carries a daily backup-cycle count for the morning report.

## Replays to run before the build is shipped
The failure cycles of 11-05, 11-06, 11-10 and 11-11 (pre-fix): B from that day's fills, positions and ledger; expected no
trip and entries open each day (no day reached a 7% loss), and B within cents of the fills-based Day+Swing P&L.

## Required questions
Q1: Removes glitch-driven entry blocks (97 blocked cycles 11-05..11-11); more data, more trades.
Q2: More trades; no new limit.
Q3: Should exist (owner direction); simplest form: the exact formula with the ledger's own claims, one-sided on the unbooked shares.
Q4: Extends KILL-JOIN-1107 / KILL-ATTR-1023 / KILL-PRIOR-CLOSE-1111; no open PR touches this path.
Q5: No new decision number; the 7% limit is unchanged; the escalation is event-based (a completed rebuild), not timed.
Q6: Static assumption removed: "an unattributable measure means stop trading". Forward build: move the exact measure to the same checkpoint + fill-journal method.
Q7: Feeds the kill switch and its dashboard snapshot (backup flagged) and the morning report (backup-cycle count).
Q8: Blocked-cycle counts above, from mtf_bot.log.
Q9: Four-day replay; wrong if B differs from the fills-based Day+Swing P&L by more than the unbooked-share move.
Q10: Not applicable because this is a safety-control repair, not a signal.
Q11: No size, frequency or exposure multiplier.
Q12: The 7% kill stays armed every cycle; QHM/F6 stay excluded except when the ledger file is unreadable (then more sensitive).
Q13: Alpaca unreadable, or a quantity with no prior close -> the existing entries-only block.
Q14: No: nothing is log-only; the backup measure decides the kill from its first cycle after deploy.
Q15: Each backup cycle logs its reason, B and the per-symbol terms.
Q16: Proof line: "kill-switch backup measure" in a cycle with no "exact tier attribution is unresolved" error.
Q17: The first ledger lag after a fill.
Q18: Owner-raised; investigated from the logs.
Q19: Not entry/exit/sizing logic.
Q20: Reviewer prompts carry the code, the facts and the replays only.
Q21: Read-only Alpaca reads already made by the measure; tests mock them.
Q22: Still fails closed when Alpaca is unreadable or a price is missing.
Q23: The four-day replay before ship.
Q24: Prior closes from lastday_price or the split-adjusted T1 daily bar.
Q25: Measured, not assumed.
Q26: T1 only: Alpaca trading API (positions, fill activities) and the T1 daily bars via data/fetcher.py; on any read failure the existing entries-only block remains (no fallback source).
Q27: No new state file (the ledger file is read, never written).
Q28: Runs on the trading thread inside the kill check; no fetch beyond the measure's own plus the cached daily bar.
Q29: P0 by owner direction; a full session lost on 11-11.
Q30: Blind spot closed: a fail-closed rule that turned routine bookkeeping lag into a trading halt.
Q31: Board round 2 also found an inherited gap in the EXACT path (a Day buy and a QHM sale in one symbol in the same minute, ledger not yet booked, can show a Day gain); the checkpoint + journal method in this build is the fix for the backup, and the same method for the exact path is the forward build (Q6). Variants: (a) rev 1, all shares one-sided (rejected by GAI: counts QHM/F6); (b) a stale snapshot as the measure (rejected by Thorp: misattributes a just-filled QHM buy); (c) equity minus last_equity (rejected: false-tripped 2026-09-16); (d) this rev 2.
Q32: Not applicable because nothing is fitted.
Q33: Not applicable because no trades are simulated beyond the replays.
Q34: Not applicable because the change adds no market exposure.
Q35: Not applicable because the change adds no market exposure.
Q36: Not applicable because no orders are placed.
Q37: Not applicable because no statistic is estimated.
Q38: Reversal: a day when B stayed above -7% while the fills-based Day+Swing loss passed -7%.
Q39: Not applicable because it sizes nothing.
Q40: Never-mask: protected shares excluded; unbooked shares one-sided; active shorts signed from the ledger; tests reproduce the board's GOOGL/NVDA short-vs-QHM-long inputs. Fill tier tags are trusted exactly as the exact measure already trusts them (the tag comes from the order's own client id; an unresolvable tag fails the fills read, which keeps the entries-only block); the risk seat's one-sided protected-fill proposal is not adopted because it would count QHM buys above the prior close as Day/Swing losses (the GAI objection).
Q41: Daily backup-cycle count in the kill snapshot and the morning report.
Q42: Alpaca positions, fills, T1 bars and the ledger file.
Q43: Conservative backup measurement instead of a halt.
Q44: The four-day replay plus the first live ledger lag.
