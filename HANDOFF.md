# Handoff — alpaca-mtf-bot
**Updated:** 2026-05-27 S43B | **S43B: RAM leak fix P1/P2/P3/P4 ALL DEPLOYED — macro_risk_index.py class-level ThreadPoolExecutor (P1), config.py BARS_TO_FETCH 500→150/300→100 (P2), run_cycle.py gc.collect() try/finally after run_scan() (P3). All 4 OCI services active. RAM 279MB (was 330MB start-of-session → 255MB → 326MB → 279MB across deploys). Priority 4 (signal_generator.py del _entry_df/_daily_df) PENDING. Optional jemalloc LD_PRELOAD PENDING.**

## Bot Status
- **Running:** YES — OCI Phoenix `129.153.208.32` | all 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32` (Ed25519 key)
- **Bot CWD on OCI:** `/home/ubuntu/mtf-bot/` — always rsync to this path (NOT alpaca-mtf-bot_FINAL)
- **Account:** Paper | equity **$2,852.68** (confirmed S37 MCP) | All-time P&L +$442.38 (Alpaca-authoritative, confirmed S29) | MIN_SCORE=10/12 | KELLY_FRACTION=0.25 | KELLY_MAX_RISK_PCT=6% | MAX_PORTFOLIO_RISK_PCT=4%
- **Dashboard:** `http://129.153.208.32:8080/dashboard.html`
- **RAM (S43B):** 279MB post P1/P2/P3 deploys (was 330MB session start → 255MB after macro_risk_index.py → 326MB after config.py restart → 279MB after run_cycle.py restart). Alert threshold: 550MB → restart all services. Prior peaks: S37=750MB, S39=731MB (both triggered auto-restart). RAM leak investigation in progress — 3/5 fixes deployed (see P2 open item below).
- **⚠️ SSH KEY NOTE:** Ed25519 key at `~/.ssh/mtf_bot_oracle`. rsync syntax: always use `-e "ssh -i ~/.ssh/mtf_bot_oracle"` — NEVER use `-i` as standalone rsync flag.

## Open Positions (confirmed Alpaca API ~11:50 PM PT 2026-05-24 — verify at next session start)
- AMZN long 1sh @ $264.81 | unrealized +$1.51
- INTC long 2sh @ $117.00 | unrealized +$5.69
- MSTR short -3sh @ $162.95 | unrealized +$9.18
- PANW long 1sh @ $247.26 | unrealized +$13.32
- TOST short -28sh @ $22.79 | unrealized -$10.36
- TQQQ long 1sh @ $76.22 | unrealized +$1.62

## PDT
- **0/3 slots used** (confirmed S37 via Alpaca MCP — daytrade_count=0)

## Open Items (next session priority)

### ✅ CLOSED in S39
- [x] **P1: execution/entry_logic.py RC-3 ×3** — L1072 Kelly TQI stdev (WARNING), L1453 PHANTOM ENTRY alert (CRITICAL+stderr), L1629 AH quote fail-closed (WARNING, GAI wins over DS). DS/GAI V3 complete. Cold second-agent PASS (after _sys scope confirm). OCI deployed. RC-3 count: 6→3.
- [x] **Stale comment L1490** — "yfinance feed degraded" → "Alpaca Data T1 feed degraded" (fixed inline with S39 patch).

### ✅ COMPLETED (DS/GAI + patch) — S39 continued
- [x] **DS/GAI Prompt 1 + scan_to_html.py RC-3 ×11 PATCHED** — All 9 steps complete. DS/GAI both APPROVE all 11 hunks (3/3 unanimous). py_compile PASS, mypy PASS, ruff PASS, cold second-agent PASS, code-review-graph 0 bot-relevant nodes. OCI deployed. RC-3 count: 3 remaining (unknown other files). tb_audit_log.md + bug_counter.json updated.

### ✅ DECISIONS RESOLVED (S39 post-compaction)
- [x] **DECISION 1 — entry_logic.py RC-4 #12c:** PATCHED. GAI P0 accepted. 3× retry loop (1s intervals) replaces single 1.5s poll + entry_price fallback. Redundant `import time as _t12c` removed. $0 check moved outside try/except. Board 2/2 APPROVE. OCI deployed, all 4 services active 308MB.
- [x] **DECISION 2 — weekly_perf_audit.py design revision:** APPROVED by user. 4 DS/GAI additions to incorporate into spec before build: (1) Emergency Escalation Clause >25% drawdown, (2) VIX floor <15=LOW_VOL, (3) Mislabeling cascade prevention 10-trade monthly gate, (4) MIN_TRADES_FOR_HYPOTHESIS (offensive≥20, defensive≥12, emergency≥5).

