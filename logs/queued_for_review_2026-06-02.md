## execution/trade_engine.py — risk.open_positions desync — queued 2026-06-02 10:51 PM PT

REASON: Board FAIL — Agent B (Malicious Red Teamer): double-increment exploit — `_reconcile_pending_overnight_orders()` runs every RTH cycle; if `promote_pending_to_active()` hasn't cleared the pending-order state before a second cycle call, the same overnight fill is processed twice → double register_open() → position limit bypass. Also: permanent divergence from tracker ground truth with no self-correction mechanism (silent open_positions corruption). Agent C (Quant Risk Manager): `register_open()` stacks a +1 onto a potentially desynced base; the old SET was idempotent and self-correcting; a monotonic increment on an unverified base is not — creates undercount risk under MAX_POSITIONS guard and is fragile to any prior desync.
FINDING: L251-253 of `_reconcile_pending_overnight_orders()` does a direct assignment `risk.open_positions = len([t for t in tracker.open_trades.values() if t.get("status") == "open"])` which bypasses the CYCLE-SYNC-GUARD monotonic-UP invariant introduced in S42. Proposed fix (replace with `risk.register_open()`) was rejected by B and C for the reasons above.
BOARD: A=PASS | B=FAIL (double-increment exploit, permanent divergence from tracker ground truth) | C=FAIL (idempotency violation, MAX_POSITIONS guard undercount risk, fragile monotonic increment on unverified base)
PROPOSED REVISED APPROACHES (both B and C preferred approach A):
  (A) Keep original SET but add directional guard — only allow increase, never decrease:
      true_count = len([t for t in tracker.open_trades.values() if t.get("status") == "open"])
      if true_count > risk.open_positions:
          risk.open_positions = true_count
          logger.info("[%s] Overnight fill: risk.open_positions SET to %d (tracker re-anchor)", symbol, risk.open_positions)
  (B) Use register_open() + immediate tracker re-anchor with desync warning if counts differ
ACTION NEEDED: Choose revised approach (A or B above) and re-run full board vote before re-proposing. Approach A preserves idempotency + self-correction + is safe under repeated cycle calls. Approach B preserves CYCLE-SYNC-GUARD monotonic invariant but requires tracker sync.
PRIORITY: P1 (risk.open_positions desync — bypasses MAX_POSITIONS guard on overnight fills)

## monthly_review.py — Dead code removal (_load_lifetime_pnl) — queued 2026-06-02 10:04 PM PT

REASON: Board FAIL — Agent A (Strict Protocol Parser): S47d board voted "4/4 MODIFY with 6 mandatory requirements" (Kim/McKinney/Harris/Majors). Proposed simple removal of _load_lifetime_pnl() does not satisfy those requirements. Agent A asserts removal discards the fix mechanism and requires either (a) implementing the 6-requirement MODIFY, or (b) a new board vote specifically authorizing pure removal.
FINDING: _load_lifetime_pnl() at L66-78 is confirmed dead code — zero callers in entire codebase (AST-verified). Function reads from lifetime_pnl_cache.json which generate_dashboard.py was designed to write but never does. _build_html() calls compute_lifetime_stats() directly (correct). B=PASS (no exploit path — dead code with no callers). C=PASS (no sizing/P&L/scoring risk).
BOARD: A=FAIL (6 requirements from S47d MODIFY not documented in handoff.md — cannot verify compliance) | B=PASS | C=PASS
ACTION NEEDED: Rafael to clarify what the 6 mandatory requirements were for monthly_review.py from S47d. Options: (1) If the 6 requirements include wiring _load_lifetime_pnl() into _build_html(), that depends on generate_dashboard.py first writing the cache — do this after generate_dashboard.py RTH patch is applied. (2) If the 6 requirements authorize pure removal of dead code, re-run board with that explicit mandate and apply. (3) If unsure, a new board vote with the question: "Is pure removal of _load_lifetime_pnl() acceptable given that generate_dashboard.py never writes lifetime_pnl_cache.json?"
PRIORITY: P1 (sub-item of Dashboard P/L mismatch — generate_dashboard.py RTH patch is the primary fix)
