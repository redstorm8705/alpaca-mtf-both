# F6 Prereq-2 arming condition: D-obs + OBS-A — DESIGN (2026-07-18 interactive)

**Status:** DESIGN for board + Gro + GAI (this session — prior-session "aligned" claim does NOT
carry across the session boundary per CLAUDE.md RULE C-2). DARK/inert pre-positioning: everything
here is dormant behind `config.OWNERSHIP_GUARD_ENFORCE=False` OR is pure observability. It is a
**prereq-#2 arming condition (b)** per `logs/f6_activation_BLOCKED_2026-07-17.md` — it does NOT
arm anything.

Full-read gate satisfied this session: `ownership_guard.py` 660L, `broker.py` 1041L, `alerts.py`
427L (all read verbatim in ≤300L chunks, declared).

---

## Problem (plain English + why it matters before arming F6)

When `OWNERSHIP_GUARD_ENFORCE` is finally flipped True (prereq #2), the never-sell floor starts
BLOCKING sell orders on protected (forever6/qhm) symbols. Two kinds of block happen:

1. **DETERMINISTIC** — the guard doing its designed job: the sell would breach the floor, so it's
   bounded or rejected. This is normal and expected; it should be **log-only** (paging on it would
   be exactly the alert-fatigue Rafael is fighting right now).
2. **AMBIGUITY (fail-closed)** — the guard *cannot determine* the true state (ledger unreadable,
   live-Alpaca net unreadable, or ledger↔Alpaca drift) and so it refuses the sell to be safe. This
   is an **operator-actionable fault** — the never-sell book is running blind and a real exit may be
   getting refused. Today these fire only `logger.critical` on OCI; **no operator PAGE.** An operator
   would never know the floor had gone blind until a nightly audit.

**OBS-A** is a correctness hole the cold-2nd found during prereq #3: `protected_floor()` does
`float(qty)` on ledger values. A schema-valid but **type-corrupt** qty (e.g. `"abc"`, a dict) passes
`load_ledger`'s schema check and makes `protected_floor` raise `ValueError`/`AttributeError`. At the
direct call sites (`close_position:887`, `partial_close_position:701`, `check_never_sell_floor:308`,
`:298 .bak`) that raise **propagates out and crashes the exit path** instead of failing closed —
the opposite of the guard's whole contract.

---

## Scope (files/functions — verified against source)

### D-obs — unified operator PAGE on guard fail-closed AMBIGUITY
Add ONE throttled, never-raises pager and call it at each ambiguity site. Deterministic rejects stay
log-only (untouched).

- **New helper** — `execution/ownership_guard.py`: `page_floor_blind(symbol, tier, kind, detail)`.
  - Routes via `alerts.send_slack` (the authorized webhook — NEVER the finance:slack MCP).
  - **Per-category throttle** (default 30 min, keyed by `kind` e.g. `ledger_unreadable`/
    `alpaca_unreadable`/`drift_freeze`) using an RC-5 atomic stamp under `data/state/`, mirroring
    `alerts.alert_crash`'s dedup — because alert fatigue is the exact failure mode we're avoiding.
  - **Never raises** (best-effort import + send inside try/except): a paging failure must never break
    the guard/order path (GAI de-risk mandate for the RTH path).
- **Call sites (ambiguity ONLY):**
  - `check_never_sell_floor`: ledger-unreadable-for-protected (L302), Alpaca-net-unavailable (L316),
    drift-field freeze (L322), reconciliation drift-freeze (L327).
  - `broker._floor_bound_stop_qty_impl`: ledger-unreadable (L391), Alpaca-net-unreadable (L406).
  - `broker.partial_close_position`: ledger-unreadable (L694), Alpaca-net-unreadable (L705).
  - `broker.close_position`: ledger-unreadable (L880), Alpaca-net-unreadable (L894).
- **NOT paged (stay log-only):** floor-binding "0 sellable"/bounded (L359/L364), short-on-ringfenced
  (L341), f6-trim rejects (L346/L350), non-positive qty / unknown tier.

### OBS-A — type-corrupt ledger fails CLOSED, never raises
**FORK for the board (Open Question Protocol):**
- **Option A (handoff scope, narrow):** wrap only the two `protected_floor` calls inside
  `check_never_sell_floor` (L298 `.bak`, L308 main).
- **Option B (complete, my full-read finding):** ALSO wrap the direct calls in `close_position:887`
  and `partial_close_position:701` (and `_floor_bound_stop_qty_impl:401`, though its wrapper already
  catches). These crash **before** `check_never_sell_floor` is reached, so Option A alone leaves the
  crash live on the two most-used close paths.
- **Fail-closed semantic:** on any exception computing `protected_floor` for a symbol, treat it as
  **PROTECTED** → REJECT/refuse the reducing order (block the sell) — never approve, never raise.
- **Recommended:** Option B (complete). Narrow is a half-fix that leaves the documented crash on the
  live close paths.

---

## Invariants preserved
- No behavior change while `OWNERSHIP_GUARD_ENFORCE=False`: D-obs sites are only reached inside the
  enforce-gated branches (or the self-gated `_floor_bound_stop_qty`); OBS-A only changes an
  otherwise-crashing path. Dormant today.
- Data tier unchanged (guard reads Alpaca net via `execution.broker`, T1). No new data source.
- Paging uses the existing authorized `alerts.send_slack` webhook only. RC-5 atomic stamp for throttle.
- Never-mask-a-loss: D-obs is observability only; OBS-A fails CLOSED (blocks a sell) — it can never
  mask a loss by approving a bad reduction.

## Failure modes
- alerts import/send fails → helper swallows, logs, returns (order path unaffected).
- throttle stamp unreadable/corrupt → treat as "send" (fail toward alerting), never raise.
- OBS-A on a NON-protected symbol with corrupt data: still fails closed here (we can't compute the
  floor, so we can't prove it's unprotected) — board to confirm this is acceptable vs the keystone
  "fail-open for non-protected" rule (tension point for the board).

## Board vote required? YES — risk-path guard, RTH-impacting when armed. Gro + GAI required.

---

## REVIEW RESULTS (2026-07-18)

### Gro (Groq llama-3.3-70b): APPROVE-WITH-CHANGES
- OBS-A: **Option B** (complete) — must wrap the direct `protected_floor` calls in
  `close_position`/`partial_close_position` too, not just inside `check_never_sell_floor`.
- Keystone tension: blanket fail-closed is NOT correct → `_cached_protected_symbols()` resolution
  needed so a non-protected symbol still fails open.
- Dedup key: prefer `(kind, symbol)` — kind-only 30-min is too coarse, risks suppressing a new fault.
- Ensure `page_floor_blind` / OBS-A wrappers are exception-free (internal try/except).

### GAI (Gemini 2.5-flash): APPROVE-WITH-CHANGES
- OBS-A: **Option B mandatory AND insufficient without** the `_cached_protected_symbols()` resolution.
  Exact logic: on a `protected_floor` raise → if symbol ∈ cached-protected → log CRITICAL + page
  (`kind='ledger_qty_corrupt'`) + REJECT (fail closed); else → fail OPEN (keystone).
- **New site (Rec 1.1):** the type-corrupt `tier_qty`/`protected_floor` raise is ITSELF an ambiguity
  → page it (when protected), not just silently fail closed. Folds D-obs into the OBS-A corrupt path.
- Dedup key: `(kind, symbol)` (explicit worked example: MSFT-blind then GOOG-blind must BOTH page).
- Rec 3.2 (v2, logged not built): a persistent fault should re-page after the window (still-present
  vs fire-and-forget). `(kind, symbol)` + 30-min sufficient for v1.
- Confirmed-send discipline: stamp only after a confirmed send (alert_crash pattern).
- Notes: .bak staleness dependency; comprehensive unit tests for all ambiguity + corrupt paths.

### Board seat verdicts (all 3 cold seats — APPROVE-WITH-CHANGES)
- **Execution-risk/ruin:** REJECTS blanket fail-closed (PLTR 800sh scenario: corrupt qhm field →
  blocked stop → −$16k vs −$4.8k, unbounded, on an intraday-only name = the keystone catastrophe
  re-created by the fix). `.bak` resolution MANDATORY; **`check_never_sell_floor:298` is the ONE
  site where fail-CLOSED is correct** (current ledger already unreadable AND .bak field corrupt =
  both records compromised, and only a protected symbol carries a protected-tier value there).
- **Reliability/data-integrity:** Option B **still too narrow** — wrapping only the `protected_floor`
  calls leaves other unguarded `float()` coercions live in `check_never_sell_floor`: `float(drift)`
  L320, `get_combined_symbol_exposure` L325 (sums ALL tiers → a corrupt *intraday* qty crashes here
  even when protected_floor succeeded), `tier_qty` L331. Correct fix = a **function-boundary
  never-raises wrapper**, copying the existing `_floor_bound_stop_qty`/`_impl` pattern
  (broker.py:337-379) onto `close_position` + `partial_close_position`, plus a top-level
  `try/except → REJECT` on `check_never_sell_floor`'s post-protection body. Confirmed the
  `_floor_bound_stop_qty_impl:401` call needs NO new wrap (its wrapper already covers it).
- **Observability (Majors):** `send_slack` returns `None` → cannot support stamp-after-confirmed-send
  → must call `_slack`/`_ntfy` directly like `alert_crash` (L380-385), **fire ntfy (phone) too**
  (refused protected exit = capital-risk), **per-kind severity** (alpaca_unreadable → WARNING
  self-healing; drift/ledger/corrupt → CRITICAL), and **cycle-level rollup + recovery/all-clear**
  (a whole-cycle blind-out = 1 page, not 6; silence must provably mean healthy).

---

## FINAL CONVERGED SPEC (5/5 voices) — split into v1 (ship) + v2 (before arming)

### v1 — CORRECTNESS-CRITICAL (ship this session; all 5 voices require these)
**OBS-A (function-boundary never-raises, NOT surgical protected_floor wraps):**
1. Refactor `close_position` → `close_position` (thin never-raises wrapper) + `_close_position_impl`
   (body), mirroring `_floor_bound_stop_qty`/`_impl` (broker.py:337-379). Same for
   `partial_close_position`. On ANY exception in the impl: consult `_cached_protected_symbols()` —
   symbol ∈ set → page(`ledger_type_corrupt`)+`logger.critical`+**fail CLOSED** (return False);
   else → **fail OPEN** (`_raw_close_position` / requested qty) per keystone.
2. `check_never_sell_floor`: top-level `try/except Exception → REJECT` around the post-protection
   body (covers L320/L325/L331 coercions). At the `.bak`-fallback branch L298 specifically: on a
   `protected_floor(_bak,…)` raise → **fail CLOSED** (both records compromised — Exec-risk ruling).
   At the main protection-determination raise (L308) and the broker impls → `.bak` resolution
   (REJECT only if cached-protected, else fail open).
**D-obs core:**
3. `page_floor_blind(symbol, tier, kind, detail)` in ownership_guard.py — import + throttle-stamp
   read/parse + send ALL inside ONE `try/except Exception → None` (never-raises; the single
   highest-risk line — Reliability Trap 1/2). Calls `alerts._slack` + `alerts._ntfy` directly (NOT
   send_slack). Stamp written ONLY on a confirmed send (`_slack`/`_ntfy` return bool). Dedup key =
   `(kind, symbol)`, 30-min window, RC-5 atomic stamp with a **unique `.{pid}.tmp`** suffix, UTC math.
   Per-kind severity: `alpaca_unreadable`→WARNING; `ledger_unreadable`/`drift_freeze`/
   `ledger_type_corrupt`→CRITICAL.
4. Wire `page_floor_blind` at every ambiguity site (check_never_sell_floor 302/316/322/327; broker
   impls' ledger-unreadable + alpaca-unreadable + the new corrupt-ledger catch). Deterministic
   rejects stay log-only.

### v2 — ALERT-QUALITY (logged; build BEFORE the OWNERSHIP_GUARD_ENFORCE flag flips, not before dark code lands)
- Cycle-level rollup (whole-cycle blind-out = 1 page listing all affected symbols).
- Recovery/all-clear notice + clear the throttle stamp on a clean cycle (silence provably = healthy;
  a recurrence after recovery re-pages immediately instead of being swallowed by a stale 30-min window).
- Guard heartbeat once armed ("floor guard healthy, N protected symbols").
- **Follow-up ticket (Reliability):** add qty-type validation to `load_ledger` (L119) to kill the
  type-corrupt class at SOURCE for ALL callers (launch_init/reconcile_drift/sync_ledger share it).

### Rationale for the v1/v2 split
The whole subsystem is DORMANT (fires only once `OWNERSHIP_GUARD_ENFORCE=True`, still gated behind
prereq #2 + a live-verify). v1 makes the guard **correct and never-crashing** — that must land before
arming. v2 is alert *polish*; `(kind,symbol)`+30-min already prevents the every-6-min storm (worst
case: 6 pages on a full API outage, then throttled). v2 must be built before the flag flips, but it
does not block the dark v1 safety code from landing now.
