# QHM saves its record when it books a fill — QHM-FILL-SAVE-1102 (signed: Claude, 2026-11-02)

Approval-ID: QHM-FILL-SAVE-1102
Covers: execution/quarterly_hold_manager.py, tests/test_qhm_fill_save.py

## Problem (measured 2026-11-02, OCI logs and state file)
- 17:58:48Z: QHM tranche-1 limit buy GOOGL 1 @ $283.43 (`c799ac62`), filled at $283.18.
- 18:06:01Z: `maybe_enter_positions` → AWAITING_FILL branch → `_check_fill_and_advance` booked the fill. It also placed
  GTC stop `946bb97d` @ $240.70 (15% floor) and set ACTIVE.
- `data/state/quarterly_holds.json` mtime stayed 18:05:59 (checked 18:13:54Z: AWAITING_FILL, qty_filled 0,
  stop_order_id null). The next save came at 18:16:07Z.
- Cause, in code (`execution/quarterly_hold_manager.py` 1121-1148): the loop saves only `if entered:`. A tranche-1
  fill submits no new order, so `entered` stays empty and the booked fill and stop id stay in memory.
- What it caused:
  - 18:09:50Z: `unmanaged_positions` CRITICAL page for GOOGL.
  - 18:16:05Z: QHM CRITICAL "stop coverage unresolved (ownership proof unavailable)" → PENDING_STOP_REPLACE.
  - Self-healed at 18:23:31Z (ACTIVE, one stop, no duplicate).
- Restart exposure (traced by the execution-risk seat): a restart inside the unsaved window reads the hold as
  AWAITING_FILL with stop_order_id null. The startup stray-sell guard (`cancel_stray_sell_orders`) then has no legit
  stop id for GOOGL and cancels the live QH stop as "unauthorized"; `_reconcile_awaiting_fill` re-books the fill and
  submits a new stop. Net: no duplicate stop, but the share is unprotected for ~1-60 s (longer if the resubmit falls
  to PENDING_STOP_REPLACE). The nightly restart is 02:00 ET; any manual or deploy restart can land in the window.
- The window opens after every tranche-1 fill booked in `maybe_enter_positions` unless another hold happens to
  trigger a save in the same pass (7 earlier tranche-1 fills in the logs since 2026-08-21).

## Change (revision 2, after board, cold-2nd and adversarial review)
- `maybe_enter_positions`, AWAITING_FILL branch: record `tranches_filled` before `_check_fill_and_advance`; if the
  call returns True OR `tranches_filled` changed (the helper's own except can return False after it already booked
  the fill and placed the stop), call `_save_state()` immediately, before any other hold is processed.
- The end-of-loop `if entered:` save is unchanged.
- Revision 1 (`booked` flag, save at loop end) was replaced: all four reviewers asked for the immediate save and the
  mutation-based trigger (later holds in the same loop can take ~17 s each in `_add_tranche_active`).
- No other behaviour changes: no order, size, stop, threshold or routing change.

## Prior work
- `logs/design_records/qhm_top3_action_2026-09-27.md`: found that tranche-1 fills set ACTIVE so tranches 2-3 never ran
  in the AWAITING_FILL branch (fixed by `_add_tranche_active`). Same branch; it did not touch the save condition.
- `logs/design_records/qhm_manual_add_adoption_2026-10-23.md` (QHM-ADD-1023): staged owner-add recovery fences
  stray cleanup while staged. Not involved here (owner_add_stage is None for GOOGL); no conflict.
- `logs/design_records/bot_audit_2026-09-24.md`: full bot audit, diagnosis only; no finding on this save path.
- `logs/design_records/f6_full_path_simulation_2026-10-23.md` and
  `logs/design_records/f6_emergency_flatten_attribution_2026-10-18.md`: F6 tier and shutdown work; they do not change
  QHM's `maybe_enter_positions`. No conflict.
- `handoff.md`: no open item covers this defect; added to the pick-up list with this ship.

## Out of scope
- The 18:00:20Z ledger replay "impossible negative protected replay GOOGL/qhm=-1" (cause not yet traced; a separate
  item).

## Required questions
Q1: Protects data collection: QHM's booked fills and stops reach disk, so the ledger and pages stay true. It is not a trading brake.
Q2: No change to trade count.
Q3: Yes, it should exist. This is the simplest fix: one flag and one condition.
Q4: Checked origin/main (f4e3ce0) and open PRs #811, #804, #727: none touch this save path.
Q5: N/A — no thresholds or numbers are added or changed; the change is a boolean save condition.
Q6: Static assumption: "a save is needed only when an order was submitted." Forward pass: save on any state mutation
    (a dirty flag in HoldPosition). That is logged as a separate item, not built here.
Q7: N/A — persistence only. The ledger and unmanaged-position checks read this file, and both benefit.
Q8: Yes — the log lines and file mtimes above, gathered before the claim.
Q9: Replayed on today's GOOGL sequence. The unit tests reproduce it: a booked fill with nothing entered gives 0 saves
    before the fix and 1 immediate save after; a booked fill whose helper returns False also saves; no fill, no save. Proven wrong if a tranche-1 fill's state file still lags its stop log line after deploy.
Q10: N/A — no signal or edge is involved; this is state persistence.
Q11: No — size, frequency and concurrency unchanged.
Q12: Strengthens "a stop on every position" (prevents a restart duplicate-stop path). No envelope change.
Q13: `_save_state` failure is already logged (RC-3) and returns False, so behaviour is no worse than today.
Q14: No — nothing is computed and left unused; the save happens live on the first fill after deploy.
Q15: Existing "tranche N filled" and "GTC stop submitted" lines remain; the state file mtime proves the save.
Q16: The next QHM tranche-1 fill: state file mtime within seconds of the "tranche 1 filled" log line; no unmanaged page.
Q17: Post-live check on OCI of the state file and logs at the first fill.
Q18: No — the anomaly was traced to code.
Q19: No change to entry, exit, sizing or routing.
Q20: Prompts state facts only.
Q21: Test stubs every broker call; no broker object reachable.
Q22: N/A — not a new control.
Q23: 3 new unit tests pass, plus 92 existing QHM tests (tranche adds, owner-buy autoheal, stop-safe add).
Q24: N/A — no claim that something does not exist or is not tradable.
Q25: N/A — no market statistics are used.
Q26: N/A — no data call is added; it only writes QHM's own state file.
Q27: Writes `data/state/quarterly_holds.json` through the existing atomic tmp→fsync→replace `_save_state`.
Q28: `run_cycle` → `qhm.maybe_enter_positions` (after 10:05 ET, run_cycle thread). One extra small file write per fill.
Q29: Top of today's queue: a live state-integrity defect with restart double-stop exposure.
Q30: Write-ahead/state durability after broker side effects; audits underweight "side effect persisted?"
Q31: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q32: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q33: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q34: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q35: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q36: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q37: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q38: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q39: N/A — no strategy, signal, sizing or replay is involved; this is a persistence fix.
Q40: Protects "QHM state equals broker truth after a fill". Check: state mtime ≥ fill log line at the next fill.
Q41: Make persistence follow every broker side effect (dirty-flag save) — logged as the forward pass.
Q42: OCI logs, state file mtimes, Alpaca orders API.
Q43: Persist state immediately after any broker side effect (write-after-side-effect).
Q44: Cold board seats, cold second agent, GAI and reviewer #2 on the exact diff.
