# Handoff — alpaca-mtf-bot
**Updated:** 2026-06-11 S58

## Bot Status
- **Running:** YES — OCI Phoenix `129.153.208.32` | all 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32` (Ed25519 key)
- **Bot CWD on OCI:** `/home/ubuntu/mtf-bot/` — always rsync to this path
- **Account:** Paper ~$2,500 start → **$2,813.82** equity | $2,932.64 cash
- **Profile:** paper | MIN_SCORE=9/12 | KELLY_FRACTION=0.35 | KELLY_MAX_RISK_PCT=4.5% | INTRADAY_STOP_ATR_MULT=1.20 | TARGET=2.5x | **VOL_TIER_HIGH_STOP=2.0** (was 1.75, S57)
- **OCI git:** ✅ latest commit `9e6b4e7` deployed and running
- **Dashboard:** `http://129.153.208.32:8080/dashboard.html`
- **PDT:** REMOVED — all PDT code eliminated from entire codebase (Tier 2 complete S55)
- **RAM:** 251MB used / 559MB avail (healthy — improved from S54 713MB)

## Open Positions (live as of S55 end)
| Symbol | Side | Shares | Entry | Unrealized |
|--------|------|--------|-------|------------|
| TOST | short | 2 | $24.40 | +$0.32 |
| UBER | short | 1 | $70.46 | +$0.12 |

Note: NFLX position from S54 is closed.

## Open Items (require action next session)

### P1 — Known CRITICAL Bug (deferred, needs DS/GAI)
- [ ] **trade_engine.py L252-254**: Direct `risk.open_positions = len(...)` assignment instead of `risk.register_open()` — bypasses CYCLE-SYNC-GUARD monotonic-UP constraint from S42. Fires every RTH cycle when pending overnight entries exist.

### P1 — Pending Approvals (logs/pending_approvals_2026-06-07.md)
- [ ] **#1** — RC-8: 9 missing `_rc8_clear_buffers()` in entry_logic.py (Board APPROVE, DS/GAI deadlocked)
- [ ] **#2** — RC-4: exit_logic.py L1345/L1939/L2032 — strategy decision on fallback price source

### P1 — RC Bug Fixes (unlocalized)
- [ ] **RC-4**: 7 remaining violations (3 fixed in exit_logic.py S55; 7 remain in unknown files)
- [ ] **RC-3**: 1 remaining violation (unlocalized)
- [ ] **RC-2**: 7 violations — run_cycle.py, entry_logic.py
- [ ] **RC-7**: 2 violations — main.py

### P2 — Post-Market Pipeline
- [x] ~~**Bridge missing**~~ ✅ FIXED S56 — `autonomous_patch_generator.py` built and deployed
- [x] ~~**alerts.py PDT cleanup**~~ ✅ Already done S54 (commit `312089c`)

## Last Session (2026-06-11 S58b — same day continuation)

### Pipeline pre-flight + Slack webhook fix ✅ (commit 4d2036f)
- All 8 pre-flight checks passed for tonight's first end-to-end autonomous run (DS+GAI APIs live from OCI, compile clean, git clean, 2 directives staged).
- Found+fixed: generator + review read env key SLACK_WEBHOOK but .env has SLACK_WEBHOOK_URL — all Stage 1.5/2 Slack posts were silently dead. Both accept either key now. Live Slack test: HTTP 200.
- One-time scheduled session 5:00 PM PT today verifies tonight's run (read-only).

### Kelly data hygiene fix ✅ (commit 63264b0)
- kelly.rebuild_from_trades() now excludes _fill_unverified trades (mirrors get_stats S47 guard). The "P&L=0.0 corrupts stats" audit finding traced here — record_exit/get_stats were already clean (live-data audit: 0 corrupt $0 external_close trades in 81 closed). Board Peterffy+LdP, DS+GAI APPROVE. Deployed, services healthy.
- Deferred P3: 0R-trades counted as losses in Kelly vs excluded in get_stats — definitional, board vote needed.

