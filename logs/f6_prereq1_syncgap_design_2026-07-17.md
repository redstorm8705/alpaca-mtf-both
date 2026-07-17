# F6 ARMING PREREQ #1 — ledger sync gap — BOARD-BLESSED DESIGN (2026-07-17)

Status: **APPROVED with binding mods** — implement per the CONDITIONS below, then gate the diff
(board-on-diff + Gro/GAI preship) and ship. Ships DARK/inert (no F6 shares exist; floor off).
Part of the 3-prereq F6-arming project (`logs/f6_activation_BLOCKED_2026-07-17.md` → mirrored in
committed `handoff.md`). This is prereq #1 of 3.

## 4-VOICE GATE RESULT (design stage)
- **Gro: APPROVE** (A1 / B1 / C1).
- **GAI: MODIFY** — mandatory seed-block; Alpaca rate-limit; concurrency.
- **Board Reliability: MODIFY** — file lock (lost-update proof); B1 must also check drift; persist +
  auto-clear the flag; bound B1 + poll order status. Confirmed full-replay is the right altitude
  (do NOT downgrade to a targeted write). Confirmed B1 necessary (healed=True with forever6=0 is expected).
- **Board Execution-risk: MODIFY** — B1 mandatory (B2 forbidden = never-mask); C2 scoped to seeding
  only (fail-closed on acquisition — safer direction); #2 arms LAST; #1 is inert-to-protective today.

### 3-POINT AI SUMMARY (design)
- **P1 ALIGNMENT — architecture:** 4/4 sound (A1 sync-in-execute_starter, B1 verify-retry, full-replay).
  Forks: A1 4/4, B1 4/4 (B2 explicitly forbidden by 2 seats — never-mask), C: 3/4 want C2-scoped (Gro C1).
- **P2 CLAUDE MISSED (both seats caught):** (a) the concurrent cron+in-process **lost-update** (never-shrink
  compares new-vs-stale-baseline, not vs current file) → needs a file lock; (b) the **feed-skew drift
  false-positive** — B1 checking only forever6_qty certifies "protected" while the symbol is drift-frozen;
  (c) the **latent drift-freeze landmine** a post-buy sync plants for a multi-tier symbol once #2 arms.
- **P3 FORWARD-LOOKING:** gate #2's arming on a clean early-open RTH re-sync before any intraday exit
  decisioning (Exec-risk). Rate-limit: bound B1 + backoff (no global limiter — consistent w/ RTH cron).

## VERIFIED FACTS (full reads this session)
- `submit_market_order(tier="forever6")` tags coid `F6-…` (broker.py L54/213) → replay attributes to forever6.
- `run_ledger_sync.sync_once()` (L97) — clean, NEVER-raises, full authoritative rebuild, returns {ok,healed}.
- `sync_ledger` never-shrink guard (ownership_guard L489-503): 0→N is an INCREASE → never trips. BUT the
  baseline is read at L480 and written at L505 with an unguarded gap → cross-process lost-update (Reliability
  proof). `check_never_sell_floor` FREEZES sells on drift (L232-235) but only AFTER the floor>0 gate
  (L221-224) → the drift landmine bites ONLY a multi-tier symbol (floor>0 + intraday add), not pure-intraday.
- The floor is DORMANT (OWNERSHIP_GUARD_ENFORCE=False). Today ONLY `orphan_manager._get_forever6_syms()`
  reads forever6 qty → writing it is strictly PROTECTIVE (removes the anchor from the orphan-adoption set).

## IMPLEMENTATION (the binding CONDITIONS)

### C-1 — file lock in `sync_ledger` (closes the lost-update; live-path, applies to ALL callers)
Wrap the `load_ledger()` (L480 baseline) → `save_ledger()` (L505) critical section of `sync_ledger` in a
cross-process advisory lock (`fcntl.flock(LOCK_EX)` on `data/state/.ledger.lock`). Kernel auto-releases on
process death (no stale-lock deadlock). Acquire with a bounded wait (LOCK_NB retry loop, ~5s cap); on
timeout → proceed WITHOUT the lock + WARNING (fail-open: save_ledger is atomic, so worst case = today's
no-lock behavior). ALWAYS release (try/finally). Both the RTH cron and the in-process caller go through
`sync_ledger`, so one lock here serializes both. This is the only edit that runs live today (every cron
pass) → verify it never hangs the cron.

### C-2 — post-buy orchestration in `forever_hold_manager` (inert until an F6 buy happens)
After `execute_starter` returns `placed` (non-empty), run the verify loop (bounded, ~3 tries, backoff ~2/4/8s):
1. call `run_ledger_sync.sync_once()`.
2. For EACH placed symbol assert BOTH: `tier_qty(ledger,sym,"forever6") >= bought_qty` **AND**
   `abs(drift) <= _QTY_EPS` (feed-skew guard — Reliability #2 / Exec-risk §2). Break when ALL pass.
3. ALSO assert no NEWLY-nonzero drift appeared on a previously-clean PROTECTED symbol; if it did →
   CRITICAL + Slack ("post-seed sync planted drift on <sym> — verify before enabling exits").
4. Before the loop, poll order status: a TERMINAL-REJECTED/canceled order short-circuits (do NOT spin the
   full retry budget, do NOT alert it as an unprotected anchor; note the monthly cap was consumed).
5. On retry-budget exhaustion with a genuinely-pending fill → CRITICAL + Slack ("F6 anchor not yet
   ledger-reflected — guard dormant anyway; verify before next restart") AND set the persisted degraded flag.

### C-3 — persisted block-further-seeding flag (C2 scoped to SEEDING only; fail-closed on acquisition)
- A durable flag (e.g. `data/state/forever6_sync_degraded.json`, RC-5 atomic). `maybe_start_accumulation`
  (or `execute_starter` entry) REFUSES to place new F6 buys while set. NEVER touches any sell/exit/GTC path.
- AUTO-CLEAR when a later `sync_once` confirms ALL expected forever6 lots present AND drift≈0 (Reliability
  caveat — else it over-blocks permanently). Human-clearable + loud.

### C-4 — ordering (locked): #1 → #3 (GTC floor check) → **#2 arms LAST**. Keep the operational
"NO SEED until all 3 land" gate as the primary safety control (prereq #1's own protection is real but
partial — orphan-exclusion only; raw-close/GTC paths stay open until #2/#3).

## FAIL-BEHAVIOR SUMMARY
- B1 mandatory; B2 (blind sleep) FORBIDDEN (never-mask).
- Never sell to "clean up" a failed sync (never-sell breach).
- A spurious `healed:False` cron alert around a seed is EXPECTED (never-shrink protecting the fresh write
  from a stale concurrent clobber) — a false-alarm cost, not a safety failure. Document it.

## GATE FOR THE DIFF (before ship)
Full read of the exact edit regions → statics (py_compile/mypy/ruff) → cold-2nd → board-on-diff (2 cold
seats: Reliability + Execution-risk, since sync_ledger is a hotspot) → Gro+GAI preship on the exact staged
bytes → commit + push (git-only; OCI restart deferred to non-RTH). Ships DARK/inert except the C-1 lock,
which runs live on the cron path → RTH-safe verification that it never hangs.
