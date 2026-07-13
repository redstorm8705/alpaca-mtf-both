# OVERNIGHT WORK ORDER — 2026-07-12 night (Rafael → bed; autonomous BGG-gated)

**RULES (Rafael, 2026-07-12 23:05 PDT):**
- BGG-approve EVERYTHING (Board + Gro + GAI). Wire + ship ONLY when all three align. Queue what doesn't.
- **Margin-aware sizing:** buying power **$7,720.63**, cash **$888.32** (NOT the ~$2,751 equity I'd been
  sizing against). Reconcile all sizing (esp. QHM dip-add) to margin/buying-power reality.
- Overnight AWP: keep working autonomously; plain-English AM summary when Rafael returns.
- Resume cron set: CronCreate 06e02d0e one-shot 03:50 PDT (after usage reset) → continues this session.

## QUEUE + STATUS
1. **Dashboard SPY GEX + S/R card** — top row, directly RIGHT of the "SPY Regime/MRI" tile. Must be LIVE
   on git + OCI so Rafael sees it in the AM. GEX data exists (data/gex.py, GEX_ENABLED=True); S/R via GEX
   flip/call-wall/put-wall levels (+ swing H/L if cheap). Feature-Design + BGG on the card → build → ship.
   STATUS: IN PROGRESS.
2. **Monthly review P/L bug** — monthly page shows "-$279.06" but the dated rows don't sum to that. Another
   P/L calc error. Diagnose (reporting/ + monthly_review generator) → BGG → fix → ship. STATUS: QUEUED.
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

## DIP-ADD BUILD — uncommitted working tree (execution/quarterly_hold_manager.py)
_maybe_dip_add + _quarter_tag + _DIP_ADD_* consts + 3 HoldPosition fields + run_weekly_check hook.
Static clean, 10/10 self-test PASS. NOT committed pending margin reconciliation + GAI counter-prompt +
threat-B guard. Decide at activation whether to ship the dormant code now or fold the margin/threat fixes in first.