### Scheduled automation (new)
- **five-hour-work-resumption** cron (every 5h, local scheduled task): resumes in-progress work per HANDOFF.md/tb_audit_log.md, else works non-RTH queue. Proposes patches to logs/pending_claude_session_*.md — never applies without Rafael's approval.
- **verify-pipeline-first-run** one-time 5 PM PT 2026-06-11.

### NOTHING IN PROGRESS — next priorities (for 5h cron / next session)
1. trade_engine.py L252-254 CYCLE-SYNC-GUARD bypass — CRITICAL, full sequence + DS/GAI
2. RAM pressure on 1GB OCI box (repeated off-hours CRITICAL restarts, 12-min RTH hang 6/9)
3. run_movers.py ImportError (P5-C2)
4. RC-2 ×7 (run_cycle.py, entry_logic.py), RC-7 ×2 (main.py)
5. Review tonight's pipeline outputs (pending_approvals_2026-06-11.md expected)

## Prior (2026-06-11 S58)

### kelly.py negative-Kelly fallback ✅ (commit a7f2a89)
- `return 0.0` → `return config.KELLY_MIN_RISK_PCT` — short_intraday entries re-enabled at floor sizing (~0.5% effective risk after hard notional cap). Board 5-0, DS APPROVE R2, GAI APPROVE R3. Deployed, services healthy.

### Autonomous pipeline repair ✅ (commits 1830fe9, 2c4552d)
- **Break 1+4 FIXED**: auto_ai_audit.py now emits structured pending_review directives (DS/GAI JSON section 6 + deterministic pipe-parse of midday/nightly Gemini NEW BUGS rows). Generator: tri-state status machine, silent-failure Slack alert, RC-3 _log fix.
- **Break 2 FIXED**: OCI git was frozen at S54 (3b9f8ac) — reset --hard origin/main, clean pulls verified. Deploy tracked files via git pull, NOT rsync-then-pull.
- **Break 3 FIXED**: auto_deploy cron 00:00→03:00 UTC (was outside its own 10 PM–6 AM ET gate every night since 6/3).
- 13 legacy blob directives migrated to context_only. Live: 2 pending_review seeded (autonomous_review.py co-author attribution; config.py BUCKET_B_MAX_POSITIONS) — generator runs tonight 23:00 UTC.
- RC-3 → **0 CLOSED** (last instance was autonomous_patch_generator._log).

### Queued from Gemini audits (not yet started — need full sequence)
- P&L=0.0 recording when Alpaca returns no fills (portfolio_tracker — corrupts Kelly stats) — HIGH
- TQI degradation / avg R -0.06 alpha review (re-evaluate after 2.0x-stop + Kelly-floor data accumulates)
- RAM pressure on 1GB OCI box (repeated CRITICAL restarts, 12-min RTH hang 6/9, gc 3-7s)
- run_movers.py ImportError (P5-C2) | BUCKET_B_MAX_POSITIONS=999 placeholder

## Prior Session (2026-06-10 S57)

### Stop multiplier tuning — VOL_TIER_HIGH_STOP_INTRADAY ✅
- **config.py** (commit `9e6b4e7`): `VOL_TIER_HIGH_STOP_INTRADAY` 1.75 → 2.0
- Motivation: live trade evidence showed SMCI/AMD/PANW/MU stopping at +0.8-0.9% while running +3.9-7.3% ($2,082 left on table). 1.75x ATR was inside noise band for HIGH-tier (60-70% rvol) names.
- R:R preserved (target scales proportionally to 4.167x ATR). Dollar risk per trade unchanged (inverse ATR sizing). Leveraged ETF guard unaffected.
- Board 18/19 APPROVE, DS APPROVE, GAI APPROVE (Round 2). All services restarted and healthy on OCI.
- **P2 roadmap (deferred):** Hybrid rvol classifier (60% 20d daily + 40% 5d intraday range) — addresses rvol_20d daily-bar estimator bias flagged by McKinney/Derman/LdP. Board vote required before implementation.
- **Kelly re-calibration note:** At 30-trade mark post-S57, re-evaluate KELLY_FRACTION=0.35 (avg win shifts with wider stops).

