## execution/entry_logic.py + main.py — Conviction Linear Spline — queued 2026-06-24 (autonomous nightly)

REASON: board FAIL [Agent A + Agent C] — config.py CONVICTION_SKIP_BELOW constraint + premature Agent A run

---

### CHANGE DESCRIPTION

Replace the current conviction cliff (score<10=skip, score=10=47.5%, score≥11=95%) with a
linear spline that uses the full 12-point scale:

  score <  9  → 0% (rejected at CONVICTION_SKIP_BELOW gate before sizing)
  score =  9  → 0%  (formula floor — dead code path with CONVICTION_SKIP_BELOW=10)
  score = 10  → 33% of BUCKET_B_ALLOCATION_PCT  (~$792 at $2.5K)
  score = 11  → 67% of BUCKET_B_ALLOCATION_PCT  (~$1,583 at $2.5K)
  score >= 12 → 100% of BUCKET_B_ALLOCATION_PCT (~$2,375 at $2.5K)

Formula: fraction = (score - MIN) / (MAX - MIN), pct = fraction × BUCKET_B_ALLOCATION_PCT
Current (before): MIN=10, MAX=11, pct_min=47.5%
Proposed (after):  MIN=9,  MAX=12, pct_min=0.0%

---

### PROPOSED DIFF

**Site 1: main.py L158-162**

```diff
-# Replaces the binary 47.5%/95% cliff with smooth interpolation across scores 9→11.
-# Score 9 → 47.5% | Score 10 → 71.25% | Score 11+ → 95%
-_LINEAR_SCORE_MIN = 10  # maps to BUCKET_B_ALLOCATION_PCT * 0.5  (raised from 9 → 10)
-_LINEAR_SCORE_MAX = 11  # maps to BUCKET_B_ALLOCATION_PCT * 1.0
+# Linear spline: score 9→0%, 10→33%, 11→67%, 12→100% of full allocation.
+# Formula: fraction = (score - MIN) / (MAX - MIN), applied to [0, BUCKET_B_ALLOCATION_PCT].
+_LINEAR_SCORE_MIN = 9   # score < 9 → skip; score = 9 → 0% (formula floor)
+_LINEAR_SCORE_MAX = 12  # score >= 12 → 100% of BUCKET_B_ALLOCATION_PCT
```

**Site 2: execution/entry_logic.py L1072-1075**

```diff
-# Rank 2: conviction sizing — linear
-#   score 10  → ½ size  (47.5%)
-#   score 11+ → full    (95%)
-_pct_min  = config.BUCKET_B_ALLOCATION_PCT * 0.5   # 47.5%
+# Rank 2: conviction sizing — linear spline: 9=0%, 10=33%, 11=67%, 12=100%
+_pct_min  = 0.0                                      # formula yields 0 at score==MIN (skip)
```

**Companion (BLOCKED — config.py forbidden):**
```diff
-CONVICTION_SKIP_BELOW = 10
+CONVICTION_SKIP_BELOW = 9   # align with _LINEAR_SCORE_MIN (board vote required)
```

---

### STATIC ANALYSIS (current files before patch)

  py_compile main.py:            PASS
  py_compile execution/entry_logic.py: PASS
  mypy main.py --warn-unreachable:     PASS (0 errors)
  mypy execution/entry_logic.py:       PASS (0 errors)
  ruff check main.py:                  PASS (0 violations)
  ruff check execution/entry_logic.py: PASS (0 violations)

---

### TESTS

  tests/test_conviction_spline.py — 12/12 PASS (created this session)
  Tests cover: boundary conditions (score 8,9,10,11,12,13), monotonicity,
  no-int-truncation, and regression vs current formula.

---

### 3-AGENT BOARD VOTE

**AGENT A: FAIL (PREMATURE)**
Agent A ran before full read of entry_logic.py was declared complete and before
test file was created. Failure reasons:
  (1) "No board vote in current session" — INVALID: 3-agent system IS the board vote
  (2) "No tests/TDD artifacts" — RESOLVED: test file created, 12/12 pass
  (3) "No Phoenix Project verification artifacts" — VALID: no rollback plan documented
Vote invalidated by premature run condition. Queuing per strict 3/3 PASS requirement.

**AGENT B: PASS**
Adversarial red team review. All 8 attack vectors verified safe:
  ✓ Score=9 "phantom pass" — two independent guards prevent any side effects:
      (1) CONVICTION_SKIP_BELOW gate at L589 (score=9 rejected before sizing)
      (2) `if dollar_cap == 0: continue` at L1090 (catches it if gate ever changes)
  ✓ RC-7 floor with dollar_cap=0 — _can_afford_one=False → shares=0 → L1255 guard skips
  ✓ entry_confirm_buffer/conviction_streak — unaffected (incremented pre-sizing)
  ✓ _LINEAR_SCORE_MAX=12 boundary — score≥12 correctly caps (score=13+ safe)
  ✓ Float vs int score — formula handles both; score always int in practice
  ✓ _pct_min reference scope — only used at L1075, L1083 within sizing block (localized)
  ✓ Boundary verification — all 4 branches (score 8, 9, 11, 12) traced correctly
  ✓ Zero-share guard — `if shares < 1: continue` at L1255 confirmed present