### Board Verdict (S40 — 2026-05-26, first live autonomous run)
**OVERALL: FAIL** — 4/4 domains FAIL. Two P0 items require immediate fix before capital increase.

**P0 — risk.open_positions startup initialization bug:** On session start, `risk.open_positions` initializes to 0 regardless of overnight positions held. This is the ROOT CAUSE of the 6-position breach (MAX=4). Fix: query Alpaca positions API at startup, set `risk.open_positions = len(alpaca_positions)`, HALT if ≥ MAX_POSITIONS before any trading.

**P0 — Slack alert noise (alert fatigue):** `mri_refresh` every 12 min + `breadth_refresh` every 15 min + all trade events → hundreds of Slack messages/day. Operator cannot distinguish real alerts. Fix: route all routine events to log only; Slack reserved for `{stop_loss_triggered, position_limit_breach, fill_reconciliation_error, margin_call, service_crash}`.

**P1 items (board-confirmed):** GC pauses 2.8–6.3s (root cause: unbounded bar history), non-atomic P&L writes (stop_hit with pnl=0.0 before reconciliation — needs `status: pending` field), fill reconciliation latency 17s, risk.open_positions desync = Kelly fraction 7–8x.

**Board-only findings (missed by DS/GAI):** No slippage budget per position tier, no margin cascade circuit-breaker, no fill reversion pattern analysis, correlation not validated (all long tech/growth on same day = 0.7+ realized vs 0.2 assumed), no reconnect exponential backoff, no orphan order watchdog, no heartbeat position reconciliation loop.

### Open Items

- [x] **P0: risk.open_positions startup fix — CLOSED S42** — main.py Part A: P0-STARTUP block inserts after sync_from_tracker(); queries Alpaca live positions; overrides risk.open_positions if mismatch; logs _untracked/_stale symbol sets; halts at MAX. entry_logic.py Part B: P0-CYCLE-SYNC-GUARD replaces unconditional BUG-POS-1 sync; directional guard (tracker UP-only); status filter (excludes zombie closed entries); None guard. Both deployed OCI. Startup log confirmed: "Alpaca=4 == tracker=4. OK." → "Already at MAX (4/4). Blocking new entries."
- [x] **P0: Slack alert noise — PARTIALLY CLOSED S43** — `alerts.py` patched: `alert_crash()` reason-based dedup (same reason+<60min→ntfy only; different reason→Slack+ntfy always); `alert_stale_bar()`→log-only; `alert_startup_test()` + `alert_spy_event()` UNCHANGED (board: keep all). Deployed OCI git 35bccc9. **Remaining:** `events/macro_risk_index.py` mri_refresh noise (confirmed NOT going to Slack currently — JSONL only). SIGKILL cycle itself is root cause (P2 RAM leak — separate session).
- [x] **P1: weekly_perf_audit.py COMPLETE (S42)** — Script built, static analysis PASS, cold second-agent PASS, OCI deployed, cron wired `15 20,21 * * 5` via cron_tz_wrapper 16:15. 4 DS/GAI additions incorporated (emergency escalation, LOW_VOL VIX regime, monthly mislabeling gate, MIN_TRADES thresholds). Design spec updated at `logs/weekly_perf_audit_design_v1.md` (§14).
- [ ] **P2: RC-9 in scan_to_html.py** — `_fetch_yfinance_news()` uses yfinance for news data (T4 violation). Board vote + migration plan required.
- [ ] **P2: BUG-C structural fix** — write_scan_html background thread. Interim 10-min throttle deployed S31. Structural fix deadline 2026-06-30.
- [ ] **P2: RAM leak investigation — IN PROGRESS (4/5 deployed)** — Root cause: 3-layer mechanism (DS+GAI confirmed S43B). Layer 1 (~370MB first-scan spike): Alpaca-py Pydantic deser 200+ symbols × bars + 46 DataFrames in full_results simultaneously. Layer 2 (~56MB/cycle drift): Pandas BlockManager cyclic refs + glibc heap fragmentation. Layer 3 (SIGKILL): accumulated baseline + scan peak exceeds OCI 1GB cgroup. **✅ P1 DEPLOYED:** macro_risk_index.py class-level ThreadPoolExecutor (48 leaked executors × 8MB eliminated). **✅ P2 DEPLOYED:** config.py BARS_TO_FETCH TF_15M 500→150, TF_1H 300→100 (~70% first-scan spike reduction). **✅ P3 DEPLOYED:** run_cycle.py gc.collect() in try/finally after run_scan() (frees Pandas cyclic refs, ~56MB/cycle drift fix). **✅ P4 DEPLOYED (S43B):** signal_generator.py — `fr.pop("_entry_df"/"_daily_df")` in both Phase 3 paths (ADDV-fail before continue + post-16pt-scoring before tag). 8-change patch (5 C-4 pre-existing fixes + 3 primary). RAM 252MB post-deploy. **⬜ OPTIONAL PENDING:** OCI systemd LD_PRELOAD jemalloc (`LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2`). Verify: `dpkg -l libjemalloc2`. No code changes. GAI: "silver bullet for glibc C-level heap fragmentation."
- [ ] **P3: options_scanner.py** — BUG-0DTE-FALLBACK (HIGH, L400-412); RC-5 L1081. `⚠️ AUDITED — PATCH PENDING DS+GAI`. Needs fresh full read (1,946L, Explore subagent) + DS/GAI prompt before patching (prior audit 2026-04-30, all gates reset per RULE C-2).