## Prior Session (2026-06-10 S56)

### Post-Market Pipeline — Stage 1.5 complete ✅
- **autonomous_review.py** (commit `fd4df2b`): Gemini model fixed (`gemini-3.1-pro-preview` → `gemini-2.5-flash`), `_GEMINI_MAX_TOKENS=16384` added to SDK + REST paths, verdict detection window [:200]→[:500]
- **autonomous_patch_generator.py** (commit `2f86d30`): New Stage 1.5 script. Reads `audit_directives.jsonl`, runs 3-agent board vote + diff generation via DS API, static analysis, cold second-agent, writes `pending_ds_gai_*.json` + `.patch`. OCI cron: `0 23 * * *` (6 PM ET). rsynced and live on OCI.

## Hard Invariants (CLAUDE.md §Architecture)
- `paper=True` hardcoded in `execution/broker.py` — NEVER change without full board vote
- SPY 5-min bar-over-bar is the SOLE entry gate
- Kill switch: 7% paper (board 25-1 Apr 2026, confirmed S50 13-0)
- All P&L sourced from Alpaca fills API only — tracker math is cross-check only
- T1 (Alpaca) for all equity/ETF data — yfinance only for ^VIX, ^VIX3M, JPY=X
- PDT REMOVED per SEC/FINRA rule amendment, board S50 28-0 — NO PDT CODE ANYWHERE
- BUCKET_B_MAX_POSITIONS = 999 (unlimited; MAX_OPEN_POSITIONS=4 is the operative cap)

## RC Bug Counts (live after S55)
| RC | Count | Top File |
|----|-------|---------|
| RC-4 Estimated exit price | **7** | unlocalized (exit_logic.py fixed) |
| RC-2 CWD-relative path | **7** | run_cycle.py, entry_logic.py |
| RC-3 Silent exception | **1** | unlocalized |
| RC-7 Zero-share sizing | **2** | main.py |
| RC-5 Non-atomic write | **1** | portfolio_tracker.py |
| RC-6 Wrong API field | **3** | portfolio_tracker.py |
| RC-8 Unbounded scan buffer | **1** | entry_logic.py (9 sites, deadlocked) |
| RC-1 Naive datetime | **0** | CLOSED |

## Full Read Gate — ZERO TOLERANCE
Every file requires FULL read before ANY analysis. Files >1,000 lines → Read tool in ≤300-line chunks. Declare "Full read complete: N lines" before findings. No grep/search/partial reads as file exploration — EVER.

## DS/GAI Protocol
- Direct API only — browser automation CONFIRMED BROKEN
- DeepSeek: `curl https://api.deepseek.com/v1/chat/completions` model=`deepseek-chat`
- Gemini: `curl .../gemini-2.5-flash:generateContent` maxOutputTokens=8192
- Keys in `/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.env`
- Same prompt to both; board is PRIMARY; DS/GAI supplement only
- Disagreement with board: counter-prompt up to 3 rounds; escalate to Rafael after round 3

## Session Start Checklist
- [ ] Read this handoff.md first (blocking)
- [ ] Query Master Brain: `notebooklm use 0203f312-f285-4f20-8b8d-ca6fde65acf7`
- [ ] Check pending_approvals_2026-06-07.md for items #1-#2
- [ ] Verify OCI services active + RAM usage
- [ ] Confirm next action: P1 — trade_engine.py L252-254 CRITICAL bug (Steps 1-9 + DS/GAI)

## References
- Pending approvals: `logs/pending_approvals_2026-06-07.md`
- Session history: `logs/session_summary_*.md`
- Audit log: `logs/tb_audit_log.md`
- Bug counts: `logs/bug_counter.json`
- CLAUDE.md: full rules, guardrails, board protocol, RC live counts
- Master Brain: `0203f312-f285-4f20-8b8d-ca6fde65acf7`
