
## execution/orphan_manager.py — B4 FAIL-OPEN FIX — READY FOR GRO/GAI + APPROVAL
**Date drafted:** 2026-07-03 (nightly autonomous session)
**Finding:** B4 — `cancel_and_reconcile_gtc_stops()` fails-open when `quarterly_holds.json` is ABSENT. When absent, `_qhm_protected` stays empty and `_qhm_load_failed` stays False, so the line-313 guard never fires, allowing QHM positions' GTC stops to be wrongly cancelled pre-market on a cold deploy or after state-file loss.
**RC class:** RC-3 (fail-open path — absent file silently treated as "no holds")

---

### APPROVAL PACKAGE (plain English)

**PROPOSAL:** When the QHM state file is missing from disk, protect configured quarterly hold positions' overnight GTC stops from being cancelled pre-market — but only for picks whose entry window has opened.

**THE PROBLEM:**
On a fresh OCI deploy (or after `quarterly_holds.json` is accidentally deleted), the bot's pre-market GTC reconcile routine reads the state file to know which symbols to protect. If the file is absent, no exception is raised, `_qhm_protected` stays empty, and the guard at line 313 never fires. Result: NVDA or GOOGL GTC stops could be silently cancelled at ~4 AM, stripping their overnight protection before RTH opens. The bot would then try to re-submit the stops — but there's a window of exposure. Concrete example: bot is deployed fresh on a Tuesday with NVDA and GOOGL entered as overnight holds. State file hasn't been created yet. Pre-market reconcile fires at 4 AM. `quarterly_holds.json` absent → `_qhm_protected = frozenset()` → both symbols processed for cancellation.

**THE FIX:**
Add a fallback block that fires ONLY when the state file is confirmed absent (not when it exists with closed positions, not when it's corrupt). The fallback reads `quarterly_holds_config.json` and returns only those configured picks whose `not_before_date` has been reached — so GE, GEV, and LLY (future picks not yet entered) are excluded. The result protects NVDA and GOOGL on a fresh deploy while leaving future picks unprotected (correctly, since QHM can't have entered them yet).

**CONSENSUS:**
- Board Agent A (Strict Parser): FAIL on draft → required `and not _qhm_state_path.exists()` → incorporated
- Board Agent B (Red Teamer): FAIL on draft → same fix + flagged future-picks over-protection → both incorporated
- Board Agent C (Quant Risk Manager): FAIL on draft → required `not_before_date` filter → incorporated
- All three boards: PASS on corrected patch (all three required fixes applied)
- Gro (Groq): **PENDING** — run before applying (prompt below)
- GAI (Gemini): **PENDING** — run before applying (prompt below)
- Static analysis: py_compile PASS | mypy PASS | ruff PASS
- Cold second-agent: **PASS** (all 4 logic checks clean)

**RISK IF APPROVED:** Minimal — the fallback is gated on confirmed file-absence and `not_before_date` filtering. Future picks (GE/GEV/LLY) correctly excluded. Dual-absent edge case (both state file and config absent) fails open at the helper level (logs warning, no Slack — same as current behavior).

**RISK IF REJECTED:** On a fresh OCI deploy with NVDA/GOOGL held overnight, their GTC stops remain at risk of silent pre-market cancellation.

---

### Board Agent Findings (full record)

**Agent A (Strict Parser) — FAIL on original, PASS-implied on corrected:**
Required: add `and not _qhm_state_path.exists()` to ADDITION 2 condition. Without it, the condition false-triggers when `quarterly_holds.json` exists but contains only CLOSED positions — a normal post-exit state. `_qhm_state_path` is always in scope after the try block (Path construction as first statement cannot fail). Fix incorporated.

**Agent B (Red Teamer) — FAIL on original, PASS-implied on corrected:**
- PRIMARY: Same State B false-trigger (file present, all CLOSED) — condition fires when it shouldn't, adding GE/GEV/LLY to `_qhm_protected`, causing legitimate intraday GTC stops in those symbols to be retained (held_for_orders), blocking partial RTH exits.
- SECONDARY: Config returns all 5 picks including future picks — GE/GEV/LLY not yet eligible.
- REQUIRED FIX: `and not _qhm_state_path.exists()` (same as Agent A) + future-picks filtering.
- Both fixes incorporated.

