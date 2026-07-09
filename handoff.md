# Handoff — alpaca-mtf-bot
**Updated:** 2026-07-08 (evening, interactive) | **HANDOFF TO A NEW CLAUDE ACCOUNT** (Rafael ~87% weekly limit).
NOT /wrap-up.

> **NEW ACCOUNT READS THESE FIRST, IN ORDER:** (1) this file, (2) `CLAUDE.md` (binding rules),
> (3) `logs/build_f_decision_2026-07-08.md` (the ACTIVE work — Build F + Forever-Hold design, decisions +
> open items), (4) `logs/api_build_packages_2026-07-08.md` (the F/A/B/E build slate, scoped),
> (5) `logs/tb_audit_log.md` (bug/patch log). Master Brain: `notebooklm use $(cat ~/.claude/master_brain_id)`.

## ⏩ LATEST (2026-07-09 wrap) — pick up here
- **Build F API build: DONE → branch `claude/build-f-2026-07-08` (`ffc3a61`), STATUS: READY FOR REVIEW+MERGE.**
  Implemented on OCI in an isolated worktree (live bot untouched, `main@02500cf`, active). Changed 5 code files
  (news_monitor keyword→context, run_cycle venue-detection + `halt_eval`, broker clock helper, alerts CRITICAL,
  trade_logger fields) + passed its gate. **NEXT ACTION: review that branch's diff, then merge to main → OCI
  `git pull` + restart to deploy.** (Do NOT let OCI commit to main; merge from the Mac/GitHub.)
- **Forever-6 = the NEXT API build** (design 100% locked in `logs/build_f_decision_2026-07-08.md`; launch it the
  same isolated-worktree/branch way once Build F is merged).
- **Quick fix queued (from 2026-07-08 audits):** `scan_to_html._fetch_options_data` — `cannot convert float NaN
  to integer` (fired 10×; display-layer, fails-closed → not urgent). One-line NaN guard. NOTE: `scan_to_html.py`
  also has a *rejected* Gro/GAI RC-3 item today — handle carefully.
- **WEEKLY AUDIT ROLLUP (2026-07-02→08) → `logs/weekly_audit_rollup_2026-07-08.md`.** A week of Gemini
  midday/post-market audits, deduped. **KEY TAKEAWAY: ~20 daily "CRITICAL" flags collapse to ONE root — P&L
  ATTRIBUTION CORRUPTION** (`FILL UNVERIFIED → external_close → $0 P&L`; drove the 07-07 false −73.86% kill-switch
  trip, the EOD P&L drifts, the RIVN $0 P&L + direction corruption, the stop-hit fidelity errors). **This is the
  single highest-value outstanding fix — build it WITH Build A + B (same fill/reconciliation family).** Tier-2:
  TOD/phase bug, scan_to_html NaN, cycle-perf/scan-timeout. Recurring FALSE POSITIVE: the VOLSHADOW "score
  discrepancy" (Gemini misreads the log-only volume shadow — verify once + rename fields). ALPHA (not bugs):
  exit R:R, high-score≠profit scoring validation, MRI data stability. Full prioritized list in the rollup.

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
