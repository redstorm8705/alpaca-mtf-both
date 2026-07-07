
## strategy/run_cycle.py — Board REJECT (1/3 APPROVE, 2/3 REJECT) — 2026-07-07
REASON: Board agents B and C both REJECT. Only agent A approved.
FINDING: Multiple EXIT events logged for the same position across run_cycle() calls because position state is not cleared immediately upon exit order submission.
ACTION: Re-design required — see board analysis below.

### Board Agent A — APPROVE
Data structure correct (module-level set, O(1) lookup, intersection_update pruning).
Pruning logic sound including empty-map edge case.
Flagged risk: once gating patch lands, if Alpaca rejects an exit order, symbol stays in
_pending_exit_symbols permanently — gating patch must remove symbol on order rejection.
No RC violations.

### Board Agent B — REJECT
Patch builds and maintains _pending_exit_symbols but never gates on it.
check_exits() called unconditionally every cycle regardless of set contents.
Duplicate exit fires unchanged after this patch ships.
Deferred gate creates inverted cross-module dependency (main.py reading run_cycle._pending_exit_symbols).
Correct architecture: pass pending_exit_syms as explicit parameter to check_exits() in exit_logic.py.
Startup-reset vulnerability: bot restart between submit and fill cold-boots set to empty, zero protection.
Recommendation: collapse Phase A and Phase B into single patch — add pending_exit_syms: set | None = None
to check_exits() signature, skip symbols in set, run_cycle.py owns and passes it explicitly.

### Board Agent C — REJECT
None of the three changes include an actual guard before submitting an exit.
Set populates and prunes correctly but is never consulted before check_exits() fires.
Bookkeeping without enforcement = same logical gap as prior local-variable draft.
Secondary: module-level mutable state bleeds across tests (no _reset_pending_exits_for_test() hook).
Resubmit with a fourth change: guard between pruning step and check_exits() call, or pass as parameter
into check_exits() to skip symbols already in _pending_exit_symbols.

### Correct Fix Architecture (consensus of B + C)
Single patch spanning run_cycle.py + exit_logic.py + main.py:
1. Add pending_exit_syms: set | None = None to check_exits() signature in exit_logic.py
2. Inside check_exits(), skip any symbol in pending_exit_syms before submitting exit
3. run_cycle.py owns _pending_exit_symbols (module-level set) and passes it explicitly
4. On exit order success: update(_pending_exit_symbols, closed)
5. On cycle start: intersection_update(_pending_exit_symbols, tracker.open_trades.keys())
6. On exit order rejection by Alpaca: discard(symbol) from _pending_exit_symbols
This eliminates implicit global coupling and delivers actual duplicate-exit prevention.
Requires full read + audit of exit_logic.py and main.py before any patch is proposed.
