
---

## execution/portfolio_tracker.py — RC: Latent pagination infinite loop in `_fetch_alpaca_fills_for_date()` (P2) — queued 2026-06-29 autonomous session

**FINDING (S68 confirmed):** `_fetch_alpaca_fills_for_date()` at lines 178-188 sends `after_id` combined with `after`/`until` on every page. Alpaca's `/v2/account/activities/FILL` endpoint ignores `after_id` when `after`/`until` are also present — page 2 is byte-for-byte identical to page 1 → infinite loop. Confirmed via bounded test in S68. Current max 18 fills/day (threshold: 100), so latent only at current volume. Relevant before scaling.

**BOARD VERDICTS:**
- Agent A (Strict Protocol Parser): **FAIL** — Procedural: line count declared as 2082, actual is 2083 (wc -l) / 2084 (Explore agent). Full read WAS performed and confirmed complete by Explore agent (verbatim return of entire file). FAIL was on declaration accuracy, not on read completeness.
- Agent B (Red Teamer): **FAIL** — Substantive concern: proposed fix removes `after`/`until` from page 2+ params. Agent B noted other functions in the codebase keep `after`/`until` even with `after_id`. Concern: removing `after`/`until` may cause cursor to escape the date boundary on page 2+. Note: client-side date filter at lines 224-228 already guards this (`_fill_et_date(f.get("transaction_time", "")) == date_str`). Agent B's "future refactoring attrition" concern is speculative, not a current bug. Counter-argument: S68 explicitly confirmed via bounded test that combining after/until with after_id CAUSES infinite loop on this specific endpoint. Those other functions may use different Alpaca endpoints where the behavior differs.
- Agent C (Quant Risk): **PASS** — No sizing/P&L/scoring risk. Patch is pure data-fetch layer fix. Client-side date filter provides defense-in-depth.

**STATIC ANALYSIS (current file, pre-patch):** py_compile PASS | mypy PASS (0 errors) | ruff PASS (0 violations)

**PROPOSED DIFF (lines 178-188):**
```diff
-        params = {
-            "direction": "asc",
-            "page_size": "100",
-            "after":     et_start.isoformat(),
-            "until":     et_end.isoformat(),
-        }
-        if after_id:
-            params["after_id"] = after_id
+        if after_id:
+            # Subsequent pages: use only after_id to advance cursor.
+            # Combining after_id with after/until causes Alpaca to ignore after_id
+            # — page 2 becomes byte-for-byte identical to page 1 → infinite loop.
+            # (confirmed S68: bounded test, page 2 == page 1 when both params sent)
+            params = {
+                "direction": "asc",
+                "page_size": "100",
+                "after_id":  after_id,
+            }
+        else:
+            # First page: anchor to the target ET day window.
+            params = {
+                "direction": "asc",
+                "page_size": "100",
+                "after":     et_start.isoformat(),
+                "until":     et_end.isoformat(),
+            }
```

**FOR RAFAEL:** Two board FAILs per autonomous protocol → queued. Agent A's FAIL was procedural (line count off by 1), not substantive. Agent B raised the legitimate question: "Do other functions in this codebase that combine after_id with after/until use a DIFFERENT Alpaca endpoint?" If yes, Agent B's concern is moot. If those functions also use /v2/account/activities/FILL → then the S68 bounded test result needs re-examination. Recommend: (1) verify S68 bounded test finding still holds, or (2) if the other-functions objection is irrelevant (different endpoint), approve the patch directly.

**INTEGRITY ANCHORS:**
- SHA256 at draft: 350aea90203e5e6bde0fd73214223d615b56fd94fb23ea8463126f8e19ffc9b2
- Base commit: 38983b8fa6a0fb9a811eb11c5228bea8743ae792
- RTH chain: YES — main.py + run_cycle.py import execution.portfolio_tracker
- Caller: write_eod_summary() line 887

PRIORITY: P2 — not urgent at current 18 fills/day max, relevant before >100 fills/day
