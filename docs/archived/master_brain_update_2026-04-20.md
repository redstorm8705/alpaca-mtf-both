# Master Brain Update — alpaca-mtf-bot
**Generated:** 2026-04-20
**Covers:** All sessions through Apr 20 2026. This is the authoritative consolidated state.

---

## BOT STATUS (as of Apr 20 2026)

- **Running:** YES — `main.py --profile paper` (PID 5820)
- **Account:** Paper ~$2,500 start → $2,903.91 (all-time realized = +$400.00, Alpaca FIFO)
- **Open positions:** NVDL long 3sh @ $94.20, SPY long 3sh @ $710.17 (verify at session start)
- **PDT used:** 3/3 slots — rolling window resets Mon Apr 20
- **Profile active:** paper | MIN_SCORE=9/12 | KELLY_FRACTION=0.25 | STOP=1.25×ATR | TARGET=3.125×ATR
- **Handoff version:** 2026.04.18.007

---

## 10-POINT REMEDIATION PLAN — BoD APPROVED APR 16, 2026

**STATUS: ALL 10 POINTS COMPLETE ✅**

| Point | Description | Status |
|-------|-------------|--------|
| 1 | Full static analysis (pylint + pyflakes) before any new features | ✅ COMPLETE — enforced at SESSION-START Step 0 every session |
| 2 | End-to-end trade path trace (signal → entry → exit → P&L) | ✅ COMPLETE Apr 18 — 4 bugs found (BUG-E2E-1 through 4); E2E-1 and E2E-4 fixed; E2E-2 and E2E-3 deferred P3 |
| 3 | Adversarial scenario testing (10 scenarios) | ✅ COMPLETE Apr 18 — S1–S3, S5–S10 PASS; S4 (kill switch restart) fixed as BUG-ADV-1 |
| 4 | File-by-file ownership + full reads on 2-week cycle | ✅ COMPLETE — all 10 files read Apr 16. Next due Apr 30, 2026 |
| 5 | Grep-verified handoff (SESSION-START CHECKLIST) | ✅ IN EFFECT — enforced every session |
| 6 | External audit schedule — biweekly | ✅ SCHEDULED — Gemini + independent AI agent audits run Apr 14, 18 |
| 7 | Feature freeze: P0+P1 resolved; P2 sprint complete | ✅ COMPLETE — all 14 P2 items deployed (incl. P2-WEEKLY-BIAS-CONFIRM Apr 18) |
| 8 | State persistence — kill switch, confirm gate, TQI history | ✅ COMPLETE — `kill_switch_state.json`, `confirm_gate.json` (atomic), `tqi_history.json`. `_atr_cache` excluded by board vote (DataFrame hazard) |
| 9 | Anomaly logging (5 proactive checks wired in RTH loop) | ✅ COMPLETE Apr 18 v.007 — ANOMALY-1 (stop failure rate), ANOMALY-2 (confirm gate stale), ANOMALY-3 (TQI degradation), ANOMALY-4 (MRI/news divergence), ANOMALY-5 (fill fallback rate). Globals `_rth_day_stop_failure_counts` + `_fill_fallback_count` declared in `main.py`, wired into `_submit_rth_day_stops()` and `_fetch_actual_fill_price()`, reset at daily EOD. |
| 10 | main.py full read each session it's touched | ✅ IN EFFECT — TB board runs full read whenever main.py is modified |

---

## GEMINI AUDIT — 10-ITEM SCORECARD (APR 18) — ALL ✅

| # | Bug | Status |
|---|-----|--------|
| 1 | Double daily reset corrupts SOD kill-switch baseline | ✅ FIXED |
| 2 | OVERNIGHT_ENTRIES_ENABLED=False bypassed on PDT=3/3 path | ✅ FIXED |
| 3 | GTC stop suppressed at PDT=3/3 (wrong FINRA interpretation) | ✅ FIXED |
| 4 | `partial_pnl` from wrong-direction accumulation corrupts exit P&L | ✅ FIXED |
| 5 | Kill switch silent disable when `daily_start_value <= 0` | ✅ FIXED |
| 6 | Zombie `qty_remaining=0` entries inflate overnight notional cap | ✅ FIXED |
| 7 | `submit_limit_order()` zero retry on transient failures | ✅ FIXED |
| 8 | `partial_close_position()` qty not validated before API call | ✅ FIXED |
| 9 | `alpaca_data.py` 429 rate-limit — no retry/backoff | ✅ FIXED |
| 10 | `alpaca_data.py` 402 stale bid/ask — no state flag | ✅ FIXED |

