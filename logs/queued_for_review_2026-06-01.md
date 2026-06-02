## reconcile_eod.py — RC-3 x4 — queued 2026-06-02 07:12 PM PT

REASON: Board FAIL — Agent B (Red Teamer) FAIL: patch is a detection aid only, not a fix; silent-skip exploit remains exploitable
FINDING: RC-3 violations (silent exception blocks) at 4 locations: L143 (_parse_fill_ts, unused fn), L155 (_parse_tracker_ts, unused fn), L221 (_weighted_avg_exit_price inner loop), L243 (_weighted_avg_exit_price outer loop). Proposed fix: add logger.debug() before existing continue/return None. Agent B argues malformed fill → silent skip → biased P&L exists pre-patch and post-patch; DEBUG logging is not remediation.
BOARD: A=PASS | B=FAIL (P5-H2: detection aid not fix — malformed fill still silently skipped in _weighted_avg_exit_price, exit price computed from fewer fills, P&L biased) | C=PASS
PROPOSED FIX FOR B SATISFACTION: Change logger.debug → logger.warning for the two ACTIVE violations in _weighted_avg_exit_price (L221, L243). Add count sentinel: if skipped_fills > 0 after loop, log WARNING "N fills skipped due to malformed qty/price — exit price may be biased." _parse_fill_ts and _parse_tracker_ts (unused) can remain at DEBUG.
ACTION: Revise patch to WARNING level for active violations + add post-loop count warning, then re-run board vote
PRIORITY: P1 (RC-3 protocol violation in active code paths)

## reporting/metrics.py — queued 2026-06-01 10:00 PM PT

REASON: Bug location misidentified — avg_r_multiple NOT FOUND after full read
FINDING: handoff.md P1 says "avg_r_multiple miscalculated in reporting/metrics.py" but full read of reporting/metrics.py (192L) confirms the function does NOT exist in this file. File only computes: total_pnl, total_trades, wins, win_rate, avg_score, avg_tqi, profit_factor, pdt_count, overnight_count. weekly_perf_audit.py (1255L) also fully read — not present there either.
BOARD: A=[not run — bug not found in target file] | B=[not run] | C=[not run]
ACTION: Check weekly_review.py (1667L) next session — likely contains Edge Ratio computation mislabeled as avg_r_multiple by Gemini. If found there, run full patch sequence for weekly_review.py.
PRIORITY: P1 (downgrade to P2 pending location confirmation)
SOURCE: handoff.md P1 item "avg_r_multiple miscalculated — reporting/metrics.py"

## execution/fill_helpers.py — queued 2026-06-01 10:01 AM PT

REASON: Board FAIL — Agent B (Red Teamer) FAIL: P5-H2 Stale Fill Crosstalk amplification
FINDING: P0 handoff finding "increase poll to 3 attempts with 3s backoff" — proposed patch changes _MAX_TOTAL_WAIT 2.5→3.0 and adds Attempt 3 block. Agent B raised: 3rd attempt increases probability (~27% cumulative vs ~19%) that Alpaca returns stale entry-fill for same-symbol sub-second rapid re-entry. submitted_after filter is partial (only filters by after-time, not by sub-second tie-breaking). Agent A also flagged minor line count discrepancy. Agent C: PASS (change safe, asymmetrically beneficial per Kelly accuracy argument).
SECONDARY FINDING: "fill_correction math wrong" (the other part of the P0) is NOT in fill_helpers.py after full read (212L). fill_correction function does not exist in this file — likely in execution/fill_reconciler.py or execution/portfolio_tracker.py. Needs investigation.
BOARD: A=FAIL (line count) | B=FAIL (P5-H2 amplification) | C=PASS
PROPOSED FIX FOR B: Before merging, fix sort tie-breaker in _query_fills to use (filled_at, order_id) tuple instead of filled_at string alone — ensures stable ordering on sub-second fills. Also add post-filter guard: reject fill where filled_at is not strictly after submitted_after + 100ms.
ACTION: Fix sort tie-breaker + add post-filter, then re-run board vote before applying
PRIORITY: P0 (but blocked on B's finding)
SOURCE: handoff.md P0 "fill_correction math wrong → P&L corruption"

## main.py — OVERNIGHT_ENTRIES_ENABLED — queued 2026-06-01 10:02 PM PT

REASON: Board FAIL — Agent A (Protocol Parser) FAIL: orphaned comment; Agent B (Red Teamer) FAIL: orphaned comment + bypass risk + missing startup log
FINDING: handoff.md P1 says "OVERNIGHT_ENTRIES_ENABLED hardcoded False at line 120 — remove override; read from config only." Full read of main.py (951L) confirmed variable at line 131 (shifted). config.py does NOT define OVERNIGHT_ENTRIES_ENABLED. entry_logic.py reads _main.OVERNIGHT_ENTRIES_ENABLED via lazy import at lines 1263, 1548, 1706.
PROPOSED FIX: (1) Remove line 131 `OVERNIGHT_ENTRIES_ENABLED = False  # Rachel Okonkwo: flip after Phase 1 validates`; (2) move comment block (lines 129-130) with it; (3) add after `import config` at line 191: `OVERNIGHT_ENTRIES_ENABLED = getattr(config, "OVERNIGHT_ENTRIES_ENABLED", False)`; (4) add startup INFO log: `logger.info("OVERNIGHT_ENTRIES_ENABLED resolved to %s", OVERNIGHT_ENTRIES_ENABLED)`.
BOARD: A=FAIL (comment block lines 129-130 orphaned — section header would point at _OVERNIGHT_CANCEL_CUTOFF, not the variable) | B=FAIL (same comment orphan; bypass risk: config.py edits carry less ceremony than main.py edits; guard comment must be co-located in config.py; startup log line missing) | C=PASS (architecturally sound, follows existing getattr pattern, functionally identical today)
REQUIRED BEFORE RE-PROPOSAL: (a) move comment block (lines 129-130) to be co-located with variable at new ~192 location; (b) add logger.info startup log for resolved value; (c) add guard comment to config.py noting OVERNIGHT_ENTRIES_ENABLED requires board vote before setting True — NOTE: config.py is normally off-limits but a comment-only addition may be acceptable; (d) confirm NameError window clear (already confirmed by grep: only line 131 in main.py, no references in lines 132-191)
ACTION: Revise patch to include comment relocation + startup log, re-run board vote
PRIORITY: P1
SOURCE: handoff.md P1 "OVERNIGHT_ENTRIES_ENABLED hardcoded False — main.py line 120"
