# RC-4 datetime-parse P&L-corruption fix — DESIGN (2026-07-19 autonomous)

**Status:** DESIGN for board + Gro + GAI. RTH P&L path → full gate. Rafael asleep, authorized
autonomous BGG-gated shipping of aligned items. Fixes the #1 open RTH bug (CATASTROPHIC, 7/17 nightly).

Full-read gate satisfied 2026-07-19: `portfolio_tracker.py` 1896L (Explore verbatim — target functions
+ full timestamp inventory), `fill_reconciler.py` 206L (Read), `state_io.py` 111L (Read),
`reporting/pnl_ledger.py:_iso_to_dt` (reference).

## Problem (verified against source)
Four sites parse a stored timestamp with RAW `datetime.fromisoformat()`, which py3.10 rejects for a
`Z` suffix and for non-3/6-digit fractional seconds. `exit_time` can be an Alpaca `filled_at` value
(portfolio_tracker.py:1093 `_ct["exit_time"] = _actual_exit_ts`), which carries exactly those. When it
fails:
- **`portfolio_tracker.py:166` `get_unverified_exits`** → WARNING + `continue` → the trade is dropped
  from the reconciliation pending list → **never patched → permanent P&L corruption** (the SMCI
  CATASTROPHIC).
- **`portfolio_tracker.py:404` `mark_fill_expired`** → WARNING + `continue` → the expired trade is
  never marked `_patch_applied_ts` → **re-queued by `_load_log()` on every restart → infinite RC-4
  CRITICAL loop** (the exact loop the T1 fix exists to prevent).
- **`fill_reconciler.py:156` `run_fill_reconciliation`** → WARNING + `continue` → the trade is skipped
  every cycle → rides to a **false RC-4 EXPIRED CRITICAL**.
- **`portfolio_tracker.py:290` `patch_exit_pnl`** → WARNING only, patch PROCEEDS (`_delay_secs` stays
  0.0 — a logging metric). Low-impact, but should still normalize.

Blast radius: `get_unverified_exits`/`mark_fill_expired` are called ONLY from `fill_reconciler.py`
(:85, :109). No other callers. P&L blast = Kelly / win-rate / EOD (NOT the kill switch — Alpaca-sourced).

## Fix
Add a tolerant `_iso_to_dt(ts)` to **`execution/state_io.py`** (leaf module, stdlib-only, already
imported by portfolio_tracker; `pnl_ledger._iso_to_dt` can NOT be reused — pnl_ledger imports
execution.quarterly_hold_manager, so execution→reporting would be circular). Mirror the proven
`pnl_ledger._iso_to_dt`: strip `Z`→`+00:00`, pad/truncate the fraction to 6 digits, `fromisoformat`;
returns `None` on genuine failure, NEVER raises. Requires `import re` in state_io.

Route the 4 raw sites through it:
- `pt:166` → `_exit_dt = _iso_to_dt(_exit_str)`; the helper returns an AWARE datetime (UTC), so the
  existing `if _exit_dt.tzinfo is None: replace(_PT)` becomes moot but is harmless; compare aware-vs-aware
  against the `_PT` cutoff (Python allows cross-tz aware comparison).
- `pt:404` → `_exit_dt = _iso_to_dt(_exit_str)`.
- `pt:290` → `_exit_dt = _iso_to_dt(...)`; on None leave `_delay_secs=0.0` (unchanged behavior, metric only).
- `fill_reconciler:156` → `if _iso_to_dt(_et_str) is None: <skip>` (replaces the raw parse+discard).

## FORK for the board — fail-mode when `_iso_to_dt` returns None (genuinely garbage timestamp)
The tolerant parser resolves the observed SMCI case (Z/fraction). But a TRULY corrupt timestamp still
returns None. Today that path silently skips → permanent-stuck (pt:166) / re-queue-loop (pt:404).
- **MINIMAL:** keep the current skip on None (now rare — only genuine garbage). SMCI resolved; the
  permanent-stuck/loop residual remains for true corruption.
- **HARDENED (recommended for pt:166 + pt:404):** on None, treat the trade as EXPIRED — mark
  `_patch_applied_ts` + surface ONE RC-4 CRITICAL ("unparseable exit_time — manual verification") so it
  is never silently stuck and never loops. For `fill_reconciler:156` keep MINIMAL skip (it just retries;
  RC-4 expiry surfaces it). Rationale: a genuinely-unparseable exit_time is a data-integrity fault that
  must SURFACE, not vanish. Risk: HARDENED changes get_unverified_exits/mark_fill_expired contract
  slightly (a None-timestamp trade now gets marked expired instead of ignored).

## Invariants / safety
- Never raises (helper returns None). Never masks a loss (this makes MORE trades reconcile → more
  accurate P&L; it can only make a suppressed/stuck P&L become correctly recorded). Kill switch
  unaffected (Alpaca-equity sourced).
- No data-source change. No new cross-module dependency beyond `fill_reconciler → state_io._iso_to_dt`.
- Follow-up (NOT this patch): de-dup `pnl_ledger._iso_to_dt` to import from state_io (leave pnl_ledger as-is now).