---

## APR 19 SESSION — VOTE-1 THROUGH VOTE-5 + NEW SCORING CONDITIONS

### New Features (board-approved, deployed Apr 19)

| ID | Description | File | Status |
|----|-------------|------|--------|
| VOTE-1 | c10 IBS: `(close-low)/(high-low) < 0.25` for LONG, `> 0.75` for SHORT — log-only in 20pt system | `signal_generator.py` | ✅ DEPLOYED |
| VOTE-2 | c11 Pivot S/R proximity — within 0.5×daily range of prior-day S1/R1 — log-only | `signal_generator.py` | ✅ DEPLOYED |
| VOTE-3 | c7 residual_rank: `ε_i = mom_12_1 - sector_avg` replaces mom_rank when available | `signal_generator.py` | ✅ DEPLOYED |
| VOTE-4 | c12 FOMC/macro day +1 LONG (log-only) — `is_macro_event_day()` gate | `signal_generator.py`, `events/calendar.py` | ✅ DEPLOYED |
| VOTE-5 | c13 PEAD +1 (log-only) — EPS beat >5% within 1-3 trading days | `signal_generator.py`, `data/fmp_client.py` | ✅ DEPLOYED |
| NEW-1 | SCORE_16PT_MAX expanded to 20 — accommodates c10–c13 (log-only, 12pt entry gate unchanged) | `signal_generator.py` | ✅ DEPLOYED |
| NEW-2 | ADDV $50M floor — symbols below avg daily dollar volume excluded from Phase 3 | `signal_generator.py` | ✅ DEPLOYED |
| NEW-3 | Phase 2b residual ranks — sector-adjusted momentum before Phase 3 | `signal_generator.py` | ✅ DEPLOYED |
| VOTE-3 SIZE | Volume Kelly cap: `dollar_cap × min(1.0, vol_ratio/1.5)` | `signal_generator.py` | ✅ DEPLOYED |
| VOTE-4 SIZE | SPY 200d MA overnight: when PDT=3/3 AND SPY < 200d MA → 0.5× size | `signal_generator.py` | ✅ DEPLOYED |
| VOTE-5 SIZE | Vol-target cap: `min(shares, (0.15/σ_20d_ann)×equity/price)` | `signal_generator.py` | ✅ DEPLOYED |

### Bugs Fixed (Apr 19 — independent AI agent audit)

| ID | Bug | Fix |
|----|-----|-----|
| C-1 | `_INTRADAY_OVERRIDE_THRESHOLD` 1.0% → 3.0% | Fixed |
| C-2 | Weekly bias cold-start returned None (fail-open) → fail-closed | Fixed |
| C-3 | `max_workers=4` → 2 (exceeded Alpaca 200 req/min) | Fixed |
| C-4 | `_get_pead_days()` silently swallowed ValueError → specific except at WARNING | Fixed |
| H-1 | `_intraday_override_cache` keyed by symbol only → keyed by `(symbol, direction)` | Fixed |
| H-4 | No per-future timeout → `timeout=20s` per future | Fixed |
| Bug-A | `_MACRO_EVENT_TYPES` missing GDP, FED_MINUTES, PPI | Fixed |
| M-4 | `get_earnings_surprise` imported inside function → module-level | Fixed |
| M-6 | MACD/EMA imported inside loop → module-level | Fixed |
| VOTE-6 | KNN signal — DEFERRED — requires CPCV framework, 500+ samples, IC ≥ 0.05 | Deferred |

---

## APR 20 SESSION — WEEKLY REVIEW FIXES

