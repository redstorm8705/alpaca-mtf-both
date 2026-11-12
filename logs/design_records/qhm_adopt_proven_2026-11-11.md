# QHM-ADOPT-1111 — QHM adopts shares its own tagged fills prove it bought, so its stop covers them

Approval-ID: QHM-ADOPT-1111
Covers: execution/quarterly_hold_manager.py, tests/test_qhm_adopt_1111.py
Not changed: execution/ownership_guard.py, execution/broker.py, execution/unmanaged_positions.py
Status: rev 3 (signed: Claude, 2026-11-11). GAI rounds 1-2 and board (Harris, risk seat) folded in.

## Owner direction (2026-11-11)
"The third share was not purchased by me. The bot needs to identify what went wrong and get the stop placed."

## What went wrong (measured, OCI logs and Alpaca orders)
- All 3 NVDA shares were bought by QHM; every buy carries QHM's own tag: QH-NVDA-b-1793978038908 (11-06, $191.85),
  QH-NVDA-b-1794236749304 (11-09 15:05 UTC, $177.80), QH-NVDA-b-1794239603609 (11-09 15:53 UTC, $179.28).
- 11-09 15:05: the dip-add buy filled, but with the capital allocator off QHM had no client id for it, so its order
  check read every answer as "contradictory order evidence" and booked nothing (state PENDING_STOP_REPLACE).
- 11-09 15:53: still believing it held 1 share, QHM dip-added again; same failure. A resync booked 1→2 at 15:53:23, a
  moment before the second fill (15:53:24), so QHM's record has been 2 ever since while Alpaca holds 3.
- Both root causes are already fixed in code by QHM-COVER-1109 (deployed 11-10): QHM mints its own client id (item A)
  and a second add the same day is blocked (item X3). Nothing repaired the share already orphaned.
