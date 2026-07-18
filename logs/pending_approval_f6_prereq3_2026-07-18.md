# APPROVAL PACKAGE — F6 Prereq #3: GTC/DAY Stop Floor Check
**Date:** 2026-07-18 (autonomous CCR session)
**Branch:** `claude/gracious-keller-v19r71`
**Patch file:** `logs/pending_patch_2026-07-18_f6_prereq3.patch`
**Base commit:** `9107d23`
**Status: BOARD 2-0 APPROVE + cold second-agent PASS (3 rounds) — PENDING Gro+GAI (no .env in CCR session)**

---

## PROPOSAL

Add a never-sell-floor guard to `submit_gtc_stop_order` and `submit_day_stop_order` in
`execution/broker.py` so a live Alpaca sell-stop cannot execute on a Forever-6 anchor.
DORMANT today (`OWNERSHIP_GUARD_ENFORCE=False`). Activates only when prereq #2 ships.

---

## THE PROBLEM (plain English + stock example)

When the bot buys 1 share of CRWD at $207 as a Forever-6 anchor, that share is supposed to
never be sold. The code that enforces this lives in `close_position()`.

But a **GTC stop order** is stored at Alpaca, not in the bot. If CRWD drops to $187 overnight,
Alpaca fires the sell directly — without ever calling `close_position()`. The never-sell floor
check is completely bypassed. The F6 anchor is sold while you're sleeping.

The specific danger path: bot restarts overnight → `orphan_manager` sees CRWD as untracked →
adopts it as "intraday" orphan → submits emergency GTC stop at ±5% → CRWD dips → stop fires →
anchor sold. No floor check was ever consulted.

---

## THE FIX (plain English)

Before submitting any GTC or DAY sell-stop to Alpaca, check the ownership ledger: if this
symbol has any `forever6` shares recorded, skip the stop (return None) and log a warning.
Callers already treat a None return as a CRITICAL alert to Slack — no escalation change needed.

Completely inactive today (`OWNERSHIP_GUARD_ENFORCE=False`). Zero behavior change.
Activates the moment prereq #2 flips the guard on.

Two exclusions deliberately carved out:
- `tier="qhm"`: QHM legitimately submits GTC stops for its own shares
- `side != "sell"`: buy stops are irrelevant (F6 universe is long-only)

---

## BGG VOTE RECORD

### Board — Reliability seat (cold subagent, prior session)
**APPROVE**
- Correct exception handling: catches `LedgerError` precisely (not bare `Exception`)
- DORMANT pattern mirrors existing `close_position()` and `partial_close_position()` guards
- Q1 race (AH ordering): structurally impossible via `run_cycle.py` — GTC stop loop (L579)
  always runs before F6 starter (`execute_starter` L864). CONFIRMED this session.
- Q5 double-failure gap (cache write failure + ledger corruption → protected set misses):
  documented residual risk, acceptable for DORMANT prereq

