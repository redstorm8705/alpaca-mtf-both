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

## execution/trade_engine.py — Approach A (directional guard UP-only) — queued 2026-06-03 07:20 PM PT

REASON: Board FAIL — Agents B and C both FAIL on Approach A.
FINDING: Approach A proposed tonight: replace L252-254 SET with conditional `if _true_open > risk.open_positions: risk.open_positions = _true_open`. Board rejected: directional guard (UP-only) prevents downward correction — if risk.open_positions is a STALE OVERCOUNT (stuck above tracker truth e.g. after desync recovery), guard silently preserves it, creating permanent overcount that blocks entry gate until EOD reset.
BOARD TONIGHT: A=PASS | B=FAIL (undercount stalling — guard blocks self-healing when _true_open < risk.open_positions; stale counter locked above truth permanently) | C=FAIL (permanent overcount risk — violates antifragility; system cannot self-heal; blocks new entries for hours)
STATUS: Both Approach A (directional guard, tonight) and Approach B (register_open, last night) have been tried and rejected by the board. All 3 board-proposed approaches have failure modes.
OPTIONS FOR USER DECISION (user mandate required):
  Option C: REMOVE L252-254 entirely — rely on CYCLE-SYNC-GUARD in entry_logic.py to count the new fill on next execute_entries() call. Risk: 1-cycle window where new overnight fill not reflected. Self-corrects next cycle automatically. Simplest change.
  Option D: Keep original SET but add WARNING log when assignment would DECREASE risk.open_positions — preserves full self-correction (SET to ground truth), adds observability for desync events. Does NOT preserve monotonic-UP invariant but original SET never did either; the CYCLE-SYNC-GUARD handles monotonic during cycle.
  Option E: Bidirectional SET with alert — set to _true_open unconditionally (like original) but add CRITICAL+Slack when _true_open < current risk.open_positions (desync alert). Preserves self-correction, adds explicit alert instead of silent reset.
RECOMMENDATION: Option D or E. Option D is the least invasive change — keep the self-correcting SET, add a WARNING log when the reconcile step decreases the counter (observable signal for ops investigation). Option E is similar but with a Slack alert for significant discrepancies.
PRIORITY: P1 (risk.open_positions desync — bypasses MAX_POSITIONS guard on overnight fills)
ACTION: Rafael to choose Option C, D, or E. Re-run board with chosen approach on next session.

## generate_dashboard.py — P1 P/L cache write + RC-6 field name — queued 2026-06-03 07:24 PM PT

REASON: Board FAIL — Agents B and C FAIL on proposed patch (Agent A misidentified "not yet applied" as protocol failure — file correctly unmodified in RTH draft phase).
FINDING: Two confirmed bugs:
  BUG 1 (P1): _build_html() L402 computes `_lt = compute_lifetime_stats(equity=float(equity))` but NEVER writes result to lifetime_pnl_cache.json. ROOT CAUSE of Dashboard P/L mismatch (S47d board 4/4 MODIFY). Fix: atomic write after L409 with fields total_pnl/win_rate/total_trades/ts using "total_pnl" key.
  BUG 2 (RC-6): L186 uses `o["order_type"]` — may KeyError on raw Alpaca v2 JSON which uses "type" not "order_type". When open GTC orders exist, entire _load_alpaca() returns zeros + shows error banner.
BOARD TONIGHT: A=FAIL (misidentified draft state as missing patch) | B=FAIL (cache write uses `_lt.get("total_pnl", 0.0)` — defaults to $0 when key missing, cache diverges from dashboard equity-based fallback) | C=FAIL (Change 2 may be wrong — Agent C asserts Alpaca REST v2 returns "order_type" not "type"; proposed fix would display "market" for all orders)
BOARD CONCERNS FOR REVISION:
  CONCERN B: Fix `_lt.get("total_pnl", 0.0)` → use `_lt.get("total_pnl")` with explicit None guard; only write cache when total_pnl is not None. Prevents $0 cache write when key is unexpectedly missing.
  CONCERN C: Verify Alpaca REST v2 /v2/orders response field name: "order_type" vs "type". Use defensive `o.get("order_type") or o.get("type", "market")` to handle both, OR drop Change 2 until verified.
REVISED PATCH PROPOSAL (addressing board concerns):
  Change 1 (REVISED):
    AFTER `_lt = {}` fallback:
    _total_pnl = _lt.get("total_pnl")
    if _lt and _total_pnl is not None:
        try:
            _lt_cache_path = LOG_DIR / "lifetime_pnl_cache.json"
            _lt_tmp = _lt_cache_path.with_suffix(".tmp")
            _lt_tmp.write_text(json.dumps({"total_pnl": round(float(_total_pnl), 4), "win_rate": round(float(_lt.get("win_rate", 0.0)), 2), "total_trades": int(_lt.get("total_trades", 0)), "ts": datetime.now(ET).isoformat()}, indent=2), encoding="utf-8")
            _lt_tmp.replace(_lt_cache_path)
        except Exception as _lt_cache_e:
            logger.warning("_build_html: lifetime_pnl_cache write failed — monthly_review.py will use stale P&L: %s", _lt_cache_e)
  Change 2 (REVISED — use defensive dual-field lookup):
    "type": o.get("order_type") or o.get("type", "market"),
DEPLOY NOTE (Harris M-4 from S47d board): Delete stale OCI cache BEFORE deploying — `rm /home/ubuntu/mtf-bot/logs/lifetime_pnl_cache.json`
ACTION: Re-run board with REVISED patch (REVISED Change 1 + REVISED Change 2). DS/GAI required (RTH-chain via run_cycle.py).
PRIORITY: P1 (Dashboard P/L mismatch — generate_dashboard.py ROOT CAUSE)
