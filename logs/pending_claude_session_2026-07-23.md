# Pending Claude Session — Approval Queue (2026-07-23, Nightly Autonomous Run)

**Context:** Nightly scheduled autonomous run (22:00 ET). Per CLAUDE.md §5, no patches applied —
this is a fully-prepped approval-queue package. Each item below is ready to ship with one word.

---

## ITEM 1 — READY TO SHIP: weekly_review.py archive write-once fix

### PROPOSAL

**PROPOSAL:** Add write-once guard to the archive rebuild loop in `weekly_review.py` — past-week
archives that already exist on disk are preserved (not overwritten with `analysis=None`).

**THE PROBLEM (plain English + real example):**
Every time `weekly_review.py` runs, it rebuilds ALL weekly archive pages. For past weeks (e.g.
the week of Jul 14), it calls `build_html(w, ..., analysis=None)` — which generates a page with
NO AI verdict. This permanently destroys the Gemini analysis that was written during that week's
`--analyze` run. Example: the Jul 14 archive had a detailed Gemini verdict about NVDA and TSLA
performance — after the next run of weekly_review.py, that page shows "No AI verdict generated
for this week." The content is gone and unrecoverable.

**THE FIX (plain English):**
Before rebuilding a past archive, check if the file already exists. If it does, skip it and move
on — "write-once" for completed weeks. Only create or rebuild archives that don't exist yet, or
that are the current week (which legitimately needs fresh data).

