# A failed position read is "unknown", never "flat" or "full size" — 2026-10-24 (signed: Claude)

Approval-ID: SAFE-1024
Covers: execution/day_trade_manager.py, execution/exit_logic.py, execution/fifo_pnl.py, tests/test_position_read_unknown.py

## Why
External QA (Meta Muse, 2026-10-24) findings D1 and E1, each verified at source by Claude the same day.
broker.get_open_position() returns None only on a confirmed 404 ("no position") and RAISES on any other error
(429 / 5xx / timeout) — execution/broker.py:215-226. Three callers collapse that raise:
- D1a execution/day_trade_manager.py flatten_position (~L1351-1359): `except Exception: pos = None` then
  `if pos is None: return True` — a read error is reported as "closed". Worst path: protection fails after an entry fill
  -> flatten_position(reason=stop_unconfirmed_flatten) -> read error -> True -> state `flattened_no_stop` while the lot
  may still be open with no stop. reconcile's naked path (~L4395) would also count it flattened and retire the record.
- D1b day_trade_manager.py _force_flat_one (~L1551-1556): same collapse -> (done=True) -> the EOD exit stops retrying
  that lot for the pass and it is not listed in the "still open at the close" page.
- E1 execution/exit_logic.py _submit_new_stop (~L337-341): on a read error it submits the uncapped tracker qty; its own
  docstring promises min(this trade's shares, the live same-side position). Input: tracker qty 10, live net 6, read
  raises -> a 10-share stop. Every caller already treats False as "retry next cycle" (_stop_sync_pending).
reconcile_open_state (~L4274-4285) already has the right rule: unreadable -> page, no action this tick.

## Design
1. flatten_position: a raised position read -> page "position unreadable", return False (not closed). Callers already
   handle False (state flatten_failed / not retired / retried next tick).
2. _force_flat_one: a raised position read -> return (False, False) (retry), never "done".
3. _submit_new_stop: a raised position read -> log, return False (the caller marks _stop_sync_pending and retries).
   Matches _capped_qty (None on read failure) and _wait_shares_free (False) in the same file.
4. exit_logic signal-exit GTC-cancel defer (~L2035-2045, found by the adversarial claims check): a raised read left
   `_defer_pos = None`, which booked an EXTERNAL CLOSE (tracker.record_exit — a live Swing trade dropped from the
   tracker, the false-drop class behind the July Movers incident). Now only a successful read that returns None books
   the close; a read error falls through to the existing "retrying next scan".
5. Late entry fill (external QA D2/D3, confirmed at source): place_entry marks the state record `entry_unterminal`
   (with the recorded qty) when the entry order is still pending cancel after the terminal poll, including the
   "never filled" branch. Each tick, reconcile first runs _heal_late_entry_fills: re-read the ENTRY ORDER (never the
   broker net — it can hold Swing/QHM shares); unreadable -> page once, no action; not terminal -> wait; terminal ->
   late = filled_qty - recorded - already closed; late <= 0 -> clear. During the session the late shares (never
   approved by sizing — the remainder was cancelled) are market-closed for the Day tier, min(late, live same-side net);
   an opposite-side or flat net -> page, nothing traded. A start marker is written BEFORE the submit and the order id
   after; a crash between them is recovered by finding that DT order (open or closed since the marker), never by a
   second close; unreadable -> page, nothing sent. The close's Alpaca fill and P&L are written to trade_events.jsonl
   (`late_fill_close`, never-mask-a-loss); a partial close keeps the remainder for the next tick. After hours: page
   only (entries stop at 3:40, so a pending-cancel order resolves long before the close).
   Review: Gro (round 2) and GAI (round 2, after a counter-prompt showing the broker net includes other tiers' shares)
   APPROVE A; board Harris + Taleb (masked-loss) APPROVE-WITH-CHANGES — all changes adopted (min(late, net), cumulative
   partial-close tracking, the trade_events record, crash recovery without a second close). Harris' "run inside
   force_flat_all" is met by reconcile running at the top of every tick, before the EOD exit.
6. execution/fifo_pnl._fetch_alpaca_fills_for_date (issue #736): paged with `after_id`, which Alpaca ignores (live
   probe 2026-10-24: page 2 == page 1); with 100+ fill activities in a day it would loop forever inside
   write_eod_summary on the trading thread (highest day in 120 days: 20 fills, so latent). Now page_token = last id
   (the paging execution/fill_notifications.py already uses), a page that does not advance raises (the caller's
   existing fallback + page), and a boundary row seen twice counts once.
No new numbers, no change to sizing, entries, stop prices or which shares are touched. A confirmed 404 keeps today's
meaning in all three.

Known limits of item 5 (cold-2nd, accepted — all on the rare crash/failure path): crash recovery matches a DT order by
side + qty only, so a same-size Day exit created after the marker could be adopted instead; an ownership-floor shrink
of the close qty breaks the match (a fresh close follows); if the late shares were already sold elsewhere, the heal
can sell up to `late` co-held shares; a 40310000 retry inside partial_close_position cancels Day-tier open orders
(the now-naked lot would then be scoped-flattened by the next tick's reconcile — never left naked past ~2 minutes).

## Not in this diff (queued)
- Same-class reads found by the claims check, judged not harmful today: day_trade_manager ~L1991 (skips a lot this
  tick without a page), ~L2983-2990 (a raise after a confirmed fill -> fill_unverified + loud page, already fail-loud),
  exit_logic ~L1083 (unguarded raise propagates). Forward pass: one shared three-state position read.
- QHM PENDING_EXIT (Muse, confirmed): _reconcile_pending_exit runs only from reconcile_on_startup; a submitted close
  that never fills is not re-checked until a restart. QHM is under active Codex work (QHM-ADD-1023); handed over.

## Required questions (RQ-4)
Q1: Protects data collection by keeping every lot protected and correctly booked; no effect on trade count or growth.
Q2: No change to the number of trades.
Q3: Simplest fix: treat the raise the way the sibling functions already do.
Q4: Mirrors reconcile_open_state's existing rule; no open PR touches these lines (checked origin/main 2026-10-24).
Q5: No new numbers or thresholds; only the handling of an error result changes.
Q6: Exposes the assumption that a read error means flat; forward pass: a single shared helper for "position unknown".
Q7: Exit/protection paths only; each change fails to "retry", never to a sell.
Q8: Verified by reading broker.get_open_position and each caller this session; the finding is in the code, not a log.
Q9: Unit tests drive each path with a raising read; expected: no "closed" result, no oversized stop.
Q10: No edge held back.
Q11: Size 0, frequency 0, concurrency 0.
Q12: Strengthens "a stop on every position": a lot is never reported flat or stopped at the wrong size on a read error.
Q13: That is the change: a missing/failed dependency now fails safe (retry + page).
Q14: Not a shadow: it acts live on every failed read.
Q15: Each unknown read is logged/paged with the symbol and path.
Q16: Log lines "position unreadable" / "stop sizing: position read failed — retry"; post-live check of the first session.
Q17: Tests run the real functions with a raising broker mock.
Q18: No anomaly explained away; found by external QA and confirmed at source.
Q19: Changes exit/protection behaviour on an error path: owner approval required (owner 2026-10-24: queued items ship on full BGGN alignment).
Q20: Reviewer prompts carry facts only.
Q21: All broker calls mocked; the broker refuses under test.
Q22: Not a new fail-closed control on live data; error-path only.
Q23: Unit tests of every changed path.
Q24: No claim that something does not exist is made here.
Q25: No market statistics used.
Q26: T1 Alpaca trading API (positions); fallback = retry next tick.
Q27: No new output file; log lines and pages only.
Q28: Day-tier runner tick (flatten/reconcile/EOD) and the main bot's exit_logic stop maintenance.
Q29: Safety defect on the protection path: ahead of strategy work.
Q30: A read error is a third state (unknown) that every caller must handle; the audit missed two of three.
Q31: N/A — no edge or performance claim, so no out-of-sample test applies.
Q32: N/A — no replay of market data is involved in this fix.
Q33: N/A — costs are unaffected; no order is added or resized upward.
Q34: N/A — no return stream is created, so no alpha or beta split applies.
Q35: Fires only on broker read errors, more common in volatile, high-request periods.
Q36: Behaviour does not depend on account size or name liquidity.
Q37: N/A — no edge estimate, so no statistical power question applies.
Q38: N/A — not a strategy, so edge decay does not apply.
Q39: N/A — sizing logic is untouched by this change.
Q40: Protects "no lot reported closed while open"; check = tests plus the post-live audit of flatten/reconcile logs.
Q41: One shared three-state position read used by every caller.
Q42: Broker position API and the decision journal.
Q43: Three-state result (present / absent / unknown) with unknown failing safe.
Q44: Muse's independent finding plus the cold second-agent review and Gro/GAI.
