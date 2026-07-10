# Handoff — alpaca-mtf-bot
**Updated:** 2026-07-08 (evening, interactive) | **HANDOFF TO A NEW CLAUDE ACCOUNT** (Rafael ~87% weekly limit).
NOT /wrap-up.

> **NEW ACCOUNT READS THESE FIRST, IN ORDER:** (1) this file, (2) `CLAUDE.md` (binding rules),
> (3) `logs/build_f_decision_2026-07-08.md` (the ACTIVE work — Build F + Forever-Hold design, decisions +
> open items), (4) `logs/api_build_packages_2026-07-08.md` (the F/A/B/E build slate, scoped),
> (5) `logs/tb_audit_log.md` (bug/patch log). Master Brain: `notebooklm use $(cat ~/.claude/master_brain_id)`.

## ⏩ LATEST (2026-07-10 interactive) — pick up here
- **Build F: ✅ MERGED + DEPLOYED — LIVE on `main@90f8bdf` (2026-07-10 00:27 UTC).** Reviewed the branch diff
  full + verified all 6 integration points; re-ran the gate this session (RULE C-2, prior OCI gate expired):
  Gro APPROVE + GAI APPROVE (both withdrew all findings on counter-prompt), cold board 2-0 (masked-loss +
  execution seats), py_compile/ruff/mypy clean, preship markers written for news_monitor/broker/run_cycle.
  Rafael APPROVED. Merged `claude/build-f-2026-07-08`→main (no-ff `90f8bdf`), pushed, OCI ff-only pull +
  restart → DEPLOY_OK, health OK, zero tracebacks. Branch `claude/build-f-2026-07-08` can be deleted.
- **TWO FOLLOW-UP ENHANCEMENTS from the Build F gate (logged, non-blocking):**
  (1) GAI: add a LOW-severity Slack heads-up if `get_asset_tradable("SPY")` returns None for several
  consecutive cycles while the SPY *bar* feed still works (a get_asset-only outage never trips the existing
  3-fail T1-FEED-DOWN alert). Path already logs a WARNING + `halt_eval` event each cycle — this just adds a
  human page for a persistent streak. (2) Masked-loss seat: cold-start edge — on the very first cycle after a
  restart, if SPY gapped down hard before the first successful 5-min fetch, `_spy_session_pct` reads 0.0
  ("no decline") for one cycle because `_main._spy_last_close` defaults to 0.0; the independent is_open /
  spy_tradable legs still catch a real halt, and it's auditable via `halt_eval`. Minor; consider a
  "warming/unknown" sentinel instead of 0.0. Both are `strategy/run_cycle.py`.
- **Forever-6 design = 100% COMPLETE + LOCKED (2026-07-09)** — but a big architecture decision reordered the build:
  - Scenario map (all 13 sell-first/no-buyers cases) + both forks locked → `logs/forever6_scenario_board_2026-07-09.md`.
    FORK A = per-rung monotonic latch; FORK B = small first rung fires + ping + human-gate deeper rungs.
  - Full read of `quarterly_hold_manager.py` (1955L) done → integration map `logs/forever6_integration_map_2026-07-09.md`.
  - **RAFAEL NEW MANDATE (2026-07-09): intraday/swing tiers must STILL trade QHM/Forever-6 names on confluence.**
    Ring-fence protects a SHARE COUNT, not the symbol. This requires the per-tier shared-lot ownership layer
    (the "real fix" the roadmap flagged; its absence retired Movers). Full board (5 voices) designed it →
    `logs/per_tier_ownership_design_2026-07-09.md`. **Rafael locked 3 decisions:** (1) shorts on ring-fenced
    names → via OPTIONS not shares (share tiers LONG-ONLY there); (2) real Alpaca GTC stops, auto-re-issued on
    every tier-qty change; (3) **BUILD ORDER = FOUNDATION FIRST, 3 PHASES.**