### Bugs Fixed in `weekly_review.py` and `execution/portfolio_tracker.py`

| ID | Severity | Bug | Fix | File |
|----|----------|-----|-----|------|
| C-1 | Critical | Footer shows wrong Stop/Target mults (reads live=1.0x/2.0x, not paper=1.25x/3.125x) | `_paper_profile = getattr(_cfg, "PROFILES", {}).get("paper", {}); _stop_mult/tgt_mult from paper profile dict | `weekly_review.py` |
| C-2 | Critical | Score 8/12 row shows "—" WR because `n_min_show=3` — should always show | `_n_min_show=1`; always compute `_wr_str`; add `(n=X)` label for small samples | `weekly_review.py` |
| C-3 | Critical | Exit reason `external_close_detected_ah` shows raw/unformatted | `_fmt_reason()` helper promoted to module level — `.title()` + explicit replacements (AH, GTC, PDT, EOD); used in both `_strategy_validation_html()` and `build_html()` | `weekly_review.py` |
| H-1 | High | Avg TQI shows "—" — `tqi_score` never written to trade record | `_record_tqi()` now writes `trade["tqi_score"] = tqi` before returning | `main.py` |
| H-3 | High | Overnights stat shows lifetime WR — inconsistent (tile says "this week") | Hoisted `_all_week_trades` build before stats block; compute `_ovnt_wk_wr` from weekly trades only | `weekly_review.py` |
| H-4 | High | Footer missing "(paper profile)" indicator | Added "(paper profile)" to footer string | `weekly_review.py` |
| H-5 | High | Avg Intraday Hold shows "—" with no explanation | Added dynamic subtitle: "no intraday closes" when None, "same-day only" otherwise | `weekly_review.py` |
| M-2 | Medium | `_classify_loss_driver()` returns display strings ("⚙ BUG IDENTIFIED") from data layer | Changed to semantic labels ("MECHANICAL"/"STRATEGY"); `build_html()` maps to display strings | `weekly_review.py` |
| M-3 | Medium | `model="claude-sonnet-4-6"` hardcoded in `_run_analysis()` | Changed to `model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")` | `weekly_review.py` |
| M-5 | Medium | `_list_archive_weeks()` may include future dates | Filter: `return sorted(w for w in weeks if w <= date.today() + timedelta(days=7))` | `weekly_review.py` |
| PNL-DAYFIX | Critical | Weekly P/L double-counts overnight partial exits (e.g. QQQ partial $22.65 counted on Apr 16 AND in full pnl on Apr 17) | For overnight positions where `partial_exit_time < today`: use `pnl_remaining` only (final-close P&L) instead of full `t["pnl"]` | `execution/portfolio_tracker.py` |

---

## OPEN ITEMS — ALL SESSIONS (as of Apr 20, 2026)

### Priority 1 — Needs Board Vote Before Implementation
| ID | Item | Notes |
|----|------|-------|
| VOTE-6 | KNN signal | Separate session; needs CPCV framework, 500+ samples, IC ≥ 0.05 validation |
| SCORE-WGT-SIZING | Score-weighted warmup sizing | Kelly fallback modulated by signal score (score/12 × MAX_PORTFOLIO_RISK_PCT). Harris proposal deferred. |
| EOD-ALPACA-FILLS | EOD recorder → Alpaca fills source | Structural change to pull realized P&L from Alpaca fills at EOD instead of tracker accumulation |

### Priority 2 — Architectural Fixes
| ID | Item | Notes |
|----|------|-------|
| M-3-SECTOR | `_SECTOR_MAP_SG` duplication | Circular import blocks `main.py` approach; needs `data/sectors.py` shared module (board vote on scope) |
| MAX-1-FLOOR | `max(1,...)` shares floor bug | Forces 1-share entries when VOTE-3/4/5 + event_size_mult compound below 1 share; needs explicit min-shares skip guard |
| MACRO-DYN | `is_macro_event_day()` dynamic events gap | Misses FMP-sourced events added via `calendar.add_event_dynamic()` at runtime; needs live calendar instance passed in |
| EOD-DRIFT | EOD→Alpaca fills drift monitoring | Phase 2 live 2026-04-19; monitor `drift` field in eod files for 10 trading days (Shaw gate before structural EOD fix) |

