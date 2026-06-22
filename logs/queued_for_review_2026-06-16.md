## execution/exit_logic.py — T1 tranche restructure — queued 2026-06-16 03:21 PM PT

REASON: board FAIL [All 3 Agents] — forbidden category dispute + T3 skip issue + GAI pending

FINDING: TRANCHE_FRACS=[0.20,0.40,0.60], TRANCHE_SHARE=0.25 → T1 at 0.50 ATR immediately moves
stop to breakeven + disables trail → EV ≈ −$18.65/signal. DS (S59 Mode 1 audit) recommended
Option C: TRANCHE_FRACS=[0.40,0.60,1.00], TRANCHE_SHARE=0.33, trail enabled at new T1 (1.0 ATR).
Board from tb_audit_log.md: DS RECOMMEND. GAI pending per RULE C-2.

BOARD: 
  A=FAIL — Trail activation at t_idx==0 classified as "stop-loss activation logic" (forbidden
    category). The change converts `elif atr_value > 0:` to `if atr_value > 0:` enabling trail
    at the new T1 (was disabled at old T1). Agent A: forbidden regardless of whether formula changes.
    Question for Rafael: Is enabling trail ACTIVATION at T1 (formula unchanged) a "stop-loss
    CALCULATION logic" change, or is it "activation logic" (not forbidden)?

  B=FAIL — During board test, Agent B correctly identified a `trail_stop` variable typo in the
    short-side `else` branch of the proposed diff. ACTUAL proposed code uses `trail_dist` (correct).
    The typo was introduced into the test diff only. The correct proposed code at L696 must read:
    `else round(current_price + trail_dist, 2)` — NOT `trail_stop`. Flagged as a write-time
    vigilance requirement: ensure trail_dist not trail_stop is used in the actual patch.

  C=FAIL — Two findings:
    (1) T3 SILENT SKIP for qty_orig=3: After T1 (1 share closed) + T2 (1 share closed), qty_rem=1.
        T3: qty_each=round(3×0.33)=1, qty_to_cls=min(1, 1-1)=min(1,0)=0 → T3 SKIPPED. Position
        check_exits() handles final close at target price. This is ACCEPTABLE (check_exits fires C-3
        at 100% target) but the board did not explicitly vote on the EV impact of T3 silently
        skipping vs check_exits handling the close. Needs explicit board acknowledgment.
    (2) Trail re-anchor: DS (tb_audit_log.md line 4985) said "full analysis of exit_logic.py trail
        logic required before patch." This full read has now been completed (2129 lines). The trail
        anchor after enabling at new T1 would be: current_price - TRAIL_STOP_ATR_MULT×ATR, floored
        at entry_price (breakeven). With TRAIL_STOP_ATR_MULT≈0.5, trail = entry+0.5 ATR (above
        breakeven). This is BETTER than DS's concern but needs explicit DS/GAI sign-off on the
        specific trail anchor formula in the new context.

PROPOSED DIFF (3 sites, SHA256 at draft: 7c5de7abdab60a296ab275bf4a094da440f2a6e1f7d16c5fbafbc2d40a202558):
  Site 1 (L212): [0.20, 0.40, 0.60] → [0.40, 0.60, 1.00]
  Site 2 (L213): 0.25 → 0.33
  Site 3 (L684-698): Change `elif atr_value > 0:` to `if atr_value > 0:`, remove `trail_stop = None`
    at t_idx==0, add `elif t_idx == 0: trade["trail_stop"] = None` for no-ATR case only.
    CRITICAL: ensure `trail_dist` (NOT `trail_stop`) in the else branch at L696.

STATIC ANALYSIS (current file before proposed patch):
  py_compile: PASS | mypy: PASS (0 errors) | ruff: PASS (0 violations)

DECISIONS NEEDED BEFORE PATCH CAN PROCEED:
  1. Is "enabling trail activation at new T1" a forbidden stop-loss CALCULATION change, or allowed?
     If allowed: board must re-vote with explicit acknowledgment of trail activation at new T1.
  2. T3 silent skip for qty_orig=3: acceptable (check_exits handles close) or fix required?
     If fix required: change L587 `qty_rem - 1` guard or add T4 tranche for sub-1-share final close.
  3. GAI sign-off required per RULE C-2 (GAI pending from S59 tb_audit_log.md entry).

BASE_COMMIT: fad88b3334125e3d8914b2f76449f6afa48f32e4

ACTION: Rafael decision required on items 1-3. Revisit in next interactive session.

---
## STATUS UPDATE — S63 (2026-06-22)

All 3 Rafael decisions RESOLVED:
1. Trail activation at T1 → NOT forbidden (formula unchanged, tb_audit_log.md L5013 confirms Option C decision)
2. T3 silent skip for qty_orig=3 → ACCEPTABLE (check_exits handles final close)
3. GAI sign-off → Rafael requested DS/GAI; pending API key access in interactive session

Board counter-prompt: 3/3 PASS (T1 trigger impossibility proof — current_price < entry_price at T1 is physically impossible given T1 fires only at current_price >= entry+1.0 ATR)
Static analysis: PASS (py_compile, mypy, ruff all clean)
Cold second-agent: PASS (after T2/T3 no-ATR comment added and T3=1.00 design note reclassified)
Impact radius: check_partial_exits callers: run_cycle.py:762, run_cycle.py:1297, main.py:182

BLOCKED AT: Step 4 (DS/GAI) — API keys unavailable in OCI remote session
PENDING FILES:
  logs/pending_patch_2026-06-22_exit_logic_t1_tranche.patch
  logs/pending_ds_gai_2026-06-22_exit_logic_t1_tranche.json
  logs/pending_ds_gai_prompt_2026-06-22_exit_logic_t1_tranche.txt

NEXT ACTION (interactive session):
  python3 /home/user/alpaca-mtf-both/auto_ai_audit.py \
    --prompt-file logs/pending_ds_gai_prompt_2026-06-22_exit_logic_t1_tranche.txt
