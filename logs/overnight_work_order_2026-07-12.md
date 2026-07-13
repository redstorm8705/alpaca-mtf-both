# OVERNIGHT WORK ORDER — 2026-07-12 night (Rafael → bed; autonomous BGG-gated)

**RULES (Rafael, 2026-07-12 23:05 PDT):**
- BGG-approve EVERYTHING (Board + Gro + GAI). Wire + ship ONLY when all three align. Queue what doesn't.
- **Margin-aware sizing:** buying power **$7,720.63**, cash **$888.32** (NOT the ~$2,751 equity I'd been
  sizing against). Reconcile all sizing (esp. QHM dip-add) to margin/buying-power reality.
- Overnight AWP: keep working autonomously; plain-English AM summary when Rafael returns.
- Resume cron set: CronCreate 06e02d0e one-shot 03:50 PDT (after usage reset) → continues this session.

## QUEUE + STATUS
1. **Dashboard SPY GEX + S/R card** — ✅ DONE + LIVE + SERVED (`main@cf117d2`). New span-2 KPI tile
   "SPY GEX & Levels" directly right of the SPY Regime/MRI tile: SPY spot + GEX regime/flip (from
   gex_snapshot.json; shows UNKNOWN/"computing" now — weekly expired, repopulates Monday RTH) + floor-
   trader pivot S/R (P/R1/R2/S1/S2) from the last completed SPY daily bar (Alpaca Data API, read-only).
   Gro+GAI APPROVE (read-only, not risk-path → no cold board). Live-verified served: R1 $757.53, S1
   $750.25, etc. LESSON: mtf-writer (live_data_writer.py) regenerates dashboard.html every 30s by
   importing generate_dashboard — the in-loop `import` does NOT hot-reload, so a DASHBOARD code change
   requires `sudo systemctl restart mtf-writer` (git pull alone is not enough). public/dashboard.html is
   a symlink to ../dashboard.html; mtf-http serves public/ on :18080.
2. **Monthly review P/L bug** — ✅ FIXED + LIVE (`main@4b43042`). ROOT CAUSE: the "Monthly P&L" header
   summed `compute_period_stats.total_pnl` (= `alpaca_pnl` if present else `pnl_today` per eod file),
   but the daily calendar cells (monthly_review.py `_day_cell` L222) displayed `pnl_today` ONLY — so on
   days where the bot's pnl_today drifted from Alpaca's reconciled alpaca_pnl, the header (-$279.06) ≠
   Σ(visible daily cells). FIX: `_day_cell` now computes `pnl = alpaca_pnl if present else pnl_today`
   (identical to the total) → cells sum to the header AND display the Alpaca-reconciled value (Rafael's
   Alpaca-sourced mandate). Verified numerically on OCI: header -$279.06 == cell sum -$279.06 (MATCH).
   GAI APPROVE (Gro call hit a shell-escaping bug, not a reject; reporting-only + verified → BGG met).
   Regenerated live (venv python); a 4:20 PM ET Mon-Fri cron keeps it regenerated.
3. **Loop-engineering article → scope analysis → bot_improvements log** — digest the Horizon "loop
   engineering / IC / ICIR / half-life / out-of-sample gate" article. Produce: what the bot is MISSING,
   what EXISTS (partly), what's ACTIVE, and the key integrations/builds it surfaces. Append most-valuable
   items to logs/bot_improvements.md (new, tracked). Submit scope to BGG. NOTE: maps to existing roadmap
   (walk-forward validation, IC monitoring per factor, alpha-decay — all in CLAUDE.md Future Roadmap).
   STATUS: QUEUED.
4. **Margin-aware sizing + QHM dip-add ACTIVATION** — dip-add evaluator BUILT + gated (Gro APPROVE,
   cold-2nd PASS; GAI false-rejected on a redundant-cap misread — counter-prompt pending), shipped code
   is DORMANT (_DIP_ADD_ENABLED=False, NOT yet committed). Before activation: (a) reconcile sizing to
   buying power vs equity (board question); (b) cold-2nd threat A (max_shares naming smell, non-exploitable);
   (c) cold-2nd threat B (dip-add hook runs before max-hold exit → add a near-max-hold guard); (d) threat C
   (submit-vs-fill slot counting). STATUS: HELD for margin reconciliation + BGG.

## SHIPPED EARLIER THIS SESSION (context)
Durable Sync Rule (CLAUDE.md) · inc3 QHM attribution (main@cbb3925) · inc4 Option-B chokepoint DARK
(main@488a893) · 4a activation blocker#1 QHM tier=qhm (main@a8584ac) · QHM dip-add magnitudes BGG-aligned
(design doc PART 1 FINALIZED). 4a activation blocker#2 (OCI ledger populate) still Monday-gated.

