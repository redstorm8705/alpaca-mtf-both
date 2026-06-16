# Handoff — alpaca-mtf-bot
**Updated:** 2026-06-16 S60

## ⚠️ THIS SESSION WAS REMOTE CLOUD SANDBOX — OCI/DEPLOY STATUS UNVERIFIED
S59 and S60 (this entry) ran in a remote cloud sandbox (`/home/user/alpaca-mtf-both`,
GitHub repo `redstorm8705/alpaca-mtf-both`, branch `claude/ds-audit-bv5-patches-dqqaqm`)
with **no SSH/OCI access and no `.env` access**. All fields below marked "last confirmed
S58" are STALE — the next session with OCI/Mac access must re-verify before trusting them.
Code commits below ARE real and on the remote branch — they just haven't been confirmed
pulled+running on OCI yet.

## Bot Status (fields below last CONFIRMED at S58 — re-verify on OCI access)
- **Running:** last confirmed YES — OCI Phoenix `129.153.208.32` | all 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32` (Ed25519 key)
- **Bot CWD on OCI:** `/home/ubuntu/mtf-bot/` — always rsync to this path
- **Account:** Paper ~$2,500 start → **$2,813.82** equity | $2,932.64 cash (S58 snapshot — stale)
- **Profile:** paper | MIN_SCORE=9/12 (now **adaptive**, see S59 below) | KELLY_FRACTION=0.35 | KELLY_MAX_RISK_PCT=4.5% | INTRADAY_STOP_ATR_MULT=1.20 | TARGET=2.5x | VOL_TIER_HIGH_STOP=2.0
- **OCI git:** last CONFIRMED commit `9e6b4e7` (S58). **NOT YET VERIFIED on OCI:** `da13ad7`, `3087360`, `ad9c239`, `216192e`, `231d093`, `5fbad86`, `b4f09af` (S59 — adaptive MIN_SCORE floor + Invariant #10 correlation gate), `b06fcd1`, `88ef84c` (S60 — tca_logger.py draft, not wired). **Run the OCI verification command below before doing anything else next session.**
- **Dashboard:** `http://129.153.208.32:8080/dashboard.html`
- **PDT:** REMOVED — all PDT code eliminated from entire codebase (Tier 2 complete S55)
- **RAM:** 251MB used / 559MB avail (S58 snapshot — stale, healthy as of then)

## Open Positions (live as of S55 end)
| Symbol | Side | Shares | Entry | Unrealized |
|--------|------|--------|-------|------------|
| TOST | short | 2 | $24.40 | +$0.32 |
| UBER | short | 1 | $70.46 | +$0.12 |

Note: NFLX position from S54 is closed.

## Open Items (require action next session)

### P0 — IMMEDIATE NEXT ACTION (S60, this session)
- [ ] **tca_logger.py — Step 4 (DS/GAI external audit)**: file is built, Steps 1-3 complete
  and clean (full read, 10-point audit + RC scan, board vote 2-1 APPROVE — see
  `logs/tb_audit_log.md` "Item #6" section). **Blocked in remote sandbox — needs
  `.env` access for `DEEPSEEK_API_KEY`/`GEMINI_API_KEY`.** Run DS/GAI MODE 1 (patch
  validation persona) with the exact same prompt used for the 10-point audit (see
  `logs/pending_approvals_2026-06-16.md` for full context), then continue Steps 5-9
  and wire into `execution/entry_logic.py` + `execution/exit_logic.py` per RULE C-6
  (each file's full Steps 1-9 independently).
- [ ] **Item #7 — bar-end adverse selection**: design locked (post-hoc analysis script,
  no runtime hook, no DS/GAI gate needed per RULE C-5) but NOT YET STARTED. See
  `logs/pending_approvals_2026-06-16.md`.
- [ ] **OCI verification**: `ssh oci 'cd ~/alpaca-mtf-bot_FINAL && git pull && git log --oneline -5'`
  — confirm S59+S60 commits (see Bot Status above) are pulled and services are healthy.

### P1 — Known CRITICAL Bug (deferred, needs DS/GAI)
- [ ] **trade_engine.py L252-254**: Direct `risk.open_positions = len(...)` assignment instead of `risk.register_open()` — bypasses CYCLE-SYNC-GUARD monotonic-UP constraint from S42. Fires every RTH cycle when pending overnight entries exist. (STALE per S58c queue sweep note below — re-verify before acting.)

