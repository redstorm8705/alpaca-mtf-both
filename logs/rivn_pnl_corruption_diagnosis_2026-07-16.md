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

## ✅ BUG E RESOLVED (2026-07-16) — NO double-sell; it was a PHANTOM, and the bot traded against it
**Complete paginated fill history 7/6-7/8 = 7 fills: bought 17, sold 17, NET 0 (FLAT). Only ONE sell
order (74f96fcb). There was NO double-sell.** The -17 short was a pure PHANTOM from Alpaca's paper
engine — note it reported avg_entry_price $17.32 (the SELL price), i.e. paper booked the single
long-closing sell as OPENING A NEW SHORT. RIVN is flat now (positions/RIVN → 404).

**SMOKING GUN — full RIVN order history 7/6-7/8:**
| UTC | order | status |
|---|---|---|
| 7/6 17:35:57 | BUY 17 market (110251a7) | filled → long @19.73 |
| 7/6 20:07:23 | SELL 17 stop @18.38 (e72ae17d) — protective | **canceled**, filled 0 |
| 7/6 21:21:55 | SELL 17 stop @18.41 (1a30ab52) — resubmit | **REJECTED**, filled 0 |
| 7/7 13:38:20 | SELL 17 market (74f96fcb) — the cover | filled → FLAT |
| 7/7 **14:14:11** | **BUY 17 stop @18.81 (f1d4e826)** | **canceled**, filled 0 |

The last row fired 5s AFTER the 14:14:06 "ORPHANED POSITION ADOPTED — -17 short, Stop=$18.81" log: the
phantom adoption made the bot **submit a REAL live BUY-stop for 17 shares against a NON-EXISTENT short**.
Had RIVN traded up through $18.81 it would have BOUGHT 17 real shares — creating an unwanted REAL long
out of a phantom. It was canceled before triggering. => **Bug B's guard (71cae8c) prevents the bot from
placing real orders against phantom positions — real capital risk, not just bookkeeping.** Guard VALIDATED:
skipping a phantom is unambiguously correct (no real position exists to manage), and the board's fail-safe
worry (a real double-sell left unmanaged) does NOT apply to this incident class.

**⚠️ RETRACTED CLAIM (corrected 2026-07-16 — I was WRONG; the earlier version of this entry said
"OM-BUG-1 is NOT benign — it cost a real loss." That is INCORRECT. Retained here for audit honesty.)**

**CORRECTED FINDING (root of the actual −$41 loss):** it was an UNHEDGEABLE OVERNIGHT GAP, NOT the stop
rejection. Evidence (Alpaca 1H bars + order objects):
- RIVN 7/6 19:00Z (3 PM ET): **C=20.11** (H 20.195) — well ABOVE the 18.38 stop, which is why the stop was
  legitimately ACCEPTED at 20:07Z (4:07 PM ET).
- RIVN 7/7 13:00Z (9:00 AM ET, RTH open hour): **O=17.745**, L=17.22. → a **~12% OVERNIGHT GAP** straight
  THROUGH the 18.41 stop. No bars in between (after-hours).
- Both stops are `extended_hours: False` → **a stop order does NOT execute outside RTH.** Even a perfectly
  live stop would have sat until the 9:30 open and triggered into the gap, filling ≈17.7 — essentially the
  same as the bot's actual 17.32 cover 8 min later. **The rejection cost ≈$0–7 of slippage, NOT the −$41.**
- Timeline of the rejection: resubmit @18.41 submitted 7/6 21:21:55Z was **ACCEPTED**; Alpaca `failed_at`
  **2026-07-07T08:00:01Z (4:00 AM ET)** — an ASYNC rejection at pre-market session open, once the gap made a
  sell-stop@18.41 sit above market. The bot's submit-time cover-on-breach pre-flight (run_cycle.py:711,
  ratified 2026-07-01) could not have known — the gap happened overnight, after submission.
