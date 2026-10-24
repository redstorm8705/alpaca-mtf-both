# External QA round 2 — exit_logic + orphan_manager fixes — 2026-10-24 (signed: Claude)

Approval-ID: AUDIT-1025
Covers: execution/exit_logic.py, execution/orphan_manager.py, tests/test_audit_1025.py

## Why
Meta Muse full-read audit (2026-10-24, 9 files, ~50 findings). D1/D2/D3/E1/EX-2 are already fixed live in SAFE-1024
(#758). QHM findings QH-1..QH-10 went to Codex on issue #756. EX-9 (kill switch fed fallback P&L) is REFUTED:
check_kill_switch does not consult risk.daily_pnl (risk_manager.py check_kill_switch docstring: "daily_pnl is NO
LONGER consulted in the kill DECISION"; the measure is fills-based). This diff fixes the High findings in the two
files Claude owns that were confirmed at source:
- EX-3 exit_logic.py ~L1104-1109: after a partial exit the qty clamp used abs() of the live net with no side check.
  Input: Swing long 20 left, live net -10 (a co-held short dominates) -> qty_remaining rewritten to 10 and a sell stop
  for 10 sent for a long whose net is short. Plus the same gap in _submit_new_stop: an opposite-side net left qty
  uncapped and sent the stop.
- OR-1 orphan_manager.py ~L1520-1544: orphan stop adoption took the FIRST stop-type order with no side, qty, status or
  tier check -> a 3-share residual, a wrong-side stop or another tier's stop could be adopted as this position's
  protection and the full-size stop was then skipped.
- OR-2 orphan_manager.py ~L1810-1815: with the Day-tier claim unreadable and the Alpaca net OPPOSITE the Swing book,
  the reconcile fell through to the direction-mismatch branch and flipped the Swing trade onto the (possibly Day)
  net. The existing rule ("with the Day count unknown, never grow the Swing book") now also covers the opposite case.
- OR-3 orphan_manager.py ~L662: a filled overnight GTC partial with no average fill price banked P&L at $0 fill
  (long entry $100 x 5 -> -$500 recorded). Now not booked; the key is kept and booked when the price exists.

## Design
1. EX-3: clamp qty_remaining only to a SAME-side live net. _submit_new_stop: an opposite-side live net -> no stop,
   return False (every caller marks _stop_sync_pending and retries; the software stop/trail keeps enforcing). Side text
   is normalized ("PositionSide.LONG" -> long); an unrecognized side falls back to the qty sign, as before.
2. OR-1: adopt an existing stop only if it is on the position's protective side, for 1..N shares (N = the position),
   not cancel-pending, and tagged intraday or untagged (operator). A smaller same-tier stop is still adopted (on a
   co-held symbol the net exceeds this tier's shares). Else it is logged and skipped, and the new-stop block places a
   stop — now with allow_cancel_blocking=False, so it can never cancel a QHM/other-tier stop to make room, and the
   PROTECTION_ALREADY_HELD / PROTECTION_UNKNOWN outcomes are handled explicitly.
3. OR-2 REVERTED after the cold-2nd FAIL: holding the Swing book on an opposite net with an unreadable Day claim left a
   genuinely reversed position with NO stop (exit_logic now refuses an opposite-side stop). The existing flip path,
   which keeps every share stopped, stays; OR-2 needs a per-tier position view (forward pass) to be fixed safely.
4. OR-3: a filled GTC partial with filled_avg_price missing/0 is not booked (no P&L, no qty change); CRITICAL log;
   the key stays for the next pass.
5. OR-4 (board Taleb: do not defer): a filled overnight GTC stop with no average price first recovers the close fill
   from Alpaca fills (fill_helpers.fetch_actual_fill_price_or_none). Only if none is found does it close at entry with
   `_fill_unverified=True` (so Kelly/TQI skip it and the existing RC-4 fill reconciler patches it when the fill appears).
Review trail: Gro + GAI preship APPROVE (round 1); cold-2nd FAIL round 1 (OR-2 no-stop, OR-1 cancel-blocking) -> fixed
as above; board Harris + Taleb APPROVE-WITH-CHANGES (OR-1 smaller-stop adoption, OR-4 now) -> adopted.
Medium/Low findings (EX-4..EX-10, OR-5..OR-10, OG-*, GX-*, F6-*, SW-*) are queued for verification.
No new numbers.

## Required questions (RQ-4)
Q1: Protects paper P&L data quality (OR-3) and keeps every Swing lot correctly protected (EX-3, OR-1) and keeps P&L fills-only (OR-3, OR-4).
Q2: No change to the number of trades; exits and protection paths only.
Q3: Simplest fix in each case: reuse filters and rules the same files already apply elsewhere.
Q4: Follows SAFE-1024 (#758); QHM items with Codex on #756; no open PR touches these lines (origin/main 2026-10-24).
Q5: No new numbers or thresholds; only side/qty/tier equality checks.
Q6: Exposes the static assumption that the broker net equals one tier's shares; forward pass: per-tier position reads.
Q7: Exit/protection/reconcile paths only; each change fails to retry or no-change, never to a trade.
Q8: Each finding verified by reading the cited lines this session; EX-9 refuted with the kill-switch code.
Q9: Unit tests for the opposite-net stop refusal and the same-side cap; OR paths reviewed by the cold second agent.
Q10: No validated edge is held back by this; it is a correctness fix.
Q11: Size 0, frequency 0, concurrency 0 — no sizing or entry change.
Q12: Strengthens "a stop on every position" (no false protection) and never-mask-a-loss (no fabricated $0 fill P&L).
Q13: Unreadable or mismatched data now leads to no change plus a retry or page, never a trade.
Q14: Not a shadow: acts live on every reconcile and stop placement.
Q15: Each skipped adoption / refused stop / held reconcile is logged with symbol, side, qty and the change tag.
Q16: Log tag `change=audit-1025`; post-live audit of the first session's reconcile and stop logs.
Q17: Unit tests run the real function with a mocked broker; the reconcile paths run at every main-bot restart.
Q18: No anomaly explained away; findings from external QA confirmed at source.
Q19: Changes exit and protection behaviour on error/mismatch paths: owner approves this exact design first.
Q20: Reviewer prompts carry facts only; one finding (EX-9) refuted with evidence rather than accepted.
Q21: All broker calls are mocked in tests; the broker refuses under test.
Q22: Error-path change; no new fail-closed control on the live ledger.
Q23: Unit tests plus the cold second-agent trace of every changed branch.
Q24: No claim that something does not exist is made here.
Q25: No market statistics are used by these fixes.
Q26: T1 Alpaca trading API (positions, orders); fallback = no change and retry.
Q27: No new output file; log lines and Slack pages only.
Q28: Main bot run_cycle thread (exit_logic stop maintenance) and startup/periodic orphan reconcile.
Q29: High-severity protection defects: ahead of strategy work.
Q30: Per-tier ownership of a shared broker net is the recurring blind spot (SAFE-1024 and this round).
Q31: N/A — correctness fixes with no edge claim, so no out-of-sample test applies.
Q32: N/A — no market-data replay is involved in these fixes.
Q33: N/A — costs unaffected; no order is added or enlarged.
Q34: N/A — no return stream is created by this change.
Q35: Fires only on co-held opposite nets, unreadable claims or missing fill prices — rare in any regime.
Q36: Behaviour does not depend on account size or liquidity.
Q37: N/A — no edge estimate, so no statistical power question applies.
Q38: N/A — not a strategy, so edge decay does not apply.
Q39: N/A — sizing logic is untouched by this change.
Q40: Protects "no false protection, no fabricated P&L"; check = tests plus the post-live log audit.
Q41: A per-tier position view from the ownership ledger for every protection decision.
Q42: Broker orders/positions and the tracker records.
Q43: Fail-safe three-state handling and exact-match adoption, as already used by _find_replacement_stop.
Q44: Muse's independent findings plus the cold second-agent review and Gro/GAI.
