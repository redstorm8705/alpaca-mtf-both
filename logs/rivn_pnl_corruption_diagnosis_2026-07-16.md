# RIVN P&L Corruption — Phase-1 Diagnosis (2026-07-16, autonomous-chain resume)

**Status:** Phase 1 (root cause) IN PROGRESS. No patch. Top unaddressed REAL item per BGG audit
(flagged 4 nightly audits: 7/7, 7/8, 7/9, 7/13). Caused the −73.86% FALSE kill-switch on 7/7.

## GROUND TRUTH (authoritative Alpaca fills API)
- **Entry:** RIVN BUY 17 @ $19.73 — 2026-07-06 ~17:35 UTC (order `110251a7`). LONG.
- **Close:** RIVN SELL 17 @ ~$17.32 — 2026-07-07 13:38:22-24 UTC (order `74f96fcb`). Closes the long → FLAT.
- **TRUE P&L:** (17.32 − 19.73) × 17 = **−$40.99** (a normal losing long round-trip). The Alpaca record is CLEAN.

## WHAT THE BOT DID WRONG (3 corruptions off one clean trade)
1. **pnl=0.0 (fill-recovery failure):** `fill_helpers` logged "FILL UNVERIFIED: could not recover a verified
   close fill. Using entry_price $19.73 as fallback" → recorded exit_price=entry_price → pnl=(19.73−19.73)×17=0.
   The close fills DEMONSTRABLY EXIST in Alpaca (order 74f96fcb). **KEY CLUE:** the bot's EXIT RECORD is
   timestamped 2026-07-07T23:00:40 PT (~2026-07-08 06:00 UTC) — ~16h AFTER the real 13:38 UTC fill. The
   recovery window/`submitted_after` bound almost certainly excluded the real fill time. Then
   `fill_reconciler` RC-4 EXPIRED after 5 min (never retried against full history) → pnl stuck unreliable.
2. **Direction flip (long→short):** `orphan_manager` adopted "-17 shares @ $17.32 (short), Stop=$18.81
   target=$14.23" (short-style levels) at 14:14:06 — for a position that was originally LONG 17 @ $19.73 and
   had just been CLOSED. The bot lost/dropped the long from trade_log, then re-adopted from Alpaca as SHORT.
3. **−73.86% corrupted loss → FALSE kill-switch (7/7):** the kill switch fired on the corrupted P&L.
   (Kill switch itself may be fine; its INPUT was corrupt — see feedback_safety_control_never_mask_loss.)

## ROOT CAUSES (cold diagnostic agents, full reads — CONFIRMED)
- **Bug A — fill-recovery forces the wrong query path (fill_reconciler.py:90 + fill_helpers.py:280-304):**
  `fill_reconciler.run_fill_reconciliation()` computes `submitted_after` from `entry_time` (L90) and passes it
  to `fetch_actual_fill_price()`, which activates the LEGACY P5-H2 path — `GetOrdersRequest(status=CLOSED,
  direction=Sort.ASC, limit=5, after=entry_time)`. ASC oldest-first + limit=5 returns the 5 OLDEST closed
  orders after entry, so a close fill ~16h later is never matched (or truncated). The CORRECT external-close
  path (fill_helpers.py:240-279, `submitted_after=None`) uses entry-derived lower bound + `filled_at DESC` and
  WOULD find it — but the reconciler never calls it that way. → no match → `_fill_unverified_fallback()`
  returns entry_price → pnl=0.0; RC-4 expires after 5 min, never retrying vs full history.
  **FIX (isolated, low-risk):** fill_reconciler calls `fetch_actual_fill_price(..., submitted_after=None,
  no_retry=True)` (external-close path) instead of deriving submitted_after from entry_time; OR add an explicit
  query-mode flag distinguishing same-session (P5-H2) vs external reconciliation. Highest-value single fix —
  it is what produces the pnl=0.0 that feeds the false kill-switch.
- **Bug B — orphan adoption infers direction from a bare qty sign (orphan_manager.py:928):**
  `reconcile_positions()` sets `_direction = "long" if _raw_qty > 0 else "short"` from Alpaca `pos.qty`, with
  NO guard against adopting a JUST-CLOSED symbol and no fill-side validation. Orphans = alpaca_symbols −
  tracker_symbols − qhm (L906-922); the direction-mismatch CORRECTION handler (L1335-1525) only runs for
  symbols ALREADY in tracker, so an orphan-adopted position bypasses it. Ground truth: only ONE sell-17 on
  7/7 (fills flat) → the −17 short read was a stale/settling read of a just-closed lot. **FIX:** guard against
  adopting net-flat/just-closed symbols (skip if recently closed / entry_time stale-but-newly-appearing);
  validate direction from the most-recent fill's SIDE, not a bare residual qty sign.
- **Upstream trigger (roadmap MOVERS-RETIRED):** the main-bot FALSE-DROP of a still-live position from
  trade_log `open` via a false `record_exit` is what makes the closed lot look like an untracked orphan →
  Bug B re-adoption. Fixing the false-drop root removes Bug B's trigger. Files: portfolio_tracker.py record_exit
  path + orphan_manager reconcile.

## PHASE-2 SHIP STATUS
- **✅ Bug A SHIPPED + LIVE (`5fb5c4e`, 2026-07-16):** fill_reconciler.py now calls
  fetch_actual_fill_price(submitted_after=None) → external-close path (entry-bounded, filled_at DESC,
  side filter, ±50% band) + direction/entry_time guards. Gate: full reads (fill_reconciler 133 +
  fill_helpers 370); statics; cold-2nd PASS; board Harris APPROVE (masked-loss check: kill switch uses
  Alpaca EQUITY not daily_pnl → phantom-proof, real gap loss captured regardless); Gro+GAI design + FINAL
  preship APPROVE (a7223e38a434). Runtime-verified on OCI.
- **⏳ Bug B NEXT:** orphan_manager.py:928 direction inference (full read + gate).
- **⏳ Bug C NEXT:** portfolio_tracker false-drop root (full read + gate).

## FOLLOW-UP (Harris board seat, out of scope for Bug A — logged not shipped)
`fill_helpers.py:_sanity_ok` (±50% band): a >50% gap fill is rejected → recorded as breakeven (masked loss),
BUT fires CRITICAL + marks _fill_unverified (not silent) AND the equity-based kill switch catches the real
loss regardless. Harris asked for (a) a doc comment flagging this + equity-backstop, and possibly (b) consider
whether a >50% gap LOSS should be handled differently than a >50% phantom (asymmetric). Non-urgent (equity kill
is the backstop); own increment (fill_helpers.py). Not a defect in Bug A's diff.

## NEXT (Phase 1 completion → Phase 2)
1. Cold agents return exact root-cause lines for Bug A + Bug B.
2. BGG (board + Gro + GAI) diagnostic on the aligned root cause.
3. Then Phase 2: draft fix(es) from the alignment → full patch sequence per file (each hotspot file its own
   full read + gate) → preship → ship. Likely multi-file (fill_helpers/fill_reconciler + orphan_manager +
   the portfolio_tracker false-drop root) — sequence each independently (RULE C-6).

**This is a genuine multi-file P0 in the hotspot execution path — the highest-value real bug open.**