### Priority 3 — Deferred / Low Priority
| ID | Item | Notes |
|----|------|-------|
| BUG-E2E-2 | `check_partial_exits()` writes partial P&L directly, bypasses `tracker.record_partial_exit()` | Low priority |
| BUG-E2E-3 | Reversal exit uses `current_price` not actual Alpaca fill | Low priority |
| KELLY-REBUILD | `kelly_stats.json` rebuild | Verify `rebuild_from_trades()` fires correctly at Monday EOD |
| STRAT-VAL | Strategy validation card | Win/loss stats still from tracker (trade_log.json); needs Alpaca fill attribution |
| TB-4-DERMAN | Derman premium approach | Raise effective MIN_SCORE by 2 when daily data missing. Fail-closed is live as interim. |
| H-7-SPREAD | `submit_limit_order()` spread validation | Redundant (upstream gate already enforces) — P3 defer |
| H-11-FILL | `_fetch_actual_fill_price()` fixed sleep | Performance improvement — P3 defer |
| M-1-SCORE-CTX | ScoreContext dataclass | 11-param function refactor — large scope, new bug risk. Deferred. |
| BACKTEST-T1 | `backtest_12pt.py` still uses `yf.download()` | T1 migration pending |

---

## ALPACA FIFO GROUND TRUTH (all sessions through Apr 18)

| Date | P&L Today | Cumulative |
|------|-----------|------------|
| Apr 6 | −$2.76 | −$2.76 |
| Apr 7 | +$57.38 | +$54.62 |
| Apr 8 | −$6.43 | +$48.19 |
| Apr 9 | +$133.15 | +$181.34 |
| Apr 10 | +$49.02 | +$230.36 |
| Apr 13 | +$5.25 | +$235.61 |
| Apr 14 | +$30.09 | +$265.70 |
| Apr 15 | +$29.19 | +$294.89 |
| Apr 16 | +$113.70 | +$408.59 |
| Apr 17 | −$8.59 | +$400.00 |
| Apr 18 | $0.00 | +$400.00 |

---

## ARCHITECTURE INVARIANTS (LOCKED — FULL BOARD VOTE REQUIRED TO CHANGE)

1. **SPY 5-min bar-over-bar is the SOLE entry gate.** MRI and Macro Regime only adjust size/score floor.
2. **Keywords CAUTION/MONITOR = zero size impact.** Only HALT = 0.0x.
3. **PDT is a hard 3-slot rolling window.** At PDT=3/3: GTC stop-market submitted.
4. **Bar staleness uses CLOSE-based age.** `_bar_ts_et + timedelta(minutes=15)`.
5. **Entry price = Alpaca Data real-time last trade + bar close fallback.**
6. **Kill switch is 15% for paper.** Revisit ONLY when equity exceeds $25K.
7. **Bucket A (TQQQ/SQQQ/TSLL) exemption from safe_close_all** — ONLY on routine news halts. Circuit-breaker closes everything.
8. **paper=True hardcoded in broker.py.** Change to False ONLY at live launch after full board vote.
9. **MRI is background only.** Sets size floor and MIN_SCORE floor. Does not gate entries directly.
10. **Max correlated exposure:** No more than 2 simultaneous positions with beta correlation >0.7.
11. **Overnight exposure budget:** Max 100% of account equity.
12. **VIX-adjusted stop widening:** VIX 25–30 → 1.5× stop mult; VIX >30 → 2.0×. Target scales proportionally.
13. **All P&L from Alpaca fills API only** — tracker math is cross-check only; not authoritative.

---

## SCORING SYSTEM

### 12-Point Live Entry System (unchanged)

