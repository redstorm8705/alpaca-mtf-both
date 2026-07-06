
---
## Nightly autonomous queue — 2026-07-06

### QUEUED-1: orphan_manager.py T2 reconcile_positions absent-file fail-open
- **File:** execution/orphan_manager.py ~L1031
- **Reason for queue:** FORBIDDEN category — fix touches stop-order reconciliation/linking logic (`gtc_stop_order_id` link in `reconcile_positions`). The fix prevents a duplicate-stop 40310000 risk when `quarterly_holds.json` is absent. Falls under "order routing/submission/cancellation" forbidden category.
- **Item:** Add Option-2 `else` clause to `if _qhm_state_path.exists():` in `reconcile_positions` — treat absent file identically to corrupt (`_qhm_load_failed=True` + CRITICAL log + Slack). Also fold in `_get_qhm_syms()` module-vs-file truth-source split.
- **Session:** This item needs Rafael approval to proceed. Recommend: schedule a dedicated hotspot session for orphan_manager.py T2 (Steps 1–9 full sequence).

### QUEUED-2: weekly_review.py UX canonical tokens migration
- **File:** weekly_review.py
- **Reason for queue:** MISSING PREREQUISITE — `weekly_field_gate.py` (visible-text invariant gate) was built in a prior session but never committed. File is absent from working tree. Cannot safely apply canonical token migration without the invariant gate. Also RTH-chain (main.py + run_cycle.py import it).
- **Item:** Apply ui_tokens.py CAT_* palette + TYPE_DENSE 12px to weekly_review.py, gated by weekly_field_gate.py. The prerequisite must be rebuilt and committed first.
- **Session:** Rebuild weekly_field_gate.py, run the invariant gate, then proceed with Steps 1–9 for weekly_review.py.
