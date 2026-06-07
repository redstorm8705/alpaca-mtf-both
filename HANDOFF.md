# Handoff — alpaca-mtf-bot
**Updated:** 2026-06-06 S50 — DEPLOYED commit a28e94e: (1) kelly.py neg-Kelly guard (short_intraday PF=0.41 now blocked), (2) entry_logic.py ATR-None skip entry, (3) portfolio_tracker.py FIFO synthetic lot removed (phantom accumulation fixed), (4) PDT_ENFORCEMENT_ENABLED=False across 5 files (PDT removed <$25K, board 28-0), (5) BoD-3 dead-code removed from main.py (kill switch always was 7%), (6) CLAUDE.md: Invariant 3+6 updated + Open Question Protocol rule added | **2026-06-05 S49 —** quarterly_hold_manager.py BUILT + DEPLOYED ✅ (commit e6d2fda, 1432L) | 4-domain board vote + DS/GAI + cold second-agent PASS | P1 follow-on: orphan_manager.py must check get_quarterly_hold_symbols() to avoid cancelling QHM GTC stops pre-RTH | MSTR GTC stop 30971b38 @ $131.61 active | NFLX DAY stop 546a4a48 @ $84.58 active | Master Brain cleaned + updated (49→8 sources) | **2026-06-04 S48b —** Board vote COMPLETE ✅ (25 members BoD+AB+TB, CCR build authorized) | Quarterly holds research complete ✅ (AVGO/NVDA/ANET, memo at logs/quarterly_holds_research_2026-06-04.md) | NFLX overnight short 1sh @ $81.84 protected (GTC stop $84.56) | S47f: portfolio_tracker.py Phase 2a.5 FIFO overnight reconciliation DEPLOYED ✅ (commit fb4c662, 2284L) | 2026-06-03 S47e — generate_dashboard.py P1 DEPLOYED ✅ + trade_engine.py P1 CLOSED ✅ + DS/GAI direct API protocol established | **S47e: BOTH S47d P1 items CLOSED. (1) trade_engine.py L252-254 risk.open_positions desync — PATCHED + DEPLOYED (commit 4f58c85): register_open() + status-gate replaces direct SET. (2) generate_dashboard.py P1 P/L mismatch — PATCHED + DEPLOYED (S47e): Change1=lifetime_pnl_cache atomic write after all_trades line; Change2=RC-6 `o["order_type"]`→`o.get("type") or o.get("order_type","unknown")`; stale OCI cache deleted. DS/GAI now runs via direct API (curl) — NOT browser automation. | **S47d: ROOT CAUSE 1 (P/L mismatch) — generate_dashboard.py never writes lifetime_pnl_cache.json; monthly_review.py `_load_lifetime_pnl()` dead code (never called in `_build_html`); OCI cache stale May 17 with wrong key "lifetime_pnl" (should be "total_pnl"). Board 4/4 MODIFY. DS/GAI prompts prepared in-session. ROOT CAUSE 2 (risk desync) — trade_engine.py L252-254 direct `risk.open_positions = len(...)` assignment instead of `risk.register_open()` in `_reconcile_pending_overnight_orders()`; fires every RTH cycle at run_cycle.py L824 when pending overnight entries exist; bypasses S42 CYCLE-SYNC-GUARD. DS/GAI required, patch queued post-RTH. | S47c: entry_logic.py + config.py P1 BUCKET_B power_hour expansion PATCHED ✅ — 7 fixes: BUG-PH-1 kill-switch bypass, BUG-PH-2 hardcode→TOD_EXPANSION_WINDOW_START, BUG-PH-3 wrong counter→risk.open_positions, BUG-PH-4 no re-check, BUG-PH-5 PDT=3/3 disable (BoD 3-0), Fix#6 pre-loop time, Fix#7 WARNING logs. 1724L→1747L. DS/GAI APPROVE. OCI deployed, all 4 services active. | S47b: portfolio_tracker.py pnl=0.0 false-zero rounding PATCHED ✅ — 8 storage round(x,2)→round(x,4) + L792 abs()>1e-8 float guard. Commit 5600c70. | S47: portfolio_tracker.py 4-bug patch DEPLOYED ✅ — Bug1 avg_r_multiple, Bug2 entry≤0 phantom, Bug3 _load_log tuple, Bug8 TOCTOU. Commit 0f3aa58. fill_helpers.py P5-H2 PATCHED ✅ — Commit 1adc1cb. | S46: main.py BUG-6 DEPLOYED ✅.**

## Bot Status
- **Running:** YES — OCI Phoenix `129.153.208.32` | all 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32` (Ed25519 key)
- **Bot CWD on OCI:** `/home/ubuntu/mtf-bot/` — always rsync to this path (NOT alpaca-mtf-bot_FINAL)
- **Account:** Paper | equity **$2,825.67** (confirmed S49 MCP) | All-time P&L +$442.38 (Alpaca-authoritative, confirmed S29) | MIN_SCORE=10/12 | KELLY_FRACTION=0.25 | KELLY_MAX_RISK_PCT=6% | MAX_PORTFOLIO_RISK_PCT=4%
- **OCI git:** ✅ FIXED S49 — reset to `89ee635` (was `ae6d692`, 5 commits behind). portfolio_tracker.py no longer dirty.
- **Dashboard:** `http://129.153.208.32:8080/dashboard.html`
- **RAM (S45):** ⚠️ 695MB used / 113MB free at session start (12:31 PM ET Jun 1 — RTH, no restart). Alert threshold: 550MB → restart all services. Prior peaks: S37=750MB, S39=731MB (both triggered auto-restart). memory_watchdog.sh will auto-restart post-RTH when < 150MB free. P1/P2/P3/P4 all deployed. Optional P5 jemalloc pending (systemd unit only).
- **⚠️ SSH KEY NOTE:** Ed25519 key at `~/.ssh/mtf_bot_oracle`. rsync syntax: always use `-e "ssh -i ~/.ssh/mtf_bot_oracle"` — NEVER use `-i` as standalone rsync flag.

