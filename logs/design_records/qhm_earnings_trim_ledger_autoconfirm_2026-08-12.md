# Design record — QHM earnings-trim → ownership-ledger auto-confirm (root fix)

**Status:** SCOPED + DESIGNED (no code). Ready to implement + gate + ship.
**Authored:** 2026-08-12 (interactive, Rafael present). Rafael chose scope = **earnings-trim (T1+T2) only**.
**Trigger:** the NVDA/qhm `sync_ledger REFUSED` / frozen-sells incident (2026-08-12). Acute instance
RESOLVED same day via `confirm_ledger_heal.py NVDA qhm 1` + `run_ledger_sync.py` (floor healed 2→1,
drift=0, confirm consumed). This record fixes the ROOT so it never recurs.

## Problem
The QHM earnings profit-take (`_maybe_earnings_trim`, #107/#108) executes a LEGITIMATE, system-initiated
reduction of a quarterly hold (T1 trims 50%, T2 exits the remainder) but does NOT tell the ownership
ledger. The standalone `run_ledger_sync.py` (RTH cron) then rebuilds the ledger, sees the protected qhm
floor (e.g. 2) > live net (1), and — correctly, by design — REFUSES to shrink a protected floor without
operator confirmation (a real trim and a breach are automation-indistinguishable). Result: ledger stuck,
CRITICAL every ~20 min during RTH, and (once the guard is wired to gate sells) NVDA sells would freeze —
until an operator runs `confirm_ledger_heal.py` by hand. That manual step is required after EVERY QHM
earnings-trim. Classic ANTI-SILO gap: a newer feature not wired into a downstream safety control.

## Mechanism to mirror (verified at source)
- `confirm_ledger_heal.py` writes `data/state/ledger_heal_confirmations.json`:
  `{"SYMBOL/tier": {"target_qty": T, "net_at_confirm": <live broker net>, "confirmed_ts": <epoch>,
  "confirmed_by": <who>}}`. It verifies `live_net >= target - 1e-6` before writing.
- `ownership_guard.sync_ledger(..., positions_settled=True)` consumes it: on a protected-tier shrink vs
  the persisted baseline, it REFUSES UNLESS a valid confirmation matches (exact target, snapshot/
  net_at_confirm match, within 2h, one-shot), then OVERRIDES the protected tier to the confirmed target
  (clamped to net) and consumes the confirmation. An UNCONFIRMED shrink still REFUSES + records a
  pending heal. (ownership_guard.py L753-775; confirmation load at L770.)

## The fix (FLAG the trim as system-confirmed — never bypass the guard)
1. **Shared helper** in `execution/ownership_guard.py` (single source of the confirmation format; DRY):
   `record_system_heal_confirmation(sym: str, tier: str, target_qty: float, source: str) -> bool`
   — reads live broker net (reporting.pnl_ledger.fetch_positions), verifies `net >= target_qty - eps`
   (the reduction genuinely happened), writes the SAME confirmation dict with `confirmed_by=source`
   (e.g. `"earnings_trim_auto"`). Atomic tmp→replace, like confirm_ledger_heal. Returns False (no write)
   if net < target (fill not reflected yet) — fail-safe: no confirm ⇒ guard still REFUSES ⇒ manual path.
   **Refactor `confirm_ledger_heal.py` to call this helper** (DRY; operator path becomes
   `record_system_heal_confirmation(sym, tier, target, os.environ.get("USER","operator"))`).
2. **Hook in `quarterly_hold_manager._maybe_earnings_trim` (L1983):** AFTER a trim's broker sell is
   CONFIRMED FILLED (T1 partial_close success; T2 close success) and the post-trim qty is known, call
   `record_system_heal_confirmation(pos.symbol, "qhm", <actual_post_trim_broker_qty>, "earnings_trim_auto")`.
   Then the next `run_ledger_sync` (RTH cron) applies the floor shrink automatically. No operator step.

## Safety constraints (MUST hold — these are the board's likely focus)
- **Fill-confirmed only.** Write the confirm ONLY after the sell actually FILLED (never on submission /
  on a reject / on a 0-share partial). A trim that didn't execute must NOT auto-confirm a reduction that
  didn't happen (never-launder-a-breach).
- **Actual, not intended, qty.** `target_qty` = the real post-trim broker net (re-read), not the planned
  qty — so a partial fill still matches reality.
- **Guard intact.** This does NOT bypass the never-shrink guard; it supplies the same confirmation an
  operator would. An unconfirmed shrink (a real breach, no matching system-trim) STILL refuses + pages.
- **Audit tag.** `confirmed_by="earnings_trim_auto"` distinguishes auto-confirms from operator confirms
  in `ledger_heal_confirmations.json` and any downstream audit.
- **Scope = earnings-trim only** (Rafael). Other system-initiated QHM reductions (full QHM exits, stop
  hits, safe_close_all-on-QHM) are OUT — add later if they show the same REFUSE.

## Files
- `execution/ownership_guard.py` — add `record_system_heal_confirmation` (+ refactor is optional).
- `confirm_ledger_heal.py` — refactor to call the helper (DRY; optional but recommended).
- `execution/quarterly_hold_manager.py` — hook call in `_maybe_earnings_trim` after T1 + T2 fills.

## Gate (both bot-code files are RTH-impacting; ownership_guard is a SAFETY control)
Full read of quarterly_hold_manager.py (Explore subagent, >1000 lines) + ownership_guard.py; 10-pt
audit + RC scan; board = **masked-loss/Taleb (never-launder-a-breach) + Reliability + Data-integrity**;
Gro + GAI; cold-2nd; statics. Markers: preship_audit + cold2 + adversarial + log-evidence
(cite the NVDA `sync_ledger REFUSED` lines, verified 78× today).

## Test plan (Rule-C front-loaded sim)
1. Simulate a T1 trim (2→1) with a confirmed fill → helper writes `{NVDA/qhm: target 1, net 1,
   confirmed_by earnings_trim_auto}` → next `sync_ledger(positions_settled=True)` heals floor 2→1,
   consumes the confirm, drift=0. (This is exactly what the manual path just did for NVDA.)
2. Simulate a trim SUBMITTED but rejected/0-fill → helper NOT called (or net<target → no write) →
   guard still REFUSES the (nonexistent) shrink → correct.
3. Simulate a genuine breach (a qhm share sold outside the trim, no auto-confirm) → guard still
   REFUSES + pages → the guard is not weakened.