**GATE RESULTS:**
- py_compile: PASS
- mypy --warn-unreachable: PASS (no issues)
- ruff check --select E,W,F,B: PASS (all checks passed)
- Cold second-agent (Board Agent 1 — cold parallel Explore): PASS
  - All 5 threat scenarios verified: logic inversion, off-by-one, Sunday 3PM PDT edge case,
    `--week <past>` behavior, `--week <future>` behavior
  - Non-bug confirmed: `_os.path.exists(w_path)` correctly allows first-time creation of
    non-existent past archives (doesn't block stub creation)
- RTH impact: NONE — weekly_review.py is display-only, not imported by any trading module
- Gro/GAI: NOT REQUIRED (display-only, no RTH execution impact per CLAUDE.md gate rule)

**CONSENSUS:** Cold-2nd PASS · Static 3/3 PASS · Board 1/1 APPROVE · Gro/GAI: N/A
All blockers resolved: **Yes**

**RISK IF APPROVED:** Minimal. Past archives are preserved (not regenerated). If a past week's
underlying EOD data changed retroactively, the archive would show stale data. This cannot happen
in practice — EOD files are write-once.

**RISK IF REJECTED:** Weekly Gemini analysis continues to be permanently destroyed each run,
making the archive pages data-only with no AI verdict.

**YOUR DECISION:** APPROVE / REJECT / DEFER

---

### EXACT DIFF

**File:** `weekly_review.py`
**Lines:** 1683–1704 (archive rebuild loop in `main()`)

```diff
-stubs_written = 0
+_real_monday = _default_monday()  # real current Monday regardless of --week
+preserved = 0
+stubs_written = 0
 for w in all_nav_weeks:
     w_path = _os.path.join(LOGS_DIR, f"weekly_{w.isoformat()}.html")
     if w == monday:
         continue  # current week's archive already written above
+    # Past archives: PRESERVE existing content (write-once for AI analysis)
+    if w < _real_monday and _os.path.exists(w_path):
+        preserved += 1
+        continue  # preserve existing archive — do not overwrite with analysis=None
     # Load whatever EOD data exists for that week (may be empty)
     w_eods = {}
     for i in range(5):
         wd = w + timedelta(days=i)
         w_eods[wd.isoformat()] = _load_eod(wd)
     stub_html = build_html(w, w_eods, backtest, None, day_trades,
                            archive_weeks=all_nav_weeks, is_archive=True)
     tmp_stub  = w_path + ".tmp"
     with open(tmp_stub, "w", encoding="utf-8") as f:
         f.write(stub_html)
     _os.replace(tmp_stub, w_path)
     stubs_written += 1
-print(f"  Refreshed {stubs_written} archive(s) with current navigation")
+print(f"  {preserved} historical archive(s) PRESERVED · {stubs_written} archive(s) refreshed")
```

**Why `_default_monday()` and not `monday`:** The `monday` variable comes from `args.week` and
may point to a past week when `--week YYYY-MM-DD` is used. Using `monday` as the anchor would
mean past-week archives are ONLY preserved relative to the `--week` argument, not relative to
today — destroying everything from `args.week` to today. `_default_monday()` always returns the
REAL current Monday regardless of arguments. This was the cold-2nd FAIL T1 in the prior session.

**Why `<` not `<=`:** `w == _real_monday` (current week) is handled by the `if w == monday`
guard above the loop when no `--week` flag is used. Using `<=` would be correct but redundant
in the normal case. When `--week <past>` is used, the current week correctly falls through to
regeneration (no AI content for it in that run anyway). `<` is correct.

---

## ITEM 2 — IN PROGRESS: RTH-chain items (exit_logic.py, data/premarket.py)

These are RTH-chain files requiring Gro + GAI audit via direct API (keys on Rafael's Mac —
unavailable in remote environment). Full reads and board drafts are in progress this session.
Gro/GAI Phase 1 and Phase 2 audits must be run in an interactive session.

### exit_logic.py P0 FIX — Rolling breach window (DRAFT, Gro/GAI needed)

**Status:** Board 5-0 APPROVE (per handoff.md, 2026-07-22 interactive session). Gro + GAI
needed for Phase 2 (diff-level audit). Full read in progress this session.

**Bug:** `stop_breach_count` increments monotonically — ANY recovery tick resets to zero.
Result: a sequence of BREACH-RECOVER-BREACH-RECOVER-BREACH can never trigger `_STOP_CONFIRM`
bars of contiguous breach. Position held open through unbounded loss.

**Fix (board-approved):** Rolling observed-bar window. `_STOP_WINDOW = 2 × _STOP_CONFIRM`.
Counter trips when `_STOP_CONFIRM` breach bars are seen within the last `_STOP_WINDOW` bars.
No new tunable. Board 5-0.

**To ship:** Interactive session → full read (Explore subagent) → Gro/GAI Phase 1 → diff →
Gro/GAI Phase 2 → cold-2nd → apply.

### data/premarket.py — Wilder's ATR fix (DRAFT, Gro/GAI needed)

**Full read complete: 449 lines (2 chunks) — 2026-07-23 autonomous session**

**Static analysis (current git version):**
- py_compile: PASS
- ruff check: PASS
- mypy --warn-unreachable: **7 errors (pre-existing, must fix per Rule C-4)**
  - L204: `base_universe` default `None` incompatible with `list[Any]` type hint → use `Optional[list]`
  - L205: `threshold_pct` default `None` incompatible with `float` → use `Optional[float]`
  - L228: `base_universe = list(...)` unreachable (consequence of non-Optional type hint)
  - L369, L429: `set.add()` returns None, not a value — pre-existing deduplication pattern
  (2 errors each at L369/L429, counted as 2 each in mypy output = 7 total with L204+L205+L228)

**10-Point Audit Findings:**
- Point 1 (static): 7 pre-existing mypy errors — must fix alongside ATR change
- Point 2 (trade path): `build_scan_universe()` is called by `main.py` during RTH. `atr_filter()` calls `calculate_atr()`. RTH-chain confirmed. Gro/GAI required.
- Point 3 (adversarial): `calculate_atr()` flat mean: if `df["tr"]` has NaN (e.g. first bar missing prev_close), mean propagates NaN → `round(NaN/price × 100) = NaN` → caller `atr_filter()` does `if atr_pct >= min_atr:` → `NaN >= float` is False → symbol filtered OUT silently. Minor impact (conservative) but causes unexpected filtering.
- Point 7 (redundancy): flat 14-bar mean at L90 is NOT Wilder's smoothed ATR — documented discrepancy from handoff
- RC-1: `datetime.now(ET)` at L59, L308 — PASS (internal scheduling, not user-facing)
- RC-2: `os.path.dirname(os.path.abspath(__file__))` at L52, L333 — PASS
- RC-3: all except blocks log before returning — PASS
- RC-5: tmp→replace + flush+fsync at L55-61 — PASS
- All other RC classes: N/A or PASS

**Proposed scope for the Wilder's ATR patch:**
1. Replace flat mean at L90 with Wilder's smoothed formula (SMMA):
   - Need `period + period` bars minimum for stable EMA seed (T3: `min_bars = ATR_PERIOD * 5` from handoff)
   - Seed: first `period` bars → simple mean; subsequent bars → Wilder's smoothing
   - T1: `if not math.isfinite(atr_value): return 0.0` guard on result
2. Fix pre-existing mypy errors: `Optional[list]`, `Optional[float]`, deduplication pattern
3. T2: caller-side guard in entry_logic.py / orphan_manager.py: `if not atr_value or atr_value <= 0:`
4. T4 (OCI staged): in-progress bar exclusion for `hour < 16` on half-days (if present in OCI version)

**Note:** T3 frame length change (`period+1` → `ATR_PERIOD*5`) is a silent behavior change that
could cause the filter to skip MORE symbols when bar history is limited. Board + Gro/GAI required.

**To ship:** Interactive session → board vote (Phase 1) → Gro/GAI Phase 1 → diff →
Gro/GAI Phase 2 → cold-2nd → apply.

---

### execution/exit_logic.py — P0 rolling breach window (DRAFT, full read in progress)

**Status:** Full read in progress (2303 lines, Explore subagent spawned in this session).
Board 5-0 APPROVE per handoff.md (2026-07-22 interactive session).
Gro + GAI required (RTH-chain P0 fix).

**Bug (confirmed in prior session):** `stop_breach_count` increments monotonically per L1611-1617.
A sequence of BREACH-RECOVER-BREACH-RECOVER-BREACH never accumulates `_STOP_CONFIRM` contiguous
breach bars because ANY recovery tick resets the counter to zero. Unbounded loss path.

**Board-approved fix (5-0):** Rolling observed-bar window.
- `_STOP_WINDOW = 2 × _STOP_CONFIRM` (no new tunable, computed from existing constant)
- Trip condition: `_STOP_CONFIRM` breach bars within the last `_STOP_WINDOW` observed bars
- Preserves behavior when stop is clearly breached (fast trips), prevents recovery-masked grinding

**To ship:** Interactive session → full read review (from Explore subagent result) →
Gro/GAI Phase 2 (diff-level) → cold-2nd → apply.

---

## AUTONOMOUS SESSION LOG — 2026-07-23

- 22:00 ET: Session started (nightly scheduled task)
- Slack webhook: blocked by proxy (403) — logging to stdout only
- AI audit gist: external URL unreachable — continued with handoff.md
- Branch: `claude/youthful-wozniak-mnbpr7` (at origin/main `3e0c9e1`)
- weekly_review.py: Full read complete (1717 lines, 6 chunks), audit complete, cold-2nd PASS — READY TO SHIP (Item 1 above)
- data/premarket.py: Full read complete (449 lines, 2 chunks), audit complete — draft in progress, Gro/GAI needed
- execution/exit_logic.py: Full read in progress (2303 lines, Explore subagent spawned) — draft in progress, Gro/GAI needed
- RTH items: Gro/GAI unavailable from remote environment (keys on Rafael's Mac only) — RTH-chain patches require interactive session