Design notes (non-blocking):
  - Score=9 in formula is dead code with CONVICTION_SKIP_BELOW=10 — semantically
    inconsistent but doubly guarded (no runtime impact)
  - Comment at entry_logic.py L1072-1074 should update to reflect new allocation percentages

**AGENT C: FAIL (CONDITIONAL — config.py BLOCKING)**
Quant risk review. Mathematical analysis complete:
  ✓ All 6 boundary conditions verified correct
  ✓ Monotonicity invariant: $0 ≤ $0 ≤ $792 ≤ $1,583 ≤ $2,375 ✓
  ✓ RC-7 floor: hard cap at L1225-1226 prevents floor from exceeding dollar_cap
  ✓ Kelly + TQI + TSMOM + Earnings + Volume + FVG cascade: all multiplicative, safe
  ✓ EV direction: POSITIVE — score 11 vs 12 is now differentiated (was identical at 95%)
  ✓ Score=12 expansion: justified — captures full 12-pt confluence resolution

  BLOCKING: CONVICTION_SKIP_BELOW = 10 (config.py L60) must be lowered to 9 for
  semantic consistency with _LINEAR_SCORE_MIN = 9.
  WITHOUT this change: score=9 is blocked at L589 before reaching sizing formula.
  _LINEAR_SCORE_MIN=9 is dead code — formula anchor without operational effect.

  AUTONOMOUS CONSTRAINT: config.py is FORBIDDEN in autonomous mode (hard rule).
  This companion change CANNOT be applied without Rafael's explicit approval and
  an interactive session board vote on config.py.

  CONDITIONAL PASS if: Rafael approves CONVICTION_SKIP_BELOW = 9 in same session.

---

### DS/GAI STATUS

  UNAVAILABLE — no .env file at expected path (macOS-specific path, not present
  in this Linux environment). DS/GAI audits deferred to interactive session.

---

### KEY DESIGN DECISION NEEDED (RAFAEL)

The board split on whether CONVICTION_SKIP_BELOW should change from 10 → 9:

**OPTION A (Conservative): Keep CONVICTION_SKIP_BELOW = 10**
  - Patch still applies (main.py + entry_logic.py)
  - score=9 never reaches sizing (filtered at L589) — dead code but safe
  - Net effect: score=10→33%, score=11→67%, score=12→100% (full range differentiated)
  - Formula anchor is _LINEAR_SCORE_MIN=9, which is mathematically correct but semantically
    inconsistent with CONVICTION_SKIP_BELOW=10
  - Simpler — no config.py change needed

**OPTION B (Full Design Intent): Lower CONVICTION_SKIP_BELOW = 9**
  - Requires config.py change (board vote needed for config.py)
  - score=9 signals reach sizing, produce dollar_cap=0, skip via L1090
  - Score=9 is now explicitly included in the "attempt but zero-size" category
  - More semantically consistent but adds no new trades (score=9 → 0 shares)
  - One extra config.py line change

**Agent C recommendation:** Option B (full semantic consistency).
**Agent B finding:** Option A is safe and doubly guarded.
**Roadmap intent (CLAUDE.md):** "9=0x, 10=0.33x, 11=0.67x, 12=1.0x" — consistent with EITHER
  option (score=9 is 0x under both — just filtered at different gates).

---

### IMPACT RADIUS

  main.py: _LINEAR_SCORE_MIN and _LINEAR_SCORE_MAX constants exported to entry_logic.py
  entry_logic.py: 4-line change in Rank-2 sizing block only
  No other files import _LINEAR_SCORE_MIN or _LINEAR_SCORE_MAX (constants live in main.py)
  kelly.py: unaffected (uses dollar_cap as output of sizing, does not set it)
  run_cycle.py: unaffected (calls execute_entries() — no direct sizing logic)
  exit_logic.py: unaffected
  portfolio_tracker.py: unaffected

---

### BASE COMMIT

  7e5c983 (current HEAD on main — includes VIX continuous curve from S63)

---

### FILES IN THIS QUEUE ENTRY

  logs/queued_for_review_2026-06-24.md — this file
  logs/pending_patch_2026-06-24_main_entrylogic.patch — exact diff for both sites
  tests/test_conviction_spline.py — test file (committed this session)

---

### ACTION REQUIRED

Rafael decision needed on:
  1. OPTION A (keep CONVICTION_SKIP_BELOW=10) or OPTION B (lower to 9)?
  2. Should Agent A's premature FAIL count as a real FAIL, or re-run all 3 agents
     in next interactive session with full package complete?
  3. DS + GAI audits required before apply (no .env in this environment) — provide
     API keys or confirm deferred to next live session with .env available.

If OPTION A approved: main.py + entry_logic.py patch can apply immediately.
If OPTION B approved: config.py board vote needed first, then 3-file patch.

Revisit in next interactive session.