- **⏭ NEXT BUILD = PHASE 0 (COMBINED: ownership foundation + BROKER-AUTHORITATIVE P&L), NOT Forever-6.**
  Rafael merged the fail-proof P&L fix INTO Phase 0 (2026-07-09) — same problem: per-strategy P&L needs
  execution-time ownership. Plan: `logs/phase0_combined_ownership_pnl_plan_2026-07-09.md`. Sub-phases:
  **P0-a** ownership (ledger + client_order_id tier-tag + `ownership_guard.py` floor chokepoint +
  `qty_bounded_partial_close` + `close_position` multi-tier hard-disallow + drift reconcile + launch init;
  also fixes Movers bug). **P0-b** broker-authoritative P&L: WIRE IN `reporting/pnl_ledger.py` (570L,
  ALREADY BUILT, confirmed UNWIRED — the abandoned stateless-FIFO-from-Alpaca-fills engine) as the SOLE
  realized source + fill ingestion; DELETE the 3 parallel engines — `fetch_actual_fill_price` (~10 call
  sites), `_fifo_reconstruct`/`fifo_pnl.py`, and STRIP P&L from `record_exit` (~14 callers) while KEEPING
  its state machine. Total P&L→portfolio-history (advisory), unrealized→position objects. **P0-c** the
  STATE-RECONSTRUCTION INVARIANT: replay fills ledger → reconstruct per-(symbol,tier) qty/avg_cost →
  compare to Alpaca live positions every cycle; confirmed break (≥2 cycles)→FREEZE+manual, never auto-
  correct; portfolio-history is NEVER the freeze trigger (board seat fix). Validation = property-based
  FIFO tests + golden-master replay of the real bugged-months fills + 2am manual-reconcile runbook.
  **Build A (safe-mode) + OPT-2 (event-sourced replay) are SUBSUMED by P0-b/P0-c — retire as separate items.**
  Then Phase 1 (per-tier P&L history + synced stops), then Phase 2 (Forever-6 tier).
  Line-scoping → `logs/phase0_ownership_scoping_2026-07-09.md` (order-path sites) + canonical guard spec
  `logs/phase0_ownership_guard_spec_2026-07-09.md`. **SCOPED (4 core order files): broker.py (780L) +
  entry_logic.py (1688L, del registry block L437-440) + exit_logic.py (2269L, 23 reducing-order sites +
  qty-valuation bug zone) + fill_helpers.py (369L).** **ownership_guard.py = FULLY SPEC'D** (chokepoint
  algorithm + ledger schema + drift + stop-sync + launch-init, Rafael decisions folded in — implementable).
  **NEXT (fresh context): portfolio_tracker fill-reconcile (add client_order_id→tier attribution + untagged→
  halt) → ~15 remaining call sites (orphan 5 / run_cycle 4 / main 6 / QHM OrderDispatcher, tier=qhm) →
  launch-init script → assemble exact Phase-0 diff → static + cold-2nd + impact + FINAL Gro+GAI → API build.**
  Phase-0 surface ≈ 45 call sites / 6+ files + ownership_guard + ledger + launch-init. ~80% scoped.
- **TWO catastrophic modes the board designed against (do NOT regress):** (a) stale resting stop eats a
  protected share → synchronous cancel-replace on every tier-qty change; (b) floor recomputed from Alpaca's
  drifted net → floor is LEDGER-derived ONLY, drift→freeze-sells; (c) F6 trim reading Alpaca blended avg_entry
  → trim reads F6's OWN basis only.
- **Quick fix queued (from 2026-07-08 audits):** `scan_to_html._fetch_options_data` — `cannot convert float NaN
  to integer` (fired 10×; display-layer, fails-closed → not urgent). One-line NaN guard. NOTE: `scan_to_html.py`
  also has a *rejected* Gro/GAI RC-3 item today — handle carefully.