- 11-10 08:18 UTC the cover recovery proved 3 QHM-tagged shares ("proved=3 ledger_qhm=2 broker=3 ... gap=1") but sizes
  its stop to min(proved, ledger_qhm) = 2; the ledger books the third share outside QHM because QHM's own record says 2
  (the ledger rebuild raises its QHM count to QHM's record, never above it). Result: 1 share with no stop since 11-09,
  paged every day by the unmanaged-position alarm.

## Change (rev 3: stop first, record second — GAI rounds 1-2; hook corrected)
Where it runs: the per-cycle QHM stray-sell guard pass, which already runs for every QHM hold each cycle and compares the
broker quantity with QHM's resting stops (log "QHM stray-sell guard: NVDA broker=3 covered_by_tier_stops=2"). Rev 1-2
placed it in `_legacy_cover_recovery`, which runs only while a hold is stuck mid dip-add (quarterly_hold_manager.py:2178)
and no longer runs for NVDA (state ACTIVE since 11-10). The proof is the existing `_qhm_proved_fill_qty(pos)` (QHM's own
tagged fills). In that pass, for an ACTIVE long hold, when
proved > QHM's record (qty_filled), the broker holds at least `proved` shares, and no other live tier manager claims the
difference (the same manager sources the unmanaged-position alarm reads: Swing tracker, Swing breakout, Day tier log,
Day->Swing hand-offs, F6):
1. Size the cover to `proved` (still bounded by broker qty minus open sells) instead of min(proved, ledger_qhm), and
   place or resize QHM's own stop to it at the existing stop price, through the recovery's existing submit/resize path.
2. Only after the broker confirms the stop rests for `proved` shares does QHM set qty_filled := proved and save, logged
   "QHM <SYM> adopted N proven share(s), stop now covers M [change=qhm-adopt-1111]". The next ledger rebuild then books
   them to QHM.
3. If the resize is unconfirmed, nothing is adopted: the record stays, the unmanaged-position alarm keeps paging, and
   the recovery retries next cycle. So the alarm can never go quiet before the stop covers the share.
4. If a source is unreadable, nothing is adopted. If another tier claims shares a QH-tagged fill proves are QHM's, that
   is a tagging contradiction: nothing is adopted and one HEADS-UP page names it (a halt would stop all trading for a
   bookkeeping conflict, against the owner's direction; the share stays paged by the alarm).
5. GAI round 2: the confirmation must be the broker order's own remaining quantity equal to `proved` (the recovery's
   existing `_order_remaining` read), not "a stop exists"; the adoption log carries the stop order id; after the save,
   a post-check compares broker qty, QHM record and the resting QHM stop and sends one HEADS-UP on any mismatch (no
   halt). The proved count, broker qty and claims are read once at the start of the recovery (one snapshot). A ledger
   rebuild landing between the confirmed resize and the record save sees the old record and books QHM 2; the next
   rebuild books 3. Nothing shrinks the stop meanwhile: the legacy cover recovery (which caps at the ledger) runs only
   in the stuck-dip-add state, not for an ACTIVE hold (verified: its only caller is quarterly_hold_manager.py:2178). An
   in-progress lock is not needed: the record is one atomic save right after the confirmation.
6. Board (Harris + risk seat, APPROVE-WITH-CHANGES): (a) no resize while the live price is at or below the stop
   price (enlarging a sell stop there would sell the share at once); log and retry next cycle; (b) when broker qty minus
   open sells is below `proved` (e.g. the owner placed his own stop on the share), make no resize call that cycle and log
   once (no refused call every cycle); (c) run the tagged-fill proof only when the broker holds more shares than QHM's
   resting stops cover, so it costs nothing when there is no gap; (d) the adoption log carries the order ids behind the
   proof, because an adopted share leaves the 7% kill's Day+Swing measure (QHM is excluded) and a tag error must be
   auditable.
7. Build finding (2026-11-11 night, verified in code): rev 3's "nothing shrinks the stop meanwhile" was false. The
   per-cycle stop reconcile (`_reconcile_stop_coverage`) resizes QHM's stop to `_strict_resync_qty`, which reads the
   ledger's QHM claim (2) until the next ledger rebuild books the adopted share. The build therefore persists
   `proven_adopt_qty` on the hold in the same save as the record; the strict resync uses it while the ledger's QHM
   claim is below it and the broker still holds it, and clears it once the ledger books it or the broker holds fewer
   shares. The adoption runs in the per-cycle stop-coverage pass of run_weekly_check (the same pass, one order-book
   snapshot, before the stray-sell guard), skips holds with an owner-buy adoption in progress, and the confirmation
   is the replaced order's own remaining quantity. Other-tier claims are read from the unmanaged-position alarm's
   sources plus the core tracker's trade log. Read-only server run 11-11 night: proved 3, other tiers 0, broker 3;
   no adoption that cycle because the owner's cancel left the QHM stop pending_cancel (live price $160.18).
NVDA today: stop 2 -> 3 shares at the existing $163.07 on the first RTH cycle after deploy, then QHM record 2 -> 3.

## Required questions
Q1: Protects a share the bot bought (a stop on every position) without changing what QHM buys; no trade-count change.
Q2: No change in trade count.
Q3: Simplest form: reuse the proof the recovery already computes; no new data source.
Q4: Builds on QHM-COVER-1109 (which fixed the causes) and the unmanaged-position alarm (which pages it).
Q5: No new number; the adoption equals the proven tagged-fill count.
Q6: Static assumption exposed: "QHM's record is right"; the forward build is a daily reconcile of every tier's record against its own tagged fills.
Q7: Feeds the ledger (QHM floor), the stop, and clears the unmanaged-position page for that share.
Q8: Alpaca order list and mtf_bot.log lines above.
Q9: NVDA replay: proved 3, record 2, broker 3, no other tier claims -> adopt 1 -> stop 3. Wrong if the stop stays at 2 after the next rebuild.
Q10: Not applicable because this is a protection repair, not a signal.
Q11: No size, frequency or exposure change: the share is already held; this only books it and stops it.
Q12: Strengthens "a stop on every position"; never-mask holds (nothing is sold or hidden).
Q13: Any unreadable source or competing claim -> no adoption, the alarm keeps paging.
Q14: No: nothing is log-only; the adoption and stop resize act on the first cycle after deploy.
Q15: The adoption logs the proof (proved, record, broker, each source's claim).
Q16: Proof line: "adopted N proven share(s) [change=qhm-adopt-1111]" then a QHM stop for 3 NVDA.
Q17: The first RTH cycle after deploy.
Q18: Owner-raised; traced from the logs, not explained away.
Q19: Not entry/exit/sizing logic; it books an existing holding.
Q20: Reviewer prompts carried the logs, the code and the facts only; no verdict was suggested.
Q21: Tests mock every broker call.
Q22: Read-only server run before live: the proof and claims for NVDA computed against the live broker and state.
Q23: Unit tests plus the read-only server run.
Q24: The three QH- tags were read from Alpaca this turn.
Q25: Not applicable because no market statistic is used.
Q26: T1 Alpaca orders/fills (already read by the recovery).
Q27: data/state/quarterly_holds.json via QHM's existing atomic save.
Q28: Inside the existing cover recovery on the trading thread; no new fetch.
Q29: P0: an unprotected share since 11-09.
Q30: Blind spot: a recovery that proves ownership but caps its action at a record it knows is short.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because no replay of prices is involved.
Q33: Not applicable because no trades are placed beyond the protective stop.
Q34: Not applicable because no new exposure.
Q35: Not applicable because no new exposure.
Q36: Not applicable because no new orders beyond the stop resize.
Q37: Not applicable because no statistic is estimated.
Q38: Reversal: an adoption of shares another tier later proves it owns.
Q39: Not applicable because it sizes nothing.
Q40: Protects "a stop on every position": the unmanaged-position alarm stops paging NVDA and the broker shows a QHM stop for 3.
Q41: A daily per-tier reconcile against tagged fills (forward build).
Q42: Alpaca's own order tags and fills.
Q43: Reconcile the book of record to the broker's tagged fills.
Q44: The broker's stop order for 3 shares and the alarm going quiet.