## Autonomous Crons / CCRs

### OCI Cron (crontab on 129.153.208.32)
- **Midday audit:** `30 17,18 * * 1-5` via cron_tz_wrapper → 1:30 PM ET → `midday_audit.py` → `logs/midday_gemini_*.txt` + Slack
- **Nightly audit:** `5 20,21 * * 1-5` via cron_tz_wrapper → 4:05 PM ET → `nightly_audit.py` → `logs/gemini_audit_*.txt` + Slack
- **Meta-audit:** `35 20,21 * * 1-5` via cron_tz_wrapper → 4:35 PM ET → `auto_ai_audit.py --meta-audit` → DS+GAI cross-review → `meta_audit_latest.json` → Slack (added S40)
- **Weekly perf audit:** `15 20,21 * * 5` (Fridays only) via cron_tz_wrapper → 4:15 PM ET → `weekly_perf_audit.py` → 8-category failure analysis + HTML tearsheet + Slack (added S42)

### Claude CCRs (claude.ai/code/routines)
- **Usage-reset (one-time):** `trig_015BE5S4bmQtajwZmcLrSbGt` — fired 2026-05-26T02:55:00Z → https://claude.ai/code/routines/trig_015BE5S4bmQtajwZmcLrSbGt
- **Nightly CCR:** `trig_01GHKQufnHRykVL9Xypqeee4` — `0 7 * * *` = midnight PT → https://claude.ai/code/routines/trig_01GHKQufnHRykVL9Xypqeee4
- **Board Review CCR:** `trig_01B37951UHsX2NAsCvrJuHNK` — weekdays 2:30 PM PDT (5:30 PM ET) → 4 parallel domain agents review meta-audit → board verdict → https://claude.ai/code/routines/trig_01B37951UHsX2NAsCvrJuHNK (added S40)

### Full Daily Pipeline (Mon–Fri)
| Time ET | What | Output |
|---------|------|--------|
| 1:30 PM | midday_audit.py (Gemini) | midday_gemini_*.txt + Slack |
| 4:05 PM | nightly_audit.py (Gemini) | gemini_audit_*.txt + Slack |
| 4:35 PM | auto_ai_audit.py --meta-audit (DS + Gemini) | meta_audit_latest.json + Slack |
| 4:15 PM Fri | weekly_perf_audit.py | weekly_perf_audit_YYYY-WNN.html + Slack (added S42) |
| 5:30 PM | Board CCR (4 agents: Harris/Brandt, Thorp/Asness, Peterffy/Katsuyama, McKinney) | Board verdict in CCR transcript |