### P1 — Pending Approvals
- `logs/pending_approvals_2026-06-16.md` — **current** — items #6 (TCA, blocked Step 4) and #7 (adverse selection, not started)
- `logs/pending_approvals_2026-06-15.md` — item #4 (correlation gate) now DEPLOYED (S59, see below) — rest of file's items #1/#3/#5 closed/deferred per that file's table
- `logs/pending_approvals_2026-06-07.md` — **STALE**, both items resolved per S58c queue sweep (#1 RC-8 CLOSED, #3/#4 closed-stale) — do not re-action without re-verifying first

### P1 — RC Bug Fixes (unlocalized)
- [ ] **RC-4**: 7 remaining violations (3 fixed in exit_logic.py S55; 7 remain in unknown files)
- [ ] **RC-3**: 1 remaining violation (unlocalized)
- [ ] **RC-2**: 7 violations — run_cycle.py, entry_logic.py
- [ ] **RC-7**: 2 violations — main.py

### P2 — Post-Market Pipeline
- [x] ~~**Bridge missing**~~ ✅ FIXED S56 — `autonomous_patch_generator.py` built and deployed
- [x] ~~**alerts.py PDT cleanup**~~ ✅ Already done S54 (commit `312089c`)

## Last Session (2026-06-16 S60 — remote sandbox, this entry)

### tca_logger.py — Item #6 (TCA Execution Quality) — IN PROGRESS, blocked
- New file `tca_logger.py` (repo root, 133 lines): observational entry/exit slippage
  logger, mirrors `trade_logger.py` pattern, writes `logs/tca_metrics.jsonl`. Zero
  control-flow impact — caller passes already-confirmed fill prices in.
- Steps 1-3 of Mandatory Patch Sequence COMPLETE and clean: full read (133 lines),
  10-point audit + RC-1–8 scan (all PASS, sign/direction logic verified correct on
  all 8 long/short × entry/exit cases), board vote (Harris/McKinney/Beck) 2-1 APPROVE
  for paper trading. Full detail: `logs/tb_audit_log.md` "Item #6" section.
- Step 4 (DS/GAI) BLOCKED — remote sandbox has no `.env`/API key access. Rafael chose
  to defer to next Mac/OCI session rather than paste keys or skip the gate.
- Committed as inert draft (`88ef84c`) — confirmed zero callers anywhere in the
  codebase, so committing has no RTH impact. NOT wired into entry_logic.py/exit_logic.py.
- Planned integration sites already documented (see `logs/pending_approvals_2026-06-16.md`):
  entry_logic.py after the fill-confirmation poll loop (~L1249-1282); exit_logic.py at
  each of the 9 `record_exit()` sites, gated on `not trade.get("_fill_unverified")`.

### Item #7 (bar-end adverse selection) — design locked, not started
- User-approved mechanism: standalone post-hoc analysis script (no runtime hook into
  entry_logic.py/exit_logic.py) — reads `trade_events.jsonl` + retroactively calls
  `fetch_bars()`. Avoids DS/GAI gate per RULE C-5 (not RTH-imported). Nothing built yet.

### Item #5 (Alpha Decay/Walk-Forward) — explicitly out of scope this session
- Remains BLOCKED — shadow 16-pt log lives on OCI only, not accessible from remote sandbox.

## Prior Session (2026-06-15 S59)

### Adaptive MIN_SCORE floor ✅ (commit `da13ad7`)
- `entry_logic.py` patched — MIN_SCORE entry threshold now scales with MRI regime
  instead of a fixed 9/12. Full sequence (Steps 1-9) complete, DS/GAI + board aligned,
  deployed. Audit log updated (`3087360`).

### Portfolio Correlation Aggregator — Invariant #10 ✅ DEPLOYED (commits `216192e`, `231d093`, `5fbad86`, `b4f09af`)
- New module `risk/correlation_matrix.py`: `would_breach_correlation_limit()` — 60-day
  rolling Spearman rank correlation, fail-CLOSED on data unavailability, directional-aware
  (only blocks same-direction pairs), threshold 0.7, max 2 correlated pairs per
  Invariant #10. Wired into `execute_entries()` in `entry_logic.py` (after sector gate,
  before position count check).