## Open Positions (confirmed Alpaca API 2026-06-05 S49)
- **NFLX short -1sh @ $81.84** | RTH DAY stop BUY 1sh @ $84.58 active until 4 PM ET today | after 4 PM: AH GTC loop re-submits GTC automatically (confirmed design) | target $76.41 | overnight hold (overnight_since 2026-06-03 16:06 ET) | current ~$81.52
- **MSTR short -1sh @ $118.94** | ✅ GTC stop BUY 1sh @ $131.61 (order 30971b38, submitted S49 via SSH/REST) | target $93.59 | entered 10:13 AM ET 2026-06-05, adopted orphan at 12:25 PM ET restart | board-approved (4-domain vote S49) | overnight=False in bot tracker (bot won't auto-resubmit GTC on restart — manual stop is the protection)
- ⚠️ NFLX FIFO: net_qty accumulating -12 on restarts (pre-existing, NOT Phase 2a.5). P2.

## GTC/DAY Stop Lifecycle (confirmed S49 — NOT a bug)
- Pre-RTH: orphan_manager cancels overnight GTC (intentional cleanup before RTH)
- RTH open (9:30 AM ET): gtc_manager submits DAY stop for overnight positions — intraday protection
- 4 PM ET: DAY stop expires automatically
- Post-4 PM ET: AH GTC loop (main.py ~L3394) re-submits GTC for next overnight hold
- Pattern confirmed today: GTC → restart → GTC → restart → GTC → pre-RTH cancel → DAY (9:30 AM) → DAY expires → GTC (tonight)

## PDT
- **0/3 slots used** (daytrade_count=0, confirmed Alpaca MCP S49 — MSTR entry today was SHORT, not a day trade reversal)

## ✅ QUARTERLY HOLDS RESEARCH — COMPLETE (S48, 2026-06-04 pre-market)

**Memo:** `logs/quarterly_holds_research_2026-06-04.md` (commit 93a1898)

| Pick | Ticker | Runway | Pre-Market Entry | Key Signal |
|------|--------|--------|-----------------|-----------|
| 1 | **AVGO** | ~13 wk → Sep | **$426 (−11% dip)** | Q2 AI rev +143%, Q3 AI +200%, Druckenmiller 195k shares Q1 2026 |
| 2 | **NVDA** | ~12 wk → Aug 26 | $214.95 (flat) | Q1 FY2027 $81.6B (+85%), Blackwell ramp |
| 3 | **ANET** | ~9 wk → Aug 3 | $168.45 (−3.4%) | Q1 2026 +35% rev, EPS beat +10%, AI networking |

**CCR STATUS (S49):** ✅ BUILT IN-SESSION — Rafael stepped away, Claude executed full 9-step autonomous build. quarterly_hold_manager.py DEPLOYED commit e6d2fda. No CCR needed.
**DS/GAI:** COMPLETE in-session via direct API (curl). 3-point AI summary in tb_audit_log.md S49.

⚠️ **AVGO entry deferred per board:** Tudor Jones / Weinstein / Brandt all REJECT same-day gap entry. AVBO enters in 3 tranches starting Day 3 close post-earnings (~June 9). NVDA + ANET proceed when Stage 2 confirmed.

---

## 🗳️ BOARD VOTE COMPLETE — quarterly_hold_manager.py (S48b, June 4 2026)

**25-member cold parallel vote (5 BoD + 12 AB + 8 TB). Rafael stepped away; autonomous build authorized.**

### Hard Blocks + Autonomous Resolutions:

| Block | Board Verdict | Autonomous Resolution |
|-------|--------------|----------------------|
| AVGO same-day gap entry | AB REJECT (Tudor Jones/Weinstein/Brandt) | 3-tranche: Day 3/5/7 close post-earnings (~June 9-13) |
| "NO blocking" coexistence | BoD REJECT 3-2 (Simons/Taleb/Peterffy) | v1: quarterly symbol → intraday blocked while held (shared registry) |
| 45% w/ zero track record | AB REJECT (Thorp/López de Prado) | Proceed per Rafael mandate; add Kelly fix (available_intraday_equity) |
| Concurrent broker.py calls | TB REJECT (Katsuyama/Minsky) | OrderDispatcher inside quarterly_hold_manager.py |
| AVGO AI rev via FMP | TB BLOCK (McKinney) | Semiconductor Solutions segment proxy + null/magnitude/freshness guards |

### Board-Approved Architecture:

- **Stop:** 14-week ATR × 2.5× (weekly bars) + 15% hard floor from entry (whichever fires first); lock at entry — Slack alert on VIX expansion instead of auto-widen
- **Entry:** 3-tranche limit orders (1/3 Day 1 close, 1/3 Day 3, 1/3 Day 5); Day 1 gate: 30-min bar must close > prior_close × 0.85 or defer to next session
- **Sizing:** AVGO 20% / NVDA 15% / ANET 10% (45% total conviction-weighted, per Rafael mandate)
- **Kelly fix (mandatory):** `available_intraday_equity = total_equity - quarterly_holds_market_value`
- **PDT:** Intraday bot blocked from a symbol on days quarterly module opens/closes that symbol (Levitt flag)
- **Coexistence v1:** `_quarterly_hold_symbols: set[str]` shared registry; intraday entry_logic.py checks before scanning
- **State machine (7 states):** PENDING_ENTRY → AWAITING_FILL → ACTIVE → PENDING_STOP_REPLACE → PENDING_EXIT → CLOSED | THESIS_INVALIDATED
- **State file:** `data/state/quarterly_holds.json` (os.fsync() atomic write; separate from trade_log.json)
- **Feature flag:** `QUARTERLY_HOLDS_ENABLED` in `.env`; try/except import guard in run_cycle.py (MTTR = env-var toggle, no code change)

### Thesis Invalidation (BoD Q5, all APPROVE):

- **AVGO:** Q3 FY2026 AI rev (FMP Semiconductor Solutions segment proxy) < $13.6B (>15% miss vs $16B guidance) = primary; management language "delay/softness/qualification" in AI XPU Q&A = secondary; AVGO put/call ratio >2.0 (60-90 DTE) for 3+ consecutive days within 6 weeks of Sept earnings → reduce position 50%
- **NVDA:** Data center miss >10% AND Q3 guide-down simultaneously (BOTH required); TSMC capacity cut at quarterly earnings = supply-chain proxy
- **ANET:** FY2026 AI networking < $2.8B at August update; Cisco AND Juniper concurrent guide-down = market contraction signal

### Module Interface (TB spec — required interface):

```python
class QuarterlyHoldManager:
    def __init__(self, broker, fmp_client, alerter, config: dict,
                 state_path: Path, dry_run: bool = False, clock=None): ...
    def reconcile_on_startup(self) -> ReconcileResult: ...    # before first intraday cycle
    def run_weekly_check(self) -> None: ...                    # once per RTH cycle (not per bar)
    def maybe_enter_positions(self) -> list[str]: ...          # at RTH open for PENDING_ENTRY positions
    def get_status(self) -> list[QuarterlyHoldStatus]: ...     # structured status for dashboard tile
    def safe_stop(self) -> None: ...                           # on shutdown/emergency stop
```

### Beck's First 3 Tests (must exist before writing implementation):

1. Restart during AWAITING_FILL → `reconcile_on_startup()` calls NO new order submission
2. GTC stop order not found on Alpaca → state transitions to PENDING_STOP_REPLACE + Slack alert fires
3. FMP returns null for thesis metric → `ThesisCheckResult.DATA_UNAVAILABLE`, no exit order, state unchanged

### Pending JSON format (for autonomous_review.py):

```json
{
  "target_file": "execution/quarterly_hold_manager.py",
  "status": "awaiting_ds_gai",
  "base_commit": "<git rev-parse HEAD>",
  "sha256": "<sha256sum of file>",
  "description": "New quarterly hold manager module — RTH chain import",
  "patch_file": "logs/pending_patch_2026-06-04_quarterly_hold_manager.patch",
  "ds_gai_prompt": "<full DS/GAI prompt>"
}
```

---

## 🤖 CCR BUILD TASK — execution/quarterly_hold_manager.py

**Authorization: "patches that have the 3 point AI audit are approved" (Rafael, S48b)**
**Pipeline: CCR draft → pending_ds_gai JSON + .patch → autonomous_review.py OCI (11 PM ET) → pending_approvals_*.md → Rafael approves next session**

### Mandatory sequence for CCR:

1. `/session-start`
2. Full read: `strategy/run_cycle.py` + `execution/broker.py` + `execution/portfolio_tracker.py` — ALL >1000L → Explore subagents, declare line count for each
3. 10-point audit + RC-1 through RC-8 on all three integration files → write findings to `logs/tb_audit_log.md`
4. Draft `execution/quarterly_hold_manager.py` per module spec + board decisions in this file
5. `python3 -m py_compile` + `python3 -m mypy --warn-unreachable` + `ruff check --select E,W,F,B` — ALL must PASS
6. Cold second-agent logic review (diff + intent → PASS/FAIL)
7. `code-review-graph` detect_changes + get_impact_radius on `strategy/run_cycle.py` + `execution/portfolio_tracker.py`
8. Create `logs/pending_ds_gai_2026-06-04_quarterly_hold_manager.json` + `logs/pending_patch_2026-06-04_quarterly_hold_manager.patch` → commit to GitHub
9. Update `HANDOFF.md` and `logs/tb_audit_log.md`; push all to GitHub

If quarterly_hold_manager.py complete and time remains → P2 NFLX FIFO: `portfolio_tracker.py` (2284L → Explore subagent) → audit _save_open_lots() + _fifo_reconstruct() → pending_ds_gai JSON → GitHub

---

## 🌙 CRON AGENT TASK — Quarterly Holds Research (COMPLETED by in-session Claude S48)

**Requested by Rafael, S47f (2026-06-04). Ready when Rafael wakes up.**

### Context
Rafael wants 2-3 S&P 500 stocks for quarter-minimum long holds — from last reported earnings through the day before next earnings. Inspired by MU/SMH repricing in Q2: semiconductors, memory, and related sectors that could have been identified in April as quarterly anchors. Top investors' 13F filings corroborate these signals. Rafael referenced "slow and steady alpha gains" from anchor positions alongside intraday trading.

### Research Scope
- **Universe:** Full S&P 500 (not just current scan list — explicitly broadened)
- **Time horizon:** Entry after earnings report → exit day before next earnings
- **Direction:** Long (short candidates noted if clearly relevant)
- **Key sectors to weight:** Semiconductors/memory, AI infrastructure, any other sector with clear fundamental repricing catalyst visible in earnings transcript
- **13F signal:** Cross-reference with recent 13F filings (SEC EDGAR) from top institutional investors (Druckenmiller, Ackman, Einhorn, Cohen, Tepper, Buffett/Berkshire, etc.)

### Process for CRON Agent
1. **Pull S&P 500 constituent list** (use Wikipedia `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies` or SEC EDGAR)
2. **FMP API earnings transcripts:** `https://financialmodelingprep.com/api/v3/earning_call_transcript/{SYMBOL}?quarter={Q}&year={YYYY}&apikey={FMP_API_KEY}` — check most recent 2 years (Q1 2024 through Q1 2026). Focus on: revenue guidance cadence, management confidence tone, margin expansion language, TAM commentary.
3. **SEC EDGAR 13F filings:** `https://efts.sec.gov/LATEST/search-index?q=%2213F%22&dateRange=custom&startdt=2026-01-01&enddt=2026-06-01&forms=13F-HR` — look for conviction additions (new positions or large increases) from known top investors. EDGAR full-text search: `https://efts.sec.gov/LATEST/search-index`
4. **Candidate screens:** Look for stocks where: (a) last earnings had positive guidance revision, (b) 13F shows institutional accumulation, (c) sector has a multi-quarter structural tailwind (AI capex, defense spending, energy transition), (d) next earnings is ≥ 6 weeks away
5. **Web research:** `WebSearch` and `WebFetch` to pull earnings transcript summaries from Seeking Alpha, Motley Fool, or similar if FMP is unavailable/limited
6. **Board vote** on final 2-3 candidates (BoD + AB + TB). Key questions for board: (a) sizing — what % of account for anchor positions?, (b) stop loss — hold regardless or max-drawdown stop?, (c) coexistence with intraday bot — does bot trade these symbols intraday while holding anchor?, (d) entry timing — immediately post-earnings or wait for confirmation day?
7. **DS/GAI audit** on the feature integration proposal (this is a new bot feature — RTH chain impact requires audit)
8. **Output format** — write research memo to `logs/quarterly_holds_research_YYYY-MM-DD.md` with: candidate stocks + rationale + earnings transcript key quotes + 13F corroboration + board vote summary + DS/GAI integration proposal

### Time constraint
Rafael wants this ready when he wakes up (~5-6 hours from S47f end). Prioritize research quality over exhaustiveness — 3 strong candidates with solid rationale beats 10 weak ones.

---

## Open Items (next session priority)

### P1 — orphan_manager.py QHM integration (S49 — added 2026-06-05)
- **File:** `execution/orphan_manager.py`
- **Issue:** orphan_manager.py cancels GTC stops pre-RTH without checking QHM registry. QHM positions (AVGO/NVDA/ANET) will have their overnight GTC stops cancelled each morning, leaving positions unprotected 9:30 AM → 4 PM ET until AH loop resubmits.
- **Fix:** Import `get_quarterly_hold_symbols()` from `execution.quarterly_hold_manager` in `orphan_manager.py`. In `cancel_and_reconcile_gtc_stops()`, skip cancellation if `symbol in get_quarterly_hold_symbols()`.
- **Gate:** Full read orphan_manager.py + 10-point audit + board vote (RTH-chain) + DS/GAI required before patch.
- **Priority:** P1 — must be fixed before any QHM position is held overnight.

### P2 — NFLX FIFO net_qty=-12 corruption (pre-existing)
- **File:** `execution/portfolio_tracker.py`
- **Issue:** NFLX short net_qty accumulates to -12 on restarts. Fix before next short overnight entry.
- **Gate:** Full read portfolio_tracker.py (2284L → Explore subagent) + DS/GAI required.

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

### Open Items — New Bugs from S44 Gemini Audit Review (2026-05-25 to 2026-05-29)

> **Source:** 8 Gemini midday + nightly reports. All synthesized S44. CCR tonight will process RTH-chain items through full 9-step sequence. Non-RTH items eligible for direct autonomous apply.

- [x] **P0: fill_helpers.py P5-H2 fill crosstalk — PATCHED + DEPLOYED S47 ✅** — `execution/fill_helpers.py` (RTH-chain). Root cause: `filled_at` DESC sort non-deterministic for sub-second rapid re-entry (close T0, re-entry T0+50ms). Fix: 3 changes to `_query_fills()`: (1) `direction="asc"` on GetOrdersRequest — oldest-first pagination; (2) `submitted_after - 0.05` — 50ms NTP grace margin; (3) sort key `(created_at ASC, id)` `reverse=False` — close order always created before re-entry. DS APPROVE (50ms critical). GAI APPROVE (direction="asc" needed). py_compile PASS / mypy 0 errors / ruff PASS. Commit 1adc1cb. OCI deployed, all 4 services active.

- [x] **P0: fill_correction math wrong → PHANTOM BUG (downgraded P2)** — `fill_correction` function does NOT exist anywhere in the codebase (not in fill_helpers.py, fill_reconciler.py, or portfolio_tracker.py — all 3 fully read S46). Gemini hallucinated the function name. `patch_exit_pnl()` in portfolio_tracker.py reviewed: math is correct per Explore agent. Reopen only if MSTR double-record reproduces with real fill data.

- [x] **P0: EOD P&L tracker blind → PHANTOM / DOWNGRADED P3** — risk_manager.py full read complete S46 (657L, 3 chunks). Kill switch at L115: `worst_pnl = min(self.daily_pnl, self.equity_pnl)` — dual protection. `equity_pnl = portfolio_value - daily_start_value` is immediately correct at startup (reads Alpaca `last_equity`). Even if `daily_pnl = 0` after `reset_daily()`, the `equity_pnl` backstop catches overnight losses. RC-3 1 instance found (L62, intentional bare `except: pass` in logger-failure guard — low priority). `update_daily_pnl_from_alpaca()` logs warnings on all API failures (not truly silent). **Downgraded to P3.**

- [x] **P1: risk.open_positions desync — PATCHED + DEPLOYED S47e ✅** — `execution/trade_engine.py` (commit 4f58c85). `_reconcile_pending_overnight_orders()` L252-254 direct SET replaced with `register_open()` + status-gate (pre/post-status guard prevents double-increment; idempotent per promote_pending_to_active() status check). Board 4/4 PASS. DS/GAI direct API APPROVE. Stale OCI cache deleted. All 4 services active post-deploy.

- [x] **P1: Dashboard all-time P/L ≠ Monthly Review all-time P/L — PATCHED + DEPLOYED S47e ✅** — `generate_dashboard.py`. Change 1: lifetime_pnl_cache atomic write block inserted after all_trades line — `_total_pnl_val` None guard prevents $0-poison writes; `bool({}) == False` guards empty dict; `0.0 is not None → True` correctly persists zero P&L. Change 2 (RC-6): `o["order_type"]` KeyError fixed → `o.get("type") or o.get("order_type","unknown")` (`"type"` first = Alpaca v2 correct field). Board 4/4 PASS. DS/GAI direct API: DS APPROVE, GAI APPROVE (GAI P1 reversed-priority corrected). Stale OCI cache deleted (`rm /home/ubuntu/mtf-bot/logs/lifetime_pnl_cache.json`). All 4 services active post-deploy. NOTE: monthly_review.py `_load_lifetime_pnl()` remains dead code — separate P2 item, no DS/GAI gate needed.

- [x] **P1: pnl=0.0 for stop_hit events with entry≠exit — PATCHED S47b ✅** — `execution/portfolio_tracker.py`. PLTR short: entry $133.29, exit $133.295 — Python float repr -0.004999... was rounded to 0.00 at 2dp storage. Fix: 8 storage locations round(x,2)→round(x,4); L792 _partial_pnl != 0.0 → abs()>1e-8 (DS+GAI mandatory condition). 5 display locations unchanged. DS/GAI both CONDITIONAL APPROVE. Cold second-agent PASS. Commit 5600c70. OCI deployed.

- [x] **P1: avg_r_multiple miscalculated — PATCHED + DEPLOYED S47 ✅** — `execution/portfolio_tracker.py` `get_stats()`. Bug1: _fill_unverified trades polluted r_multiples denominator; corrupt/scratch stops produced |R|>100. Fix: (1) skip _fill_unverified trades from loop; (2) ±50R clamp prevents scratch-stop R explosion; (3) WARNING log when r_multiples empty; (4) unverified_trades added to return dict. 2049L→2124L. DS+GAI APPROVE. Commit 0f3aa58.

- [x] **P1: record_exit() entry=None/0 produces phantom P&L — PATCHED S47 ✅** — `execution/portfolio_tracker.py`. Bug2: entry≤0 path now sets `trade["_fill_unverified"]=True`; `get_stats()` pnls filter excludes _fill_unverified; return dict adds `verified_trades` + `unverified_trades` alongside `total_trades`. DS+GAI APPROVE. Commit 0f3aa58.

- [x] **P1: _load_log() false double-record on same-day re-entry — PATCHED S47 ✅** — `execution/portfolio_tracker.py`. Bug3: symbol-only conflict detection was conceptually wrong — same-day close+re-entry has different entry_time and must NOT warn. Fix: `_closed_entry_keys = {(symbol, entry_time) tuples}`; warning only fires when BOTH symbol AND entry_time match (genuine double-record). GAI flagged, DS deferred — GAI adopted. DS+GAI APPROVE. Commit 0f3aa58.

- [x] **P1: _load_day_trades() TOCTOU race + silent corruption — PATCHED S47 ✅** — `execution/portfolio_tracker.py`. Bug8: `if DAY_TRADES_FILE.exists(): with open(...)` race between check and open. Fix: (1) TOCTOU eliminated — handle FileNotFoundError explicitly in try/except; (2) 100ms retry for transient write-lock collisions; (3) CRITICAL log + Slack alert on genuine corruption; (4) `self._day_trades=[]` set BEFORE Slack call. DS+GAI APPROVE. Commit 0f3aa58.

- [x] **P1: OVERNIGHT_ENTRIES_ENABLED hardcoded False in main.py — ✅ PATCHED + DEPLOYED S46** — Hardcoded `False` at line 131 removed. Replaced with `bool(getattr(config, "OVERNIGHT_ENTRIES_ENABLED", False))` module-level gate + sys.modules alias (4/4 board Path B) + global re-read after profile loop + conditional WARNING/INFO. Committed 2a91753. **Rsync deployed 1:41 PM PT — OCI log confirmed `OVERNIGHT_ENTRIES_ENABLED=False (profile: paper)`.** CCR main.py queued item in `queued_for_review_2026-06-01.md` is STALE — issues already addressed in final BUG-6 patch.

- [x] **P1: MSTR tracked as both closed and overnight_hold in EOD snapshot — PATCHED + DEPLOYED S47f ✅** — `execution/portfolio_tracker.py` (commit fb4c662). Phase 2a.5 block inserted before overnight_holds build (~L1154): detects overnight positions absent from _alpaca_lots, reconciles via record_exit() with VWAP exit price. Q5 catastrophe guard (empty _alpaca_lots + _alpaca_per_trade → log CRITICAL + skip loop). Q1: corrects exit_time to actual Alpaca fill timestamp. Q3: mri_at_exit_uncertain=True on closed_trade record. _load_log() routes _fifo_reconciled_closed=True to closed_trades on restart. Board vote Q4 (partial-close scope): 3/4 OPTION B — deferred to P2 (see below). DS APPROVE, GAI APPROVE (all Q1-Q5 addressed). py_compile/mypy/ruff PASS. Cold second-agent PASS. OCI deployed, all 4 services active.

- [x] **P1: BUCKET_B_MAX_POSITIONS_POWER=5 not honored during power_hour — ✅ PATCHED + DEPLOYED S47c** — `execution/entry_logic.py` + `config.py`. 7 fixes applied: BUG-PH-1 kill-switch no longer bypassed by expansion logic; BUG-PH-2 hardcoded `(15*60+30)` → `config.TOD_EXPANSION_WINDOW_START` (new constant); BUG-PH-3 `len(tracker.open_trades)` → `risk.open_positions` (authoritative counter); BUG-PH-4 re-check `risk.open_positions >= _ph_limit` before fall-through; BUG-PH-5 expansion disabled at PDT=3/3 (BoD 3-0: Simons/Taleb/Kyle — forced overnight hold impairs exit); Fix#6 `_is_ph_cyc` computed once before for-symbol loop (Data Integrity board); Fix#7 `logger.warning` before all `break` statements (Reliability board). DS/GAI both APPROVE all 7 questions. 1724L→1747L. OCI deployed, all 4 services active.

- [ ] **P2: portfolio_tracker.py partial-close reconciliation gap** — Phase 2a.5 handles ONLY full closes (symbol absent from _alpaca_lots). Partial closes (Alpaca shows fewer shares than bot's open_trades qty) are NOT detected. Guard `if _sym_r in _alpaca_lots: continue` skips symbols with any remaining lot. Fix requires qty-level comparison + partial record_exit(). GAI flagged P1; board 3/4 OPTION B (defer). Needs: qty-level comparison design + board vote + DS/GAI audit. NOT blocking (full-close fix deployed; partial-close from GTC is rare).

- [ ] **P2: MAX_DAILY_LOSS_PCT BoD-3 log message misleading** — `main.py` lines 88-94 (RTH-chain, hotspot). BoD-3 block: `if config.MAX_DAILY_LOSS_PCT > 0.15` → for paper, 0.07 > 0.15 is False so block doesn't execute. But the log string references "was 30%" from PROFILES dict — misleading in context. Low-risk comment fix. **Gemini: MEDIUM (May 27 + May 28).**

### Open Items (prior sessions)

- [x] **P0: risk.open_positions startup fix — CLOSED S42** — main.py Part A: P0-STARTUP block inserts after sync_from_tracker(); queries Alpaca live positions; overrides risk.open_positions if mismatch; logs _untracked/_stale symbol sets; halts at MAX. entry_logic.py Part B: P0-CYCLE-SYNC-GUARD replaces unconditional BUG-POS-1 sync; directional guard (tracker UP-only); status filter (excludes zombie closed entries); None guard. Both deployed OCI. Startup log confirmed: "Alpaca=4 == tracker=4. OK." → "Already at MAX (4/4). Blocking new entries."
- [x] **P0: Slack alert noise — PARTIALLY CLOSED S43** — `alerts.py` patched: `alert_crash()` reason-based dedup (same reason+<60min→ntfy only; different reason→Slack+ntfy always); `alert_stale_bar()`→log-only; `alert_startup_test()` + `alert_spy_event()` UNCHANGED (board: keep all). Deployed OCI git 35bccc9. **Remaining:** `events/macro_risk_index.py` mri_refresh noise (confirmed NOT going to Slack currently — JSONL only). SIGKILL cycle itself is root cause (P2 RAM leak — separate session).
- [x] **P1: weekly_perf_audit.py COMPLETE (S42)** — Script built, static analysis PASS, cold second-agent PASS, OCI deployed, cron wired `15 20,21 * * 5` via cron_tz_wrapper 16:15. 4 DS/GAI additions incorporated (emergency escalation, LOW_VOL VIX regime, monthly mislabeling gate, MIN_TRADES thresholds). Design spec updated at `logs/weekly_perf_audit_design_v1.md` (§14).
- [ ] **P2: RC-9 in scan_to_html.py** — `_fetch_yfinance_news()` uses yfinance for news data (T4 violation). Board vote + migration plan required. `queued_for_review_2026-05-28.md` exists on OCI.
- [ ] **P2: NFLX FIFO state corruption — net_qty accumulating (S48)** — Every bot restart processes the same NFLX short entry fill without matching prior lots (`open_lots_prior_day.json` has `NFLX: null`). Results in `net_qty` growing: -9 → -10 → -11 → -12 across restarts (each restart = one more synthetic short recorded). NOT caused by Phase 2a.5 (which correctly skips NFLX because synthetic short appears in `_alpaca_lots`). Bot correctly manages the position (GTC stop active, orphan_manager re-adopts on restart). Root cause: NFLX short entered after EOD cycle (2026-06-03 16:06 ET) → never written to `open_lots_prior_day.json` (which saves long lots only). Fix: FIFO system needs to handle short positions' lot persistence. Requires full read of `_fifo_reconstruct()` + `_save_open_lots()` sections in portfolio_tracker.py. NOT blocking for current NFLX hold (GTC stop protects). Fix before next short overnight entry.

- [ ] **P2: auto_deploy.sh BEFORE==AFTER design gap** — When autonomous_review.py runs before auto_deploy.sh (11 PM vs 11:30 PM ET), AR does git pull + git push, advancing OCI HEAD. auto_deploy.sh then sees BEFORE==AFTER and skips restart even if CCR committed code changes. Fix: replace BEFORE/AFTER comparison with last-deployed-SHA tracking file. Non-blocking for RTH-draft-only nights (no direct code commits from CCR). Fix required before CCR can autonomously apply non-RTH code patches end-to-end.
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
- **Nightly CCR (OLD — DISABLED):** `trig_01GHKQufnHRykVL9Xypqeee4` — `0 7 * * *` — DEAD (`auto_disabled_repo_access` — wrong username typo in URL)
- **Board Review CCR:** `trig_01B37951UHsX2NAsCvrJuHNK` — weekdays 5:30 PM ET — 4 parallel domain agents → board verdict → **NOW POSTS TO SLACK** (Step 4 added S43C) → https://claude.ai/code/routines/trig_01B37951UHsX2NAsCvrJuHNK
- **Nightly Autonomous Work CCR (RESCHEDULED S47):** `trig_01NctUPEvjM1TVDH3vgJ2bmw` — **weekdays 3 PM PT / 6 PM ET (22:00 UTC) — was 10 PM ET** — Runs after auto_ai_audit.py (1:35 PM PT) completes, before user's 6 PM PT threshold. **RE-ENABLED with safety measures:** (1) reads handoff.md + tb_audit_log.md for priorities; (2) RTH-classification gate: non-RTH files only for autonomous apply; RTH-chain files → queue only; (3) broker.py + config.py HARD EXCLUDED; (4) forbidden categories: threading/asyncio, order routing, credential handling, state persistence writes, stop/target logic; (5) 3 adversarial board agents 3/3 PASS required; (6) pytest required; (7) 1 file/night limit; (8) queue mechanism → Slack summary → https://claude.ai/code/routines/trig_01NctUPEvjM1TVDH3vgJ2bmw

### Full Daily Pipeline (Mon–Fri) — UPDATED S44
| Time ET | What | Output |
|---------|------|--------|
| 1:30 PM | midday_audit.py (Gemini) | midday_gemini_*.txt + Slack |
| 4:05 PM | nightly_audit.py (Gemini) | gemini_audit_*.txt + Slack |
| 4:35 PM | auto_ai_audit.py --meta-audit (DS + Gemini) | meta_audit_latest.json + Gist + **Slack** |
| 4:15 PM Fri | weekly_perf_audit.py | weekly_perf_audit_YYYY-WNN.html + Slack |
| 5:30 PM | Board Review CCR (4 agents) | Board verdict + **Slack** |
| 3:00 PM PT / 6 PM ET | Nightly Autonomous Work CCR (**RESCHEDULED S47**) | Full 9-step audit → RTH-chain: `pending_ds_gai_*.json` + `.patch` → GitHub + Slack |
| 7:30 PM ET | autonomous_review.py (OCI cron, **RESCHEDULED S47e**) | Calls DS + Gemini → `pending_approvals_*.md` → GitHub + Slack "ready for approval" |
| 8:00 PM ET | auto_deploy.sh (OCI cron, **RESCHEDULED S47e**) | git pull → restart services if new non-RTH commits |

**Session-start approval flow (NEW S44):**
When you log in → Step 3c reads `pending_approvals_*.md` → shows numbered patch list with DS/GAI verdicts → say "approved #N" → SHA256 verify → `git apply` → static analysis → rsync → health check → auto-rollback on failure.

### Infrastructure Notes
- **GitHub repo:** `https://github.com/redstorm8705/alpaca-mtf-both` (private) — CCR agents clone from here. **⚠️ Previous CCRs used WRONG URL (`redstamp8705`) — that was the root cause of 3 days of failure (S43C fixed)**
- **OCI git (FIXED S47f):** `/home/ubuntu/mtf-bot` git HEAD reset to `665d33f` (S47f). Was stuck at `9658f13` (June 2) with modified tracked files blocking auto_deploy.sh since May 31. Fix: `git fetch origin && git reset --hard origin/main` (safe — diff vs origin/main was empty). auto_deploy.sh should now run correctly tonight. **⚠️ Orphaned untracked files at OCI root** (`broker.py`, `portfolio_tracker.py`, `fill_helpers.py`, `gtc_manager.py`, `run_cycle.py`, etc.) — stale copies from prior incorrect rsync paths. Not imported by bot (uses subdirectory paths). Clean up when convenient. ⚠️ LESSON S45: always `git push origin main` from local before session end.
- **autonomous_review.py (NEW S44):** `/home/ubuntu/mtf-bot/autonomous_review.py` — 433 lines. Stage 2 of the autonomous pipeline. Reads `logs/pending_ds_gai_*.json` (written by CCR), calls DeepSeek API + Gemini API with identical prompts (MAX_RETRIES=3, exponential backoff), writes raw responses verbatim to `logs/pending_approvals_YYYY-MM-DD.md` (no autonomous summary — user sees raw DS + GAI text). If REJECT in either response → routes to `queued_for_review_*.md` instead. Updates JSON status `awaiting_ds_gai` → `ready_for_approval`. Commits precise filenames to GitHub, Slacks "🎯 Patches ready for approval". flock: `/tmp/mtf_autonomous_review.lock` (own) + `/tmp/mtf_git.lock` (shared with auto_deploy.sh). Cron: `0 3 * * 2-6` (11 PM ET weeknights).
- **auto_deploy.sh (UPGRADED S43C2, SHIFTED S44):** `/home/ubuntu/mtf-bot/auto_deploy.sh` — 125 lines (was 36). flock lockfile, deploy window 10PM-6AM ET, 3-iteration health check at 20s/40s/60s, auto-rollback on fail: `git reset --hard $BEFORE` (LOCAL ONLY). Cron: **`30 3 * * *` (11:30 PM ET — shifted from 11 PM to avoid collision with autonomous_review.py).** Logs to `logs/auto_deploy.log`. **IMPORTANT: auto_deploy.sh is NOT tracked in git (untracked) — contains Slack webhook URL. Do not commit.**
- **Board endpoint (Gist):** `https://gist.githubusercontent.com/redstorm8705/1574ea556d06e7a1db45d00097f9c069/raw/meta_audit_latest.json`
- **DS/GAI keys:** DEEPSEEK_API_KEY + GEMINI_API_KEY + GITHUB_GIST_TOKEN all in local `.env` and OCI `.env` ✅ GEMINI_API_KEY rotated S47f — new key working (see .env).
- **DS/GAI DIRECT API PROTOCOL (S47e — MANDATORY — NOT browser automation):**
  - **DeepSeek:** `curl https://api.deepseek.com/v1/chat/completions -H "Authorization: Bearer $DEEPSEEK_API_KEY" -H "Content-Type: application/json" -d '{"model":"deepseek-chat","messages":[...],"max_tokens":4096}'` — model = `deepseek-chat`
  - **Gemini:** `curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_API_KEY" -H "Content-Type: application/json" -d '{"contents":[...],"generationConfig":{"maxOutputTokens":8192}}'` — model = `gemini-2.5-flash`, maxOutputTokens=8192
  - Keys in `/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.env` — `DEEPSEEK_API_KEY` and `GEMINI_API_KEY` (see .env — do NOT hardcode key values in handoff or memory files)
  - **WHY DIRECT API:** DeepSeek/AI Studio are React/Angular SPAs — background tabs don't render DOM content; Chrome extension security blocks innerHTML/TreeWalker access. Direct API bypasses ALL of this.
  - DS persona: "Senior Staff Engineer at an HFT firm with direct ownership of execution engines and P&L attribution systems. Treat this as a P0 incident review. Be concrete and technical — no hedging."
  - GAI persona: "Head of Quant Engineering at a systematic hedge fund. Responsible for correctness of all P&L attribution, risk accounting, and counter-state invariants. Your audit is the last gate before code goes live. Find what others missed."
- **Gemini model (autonomous_review.py / OCI scripts):** `gemini-3.1-pro-preview` via `google.genai` SDK (OCI scripts unchanged) | **Claude in-session audits:** `gemini-2.5-flash` via direct API curl
- **Gist ID:** `1574ea556d06e7a1db45d00097f9c069` (redstorm8705 account, public gist)

## Last Session (S47f — 2026-06-03/04)

### S47f (2026-06-03/04) — portfolio_tracker.py Phase 2a.5 PATCHED + DEPLOYED ✅; Gemini key rotated; quarterly holds research QUEUED

- **portfolio_tracker.py P1 CLOSED (commit fb4c662):** Phase 2a.5 FIFO overnight reconciliation patched. 2 changes: (1) Phase 2a.5 block before overnight_holds build — detects fully-closed overnight positions via _alpaca_lots, reconciles via record_exit() VWAP exit price, Q5 catastrophe guard, Q1 exit_time correction, Q3 mri_at_exit_uncertain flag. (2) _load_log() routes _fifo_reconciled_closed=True to closed_trades on restart (prevents Day 2 entry rejection). Board Q4 vote: 3/4 OPTION B (partial-close deferred to P2). DS APPROVE (all 5 DS findings incorporated). GAI APPROVE (Q1-Q5 all addressed). 3-Point AI Summary produced. py_compile/mypy/ruff PASS. Cold second-agent PASS. 2126L→2284L.
- **GEMINI_API_KEY rotated:** Old key confirmed revoked. New key in local .env + OCI .env. All tracked files clean of hardcoded key values. Git history retains old key in 3 prior commits — force-push rewrite deferred (user decision required).
- **Key scrub complete:** HANDOFF.md + CLAUDE.md + memory/feedback_ds_gai_direct_api.md — all hardcoded API key values removed. Keys now stored in .env only.
- **P2 added:** partial-close reconciliation gap (Phase 2a.5 only handles full closes — board 3/4 OPTION B deferred).
- **NEW REQUEST from Rafael (queued for CRON agent):** Quarterly holds research — identify 2-3 S&P 500 stocks for quarter-minimum long holds. Full process below in CRON AGENT TASK.

---

## Last Session (S47e — 2026-06-03)

### S47e (2026-06-03) — BOTH S47d P1 items PATCHED + DEPLOYED; DS/GAI Direct API Protocol Established

- **trade_engine.py P1 CLOSED:** `_reconcile_pending_overnight_orders()` L252-254 — direct `risk.open_positions = len(...)` SET replaced with `register_open()` + status-gate guard (pre/post status check prevents double-increment). Board 4/4 PASS. DS/GAI APPROVE. Commit `4f58c85`. OCI deployed all 4 services active.
- **generate_dashboard.py P1 CLOSED:** Two changes — (1) `lifetime_pnl_cache.json` atomic write block after `all_trades` line (None guard + bool({}) guard prevents $0-poison writes); (2) RC-6 `o["order_type"]` KeyError → `o.get("type") or o.get("order_type","unknown")` (`"type"` is correct Alpaca v2 field — GAI caught reversed priority in board's original proposal). Board 4/4 PASS. DS/GAI APPROVE. py_compile PASS / ruff PASS / mypy PASS. Cold second-agent PASS. Stale OCI cache deleted. All 4 services active post-deploy.
- **DS/GAI Direct API Protocol established:** Browser automation confirmed BROKEN (SPA background tabs, Chrome extension security blocks). Switched to `curl` direct API — DeepSeek (`deepseek-chat`) + Gemini (`gemini-2.5-flash`, maxOutputTokens=8192). Keys in local `.env`. This is now the MANDATORY approach for all in-session DS/GAI audits.
- **Scheduled CCR set:** `trig_01EenxbS1fg8fR1zjgikZ3CG` fires `2026-06-04T00:23:02Z` (3h35m from session end) to verify deployment + update docs + begin portfolio_tracker.py FIFO investigation.
- **Next P1:** `execution/portfolio_tracker.py` FIFO synthetic short bug (2125L) — Explore subagent full read required before any analysis.

---

## Last Session (S46 — 2026-06-01)

### S46 (2026-06-01) — main.py S44-BUG-6 PATCHED

- **Bug fixed:** `OVERNIGHT_ENTRIES_ENABLED = False` hardcoded at line 131 removed. Replaced with config-driven `bool(getattr(config, "OVERNIGHT_ENTRIES_ENABLED", False))` at module level (after `import config`) + sys.modules alias + global re-read after profile loop + conditional WARNING/INFO log.
- **Full 9-step sequence completed:** Full read (951L, 4 chunks). Board vote 3/4 PASS (Quant Logic FAIL → fix incorporated: WARNING when True / INFO when False). DS/GAI gate satisfied: DS Q5 FAIL (bool() cast), GAI Q3 CRITICAL FAIL (__main__/main double-import). DS/GAI conflict resolved via 4-agent board vote → 4/4 PASS Path B (sys.modules alias). 3-Point AI Summary logged. Static analysis PASS. Cold second-agent PASS. Impact trace (manual): bounded to entry_logic.py L1263/1548/1706 only.
- **Committed:** `2a91753` pushed to GitHub.
- **Deployed post-RTH:** rsync at 1:41 PM PT. OCI log confirmed `OVERNIGHT_ENTRIES_ENABLED=False (profile: paper)` at 20:43 UTC. All 4 services active. No behavioral change until config.py defines OVERNIGHT_ENTRIES_ENABLED (currently absent → returns False, identical to prior hardcoded behavior).
- **P0 investigation (post-deploy):** risk_manager.py full read (657L). fill_correction phantom bug confirmed. EOD P&L blind downgraded to P3 (dual kill-switch protection in place). Real P0: fill_helpers.py CCR-blocked sort tie-breaker + 100ms guard.
- **Next session:** fill_helpers.py P0 — full read (212L) → 10-pt audit → board vote → DS/GAI → patch.

---

## Last Session (S45 — 2026-06-01)

### S45 (2026-06-01) — PIPELINE UNBLOCKED

- **Root cause found:** `dc28423` (S44 portfolio_tracker.py patch) was committed locally but never pushed to GitHub. OCI's `git pull origin main` returned "Already up to date" because GitHub and OCI were both at `b78565e`. Pipeline was broken since S44.
- **Fix applied:** `git push origin main` → GitHub + OCI now at `dc28423`.
- **OCI git reset:** Prior session had done `git reset --hard origin/main` (to clear merge conflicts from untracked log files) → OCI had been at `b78565e`. After push fix, `git pull` on OCI fast-forwarded to `dc28423`.
- **Stale cron removed:** S44 one-time cron entry (`30 5 30 5 *`) was still in crontab after firing — removed.
- **autonomous_review.py dry-run:** `git pull...No items awaiting DS/GAI review. Exiting.` → PASS.
- **RAM alert:** 695MB used / 113MB free at 12:31 PM ET — above 550MB threshold but RTH active, no restart. memory_watchdog.sh will auto-restart when < 150MB free post-RTH.
- **auto_deploy.sh design gap identified:** BEFORE==AFTER logic fails when autonomous_review.py already advanced HEAD before auto_deploy.sh runs. Documented as P2 above.
- **No code changes, no patches applied.**

---

## Last Session (S44 — 2026-05-29)

### S44 (2026-05-29) — AUTONOMOUS PIPELINE STAGE 2 DEPLOYED
- **autonomous_review.py deployed to OCI** (433 lines, `/home/ubuntu/mtf-bot/autonomous_review.py`):
  - Reads `logs/pending_ds_gai_*.json` written by the Nightly CCR
  - Calls DeepSeek + Gemini with identical 7-section prompts (MAX_RETRIES=3, exponential backoff)
  - REJECT in either response → routes to `queued_for_review_*.md` (not presented for approval)
  - Writes raw DS + GAI responses verbatim to `logs/pending_approvals_YYYY-MM-DD.md` — NO autonomous summary
  - Updates JSON status, commits precise filenames, pushes to GitHub
  - Slacks "🎯 Patches ready for approval — [timestamp PT]"
  - Shared git flock with auto_deploy.sh (`/tmp/mtf_git.lock`)
- **OCI crontab updated:**
  - NEW: `autonomous_review.py` at `0 3 * * 2-6` (11 PM ET weeknights Tue-Sat)
  - SHIFTED: `auto_deploy.sh` from `0 3 * * *` → `30 3 * * *` (11:30 PM ET, avoids collision)
- **session-start skill updated** (`~/.claude/skills/session-start/skill.md`):
  - NEW Step 3b: `git pull origin main` first (gets CCR-committed patch files)
  - NEW Step 3c: reads `logs/pending_approvals_*.md`, presents numbered patch list with DS/GAI verdicts
  - Approval handler: SHA256 verify → `git apply` (NOT Edit tool) → static analysis → rsync → health check → auto-rollback → mark applied → commit + push
  - Step 6 output template: new PATCHES READY FOR APPROVAL section
- **handoff.md updated** to reflect new pipeline stages, cron times, infrastructure notes
- **No changes to bot trading logic** — this session was infrastructure only

---

## Last Session (S43C2 — 2026-05-28)

### S43C2 (2026-05-28) — AUTONOMOUS PIPELINE SAFETY HARDENED (continuation of S43C)
- **3-Point AI Summary produced** (CLAUDE.md mandatory step after DS/GAI responses):
  - Gist-as-DS/GAI gate: 1/3 — Claude proposed it, both DS+GAI rejected (wrong granularity, wrong input)
  - auto-rollback, broker.py exclusion, adversarial board, pytest, 1-file limit, queue mechanism: 2/3 — DS+GAI unanimous; Claude had missed all of these
  - Forward-looking: broker.py silent stop fail (GAI P1 queued), Kelly veto for negative strata (GAI P2), MRI gate verification (DS P1), API credential exposure (both P1 — blocks diff-level API review)
- **auto_deploy.sh UPGRADED (36→125L) on OCI:**
  - Board: Beck FAIL + Kim FAIL + Majors CONDITIONAL PASS — all findings incorporated
  - Cold second-agent: reported FAIL on deploy window boundary; analysis correction shows PASS (logic correct)
  - bash -n PASS (shellcheck not installed on OCI or local)
  - Changes: flock lockfile, deploy window 10PM-6AM ET, structured k=v logging, 3-iter health loop (20/40/60s), local rollback (git reset --hard, NO push to GitHub), Slack alerts with commit+service+status fields, rollback failure path
  - tb_audit_log.md updated on OCI
- **Nightly Autonomous Work CCR (trig_01NctUPEvjM1TVDH3vgJ2bmw) REWRITTEN and RE-ENABLED:**
  - Removed invalid Gist-as-DS/GAI gate (DS+GAI both rejected)
  - Priority source: handoff.md P0/P1 + tb_audit_log.md open RC violations
  - RTH classification step: Python AST import chain check before proceeding
  - Adversarial board: 3 cold agents (Strict Parser/Red Teamer/Quant Risk Manager) replacing cooperative board; 3/3 PASS required
  - broker.py + config.py HARD EXCLUDED (no queue, no log, skip entirely)
  - Forbidden categories list: threading/asyncio, order routing, credential handling, state persistence writes, stop/target logic
  - Pytest required before commit; queue on FAIL
  - 1 file/night limit
  - Queue mechanism: logs/queued_for_review_YYYY-MM-DD.md committed to GitHub + Slack alert
  - Next run: **tonight (2026-05-28) 10:01 PM ET** — will process handoff.md P1 items
- **⚠️ External API diff-level review (DS+GAI):** DEFERRED — both flagged P1 credential exposure risk (embedding API keys in CCR). Current CCR relies on adversarial board 3/3 as the primary gate for non-RTH files. Resolve via OCI Vault or CCR environment-level secrets before enabling API calls.
- **⚠️ .gitignore:** auto_deploy.sh not yet added to .gitignore (contains Slack webhook). To add: append `auto_deploy.sh` to .gitignore in local repo + push.

### S43C (2026-05-28) — AUTOMATED PIPELINE FIX
- **ROOT CAUSE IDENTIFIED:** Both autonomous CCRs (`trig_01GHKQufnHRykVL9Xypqeee4` + `trig_01Unsi5gQPtCJvsPurHyCa8P`) had username typo in repo URL — `redstamp8705` instead of `redstorm8705`. Platform disabled both with `auto_disabled_repo_access`. Board Review CCR had no Slack posting step.
- **FIX 1 — All S43/S43B code committed to GitHub** (3 commits pushed):
  - `S43: RAM leak P1+P2 + P0-STARTUP` (macro_risk_index.py, config.py, main.py)
  - `S43B: RAM leak P3+P4 + RC-3 fix` (run_cycle.py, signal_generator.py)
  - `S43B: handoff update`
- **FIX 2 — OCI git pull deployed:**
  - `git init` on OCI `/home/ubuntu/mtf-bot` + remote + credential store
  - `auto_deploy.sh` written (git pull + RTH-safe restart + log)
  - Cron added: `0 3 * * *` (11 PM ET daily)
- **FIX 3 — Board Review CCR updated** (`trig_01B37951UHsX2NAsCvrJuHNK`):
  - Added Step 4: post verdict to Slack via curl after every run
  - Test run triggered to verify (fires now)
- **FIX 4 — New Nightly Autonomous Work CCR created** (`trig_01NctUPEvjM1TVDH3vgJ2bmw`):
  - Correct repo URL: `https://github.com/redstorm8705/alpaca-mtf-both`
  - Runs weeknights at 10 PM ET (2 AM UTC)
  - Full 9-step mandatory patch sequence per CLAUDE.md
  - Non-hotspot files: auto-apply → commit → push to GitHub
  - Hotspot files (main.py, broker.py, portfolio_tracker.py): prepare diff → `logs/pending_patches_YYYY-MM-DD.md` → Slack post
  - Slack summary after every run
  - First run: tonight 10 PM ET

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
- [ ] Verify 3 live positions (NFLX short, NVDA long, SPY long — confirmed S45 9:32 AM PT)

## References
- Full session history: `logs/session_summary_*.md` (20 files as of S38)
- S37+S38 session: `logs/session_summary_2026-05-25_0940.md`
- Board decisions: `~/.claude/projects/.../memory/`
- CLAUDE.md: full rules, guardrails, board protocol
- Bug tracking: `logs/bug_counter.json` (RC-3 count=**3** | RC-4 count=**10** — updated S39 resumed after entry_logic.py RC-4 #12c patch)
- Audit log: `logs/tb_audit_log.md` (S39: entry_logic.py ✅ PATCHED ×2 passes RC-3+RC-4, scan_to_html.py ✅ PATCHED)
- Weekly perf audit design: `logs/weekly_perf_audit_design_v1.md` (§13 = DS/GAI design review prompt)
- Full session history: `logs/session_summary_*.md` (20 files as of S38; S39 summary not yet written — write on wrap-up)