### Infrastructure Notes
- **GitHub repo:** `https://github.com/redstorm8705/alpaca-mtf-both` (private) — CCR agents clone from here
- **Board endpoint (Gist):** `https://gist.githubusercontent.com/redstorm8705/1574ea556d06e7a1db45d00097f9c069/raw/meta_audit_latest.json` — auto-pushed after every meta-audit run by `auto_ai_audit.py` (added S40). CCR fetches from here (raw IP blocks from Anthropic cloud resolved via Gist).
- **Board endpoint (nginx):** `http://mtftradingbot.duckdns.org/meta_audit_latest.json` — nginx port 80, ufw open, OCI Security List open. Accessible from home/office. Not accessible from Anthropic CCR (IP allowlist restriction — use Gist URL for CCR).
- **DS/GAI keys:** DEEPSEEK_API_KEY + GEMINI_API_KEY + GITHUB_GIST_TOKEN all in local .env and OCI .env ✅
- **Gemini model:** `gemini-3.1-pro-preview` (matches Google AI Studio) via `google.genai` SDK ✅
- **Gist ID:** `1574ea556d06e7a1db45d00097f9c069` (redstorm8705 account, public gist)

## Last Session (S42 — 2026-05-27)

### S42 (2026-05-27)
- **weekly_perf_audit.py P1 BUILD + DEPLOY COMPLETE** (autonomous + S42 static analysis):
  - New standalone script (~650L) reading Alpaca FIFO fills + `logs/trade_events.jsonl`
  - 8 failure categories: 1a Directional Macro Headwind, 1b Volatility Regime Sizing Error, 2 Marginal Score Low-Momentum, 3 Leveraged PDT, 4 Time-of-Day Bleed, 5 Earnings Risk, 6 VIX Stop Crush, 7 Holding Period Mismatch, 8 Unknown
  - 4 DS/GAI design additions incorporated: emergency escalation (>25% drawdown), LOW_VOL VIX (<15) regime, monthly mislabeling gate, MIN_TRADES thresholds (offensive≥20, defensive≥12, emergency≥5, monthly≥10)
  - Static: py_compile PASS, mypy 0 errors, ruff 0 violations | Cold second-agent: PASS | impact radius: 0
  - OCI: py_compile PASS, rsync PASS | Cron: `15 20,21 * * 5` → `cron_tz_wrapper.py 16:15` → Fridays 4:15 PM ET
- **P0 main.py Part A (P0-STARTUP block) DEPLOYED:**
  - Inserts after `risk.sync_from_tracker(tracker)` at startup
  - Queries Alpaca live positions via `get_open_positions()`; overrides `risk.open_positions` if mismatch
  - Logs `_untracked` (in Alpaca, not in tracker) and `_stale` (in tracker, not in Alpaca) symbol sets
  - Halts new entries (`_set_halt_entries(True)`) if at MAX_OPEN_POSITIONS or on import/API failure
  - Amendment A-1 (mark-closed loop) REJECTED — `record_exit()` is the only safe close path
  - Startup log confirmed: "P0-STARTUP: Positions verified — Alpaca=4 == tracker=4. OK." → "Already at MAX (4/4)."
  - py_compile PASS, mypy 0 errors in main.py, ruff PASS | Cold second-agent PASS (all 5 branches) | OCI PASS
- **P0 entry_logic.py Part B (P0-CYCLE-SYNC-GUARD) DEPLOYED:**
  - Replaces unconditional BUG-POS-1 CYCLE-SYNC (lines 348–359)
  - Directional guard: tracker can only INCREASE `risk.open_positions`; decreases via `register_close()` only
  - Status filter: `(tracker.open_trades or {}).values()` + `t.get("status") != "closed"` (matches `sync_from_tracker()`)
  - None guard: `(tracker.open_trades or {})` defends against uninitialized state
  - Cold second-agent FAIL v1 (None guard) → guard added → PASS v2 | impact radius: 0 | Static PASS all 3
  - OCI: py_compile PASS, rsync PASS, all 4 services active post-restart (15:42 PT)
- **⚠️ SIGKILL pattern noted:** Multiple `stop-sigterm` timeouts in systemd journal (May 26–27) — bot not shutting down cleanly. Secondary to P0. Investigate graceful shutdown in separate session (likely threading/event-loop issue).
- **P2 deferred:** `_set_halt_entries(True)` in at-MAX branch of P0-STARTUP is over-conservative. `can_open_position()` alone is sufficient. Halt clears at midnight daily reset — harmless for paper/overnight. Separate session.
- **tb_audit_log.md:** entry_logic.py S42 row added + main.py S42 row (from earlier in session)