- Full Steps 1-9 complete: board vote, DS/GAI (one DS↔GAI split resolved via tie-breaker
  counter-prompt — Finding 6 pair-counting interpretation reached 3/3 consensus after
  counter-prompt), static analysis clean, cold second-agent PASS, impact radius confirmed
  (`execute_entries()` only caller is `strategy/run_cycle.py:1490`).
- Cosmetic fix bundled: BoD-1 confirmation gate log string now correctly shows
  `_adaptive_min_score` instead of stale `CONVICTION_SKIP_BELOW` reference.
- Phase 2 items logged (not blockers): pre-scan caching of daily returns, pre-scan
  open_positions snapshot to eliminate intra-cycle staleness, 20-day Pearson eval
  alongside 60-day Spearman.

### Item #3 (Breadth → MRI) — CLOSED, no action needed
- Board reviewed: current design (breadth not wired into MRI) is correct as-is —
  10-min breadth refresh vs 24h MRI window is a temporal mismatch that would corrupt
  the signal if wired in naively. REJECT on wiring it in; no further action.

## Last Session (2026-06-11 S58b — same day continuation)

### Pipeline pre-flight + Slack webhook fix ✅ (commit 4d2036f)
- All 8 pre-flight checks passed for tonight's first end-to-end autonomous run (DS+GAI APIs live from OCI, compile clean, git clean, 2 directives staged).
- Found+fixed: generator + review read env key SLACK_WEBHOOK but .env has SLACK_WEBHOOK_URL — all Stage 1.5/2 Slack posts were silently dead. Both accept either key now. Live Slack test: HTTP 200.
- One-time scheduled session 5:00 PM PT today verifies tonight's run (read-only).

### Kelly data hygiene fix ✅ (commit 63264b0)
- kelly.rebuild_from_trades() now excludes _fill_unverified trades (mirrors get_stats S47 guard). The "P&L=0.0 corrupts stats" audit finding traced here — record_exit/get_stats were already clean (live-data audit: 0 corrupt $0 external_close trades in 81 closed). Board Peterffy+LdP, DS+GAI APPROVE. Deployed, services healthy.
- Deferred P3: 0R-trades counted as losses in Kelly vs excluded in get_stats — definitional, board vote needed.

### GTC stop churn fix ✅ (commit 1639e91 — S58c)
- orphan_manager.py: closed-phase restarts now ADOPT matching GTC stops instead of cancel+resubmit. Verified live: TOST/UBER adopted, zero churn. Stops placed once, survive restarts. First use of the DS/GAI tie-breaker protocol (GAI REJECT overruled board 3-0; side check still incorporated).
- P3 queued: reorder reconcile_positions() before GTC reconciliation (Minsky).

### Queue sweep S58c (afternoon) ✅
- **trade_engine.py CRITICAL — STALE, CLOSED** (fixed 4f58c85/S47d; register_open pattern confirmed by full read).
- **run_movers.py P5-C2 — FIXED** (60bf9ee): reset_day→reset_daily(sod_equity); lxml installed in OCI venv.
- **RC-8 — CLOSED (0)**: 9 sites were already applied (b2e61f7, 6/8); DS+GAI retracted IO rejection via tie-breaker counter-prompt. pending_approvals #1 resolved.
- **pending #3 (QHM GTC) — STALE, CLOSED**: _qhm_protected check already implemented in orphan_manager.
- **pending #4 (exit_logic PDT refs) — STALE, CLOSED**: mypy clean, 0 refs (S55 Tier 2 did it). NOTE: the June-7 RC-3 diff for exit_logic L1996 is now unblocked but its DS/GAI gates EXPIRED (RULE C-2) — needs fresh full sequence; also re-verify whether that instance still exists (RC-3 counter currently 0; possible undercount).
- **RAM**: rss_trend.csv sampler live (10-min cron). Chain should analyze the curve after ≥6h data and propose the leak fix.
- **pending #2 (RC-4 fallback strategy)**: Open Question Protocol run — DS=A, GAI=A, McKinney=A, Thorp=A-leaning. Awaiting Rafael's choice; on approval, full sequence on exit_logic.py implementing Option A at L1345/L1939/L2032.