| Component | Points | Timeframe | Data |
|-----------|--------|-----------|------|
| Weekly trend bias | 2 | Weekly | `fetch_bars(TF_WEEKLY, 14)` T1 |
| Intraday override | 1 | Daily | `fetch_bars(TF_DAILY, 2)` T1 |
| 5-min bar-over-bar (SPY gate) | 2 | 5-min | Alpaca T1 |
| RSI position | 2 | 15-min | Alpaca T1 |
| VWAP position | 2 | Intraday | Alpaca T1 |
| FVG (Fair Value Gap) | 1 | 15-min | Alpaca T1 |
| Volume confirmation (RVOL) | 1 | 5-min | Alpaca T1 |
| Pre-market range confluence | 1 | Pre-market | Alpaca T1 |
| **Total** | **12** | | |

### 20-Point Validation System (log-only — does NOT gate entries)

| Component | Points | Notes |
|-----------|--------|-------|
| 12pt live system | 12 | Live gate |
| c10 IBS | 1 | Log-only |
| c11 Pivot S/R | 1 | Log-only |
| c12 FOMC macro day | 1 | Log-only (LONG only) |
| c13 PEAD | 1 | Log-only |
| Residual rank (c7 upgrade) | — | Replaces mom_rank in phase 2b |
| **Total** | **16 active / 20 max** | |

---

## KEY USER PREFERENCES

1. **Approval required before editing any code file** — present diff and wait for confirmation
2. **Single-word approvals accepted** — "yes", "go", "approved", "proceed"
3. **All P&L from Alpaca fills only** — user will challenge any figure not Alpaca-sourced
4. **All timestamps in PT** (America/Los_Angeles) — never ET or UTC in user-facing output
5. **Terse responses** — no trailing summaries or "in conclusion" paragraphs unless asked
6. **Board audit protocol is mandatory** — pre-existing bug scan of every opened file required, not optional
7. **TB board failure acknowledged** — Apr 19: independent AI agent found 4 critical bugs TB missed; root cause was board not running pre-existing scan protocol per CLAUDE.md
8. **RTH block enforced** — no analysis/backtest scripts during 9:30 AM–4:00 PM ET weekdays

---

## SESSION HISTORY REFERENCE

| Date | Key Work | Files |
|------|----------|-------|
| Apr 7 | Emergency session — GTC cancel bug, watchdog, partial close alerting | `main.py`, `generate_dashboard.py` |
| Apr 8 | Stale bar fix, entry price, yfinance timeouts, PT timestamps | `main.py`, `signal_generator.py`, `alerts.py`, `weekly_review.py` |
| Apr 8–9 | Multi-session audit: P5 sprint (17 fixes), PDT centralization, dashboard redesign | Multiple |
| Apr 13 | Lock fix, Build #4 Layer 4 MIN_SCORE gate + win-side size boost | `main.py` |
| Apr 14 | Gemini audit GEM-1–6, phantom GTC fix, weekly review redesign (WRD-1–14) | Multiple |
| Apr 15 | DATA migration (yfinance→T1), FMP T2 client, structured JSONL logging, PT timezone fixes | Multiple |
| Apr 15–16 | Bot restart patches, external close P&L fix, P0/P1 full-file audit | Multiple |
| Apr 16 | Direction-mismatch fix, 10-point plan initiated | `main.py` |
| Apr 17 | Gemini audit 10-point scorecard (all ✅), P2 sprint (14 items), adversarial testing | Multiple |
| Apr 18 | Independent audit triage (26 findings), 6-item sprint, MRI breakeven push, Kelly calibration, state persistence atomic fix | Multiple |
| Apr 18 (evening) | P&L correction to Alpaca FIFO ground truth, EOD file bulk-update, dashboard Total P&L fix | `generate_dashboard.py`, EOD files |
| Apr 19 | VOTE-1/2/3/4/5, NEW-1/2/3, c10–c13 scoring, 13-bug independent AI audit response, anomaly logging wired | `signal_generator.py`, `main.py`, `events/calendar.py`, `data/fmp_client.py` |
| Apr 20 | Weekly review 11-bug sprint (C-1 through M-5), P/L double-count fix, `_fmt_reason()` module-level | `weekly_review.py`, `execution/portfolio_tracker.py`, `main.py` |
