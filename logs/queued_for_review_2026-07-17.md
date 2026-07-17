# Queued for Review — 2026-07-17 (nightly autonomous)

## reconcile_eod.py — _qty_at_close clobber fix — Board Agent B FAIL

**Finding (board-logged):** `reconcile_eod.py:475` — `trade["_qty_at_close"] = actual_qty`
runs BEFORE the `if new_exit is None: continue` guard. When Alpaca fills aren't found,
`_weighted_avg_exit_price` returns `(None, 0)`, so `actual_qty = 0` is written to
`_qty_at_close` even when reconciliation fails. Later, `portfolio_tracker.patch_exit_pnl`
recomputes P&L as `(fill - entry) * _qty_at_close = (fill - entry) * 0 = 0` — fabricating
a breakeven. Root-cause documented: `logs/pnl_zero_root_cause_2026-07-16.md`.

**Proposed fix:** Move `trade["_qty_at_close"] = actual_qty` to AFTER the `if new_exit is None: continue`
block. Safe because `_weighted_avg_exit_price` guarantees `actual_qty == 0 ↔ new_exit is None`.

**Board verdict:**
- Agent A (Strict Protocol Parser): FAIL — CHECK3: modifies a data/state/ write path; downstream consumer (portfolio_tracker.patch_exit_pnl) handling of no-overwrite case not verified in diff
- Agent B (Red Teamer): FAIL — argued `_qty_at_close = 0` is a "failure marker"; removing
  it means stale values survive a failed reconciliation run.
- Agent C (Quant Risk): PASS — fix strictly improves Kelly/TQI input accuracy.

**Note for Rafael (autonomous agent assessment):**
Agent B's FAIL has a logical flaw:
1. The actual failure marker is `_fill_unverified = True` (set on line 521, cleared on
   success). `_qty_at_close = 0` is NOT a reliable failure marker — it could be 0 for
   other reasons and is not checked as a failure signal by any downstream consumer.
2. In Agent B's "double-run" scenario (first run succeeds → `_qty_at_close = 50`;
   second run fails): OLD code overwrites valid 50 with 0 → corrupts correct data.
   NEW code preserves 50 → correct behavior. Agent B argued the opposite.
3. The comment change from "for audit" to "fill-confirmed qty (only when valid)" accurately
   describes the new semantics; "audit" in the original comment documented an incorrect
   behavior that caused real P&L corruption.

**RTH chain:** NO (reconcile_eod.py has zero RTH importers — confirmed via import trace)
**Recommendation:** APPROVE the fix. Pending Rafael review.
**Patch diff:**
```diff
--- a/reconcile_eod.py
+++ b/reconcile_eod.py
@@ -472,10 +472,9 @@ def reconcile(date_str: str, suppress_slack: bool = False) -> None:
         new_exit, actual_qty = _weighted_avg_exit_price(
             matched, qty_remaining, partial_qty
         )
         actual_qty = min(actual_qty, total_qty)  # Harris-Edge-C: cap at position size
-        trade["_qty_at_close"] = actual_qty  # Harris-2: set before None check for audit
         if new_exit is None:
             logger.warning(  # noqa: E501
                 "[%s] Could not compute avg exit price — leaving as-is.", symbol
             )
             continue
+        trade["_qty_at_close"] = actual_qty  # Harris-2: fill-confirmed qty (only when valid)
```