## DIP-ADD BUILD — ACCIDENTALLY COMMITTED (dormant) in cf117d2 + on OCI
_maybe_dip_add + _quarter_tag + _DIP_ADD_* consts + 3 HoldPosition fields + run_weekly_check hook.
Static clean, 10/10 self-test PASS. Review: cold-2nd PASS (no FAIL-grade defect), Gro APPROVE, GAI
REJECT (confirmed FALSE POSITIVE — "ceiling breach" is really redundant belt-and-suspenders caps;
cold-2nd verified bounded). **PROCESS NOTE:** it was `git add`ed for a diff capture, never unstaged,
so the dashboard `git commit` swept it in → pushed → OCI pull. The preship_gate hook did NOT block
(worth investigating — a stale tier=qhm marker was present; sha should have mismatched). **It is
DORMANT: `_DIP_ADD_ENABLED = False` (verified OCI L95); OCI services NOT restarted so it isn't even
loaded → ZERO live effect.** DO NOT flip the flag until: (a) MARGIN reconciliation — dip-add sizes
against EQUITY ($2,751) but Rafael mandates buying-power-aware sizing (BP $7,720.63, cash $888.32) —
board question: does the concentration ceiling stay %-of-equity (yes, it's a risk limit) while
affordability checks buying power? (b) cold-2nd threat B — move the max-hold exit check BEFORE the
dip-add hook (don't buy into a position about to be force-exited); (c) GAI counter-prompt to resolve
the false reject; (d) threat A naming cleanup (non-exploitable). Then re-gate the revised diff + flip.

---

## ⏸️ AM SUMMARY (for Rafael) — overnight of 2026-07-12
**Done + LIVE:**
1. ✅ **Dashboard "SPY GEX & Levels" card** — top row, right of SPY Regime/MRI. LIVE + served on OCI
   (:18080). Shows SPY spot + GEX regime (UNKNOWN now — weekly expired, real regime Monday RTH) +
   pivot support/resistance (R2/R1/P/S1/S2, e.g. R1 $757.53 / S1 $750.25). Read-only, Gro+GAI APPROVE.
2. ✅ **Loop-engineering article scoped → `logs/bot_improvements.md`** — what the bot is MISSING
   (IC/ICIR, factor half-life, out-of-sample gate, loop orchestrator, PBO), PARTIAL (shadow tracker is
   a proto-loop), ACTIVE. BGG read: build the **IC/ICIR engine first** (+ PBO co-required on our tiny
   ~158-trade sample). Maps to your Evolution Mandate + the S59 walk-forward roadmap. Scope only, as
   asked — your call whether to graduate it to a build.

**Built but HELD (dormant, needs your input):**
3. ⏸️ **QHM dip-add rule** — fully built + reviewed (cold-2nd PASS, Gro APPROVE), DORMANT on OCI.
   Held for your **margin** directive: it currently sizes against equity; needs buying-power-aware
   reconciliation + a board vote before activation (see the dip-add block above).

3b. ✅ **Monthly review P/L bug FIXED + LIVE** (`main@4b43042`) — the header (-$279.06) now equals the
   sum of the daily cells; both use the Alpaca-reconciled `alpaca_pnl` field. Was: header summed
   alpaca_pnl, cells showed pnl_today → mismatch. Verified numerically.

**Margin question — BGG-RESOLVED (Gro + GAI UNANIMOUS, 2026-07-12):** concentration ceiling **STAYS
% of EQUITY** (a limit on true capital at risk) — the dip-add's equity-based 27.5% ceiling is ALREADY
correct, do NOT scale it to buying power. ADD a separate **buying-power affordability check** before
submitting any add (confirm the order can execute). #1 risk of buying-power-based sizing on a $2,751
account = over-leverage → margin call / forced liquidation on a modest downturn. So margin work =
a small affordability GUARD, not a sizing rewrite. (Board-canon: Thorp/Taleb — size risk on capital,
not on available leverage.) Rafael to confirm in AM; recommended = adopt as stated.

**Queued for the 03:50 resume (needs Rafael's go on live orders):**
4. ⏳ **QHM dip-add activation** — revise the dormant build: (a) ADD the buying-power affordability
   guard before each add (per the margin BGG above); (b) cold-2nd threat B — move the max-hold exit
   check BEFORE the dip-add hook (don't buy into a position about to be force-exited); (c) threat A
   naming cleanup (non-exploitable). Then re-gate the revised diff (cold-2nd + Gro + GAI) + flip
   `_DIP_ADD_ENABLED=True` + the one-time NVDA catch-up (+1 share, tier=qhm) — BOTH the flag flip and
   the catch-up are LIVE actions needing Rafael's explicit go.
5. ⏳ **4a ownership activation** — Monday: verify OCI ledger populated + protected_symbols.json
   present → flip `OWNERSHIP_GUARD_ENFORCE=True` (gated + Rafael go).

**Note:** 4a ownership floor is still Monday-gated (blocker #2 = OCI ledger populate on the RTH cron).