**Conditions binding BEFORE ARMING (prereq #2), NOT before ship:**
1. ~~Verify run_cycle.py AH ordering~~ → CONFIRMED this session (L579 before L864)
2. Promote `ownership_guard.py:134-145` cache write failure from WARNING → CRITICAL + Slack
3. Document multi-tier coexistence: guard blocks entire intraday stop (not qty-bounded);
   intraday shares co-held with F6 anchor lose overnight exchange-level stop protection

### Board — Execution-risk seat (Harris/Thorp, cold subagent, 2026-07-18)
**APPROVE**
- Q1 (fail-closed): CORRECT — matches `close_position()` pattern exactly
- Q2 (`side=="sell"` scope): CORRECT — F6 is long-only; buy stops not relevant
- Q3 (ledger-sync race): DOCUMENTED in patch comment — race gap (0–14s during C-2 retry)
  is bounded, documented, and prereq #2 must verify ledger before flip
- Q4 (over-blocking): accepted structural limitation; caller escalation chain already correct
- Q4 (local var): cosmetic double-call → fixed (stores result in `_p3_f6_qty`)

**Required condition satisfied:** race gap documented in code comment ✓

### Cold second-agent (round 1) — FAIL → FIXED
**Bug found:** `protected_floor()` returns `forever6_qty + qhm_qty`.
Would block intraday stops on QHM-only symbols with zero F6 shares.
**Fix:** replaced with `tier_qty(ledger, symbol, "forever6")` (F6-only)
LedgerError fallback: replaced `_cached_protected_symbols()` alone with
`_cached_protected_symbols() & set(FOREVER6_UNIVERSE)` — F6-only intersection

### Cold second-agent (round 2) — FAIL → FIXED
**Bug found:** `FOREVER6_SYMBOLS` does not exist in config.py.
Config uses `FOREVER6_UNIVERSE` (line 383). The attribute name mismatch made `getattr()`
return `[]`, making `_p3_f6_set` always an empty set → LedgerError fail-closed path was
dead code → every F6 symbol silently passed through on ledger error.
**Fix:** `FOREVER6_SYMBOLS` → `FOREVER6_UNIVERSE` in both GTC and DAY blocks + comment.

### Cold second-agent (round 3) — **PASS**
All four checks clear. Key confirmations: `tier_qty()` signature correct; `_cached_protected_symbols()`
returns a Python `set` (& operator type-correct); `FOREVER6_UNIVERSE` exists in config.py (list of
strings); absent ledger returns `_empty_ledger()` not LedgerError → guard does not block when ledger
missing (correct). All four terminal states (block/allow × ledger-ok/ledger-error) verified reachable.

### Gro (Groq) — NOT AVAILABLE (no .env in CCR session)
### GAI (Gemini) — NOT AVAILABLE (no .env in CCR session)
**MANDATORY: Gro + GAI must run on the exact final diff (below) before ship.**

---

## EXACT FINAL DIFF (after all three bug fixes)

```diff
--- a/execution/broker.py
+++ b/execution/broker.py
@@ -356,6 +356,40 @@
         logger.warning(f"[{symbol}] GTC stop skipped: qty={qty}, stop=${stop_price}")
         return None
 
+    # F6 PREREQ #3 — never-sell-floor guard on GTC stop path.
+    # DORMANT while OWNERSHIP_GUARD_ENFORCE=False. A GTC stop triggered by
+    # Alpaca overnight bypasses close_position's floor check — this is the
+    # only intercept point. Inert today; activates only once prereq #2 ships.
+    # tier="qhm" excluded: QHM has independent share ownership and may stop its own qty.
+    # RACE NOTE: floor=0 during C-2 verify-loop (0–14s post-F6-buy) → guard does not block
+    # during that window; fresh anchor is exposed until ledger syncs. Prereq #2 must verify
+    # ledger-reflected before flipping OWNERSHIP_GUARD_ENFORCE=True.
+    import config as _p3cfg
+    if side == "sell" and tier != "qhm" and getattr(_p3cfg, "OWNERSHIP_GUARD_ENFORCE", False):
+        from execution import ownership_guard as _og_p3
+        try:
+            _p3_ledger = _og_p3.load_ledger()
+            _p3_f6_qty = _og_p3.tier_qty(_p3_ledger, symbol, "forever6")
+            if _p3_f6_qty > _og_p3._QTY_EPS:
+                logger.warning(
+                    f"[{symbol}] submit_gtc_stop_order SKIPPED (tier={tier!r}) — "
+                    f"forever6 qty={_p3_f6_qty:.0f} share(s); "
+                    f"live stop would bypass floor if triggered overnight."
+                )
+                return None
+        except _og_p3.LedgerError as _p3e:
+            # F6-only fallback: intersect protected cache with FOREVER6_UNIVERSE so we
+            # never fail-closed on QHM-only symbols during ledger errors.
+            _p3_f6_set = _og_p3._cached_protected_symbols() & set(
+                getattr(_p3cfg, "FOREVER6_UNIVERSE", [])
+            )
+            if symbol in _p3_f6_set:
+                logger.warning(
+                    f"[{symbol}] submit_gtc_stop_order SKIPPED (tier={tier!r}) — ledger "
+                    f"unreadable for F6-protected symbol (fail-closed): {_p3e}"
+                )
+                return None
+
     client     = _get_trading_client()
     order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
     order_data = StopOrderRequest(
@@ -473,6 +507,33 @@
         logger.warning(f"[{symbol}] DAY stop skipped: qty={qty}, stop=${stop_price}")
         return None
 
+    # F6 PREREQ #3 — never-sell-floor guard on DAY stop path (mirrors GTC guard above).
+    # DORMANT while OWNERSHIP_GUARD_ENFORCE=False. tier="qhm" excluded.
+    # RACE NOTE: same sync-gap caveat as GTC block above — see that comment.
+    import config as _p3cfg
+    if side == "sell" and tier != "qhm" and getattr(_p3cfg, "OWNERSHIP_GUARD_ENFORCE", False):
+        from execution import ownership_guard as _og_p3
+        try:
+            _p3_ledger = _og_p3.load_ledger()
+            _p3_f6_qty = _og_p3.tier_qty(_p3_ledger, symbol, "forever6")
+            if _p3_f6_qty > _og_p3._QTY_EPS:
+                logger.warning(
+                    f"[{symbol}] submit_day_stop_order SKIPPED (tier={tier!r}) — "
+                    f"forever6 qty={_p3_f6_qty:.0f} share(s); "
+                    f"live stop would bypass floor if triggered during RTH."
+                )
+                return None
+        except _og_p3.LedgerError as _p3e:
+            _p3_f6_set = _og_p3._cached_protected_symbols() & set(
+                getattr(_p3cfg, "FOREVER6_UNIVERSE", [])
+            )
+            if symbol in _p3_f6_set:
+                logger.warning(
+                    f"[{symbol}] submit_day_stop_order SKIPPED (tier={tier!r}) — ledger "
+                    f"unreadable for F6-protected symbol (fail-closed): {_p3e}"
+                )
+                return None
+
     client     = _get_trading_client()
     order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
```

---

## STATIC ANALYSIS — FINAL DRAFT

- `py_compile`: **PASS**
- `mypy --warn-unreachable`: **PASS** (0 errors)
- `ruff check --select E,W,F,B`: **PASS** (0 violations)
- `git apply --check`: **PASS** (base commit `9107d23`)
- Patch SHA256: `bf242f1d20bfcba5f655c8f250edf38350efd8f7b9640006d07c976db8b16317`

---

## CONSENSUS SUMMARY

| Voice | Vote |
|-------|------|
| Board — Reliability seat | **APPROVE** (conditions before arming) |
| Board — Execution-risk seat | **APPROVE** |
| Cold second-agent R1 | FAIL → fixed (protected_floor→tier_qty) |
| Cold second-agent R2 | FAIL → fixed (FOREVER6_SYMBOLS→FOREVER6_UNIVERSE) |
| Cold second-agent R3 | **PASS** |
| Gro | **PENDING** (CCR session; no .env) |
| GAI | **PENDING** (CCR session; no .env) |

All blockers resolved: **NO** — Gro + GAI required before ship (next interactive session).

---

## YOUR DECISION

**APPROVE** → Gro+GAI must first audit the exact diff above (FINAL PRE-SHIP rule).
Then: `git apply logs/pending_patch_2026-07-18_f6_prereq3.patch`, commit, push,
OCI `git pull --ff-only`. NO restart needed (patch is DORMANT). After ship, prereq #2
(arm `OWNERSHIP_GUARD_ENFORCE=True`) is the final F6 arming step.

**REJECT/DEFER** → F6 arming remains blocked. GTC/DAY stop bypass path stays open.

## RISK IF APPROVED
Zero today (DORMANT). When prereq #2 arms: intraday GTC stops blocked for F6 symbols.
Intraday shares co-held with F6 anchor lose exchange-level stop protection (documented;
accepted cost of the never-sell guarantee).

## RISK IF REJECTED
GTC/DAY stop path bypasses the F6 floor. Alpaca can execute a triggered sell-stop on an
F6 anchor even after prereq #2 arms — defeating the entire never-sell guarantee.

---

## PREREQ ORDERING (binding from f6_prereq1_syncgap_design_2026-07-17.md C-4)
#1 (ledger sync — COMPLETE: 800815e+9ad926d) → **#3 (this patch)** → #2 (arm guard, LAST)