**=> OM-BUG-1's "KNOWN BENIGN" classification is DEFENSIBLE for this case.** The recovery path worked as
designed: premarket reconcile cleared the dead ID; RTH cover-on-breach (gtc_manager:173) closed at the open.
**RESIDUAL (real but LOW impact, NOT worth a risky fix):** "phantom protection" — the bot polls the GTC stop
status only ~1s after submit (run_cycle.py:793-824), so an ASYNC rejection hours later is undetected until
the next premarket reconcile. Practically harmless because stops don't execute pre-market anyway, and the
RTH cover-on-breach backstops it. Note `gtc_manager.py:302` / `:381 _TERMINAL` omit `"rejected"` from their
terminal-status sets (a rejected order falls into "else: still live") — a latent inconsistency worth a
cheap hardening someday, but it did NOT cause this loss.
**REAL LESSON (not a code bug):** a 12% overnight gap is unhedgeable by a stop. This is an OVERNIGHT-GAP /
position-sizing exposure question (Architecture Invariant #11 overnight budget), not a stop-mechanics bug.

## ✅ RESOLVED (2026-07-16) — RIVN corruption chain fully broken
- **Bug A SHIPPED (`5fb5c4e`):** fill_reconciler external-close path → pnl=0.0 fixed.
- **Bug B SHIPPED (`71cae8c`):** orphan_manager recent-close guard (window 120min config) + CRITICAL
  mismatch alert → wrong-direction re-adoption prevented. reconcile is startup-only → alert re-fires
  every restart; a real position auto-adopts on the next restart past the window (self-heals). Gate:
  full read 1625L, statics, cold-2nd PASS, board Majors/Kim APPROVE-w-changes (incorporated: throttle
  removed, window 120 documented), preship gai=APPROVE gro=WAIVED (TPD) marker 2bcc8142743d. Live+verified.
- **Bug C RESOLVED via Option B (board decision):** the pop@portfolio_tracker:1543 is CORRECT; the Bug B
  guard makes the false-drop harmless (reconciler no longer treats a just-closed lot as an orphan). The
  risky Option A lifecycle rewrite (status="closing" in open_trades + exit-manager guards) is deliberately
  NOT done — unnecessary + high-risk in the #1 hotspot. All 3 bugs addressed.
- **Follow-ups (logged, non-blocking):** Bug E (was −17 a real double-sell? GTC-stop-vs-cover lifecycle);
  Harris masked-loss doc-comment in fill_helpers._sanity_ok; persist-then-auto-adopt is partially built-in
  (window-expiry auto-adopt) — a within-window intra-restart unmanaged window remains for a real double-sell
  (bounded $, alerted every restart); optional tightening later.

## PHASE-2 SHIP STATUS
- **✅ Bug A SHIPPED + LIVE (`5fb5c4e`, 2026-07-16):** fill_reconciler.py now calls
  fetch_actual_fill_price(submitted_after=None) → external-close path (entry-bounded, filled_at DESC,
  side filter, ±50% band) + direction/entry_time guards. Gate: full reads (fill_reconciler 133 +
  fill_helpers 370); statics; cold-2nd PASS; board Harris APPROVE (masked-loss check: kill switch uses
  Alpaca EQUITY not daily_pnl → phantom-proof, real gap loss captured regardless); Gro+GAI design + FINAL
  preship APPROVE (a7223e38a434). Runtime-verified on OCI.
- **⏳ Bug B + C = ONE JOINT FIX (BGG design LOCKED 2026-07-16, GAI + cold board; Gro TPD-skipped per Rafael rule):**
  - **PRIMARY (Bug C, portfolio_tracker):** stop the FALSE-DROP — a just-closed/fill-unverified position stays in
    tracker.open_trades in a "closing/pending-verification" state until CONFIRMED flat (get_open_positions returns
    none for it). Then it never appears as an orphan. This is the root fix; RIVN incident disappears.
  - **GUARD (Bug B, orphan_manager reconcile_positions, defense-in-depth):** skip adopting a symbol present in
    tracker.closed_trades with exit within a recent window (config `RECONCILE_RECENT_CLOSE_WINDOW_MINUTES`, default ~5m);
    ALWAYS Slack-alert on a bot-flat-vs-Alpaca-position mismatch (signals a double-sell OR stale read); FAIL-SAFE: if a
    skipped orphan PERSISTS beyond a longer threshold (~15m), adopt it with a CRITICAL alert (never leave a real
    crash-orphan stopless). Line 928 direction inference is CORRECT and stays; the fix is the adoption GUARD, not the formula.
  - **Bug E (open question):** was the −17 short a REAL double-sell (stale GTC stop firing after the cover) or a stale
    Alpaca read? The "always-alert on mismatch" guard surfaces it going forward; a dedicated check of the GTC-stop
    lifecycle vs the manual cover is a follow-up.
  - **Sequencing:** fix Bug C (false-drop) FIRST (root), then Bug B guard (defense-in-depth). Each its own full-read gate
    (portfolio_tracker.py 2268L + orphan_manager.py 1624L) + BGG + preship. Diagnose the record_exit false-drop next.

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