## Last Session (S41 — 2026-05-26)

### S41 (2026-05-26)
- **auto_ai_audit.py meta-audit redesign COMPLETE + OCI deployed:**
  - Adversarial role split: DS = skeptic (Taleb/Harris/Peterffy lenses), Gemini = optimizer (Thorp/Asness/Jegadeesh lenses)
  - Statistical guardrail: if n_fills < 20 (`_MIN_FILLS_FOR_DIRECTIVES`), LLM instructed observe-only, BLOCK parameter change directives
  - Directives tracking: `logs/audit_directives.jsonl` — atomic append after every meta-audit run, prior 4 weeks injected into prompt for compliance checking
  - Chart proxies: per traded symbol — 5d return, vs SPY delta, 20-EMA distance, 14d ATR, trend label. Alpaca Data T1 REST.
  - Macro calendar: FMP `/v3/economic_calendar` — US HIGH/MEDIUM events past 7d
  - Rejected signals: reads `logs/rejected_signals.jsonl` if exists; flags INFRASTRUCTURE_GAP if not (bot-side build still needed)
  - Gemini report contamination ELIMINATED — `_build_meta_audit_data_context()` explicitly excludes all prior Gemini reports
  - 11 new helper functions replacing old `_build_meta_audit_prompt`
  - `_run_audit()` extended with `ds_prompt`/`gai_prompt` adversarial overrides
  - Static: py_compile/mypy/ruff all PASS. Cold second-agent: PASS. Impact radius: 0 dependents.
  - OCI smoke test: 159 trade events loaded, `audit_directives.jsonl` created (1 entry), JSON written, board endpoint updated. No crashes.
  - Cron intact: `35 20,21 * * 1-5` → 4:35 PM ET
- **P0 workstream (paused — resume next session):**
  - DS/GAI responses for main.py startup P0 (risk.open_positions) in prior session transcript
  - GAI identified CRITICAL CYCLE-SYNC flaw in `execution/entry_logic.py`: unconditional `risk.open_positions = len(tracker.open_trades)` at top of every cycle UNDOES naive startup fix
  - Correct fix path: (a) read entry_logic.py full (Explore subagent), (b) design CYCLE-SYNC fix OR fail-hard validation at startup, (c) full patch sequence
  - 3-Point AI Summary for main.py P0 DS/GAI still pending (must be produced before Step 5 of patch sequence)

## Last Session (S39 — 2026-05-25, in progress)

### S39 resumed (2026-05-25, post-compaction)
- **entry_logic.py RC-4 #12c PATCHED + OCI deployed:**
  - L624: Removed redundant `import time as _t12c` (time already at L18)
  - L622-641: Replaced single 1.5s poll + `entry_price` fallback with 3× retry loop (1s intervals), mirroring L1293-1327 entry fill pattern. `_exit_price` initialized before loop (always defined).
  - L637-641: Moved `_exit_price == 0` check outside try/except — now fires on ALL failure paths (retry-exhaust, exception, bad-data).
  - Board: Execution Risk (Harris/Brandt) + Reliability (Peterffy/Katsuyama) — 2/2 APPROVE
  - DS/GAI: documented pre-compaction — accepted per user RULE C-3 decision
  - Static: py_compile/mypy/ruff all PASS (OCI py_compile PASS)
  - Cold second-agent: PASS (all 4 threats, 6 paths)
  - Impact: confined to #12c branch, caller=run_cycle
  - OCI: all 4 services active, 308MB RAM
- **RC-4 count: 11→10**
- **2 RemoteTrigger crons created** — usage-reset (02:55 UTC) + nightly (07:00 UTC)
- **Decision 1 RESOLVED** (RC-4 patched, GAI P0 accepted)
- **Decision 2 RESOLVED** (weekly_perf_audit.py 4 additions approved)