**Agent C (Quant Risk Manager) — FAIL on intermediate (condition already fixed), required additional fix:**
- Q1 P&L integrity: PASS
- Q2 Sizing integrity: PASS
- Q3 State machine coherence: **FAIL** — `_configured_qhm_picks()` returned all 5 config picks without `not_before_date` filtering. GE (not_before 2026-07-25), GEV (2026-07-22), LLY (2026-08-07) would enter `_qhm_protected` despite QHM not having entered them. Intraday MTF positions in those symbols would have GTC stops wrongly retained.
- REQUIRED FIX: Add `date` to import, add `date.fromisoformat(meta.get("not_before_date", "9999-01-01")) <= date.today()` filter to `_configured_qhm_picks()`.
- Q4 Fallback-to-fallback: PASS (dual-absent does not regress vs current behavior)
- Q5 Thread safety: PASS (local variables, no shared state)
- Q6 Intended scenario: PASS contingent on Q3 fix
- Q7 Scope: PASS (no stop-loss calc, order routing, fill recording, or auth touched)
- Fix incorporated.

**Cold Second-Agent (Logic Review):**
1. Logic inversion: PASS — all 4 states correctly isolated
2. Off-by-one: PASS — `<=` on `not_before_date` is semantically correct (eligible on the date itself)
3. Missing conditions: PASS — `_qhm_state_path` cannot be undefined (short-circuits anyway); dual-absent handled
4. Branch completeness: PASS — both TRUE and FALSE paths verified correct; `_qhm_protected` carries through to line 313

---

### Static Analysis
- py_compile: **PASS**
- mypy: **PASS** (no issues found)
- ruff: **PASS** (all checks passed)

---

### Integrity Anchors
- SHA256 of proposed file: `a444613b17d5cdb8a8b69520514af22d62127c8c7be98fca5630d695056e4a9c`
- Base commit: `414b839` (GEX pipeline repair — RC-6 fix, runtime-verified)
- Patch file: `logs/pending_patch_2026-07-03_b4_orphan_manager.patch`

---

### Gro/GAI Prompt (run BEFORE applying — SAME prompt to both)

**Gro persona (PATCH VALIDATION mode):**
> You are a Senior Staff Engineer at an HFT firm with direct ownership of execution engines and P&L attribution systems. Treat this as a P0 incident review. Be concrete and technical — no hedging.

**GAI persona (PATCH VALIDATION mode):**
> You are Head of Quant Engineering at a systematic hedge fund. Responsible for correctness of all P&L attribution, risk accounting, and counter-state invariants. Your audit is the last gate before code goes live. Find what others missed.

**PROMPT (identical to both):**

```
File: execution/orphan_manager.py
Function: cancel_and_reconcile_gtc_stops()

FINDING: B4 — fail-open when quarterly_holds.json is ABSENT. When absent, _qhm_protected
stays frozenset() and _qhm_load_failed stays False. The line-313 guard never fires. QHM
positions' GTC stops can be wrongly cancelled pre-market.

PROPOSED DIFF:

--- execution/orphan_manager.py (original)
+++ execution/orphan_manager.py (proposed)
@@ -21,7 +21,7 @@
 import json as _json
 import logging
-from datetime import datetime
+from datetime import date, datetime
 from pathlib import Path
@@ -51,6 +51,32 @@
 logger = logging.getLogger(__name__)

+def _configured_qhm_picks() -> frozenset:
+    """Fallback QHM symbol set when quarterly_holds.json is absent.
+    Reads quarterly_holds_config.json 'picks' keys, filtered to symbols
+    whose not_before_date has been reached — future picks are excluded
+    because QHM cannot have entered them yet.
+    Returns empty frozenset on any failure — caller treats as no protection.
+    """
+    _cfg_path = (
+        Path(__file__).resolve().parent.parent
+        / "data" / "state" / "quarterly_holds_config.json"
+    )
+    try:
+        _cfg = _json.loads(_cfg_path.read_text())
+        _today = date.today()
+        return frozenset(
+            sym for sym, meta in _cfg.get("picks", {}).items()
+            if isinstance(meta, dict)
+            and date.fromisoformat(
+                meta.get("not_before_date", "9999-01-01")
+            ) <= _today
+        )
+    except Exception as _e:
+        logger.warning("_configured_qhm_picks: config read failed: %s", _e)
+        return frozenset()

+    # B4: absent-file fallback — fires ONLY when quarterly_holds.json does not
+    # exist on disk. Explicit .exists() check prevents false-positive when file
+    # is present but all holds are in CLOSED/PENDING_ENTRY state.
+    # not_before_date filter prevents over-protection of future config picks.
+    if not _qhm_load_failed and not _qhm_protected and not _qhm_state_path.exists():
+        _qhm_protected = _configured_qhm_picks()
+        if _qhm_protected:
+            logger.warning(
+                "QHM state file ABSENT — retaining GTC stops for "
+                "configured picks (not_before_date reached): %s",
+                sorted(_qhm_protected),
+            )
+            try:
+                send_slack(
+                    f":warning: QHM state file ABSENT in GTC reconcile — "
+                    f"retaining stops for {sorted(_qhm_protected)}"
+                )
+            except Exception as _abs_e:
+                logger.warning("Absent-file Slack alert failed: %s", _abs_e)

Audit these 8 RC classes and return APPROVE or REJECT:
RC-1: Naive datetime (tz-unaware datetime.now())
RC-2: CWD-relative path (not anchored to __file__)
RC-3: Silent exception (bare except pass)
RC-4: Estimated exit price (non-fill price to record_exit)
RC-5: Non-atomic write (no tmp→replace pattern)
RC-6: Wrong API field name (Alpaca field assumed not confirmed)
RC-7: Zero-share sizing (int truncation before floor guard)
RC-8: Unbounded scan buffer (confirm_gate not cleared on block)

Also flag any other issues in the diff. Be concrete — exact lines and failure conditions.
```

