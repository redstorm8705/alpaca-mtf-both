# Handoff — alpaca-mtf-bot
**Updated:** 2026-05-25 S40 | **S40: auto_ai_audit.py BUILT (patch-gate + meta-audit + board CCR); GitHub repo live; DeepSeek API wired; gemini-3.1-pro-preview; full autonomous audit pipeline deployed; weekly_perf_audit.py still next P1**

## Bot Status
- **Running:** YES — OCI Phoenix `129.153.208.32` | all 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32` (Ed25519 key)
- **Bot CWD on OCI:** `/home/ubuntu/mtf-bot/` — always rsync to this path (NOT alpaca-mtf-bot_FINAL)
- **Account:** Paper | equity **$2,852.68** (confirmed S37 MCP) | All-time P&L +$442.38 (Alpaca-authoritative, confirmed S29) | MIN_SCORE=10/12 | KELLY_FRACTION=0.25 | KELLY_MAX_RISK_PCT=6% | MAX_PORTFOLIO_RISK_PCT=4%
- **Dashboard:** `http://129.153.208.32:8080/dashboard.html`
- **RAM (S39):** Started at 731MB (threshold exceeded → auto-restarted → 301MB → 211MB after entry_logic.py patch restart). Monitor at each session start. Alert threshold: 550MB → restart all services. Second 731MB peak this cycle (S37 also peaked at 750MB) — RAM leak investigation P2.
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

### Open Items

- [ ] **P1: weekly_perf_audit.py build** — Design spec at `logs/weekly_perf_audit_design_v1.md`. DS/GAI design review COMPLETE (both APPROVE). **4 DS/GAI additions APPROVED** — must be incorporated into spec before coding: (1) Emergency Escalation Clause >25% drawdown, (2) VIX floor <15=LOW_VOL, (3) monthly 10-trade mislabeling gate, (4) MIN_TRADES thresholds. NOT in RTH chain → no DS/GAI gate. RTH block + atomic write required. Cron: Friday 4:15 PM ET. UNBLOCKED — ready to build.
- [ ] **P1: weekly_perf_audit.py design spec update** — Add 4 DS/GAI additions to `logs/weekly_perf_audit_design_v1.md` before build begins (separate step from coding).
- [ ] **P2: RC-9 in scan_to_html.py** — `_fetch_yfinance_news()` uses yfinance for news data (T4 violation). Board vote + migration plan required.
- [ ] **P2: BUG-C structural fix** — write_scan_html background thread. Interim 10-min throttle deployed S31. Structural fix deadline 2026-06-30.
- [ ] **P2: RAM leak investigation** — 731MB at S39 start (second occurrence after S37's 750MB). Restarted → 211MB. alerts.py GTC retry accumulation suspected (DS S32 audit). Dedicated debug session needed.
- [ ] **P3: options_scanner.py** — BUG-0DTE-FALLBACK (HIGH, L400-412); RC-5 L1081. `⚠️ AUDITED — PATCH PENDING DS+GAI`. Needs fresh full read (1,946L, Explore subagent) + DS/GAI prompt before patching (prior audit 2026-04-30, all gates reset per RULE C-2).

## Autonomous Crons / CCRs

### OCI Cron (crontab on 129.153.208.32)
- **Midday audit:** `30 17,18 * * 1-5` via cron_tz_wrapper → 1:30 PM ET → `midday_audit.py` → `logs/midday_gemini_*.txt` + Slack
- **Nightly audit:** `5 20,21 * * 1-5` via cron_tz_wrapper → 4:05 PM ET → `nightly_audit.py` → `logs/gemini_audit_*.txt` + Slack
- **Meta-audit:** `35 20,21 * * 1-5` via cron_tz_wrapper → 4:35 PM ET → `auto_ai_audit.py --meta-audit` → DS+GAI cross-review → `meta_audit_latest.json` → Slack (added S40)

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
| 5:30 PM | Board CCR (4 agents: Harris/Brandt, Thorp/Asness, Peterffy/Katsuyama, McKinney) | Board verdict in CCR transcript |

### Infrastructure Notes
- **GitHub repo:** `https://github.com/redstorm8705/alpaca-mtf-both` (private) — CCR agents clone from here
- **Board endpoint:** `http://129.153.208.32:8080/meta_audit_latest.json` — nginx serves from `/var/www/mtf-bot/` (no auth)
- **DS/GAI keys:** DEEPSEEK_API_KEY + GEMINI_API_KEY both in local .env and OCI .env ✅
- **Gemini model:** `gemini-3.1-pro-preview` (matches Google AI Studio) via `google.genai` SDK ✅

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