- **WEEKLY AUDIT ROLLUP (2026-07-02→09) → `logs/weekly_audit_rollup_2026-07-08.md`.** A week of Gemini
  midday/post-market audits, deduped to **TWO dominant roots:** (#1) **P&L ATTRIBUTION CORRUPTION** (`FILL
  UNVERIFIED → external_close → $0 P&L`; drove the 07-07 false −73.86% kill-switch trip, the EOD P&L drifts, RIVN
  $0-P&L + direction corruption, stop-hit fidelity) — build it WITH Build A + B (same fill/reconciliation family);
  (#2, new on 07-09) **ENTRY-PIPELINE THROTTLING** — sizing-cap stacking → 0 shares on high-priced names, silent
  "no allocation" skips, ANOMALY-2 confirm_gate stalls; qualified signals not being entered (costing trades now).
  Tier-2: TOD/phase, scan_to_html NaN, cycle-perf. Recurring FALSE POSITIVE: VOLSHADOW "score discrepancy" (Gemini
  misreads the log-only volume shadow — verify + rename). ALPHA reviews: exit R:R, high-score≠profit, MRI data.
  Full prioritized where-to-continue in the rollup.

## ⚡ 2026-07-10 SHIPPED (P&L fixes — LIVE)
- **Kill-switch phantom-proof** (commit `24c542a`, live): drop corrupt daily_pnl; measure = Alpaca
  equity delta − QHM unrealized (position objects, no fill-matching), fail-safe to account-level.
  Fixes the 07-07 false −73.86% trip. Board(masked-loss+reliability)+Gro+GAI, marker 79a4c2c8.
- **pnl_ledger pagination fix** (commit `e1e7416`, live): Alpaca activities use page_token NOT after_id
  (after_id ignored → infinite loop → 14-min hang on 367 fills — the "stayed stuck" issue). Now 3.7s.
- **P&L heal APPLIED + cron**: `python3 -m reporting.pnl_ledger --heal-apply` healed 51 eod days to true
  Alpaca-FIFO (7/02: −251.12→+41.08); invariant ok drift $0.65. **OCI cron `30 20,21,22 * * 1-5`**
  (post-close, both DST; idempotent/fail-closed/singleton). Dashboard regenerates from healed files.
  **TRUE LIFETIME REALIZED = +$282.88** (account UP ~$288 since inception; the losses were mostly
  corrupt reporting + the 07-08 false-HALT day). NOTE: crontab is NOT in git — re-add on host rebuild.
- **P0-b LIVE P&L authoritative — SHIPPED (commit `280cdec`, live)**: `portfolio_tracker.write_eod_summary`
  now sources `pnl_today` from `pnl_ledger.build_ledger()` (authoritative Alpaca-FIFO), invariant-gated
  ($5 tol), PT-keyed, A-4-gap-guarded, dual-compute fallback on any fail. Also fixed `pnl_ledger` to key
  per_day by PT date (was UTC → AH/overnight fills mis-bucketed; commit `1175596`). Gate: full read +
  static + cold-2nd PASS (caught the TZ bug) + Gro + GAI APPROVE. eod files re-healed with PT-keys. So
  the LIVE pnl_today the bot writes each cycle is now Alpaca truth, not corrupt-then-heal. New eod fields:
  pnl_today_qhm / pnl_today_total / pnl_ledger_authoritative.
- **STILL OWED (the last big build): per-strategy attribution + the ownership layer (P0-a, ~45 call
  sites + never-sell-floor guard)** — `logs/phase0_combined_ownership_pnl_plan_2026-07-09.md`. This is
  the one that makes per-tier P&L correct + ring-fences Forever-6; ships as its own gated build.

## Bot Status
- **Running:** 4 services active on OCI 129.153.208.32 (mtf-bot, mtf-writer, mtf-http, nginx). Git HEAD `9d03be1`
  (local = GitHub = OCI, verify with `git rev-parse HEAD` + OCI ssh). paper=True. Account ~$2,782, kill switch clear.
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32`. Deploy = git single channel (Mac commit→push→
  OCI `git pull --ff-only`+restart). NEVER rsync tracked files.
- **Permissions:** project `.claude/settings.local.json` `defaultMode: bypassPermissions` (set 2026-07-08 so
  scheduled/headless sessions don't stall on Bash approval). Backup: `settings.local.json.bak-20260708`.
- **Cost protocol (CLAUDE.md, LIVE):** interactive scopes/designs + runs board/Gro/GAI on the DESIGN; API
  (OCI headless) implements the pre-scoped diff + gates the DIFF + ships. Only Rafael changes it. Budget $20/mo.

## SHIPPED this session (2026-07-08)
- **F-INTERIM — commit `9d03be1`, LIVE + health-verified.** A false news-keyword HALT ("Can Trump cut off all
  trade with Spain?" matched substring "national emergency") had `safe_close_all`-liquidated the whole non-QHM
  book (6 pos, ~-$26, 11s). Interim: news-keyword HALT now BLOCKS NEW ENTRIES only, never liquidates. Full gate
  passed (board 3/3 + Gro + GAI; both final-pre-ship REJECTs were misreads, withdrawn on counter-prompt).
- **Cost fix:** morning health report retuned $0.86 → **$0.044/run** (`scripts/oci_report_runner.sh` Haiku +
  `scripts/collect_health_facts.py`; OCI cron 6 AM PT). Zero Mac dependency.
- **Scheduler root-cause fixed + validated:** crons failed on PERMISSIONS (no broad Bash allow → headless
  session stalled → wedged the slot), not scheduling. Fixed via bypassPermissions; live-test-fired 2026-07-08
  12:40 PT, wrote its marker cleanly. **In-thread pickup = CronCreate (not scheduled-tasks routines).**

## ACTIVE WORK — Build F + Forever-Hold (design DONE, decisions in `logs/build_f_decision_2026-07-08.md`)
**Build F (HALT/mass-liquidation redesign) — DECIDED (Rafael): "No reflex + halt observability."**
News = context/display only (delete `get_news_size_multiplier` 0.0 branch); NO automated close-all re-added
(liquidation authority stays with per-position stops + SPY-EXTREME entry-block + 7% kill-switch; `safe_close_all`
= user-shutdown only); ADD Alpaca venue-state detection → block entries + CRITICAL alert + `halt_eval` event
(never blind to a real halt). QHM exempt. Bucket-A-on-CB reversal → deferred to a board re-vote (moot w/o CB-liq).
KEY FACT: the 7% kill-switch does NOT liquidate — only blocks entries; only live `safe_close_all` caller = user-shutdown.

**Forever-Hold Accumulation — Rafael mandate, SEPARATE TIER above QHM (design done, guardrails locked, some open).**
- FOREVER-6: **TSLA, GOOGL, AMZN, CRWD, META, NVDA** — never sold (T1 = +1000%/10x → trim 25%; no other sell).
  Own bucket/logic/rules; ring-fenced (other strategies never dip into forever-6 shares). Curated 3–10yr secular
  names (NOT valuation-based), added manually over time; an extension of QHM. Crash/halt/CB/flash-crash = BUY.
  EXEMPT from kill-switch + halt-entry-block.
- **Board (Gro/GAI/Sosnoff/Thorp+Taleb) = SHIP-WITH-GUARDRAILS.** MANDATORY guards (see decision doc): (1)
  CASH-FUNDED ONLY (settled cash ≥ cost, never margin — makes margin-call impossible; THE load-bearing guard),
  (2) CAP on FIXED reserve / min(current,prior) equity not live equity, (3) per-name cap min(1sh, ~8% eq), (4)
  one-shot latch per (symbol, date) + ≤3 buys/day, (5) marketable limit never market, (6) data-quality gate
  before flash detector, (7) exemption from kill-switch/halt only — never from cash floor/CAP.
- **OPEN (Rafael + board):** sizing fork (scale-up vs flat/"more-levels" synthesis — recommend more-levels);
  CAP number (~30–40%); the EXPANDED crash-scenario board pass Rafael asked for (rules for flash/halt/CB/intraday
  crash/weekly crash/bear market — the full sell-first/no-buyers set); full-read `quarterly_hold_manager.py`
  (1954L); then final board+Gro+GAI on the combined proposal → **API build** (Rafael: ship to API to build).

## BUILD SLATE (Rafael): F → (Forever-6) → A → B → E  — full scope in `logs/api_build_packages_2026-07-08.md`
- **A — Data-Integrity Safe Mode** (2026-07-07 Alpaca-desync fix). Scoped to the LINE (full reads done:
  run_cycle 1865, orphan_manager 1624, risk_manager 802). Explained-P&L glitch validator → safe-mode → glitch-vs-
  real kill tagging → orphan/trade gating. Masked-loss seat MANDATORY (Scenario-7 test).
- **B — Orphan-stop root.** Scoped to the LINE (orphan_manager L714 + L1320; one confirming read left:
  portfolio_tracker.py:~965). Cancel stops BEFORE record_exit in external-close paths, fail-closed, + sweep.
- **E — QHM accumulation** (never-sell + buy-on-dips + `max(1, floor(0.03·eq÷price))`, 20% cap). Full read of
  quarterly_hold_manager.py (1954L) owed. **Note: Forever-6 supersedes/extends this — reconcile E with Forever-6.**

## OPEN ITEMS / BUGS / UNRESOLVED (as of 2026-07-08)
- **P0 (from incidents, in the slate):** Build A (glitch safe-mode), Build B (orphan-stop naked-short root).
- **Build F implementation** (decided, not built) + **Forever-6 implementation** (guardrailed; sizing/CAP + expanded
  crash board owed) → then API build.
- **Bucket-A-on-circuit-breaker reversal** — needs a fresh board vote (reverses Apr-8 ruling).
- **RC-4 open sites:** portfolio_tracker.py L1200/L1753 (per tb_audit_log hotspot table) — verify.
- **Main-bot false-drop root** (`record_exit` dropping a still-live position) — corrupts P&L; feeds the retired-Movers
  adopt vector. Pre-existing P0 (see FUTURE ROADMAP LOG MOVERS-RETIRED).
- **Cross-strategy audit** (strategies sharing Alpaca lots w/o ownership tags) — ongoing (roadmap 2026-06-30).
- **autonomous_review.py pushes to main** — conflicts with git-single-channel; needs branch+PR redesign (roadmap 2026-07-03).
- **Pre-incident:** Options two-column 0DTE page (SPX-source blocker — Alpaca has no SPX; mockup at
  logs/mockups/options_scanner_mockup_2026-07-06.html); OPT-2 event-sourced replay; CLAUDE.md Open-Question
  loophole removal; RBLX phantom lot; UX redesign of the 5 HTML pages; volume/16pt/delta/GEX/TSMOM shadow flips
  (see CLAUDE.md SHADOW STRATEGY TRACKER + FUTURE ROADMAP LOG).
- **Desktop scheduled-task `resume-build-f-2026-07-08`** — DISABLED (switched to in-thread CronCreate). Stray
  in-thread cron jobs are session-only (die on session end).

## Process / Invariants (binding — CLAUDE.md is authoritative)
- Board + Gro + GAI on EVERY fork BEFORE Rafael; the board+Gro+GAI audit of the FULLY-MAPPED proposal is the
  ABSOLUTE LAST STEP before API (account cross-strategy + existing bugs + hotspots). Final Gro+GAI pre-ship on the
  exact diff (preship_gate enforces on commit). Cold board MANDATORY on every risk-path diff.
- Safety: a control must NEVER mask a real loss. paper=True locked. SPY 5-min = sole entry gate. P&L from Alpaca fills only.

## References (all committed to git for the handoff)
- `logs/build_f_decision_2026-07-08.md` — Build F + Forever-Hold decisions + board results + open items (THE active doc)
- `logs/api_build_packages_2026-07-08.md` — F/A/B/E build slate (scoped to the line)
- `logs/tb_audit_log.md` — bug/patch log · `logs/incident_2026-07-07_alpaca_desync.json` — desync forensics
- `logs/safe_mode_spec_2026-07-07.md` — Build A spec