### Next queue (for chain / next session)
1. RC-4 Option A implementation in exit_logic.py (after Rafael approves strategy) — full sequence + DS/GAI
2. RC-2 ×7 (run_cycle.py, entry_logic.py) — full sequence
3. RC-7 ×2 (main.py, hotspot) — full sequence + DS/GAI
4. RAM leak analysis from rss_trend.csv
5. MRI-HALT buffer-clear follow-up; reconcile_positions-before-GTC reorder (P3, board vote)

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

## RC Bug Counts (live as of 2026-06-15 S59 — matches CLAUDE.md + bug_counter.json; S58 table below was stale)
| RC | Count | Status | Top File(s) |
|----|-------|--------|-------------|
| RC-1 Naive datetime | **0** | CLOSED (all 16 fixed 2026-04-28) | — |
| RC-2 CWD-relative path | **0** | CLOSED (kelly.py 4/18, run_cycle.py 5/3, confirmed S58c) | — |
| RC-3 Silent exception | **0** | CLOSED (last instance S58: autonomous_patch_generator.py L67) | — |
| RC-4 Estimated exit price | **≤4** | OPEN — upper bound; confirmed unaudited: portfolio_tracker.py L1200/L1753, run_cycle.py L583 | portfolio_tracker.py, run_cycle.py |
| RC-5 Non-atomic write | **1** | OPEN — manual_audit.jsonl append, low risk | portfolio_tracker.py L1711 |
| RC-6 Wrong API field name | **0** | CLOSED (3 historical patches confirmed applied) | — |
| RC-7 Zero-share sizing | **0** | CLOSED (guard confirmed entry_logic.py L1127-1190) | — |
| RC-8 Unbounded scan buffer | **0** | CLOSED (9 sites b2e61f7 + L663 bonus, DS/GAI IO objection retracted) | — |

**Top hotspot files (patch count, S59):** `execution/portfolio_tracker.py` (36, CRITICAL — RC-4/RC-5 open), `main.py` (33, CRITICAL — no open RC items), `execution/exit_logic.py` (9, HIGH — RC-4 confirmed fixed), `execution/entry_logic.py` (3, HIGH — RC-7/RC-8 closed), `strategy/run_cycle.py` (9, MEDIUM — RC-4 unaudited L583).

## Rafael's Role — CHAIRMAN / CEO (Mandate 2026-06-14)
Rafael sees proposals ONLY when board + DS + GAI are fully aligned. Every approval package delivered in plain English with a real stock example. "Let's do it" = apply immediately. See CLAUDE.md §RAFAEL'S ROLE for full format. Board/DS/GAI handle all technical deliberation autonomously — Rafael decides, not deliberates.

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
- [ ] Verify branch: `git fetch origin claude/ds-audit-bv5-patches-dqqaqm && git checkout claude/ds-audit-bv5-patches-dqqaqm && git pull` (repo `redstorm8705/alpaca-mtf-both`) — confirm latest commit is `88ef84c` or later
- [ ] Run OCI verification: `ssh oci 'cd ~/alpaca-mtf-bot_FINAL && git pull && git log --oneline -5'` — confirm S59+S60 commits pulled, services healthy, RAM normal
- [ ] Check `logs/pending_approvals_2026-06-16.md` (current) for item #6 (TCA, Step 4 blocked) and #7 (adverse selection, not started)
- [ ] Confirm next action: **P0 — tca_logger.py Step 4 (DS/GAI)** — file ready, audit/board clean, only blocked on `.env` access (now available on Mac/OCI session)

## References
- Pending approvals: `logs/pending_approvals_2026-06-16.md` (current), `logs/pending_approvals_2026-06-15.md` (item #4 now deployed), `logs/pending_approvals_2026-06-07.md` (stale)
- Session history: `logs/session_summary_*.md`
- Audit log: `logs/tb_audit_log.md` (see "Item #6" section, end of file, for tca_logger.py status)
- Bug counts: `logs/bug_counter.json` (last_updated: 2026-06-15 S59)
- CLAUDE.md: full rules, guardrails, board protocol, RC live counts
- New file this session: `tca_logger.py` (repo root, draft, uncalled)
- Master Brain: `0203f312-f285-4f20-8b8d-ca6fde65acf7`
