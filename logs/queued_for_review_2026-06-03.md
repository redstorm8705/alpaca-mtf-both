## reconcile_eod.py — MSTR P1 PHANTOM (_fifo_reconstruct) — queued 2026-06-03 03:14 PM PT

REASON: Bug location does not exist — _fifo_reconstruct NOT FOUND after full read
FINDING: handoff.md P1 says "reconcile_eod _fifo_reconstruct failing to clear closed positions from overnight dict." Full read of reconcile_eod.py (589L post-patch) confirms the function _fifo_reconstruct does NOT exist anywhere in this file. reconcile_eod.py handles only eod["trades"] (closed list) — it has no overnight_holds dict, no FIFO reconstruction, and no mechanism to write back to portfolio_tracker state. MSTR overnight_holds tracking is in execution/portfolio_tracker.py (RTH-chain). Gemini likely hallucinated the function name (same pattern as "fill_correction" phantom in S46).
BOARD: Not run — bug location does not exist in stated file
ACTION: Investigate execution/portfolio_tracker.py for overnight_holds logic (RTH-chain — requires Explore subagent full read + DS/GAI before any patch). Gemini source: "May 27 CRITICAL" — treat as unverified until confirmed in actual portfolio_tracker.py code.
PRIORITY: P1 (downgrade pending location confirmation — may be phantom)
SOURCE: handoff.md P1 "MSTR tracked as both closed and overnight_hold in EOD snapshot"

## main.py — BoD-3 MAX_DAILY_LOSS_PCT comment stale — queued 2026-06-03 03:16 PM PT

REASON: Cannot draft safely — requires config.py PROFILES dict values; config.py is HARD EXCLUDED.
FINDING: L305-315 BoD-3 block comment says "config.py paper profile sets MAX_DAILY_LOSS_PCT=0.30" but Gemini (May 27+28) flags this as potentially stale. The condition  only fires when the paper profile value > 0.15. HANDOFF hardcoded invariants say "Kill switch: 15% for paper" — consistent with either (a) PROFILES has 0.30 → BoD-3 fires → caps to 0.15, or (b) PROFILES has 0.15 → condition False → already at 0.15. Cannot verify without reading config.py PROFILES["paper"]["MAX_DAILY_LOSS_PCT"].
BOARD: Full read complete (975L, 4 chunks). RC audit complete. Board vote NOT run — safe to audit but cannot draft without config values.
10-POINT AUDIT NOTES: RC-1 PASS, RC-2 PASS, RC-3 minor L224-229 (no exception log in inode guard — low risk, logging setup only), RC-4 N/A, RC-5 PASS, RC-6 PASS, RC-7 N/A, RC-8 PASS.
ACTION: User to confirm current value of config.PROFILES["paper"]["MAX_DAILY_LOSS_PCT"]. If != 0.30, update comment at L306. If == 0.30, no fix needed (comment is accurate, block fires correctly).
PRIORITY: P2 (comment/log misleading — no functional impact on kill switch safety)
SOURCE: handoff.md P2 "MAX_DAILY_LOSS_PCT BoD-3 log message misleading — Gemini May 27+28"
