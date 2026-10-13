# Stops never loosen + Swing profit lock (2026-10-13, Claude)

**Trigger (CEO 2026-10-13):**
- "We need to build code that takes profit when it's supposed to or raises stops as it's supposed to. What's
  happening right now is leaving money on the table."
- "if the bot is running scans overnight, the stops need to also be updating as price moves higher or lower AH for
  all tiers (swing, QHM)."

## Findings (verified at source)
1. **1-share Swing lots never got profit protection.** `exit_logic.py` `check_partial_exits` skipped them with
   `if qty_rem < 2 and not _t3_pending: continue`, so the "too small to split" T1 branch was dead code.
   - EWY: short 1 @ $181.46, crossed T1 and T2 on 10/12, protected only at breakeven while about +$17.
2. **A partial banked by reconciliation never started the trail.**
   - PLTR: T1 banked by `orphan_manager` on 9/23. It sat at tranche 1 with no trail, stop at breakeven, while
     +$27 and past T2.
3. **A restart or pre-market reconcile loosened an operator-raised stop.** `cancel_and_reconcile_gtc_stops`
   handled the two cases this way:
   - A cancelled stored id was cleared, then Patch 1 re-submitted at the tracker's $181.72.
   - A live stop that did not match the tracker was cancelled as "stale".
   - In pre-market, the RTH DAY stop was then placed at the tracker level.
   - PLTR: the operator moved the stop to $205.40 (cancel + new order, untagged).
4. **The overnight Swing trail was software-only.** `_check_exits_extended_hours` ratcheted
   `tracker.update_trail_stop` but never moved the broker GTC stop. It also trailed only `partial_exited` trades,
   with a fixed 0.5 x ATR.
5. **Breakeven promotion (block 0C) and `portfolio_tracker.record_partial_exit` could lower a stop already past
   entry.**
6. **QHM profit-lock** (`_maybe_trailing_lock`) is limited: RTH only, once a day, cancel then resubmit.

## Fixes
- **A. Never loosen** (`execution/orphan_manager.py`, ships first, before the 08:45 ET pre-market reconcile):
  - Follow the `replaced_by` chain to the live stop.
  - When the stored stop is gone, adopt the single live GTC plain-stop for exactly this trade's shares, on the
    protective side, tagged Swing or untagged (an operator order).
  - Raise the tracker's effective stop to a tighter live stop:
    - `original_stop` is frozen first, so Kelly and R readers keep the sized risk;
    - `be_stop_promoted` is set when the adopted level is at or past entry;
    - only from orders in force (new/accepted/held/pending_new).
  - Tests: `tests/test_reconcile_adopts_tighter_stop.py`.
- **B. Swing profit lock** (`execution/exit_logic.py`, `execution/portfolio_tracker.py`):
  - A trade past T1 (price actually beyond the T1 level) with a banked tranche and no trail starts its phase
    trail. The floor is breakeven or a tighter stop.
  - A 1-share lot at T1/T2 locks instead of being skipped: stop to at least breakeven, trail started or tightened
    to the phase, never looser. T3 still closes it.
  - The T1 partial, block 0C, and `record_partial_exit` are tighten-only.
  - Kelly `record_trade` uses `original_stop`.
  - Overnight: any trade with a trail is trailed at its phase distance, and the broker stop is moved in place
    (`_move_stops`, never looser), with a retry flag.
  - Tests: `tests/test_exit_logic_stop_moves.py`.
- **C. QHM overnight** (separate diff). Board (Harris + Thorp) chose option B:
  - Run the profit-lock every cycle outside RTH on a confirmed price: the lowest of this cycle's mark, the previous
    cycle's mark, and the consolidated last trade. No raise if any is missing.
  - Replace the stop in place; never cancel and resubmit off-hours.
  - Remove the once-a-day limit, replacing it with a minimum 0.25% step.
  - Size, frequency, and concurrency delta: zero.

## Size / frequency / concurrency
- **Size and frequency:** zero for A and B. They only move stops tighter, never open or add.
- **Concurrency:** earlier exits free capacity sooner, still bounded by the existing caps. Not risk-path under
  Rule E. The masked-loss seat was still run on both.

## Reviews
- **A:** cold-2nd FAIL (0C pull-back) -> fixed -> PASS. Masked-loss seat FAIL (Kelly R via trade["stop"]) ->
  fixed (`original_stop` frozen).
- **B:** cold-2nd PASS. Masked-loss seat FAIL (lock on a reconciliation-booked loss) -> fixed (gate on price past
  T1, breakeven floor).