## Board vote required? YES — RTH P&L reconciliation path, hotspot file. Gro + GAI required.

---

## BGG VOTES (2026-07-19)
### Gro — APPROVE-WITH-CHANGES; recommends **HARDENED**
- Mirror is correct/complete; state_io right home; no circular import. TZ: OLD pt:166 DID mis-interpret
  naive-UTC as PT (~7h age-gate error) — fix corrects it; aware-UTC vs aware-PT compare is correct.
- HARDENED: never masks a real P&L (only marks genuinely-corrupt as expired); marked once via
  `_patch_applied_ts` → CRITICAL fires once. Add: log/audit the expired-on-unparseable trades.

### GAI — APPROVE-WITH-CHANGES; recommends **HARDENED**
- Confirms the TZ mis-interpretation is a "critical finding" the fix corrects (Alpaca filled_at is UTC;
  old code assigned _PT to naive → ~7h error). aware-UTC vs aware-PT compare correct.
- HARDENED for pt:166 + pt:404; MINIMAL skip for fill_reconciler:156. Risk-of-masking acceptable (a
  genuinely-unparseable exit_time can't be attributed anyway; surface + manual-verify beats silent-stuck).
- Idempotency confirmed (given _patch_applied_ts is checked at the entry of get_unverified_exits +
  mark_fill_expired — it is, ~L150). Never-raise confirmed.
- **Required additions:** (a) include the offending `exit_time` value in the CRITICAL/WARNING for
  investigation (pt:166, pt:404, and enhance the pt:290 WARNING); (b) unit-test `_iso_to_dt` across
  Z/1-9-digit-fraction/malformed/None/empty + the HARDENED idempotency.

### Board seats (both APPROVE-WITH-CHANGES, HARDENED)
- **Data-integrity:** `_iso_to_dt` correct vs every real Alpaca form; state_io right home (leaf, no cycle).
  CORRECTIONS: (a) `_iso_to_dt` returns aware ONLY for offset/`Z` input, else NAIVE → keep the tzinfo guard;
  (b) the "~7h naive-as-PT" error is NOT active today (a `Z` string throws before reaching that branch →
  it's a total parse-fail→skip); (c) fill_reconciler:156 parses **entry_time** (bot PT, no `Z`) — already
  parseable, routing through the helper is harmless defense-in-depth, not a live fix. Follow-up ticket:
  pt:448 `.startswith(today)` UTC-vs-PT overnight-fill mis-bucketing (needs `_pt_date`-style conversion).
- **Reliability/P&L:** FACT — recovery is bounded by `entry_time`, never `exit_time`; `exit_time` only
  CLASSIFIES (age/expiry) + a log metric; P&L is from exit_price+entry_price → parser never feeds P&L
  (never-mask confirmed). MINIMAL at pt:166 is WORSE than stated: skipped before the expiry check →
  never reaches `expired` → **NO CRITICAL fires** → SILENT permanent-stuck. HARDENED required.

## FINAL SPEC (4/4 aligned — SHIP THIS)
1. **`execution/state_io.py`:** add `import re` + `_iso_to_dt(ts)` (verbatim mirror of pnl_ledger's:
   strip `Z`→`+00:00`, pad/truncate fraction to 6, `fromisoformat`; None on failure; never raises).
2. **pt:166 get_unverified_exits:** `_exit_dt = _iso_to_dt(_exit_str)`; **(A)** `if _exit_dt is None:` →
   route to `expired` (+`continue`, mirroring the pt:177-179 append+continue so it isn't also left in
   pending) — log the offending `_exit_str`. **(D)** KEEP `if _exit_dt.tzinfo is None: replace(_PT)`.
3. **pt:404 mark_fill_expired:** `_exit_dt = _iso_to_dt(_exit_str)`; **(A)** `if _exit_dt is None:` → set
   `_patch_applied_ts` (mark expired) + `_fill_reconcile_expired`, `found=True`, log the offending value —
   write `_patch_applied_ts` BEFORE anything else. **(D)** KEEP the tzinfo naive/aware coercion.
4. **pt:290 patch_exit_pnl:** `_exit_dt = _iso_to_dt(...)`; **(B)** keep the try/except; add `if _exit_dt is
   None:` → leave `_delay_secs=0.0`; enhance the WARNING to include the offending value.
5. **fill_reconciler:156:** `if _iso_to_dt(_et_str) is None:` → skip (MINIMAL, unchanged behavior).
6. **Unit tests (GAI):** `_iso_to_dt` across Z / 1-9-digit fraction / +00:00 / naive / empty / None /
   non-str / garbage; + HARDENED idempotency (marked once, excluded thereafter, one CRITICAL).

## LOGGED FOLLOW-UPS (NOT this ship)
- (C) route a None-`exit_time` trade through ONE `entry_time`-bounded fetch attempt (+ entry_time age
  fallback) BEFORE marking expired — the only path that recovers a corrupt-exit/valid-entry fill.
  Non-blocking (MINIMAL doesn't recover it either; HARDENED surfaces loudly, doesn't mask).
- pt:448 UTC-vs-PT `.startswith(today)` overnight-fill mis-bucketing (`_pt_date`-style fix).
