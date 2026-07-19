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