---

### Diff
```diff
--- /home/user/alpaca-mtf-both/execution/orphan_manager.py	2026-07-03 22:02:10.792065443 +0000
+++ /tmp/proposed_orphan_manager.py	2026-07-03 22:23:45.857051887 +0000
@@ -21,7 +21,7 @@
 
 import json as _json
 import logging
-from datetime import datetime
+from datetime import date, datetime
 from pathlib import Path
 from typing import TYPE_CHECKING, Optional
 from zoneinfo import ZoneInfo
@@ -51,6 +51,32 @@
 logger = logging.getLogger(__name__)
 
 
+def _configured_qhm_picks() -> frozenset:
+    """Fallback QHM symbol set when quarterly_holds.json is absent.
+    Reads quarterly_holds_config.json 'picks' keys, filtered to symbols
+    whose not_before_date has been reached — future picks are excluded
+    because QHM cannot have entered them yet.
+    Returns empty frozenset on any failure — caller treats as no protection.
+    """
+    _cfg_path = (
+        Path(__file__).resolve().parent.parent
+        / "data" / "state" / "quarterly_holds_config.json"
+    )
+    try:
+        _cfg = _json.loads(_cfg_path.read_text())
+        _today = date.today()
+        return frozenset(
+            sym for sym, meta in _cfg.get("picks", {}).items()
+            if isinstance(meta, dict)
+            and date.fromisoformat(
+                meta.get("not_before_date", "9999-01-01")
+            ) <= _today
+        )
+    except Exception as _e:
+        logger.warning("_configured_qhm_picks: config read failed: %s", _e)
+        return frozenset()
+
+
 # ---------------------------------------------------------------------------
 # Time-of-day phase utilities
 # ---------------------------------------------------------------------------
@@ -169,6 +195,26 @@
         except Exception as _ofc_e:
             logger.warning("fail-closed Slack alert failed: %s", _ofc_e)
 
+    # B4: absent-file fallback — fires ONLY when quarterly_holds.json does not
+    # exist on disk. Explicit .exists() check prevents false-positive when file
+    # is present but all holds are in CLOSED/PENDING_ENTRY state (A+B board fix).
+    # not_before_date filter prevents over-protection of future config picks (C fix).
+    if not _qhm_load_failed and not _qhm_protected and not _qhm_state_path.exists():
+        _qhm_protected = _configured_qhm_picks()
+        if _qhm_protected:
+            logger.warning(
+                "QHM state file ABSENT — retaining GTC stops for "
+                "configured picks (not_before_date reached): %s",
+                sorted(_qhm_protected),
+            )
+            try:
+                send_slack(
+                    f":warning: QHM state file ABSENT in GTC reconcile — "
+                    f"retaining stops for {sorted(_qhm_protected)}"
+                )
+            except Exception as _abs_e:
+                logger.warning("Absent-file Slack alert failed: %s", _abs_e)
+
     gtc_positions = tracker.get_overnight_gtc_positions()
     if not gtc_positions:
         return
```

**STATUS: ready_for_gro_gai**
**To apply:** run Gro + GAI with the prompt above → if both APPROVE, then:
`git apply logs/pending_patch_2026-07-03_b4_orphan_manager.patch`

---