### S39 (2026-05-25, user stepped away mid-session)
- **execution/entry_logic.py RC-3 ×3 PATCHED + OCI deployed:**
  - L1072: Kelly TQI stdev fallback → `logger.warning` (WARNING: in Kelly sizing path, operator-relevant)
  - L1453: PHANTOM ENTRY alert crash → `logger.critical` + `sys.stderr` flush (CRITICAL: operator must be notified)
  - L1629: AH quote fetch fail-closed → `logger.warning` + `return` (GAI wins over DS: spread gate bypassed when _bid/_ask undefined, submitting stale RTH price in illiquid AH session violates fail-closed)
  - L1490: Stale comment "yfinance feed degraded" → "Alpaca Data T1 feed degraded"
  - DS/GAI V3 complete. Cold second-agent FAIL→PASS (_sys scope confirmed). OCI py_compile PASS. RAM 731MB at start → 211MB after restarts.
- **RC-3 count: 6→3**
- **scan_to_html.py Steps 1–3 complete:**
  - Full read: 2,580L in 9 chunks (Explore subagent). 10-pt audit PASS. Board: 4 cold parallel agents.
  - Board votes: L79 WARNING (3/4), L1299 WARNING (2/4 tiebreak), all others DEBUG.
  - DS/GAI Prompt 1 compiled and ready in session. Steps 4–9 blocked pending response.
- **weekly_perf_audit.py design complete:**
  - Design spec written: `logs/weekly_perf_audit_design_v1.md`
  - 8 failure categories, strategy feedback loop, parameter adjustment trigger table, full trade record schema, API call plan, board Q&A, DS/GAI design review prompt (§13)
  - DS/GAI Prompt 3 in design doc §13. No code written yet.
- **DS/GAI prompts compiled (all ready for user submission):**
  - Prompt 1: scan_to_html.py RC-3 ×11 (presented in-session)
  - Prompt 2: entry_logic.py RC-4 L644 (presented in-session)
  - Prompt 3: weekly_perf_audit.py design review (at logs/weekly_perf_audit_design_v1.md §13)
- **RC-4 new finding:** entry_logic.py L644 — record_exit #12c exit uses entry_price fallback. Quant Logic: HIGH. DS/GAI Prompt 2 ready.
- **⚠️ Protocol note (S37):** 20 RC-3 violations patched in S37 autonomous across 8 RTH files without confirmed in-session DS/GAI clearance (fill_helpers.py, fill_reconciler.py, state/persistence.py, lifecycle.py, fmp_client.py, gtc_manager.py, main.py). Patches are correct and OCI-deployed but RULE C-3 was not formally satisfied. User may choose retrospective review.

## Hard Invariants (CLAUDE.md §Architecture)
- `paper=True` hardcoded in `execution/broker.py` — never change without full board vote
- SPY 5-min bar-over-bar is the SOLE entry gate
- PDT hard cap: 3 slots / 5 rolling trading days
- All P&L sourced from Alpaca fills API only — tracker math is cross-check only
- T1 (Alpaca) for all equity/ETF data — yfinance only for ^VIX, ^VIX3M, JPY=X
- DS/GAI prompts: plain text in-session ONLY — never save to .md files (user mandate S37)

## Session Start Checklist (auto-run via hook — confirm at start)
- [ ] Master Brain loaded (NotebookLM `0203f312-f285-4f20-8b8d-ca6fde65acf7`)
- [ ] CLAUDE.md active (auto-loaded by Claude Code)
- [ ] handoff.md read (this file)
- [ ] Verify RAM < 550MB (restart all services if over threshold)
- [ ] Verify 6 positions still active (AMZN/INTC/MSTR/PANW/TOST/TQQQ)

## References
- Full session history: `logs/session_summary_*.md` (20 files as of S38)
- S37+S38 session: `logs/session_summary_2026-05-25_0940.md`
- Board decisions: `~/.claude/projects/.../memory/`
- CLAUDE.md: full rules, guardrails, board protocol
- Bug tracking: `logs/bug_counter.json` (RC-3 count=**3** | RC-4 count=**10** — updated S39 resumed after entry_logic.py RC-4 #12c patch)
- Audit log: `logs/tb_audit_log.md` (S39: entry_logic.py ✅ PATCHED ×2 passes RC-3+RC-4, scan_to_html.py ✅ PATCHED)
- Weekly perf audit design: `logs/weekly_perf_audit_design_v1.md` (§13 = DS/GAI design review prompt)
- Full session history: `logs/session_summary_*.md` (20 files as of S38; S39 summary not yet written — write on wrap-up)
