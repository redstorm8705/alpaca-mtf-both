# CLAUDE.md roadmap log and deferred items (moved verbatim from CLAUDE.md, TOKEN-CUTS-1108, 2026-11-08)

This text was in CLAUDE.md, which loads into every session and subagent. It is history and ideas, not rules,
so it lives here and is read on demand. Add new future ideas here (see the pointer in CLAUDE.md).

## FUTURE ROADMAP LOG

**Purpose:** Capture items mentioned in passing — innovations, moonshots, full-session-scope ideas —
so they are never lost between sessions. These are NOT open items (no action this session).
They are logged here so that when we have bandwidth, we know exactly what to build next.

**Add rule:** Any time a future-state idea, novel approach, or multi-session scope item is mentioned
in conversation — even briefly — log it here before the session ends. Format: `[DATE SESSION] Description`.

**Review rule:** At the start of any session where the user asks "what's next" or "what should we build,"
scan this log first alongside the open items list.

---

### SHADOW STRATEGY TRACKER — revisit each every session (Rafael mandate 2026-07-06)

> **SUPERSEDED 2026-09-26 (Rafael):** no shadow periods until $25K. The entries below either go LIVE after a front-loaded replay and the gate, or are removed. The sample-count triggers no longer apply. See "THIS IS A PAPER TRADING ACCOUNT".

**Every shadow signal is logged-but-not-live until it earns its weight. Check this table each session; when a revisit trigger is met, run the analysis and bring the flip to the board.** A shadow that is never revisited is wasted data.

| Shadow | Flag (config.py) | What it logs | Revisit trigger | Flip requires |
|--------|------------------|--------------|-----------------|---------------|
| **Delta-of-signal** (Cedar "trade the delta") | `DELTA_SCORING_ENABLED=False` | `delta_shadow` events in `trade_events.jsonl` — bar-over-bar change in the feature-isolated (non-momentum_12_1) confluence score, off_the_fence, persistence, `momentum_flipped` | **≥50 delta_shadow samples** (excluding `momentum_flipped=True` rows), then an **LdP feature-importance check**: does the delta explain **>5% of win variance independent of EMA/MACD/momentum_12_1**? | Fresh **board vote** (Architecture Invariant #1) → flip flag + wire the +0.5→1 bonus into scoring. Est. added 2026-07-06. |
| **Volume confirmation** | `VOLUME_CONFIRMATION_ENABLED=False` | `VOLSHADOW` log lines — vol_ratio, would_pass, closed-bar pctile | **≥30 post-close STOD-normalized samples** → derive `VOLUME_THRESHOLD` percentile-based (currently a static 1.5× = ~8.8% pass rate, miscalibrated) | Board vote + the two-step atomic toggle (swap `rsi_in_range`→`volume_confirmed` in SCORE_WEIGHTS simultaneously). |
| **16-point scoring** | log-only (shadow 16pt beside live 12pt) | `score_16pt` in entry events | **50 / 100 / 150 / 200 evaluable trades** (`score16_milestones.json` Slack reminder) | Board review of the 16pt-vs-12pt edge before any weight migration. |

*(GEX Layer-8 and TSMOM vol-scaling are staged-ACTIVE, not pure shadows — tracked under Standing Conditions in handoff.md, not here.)*

### Logged Items

**[2026-07-03 MOVERS-RETIRED] Movers bot RETIRED — do NOT re-enable without per-strategy ownership fix (Rafael decision)**
The Movers bot (`run_movers.py` / `strategy/movers/strategy.py`) is RETIRED. Its cron was DISABLED 2026-07-02 after a mass-dump incident (09:32 ET it flattened 8 positions in 3s — HOOD + GOOGL/MARA/NVDA/PANW/RIVN/SNOW/TSLA, including QHM holds GOOGL+NVDA). Full Phase-1 diagnosis 2026-07-03 (board 2 cold agents + Gro + GAI, all 4 aligned): the main bot and Movers share the SAME per-symbol Alpaca lots with NO ownership tag, so `broker.close_position(sym)` (non-divisible) closes the whole symbol regardless of which strategy opened it — **7 bidirectional collision vectors**. TRUE root = the main bot dropping a still-live position from `trade_log.json` `open` via a false `record_exit` (`portfolio_tracker.py:1805` via `orphan_manager.py:1195`), which makes the lot look unowned so Movers' adopt-by-default `reconcile_on_startup` sweeps it up. The "obvious" guard fix (main-bot open-book check in `check_exits`/`flatten`) is only ~60% and does NOT stop the actual HOOD path. **DECISION: keep Movers retired.** All 7 vectors are dormant while it's off, so no partial patch was shipped. **Re-enabling is FORBIDDEN until the real fix ships: per-strategy `client_order_id` ownership tags + qty-bounded partial close (Feature Design Protocol design session).** Remaining work regardless of Movers: fix the main-bot false-drop ROOT (existing P0 fill-matching item — corrupts main-bot P&L independent of Movers). Cron line stays commented in the OCI crontab with its DISABLED note. Optional hardening not yet applied: a refuse-to-run guard at the top of `run_movers.py:main()` to make retirement bulletproof against manual/accidental launch.

**[2026-07-03 DEPLOY-FIX] autonomous_review.py pushes directly to `main` — conflicts with git-single-channel deploy (Gro + GAI Point-3, board vote required)**
During the Option-A deploy-mechanism codification audit, both Gro and GAI independently flagged that `autonomous_review.py` (nightly cron on OCI) runs `git pull`/`git apply`/`git commit`/`git push` directly against `main` — the same branch that is now the sole deploy channel. This means `main` can advance out from under an operator deploy (non-fast-forward push rejection) or interleave an un-approved autonomous commit into a live deploy. The mitigations shipped 2026-07-03 (capture `DEPLOY_SHA` for rollback; push-rejection retry-with-rebase; `DEPLOY_OK` marker) handle the symptom. The structural fix GAI recommends: `autonomous_review.py` should push to a dedicated branch (e.g. `autonomous/suggestions`) and open a PR / require an explicit merge to `main`, rather than pushing to `main` directly. This is a redesign of the autonomous pipeline's git flow — separate design session, Feature Design Protocol gate first, full board + Gro/GAI. Files: `autonomous_review.py`, `auto_deploy.sh`. Not urgent (nightly crons rarely collide with ad-hoc deploys) but it is the last structural gap in the single-channel model.

**[2026-07-03 S-GEX] UX total redesign — all 5 HTML pages (Rafael directive, QUEUED behind critical bugs)**
Rafael wants a total UX redesign of dashboard.html, weekly_review.html, scan_results.html, options page, and monthly_review.html. Explicitly queued behind critical-bug work. When picked up: Feature Design Protocol gate first (data sources unchanged — this is presentation-layer only), Luke Wroblewski (TB, UI/UX mobile-first seat) leads the board input. Scope: 5 pages, shared design system, PT timestamps preserved, mobile-first.

**[2026-07-03 S-GEX] Evolution mandate — bot must be constantly learning, not just reactive (Rafael directive)**
Rafael: "I want a trading bot that is constantly evolving leveraging new insights, strategies, and inputs." Static-vs-dynamic audit (this session) found the structural gap: the bot has strong REACTIVE dynamism (VIX continuous curve, MRI, 9-layer dynamic floor, session-P&L feedback) but near-zero LEARNING dynamism — the only parameter that updates from its own trade outcomes is Kelly. All four designed learning loops had broken evidence chains (GEX: dataless 9 weeks, fixed 2026-07-03; score_16pt: never reached trade_events; TSMOM: signal dicts never carried the fields, sizing multiplier was a permanent no-op despite board 17-0 "active"; volume shadow: collecting fine but threshold statically miscalibrated at 1.5x = 8.8% pass rate). Path: (1) fix evidence chains, (2) stand up the walk-forward/IC recalibration engine (S59 roadmap item) as a standing weekly job producing parameter-update proposals for board approval, (3) re-derive miscalibrated statics from accumulated shadow data (volume threshold percentile-based; conviction spline S63).

**[2026-06-30 CROSS-STRATEGY AUDIT — ongoing MTF FULL AUDIT work] Full cross-strategy audit required: strategies sharing Alpaca lots without mutual awareness**
The QHM/Movers collision (NVDA and GOOGL quarterly holds repeatedly reset by Movers strategy exits) confirmed a class of bug that static file-by-file audits cannot catch: two independent strategies trading the same Alpaca symbol simultaneously, with no cross-strategy ownership protocol. The Movers fix (`get_quarterly_hold_symbols()` guard before `close_position()`) addresses the confirmed symptom, but a broader cross-strategy audit is needed to find all other potential collisions:
(1) Can the main intraday MTF bot and the Movers strategy conflict on the same symbol in similar ways? The `_quarterly_hold_symbols` block in `entry_logic.py` prevents intraday ENTRIES into QHM symbols, but does it also prevent exits? (2) Can the main bot's `safe_close_all()` or `check_exits()` paths close a QHM position? (3) Does the QHM's own `_detect_external_close()` correctly handle a partial close (e.g., main bot exits its 1-share position while QHM still holds 1 share of the same symbol)? (4) Are there any other strategy-pair interactions where one strategy's action can misfire the other's state machine? This audit should be part of Phase 3 of the ongoing MTF FULL AUDIT and must run domain agents across strategy/movers/strategy.py + execution/quarterly_hold_manager.py + execution/entry_logic.py + strategy/run_cycle.py's exit/close paths simultaneously.

**[2026-06-30 ATH STOP-TIGHTENING — Open Question Protocol resolved, implementation queued] ATH-relative stop tightening at entry**
Board 3-1 (Simons/Thorp/Brandt vs Taleb), Gro NEEDS-CHANGES, GAI NEEDS-CHANGES — all 6 voices converged on the same approved mechanism: entry-level only (not trailing), 10% tighter stops (0.90x scalar) when SPY is within 1% of its 52-week high, applied after VIX scalar, before leverage multipliers in `execution/risk_manager.py:get_stop_and_target()`. Unanimous on using ATR-based thresholds (not fixed -5%/-10%) if a trailing variant is ever explored. Trailing tightening at -5% from ATH was voted 3-1 against (whipsaw cost ~200-300 bps annually outweighs protection benefit when T1/T2 ratchets already exist). Separate board session required before adding any trailing variant. This is a 1-session implementation: find the `stop_mult` computation in `get_stop_and_target()`, multiply by `ath_proximity_scalar = 0.90` after VIX scalar, apply same scalar to `target_mult` to preserve R:R.

**[2026-06-28 MTF FULL BOT AUDIT — Phase 2 redo] news_monitor.py: macro-event classification is recency-based, not domain-severity-weighted (governance question)**
Three related findings from the same underlying design choice, surfaced by the Quant-logic and Execution-risk domain agents during the Phase 2 full-board redo: (1) `get_active_event_type()` iterates active alerts newest-first and returns on the first domain match for that one alert -- documented as a deliberate prior fix ("Finding 3", 2026-04-06: "most recent alert wins classification over stale older alerts"). This means a less-severe but more-recent alert (e.g. a Fed rate cut) can override classification even while a more-severe, slightly-older alert (e.g. an active geopolitical conflict) is still within the 30-min active window -- recency wins over domain severity by design, not by accident. (2) `_macro_risk_active`'s threshold logic (`>= 3` to activate, `== 0` to deactivate) has no hysteresis, so it can flicker true/false near the 3-alert boundary even when the underlying situation is stable; `get_active_event_type()`'s fallback path consumes this exact flag, so flickering could flip the hybrid engine's chosen scan-persistence-duration (3 to 8 scans depending on event type) for the same real-world event. (3) That same fallback hardcodes "GEO_CONFLICT" for any active-but-unmatched state regardless of what's actually in the 24h macro-risk window (could be FX/credit/monetary alerts, not conflict-related at all) -- documented as "most common backdrop," not derived from the window's actual contents. All three are real, verified-against-source design choices, not unintended bugs -- but together they represent a coherent improvement opportunity (score by domain frequency/severity across the window instead of recency + hardcoded fallback) that deserves explicit board input given the cross-file consequence on MacroRiskIndex's scan-persistence logic. Files: `events/news_monitor.py` (`get_active_event_type()`, `_macro_risk_active` threshold logic).
`_get_quarterly_notional_excl()` (used to subtract "other live quarterly hold notional" from available equity before sizing the next tranche, per the Derman/Thorp Kelly fix) only counts positions in `ACTIVE`/`AWAITING_FILL` state. Positions in `PENDING_STOP_REPLACE`, `PENDING_EARNINGS`, or `PENDING_EXIT` are fully filled and economically equivalent to ACTIVE (same qty_filled, same market exposure — just temporarily stop-less or earnings-paused) but are excluded from this sum, meaning available_equity is overstated whenever any quarterly hold sits in one of those three states. Found by the Quant-logic domain agent during the Phase 2 full-board redo of this file, with a worked example showing an 18% overstatement of available equity in a 3-position scenario. Not patched — this is a sizing-policy question (does the board want non-ACTIVE-but-filled quarterly holds to reserve margin the same way ACTIVE ones do?), not an unambiguous bug, and deserves explicit board input rather than a unilateral fix. File: `execution/quarterly_hold_manager.py`.

**[2026-06-28 MTF FULL BOT AUDIT — Phase 2 redo] broker.py: inconsistent 40310000 retry/escalation across 4 sibling functions**
`submit_gtc_stop_order()`, `submit_day_stop_order()`, `partial_close_position()`, and `close_position()` all handle Alpaca's `40310000` (held_for_orders) error, but inconsistently: GTC waits 3s then polls up to 60s and fires a Slack CRITICAL alert on exhaustion (well-documented MSTR incident, board vote 2026-04-27/30); the other three cancel blocking orders and retry exactly once with zero wait, and none of them alert on exhaustion (a DAY stop or partial/full close that fails after the single retry leaves a position unprotected or stuck, silently). Quant-logic domain agent flagged this as a model-risk-style coherence gap during the Phase 2 board redo. Not acted on — speculative without a documented incident for the other three functions (unlike GTC's MSTR case), and GAI/Gro both explicitly agreed this should be a future review item, not an immediate fix. Revisit if any of the three undocumented functions has a real incident, or as part of a future consistency pass. File: `execution/broker.py`.

**[2026-06-28 MTF FULL BOT AUDIT — Phase 2 redo] run_cycle.py AH GTC: no H2-override awareness**
`execution/risk_manager.py`'s RTH stop-widening path checks for an `h2_scalar` (vol-regime) override and skips the VIX continuous-curve formula entirely when active (`if h2_scalar is not None and h2_scalar > 0: ... elif vix > 0: ...`). `strategy/run_cycle.py`'s after-hours GTC overnight-stop block (just brought back into VIX-curve parity, commit `3a7677b`) has no equivalent H2-awareness — it always uses the raw VIX curve, with no check for whether RTH set the position's actual stop using an H2 override instead. Not a regression (the AH block never had H2 awareness, before or after the VIX-parity fix) and out of scope for that narrow fix. Flagged by the cold second-agent during that patch's review. If H2 overrides ever diverge meaningfully from the raw VIX curve in practice, this could reintroduce a smaller version of the same parity gap. File: `strategy/run_cycle.py`. No board vote required yet (informational — revisit if H2 override usage becomes material).

**[2026-06-28 MTF FULL BOT AUDIT] news_monitor.py executor health-check / periodic recreation**
`scan_breaking_news()`'s `self._executor` is a persistent `ThreadPoolExecutor(max_workers=6)` reused every RTH scan cycle. On a 12s `as_completed()` timeout it correctly does NOT call `shutdown()` (that would permanently break every future cycle's `.submit()` on this same persistent pool) — it just logs the skipped source names and lets the stuck thread keep running in the background. But a thread that hangs past 12s without ever returning (bypassing its `requests.get(timeout=...)` bound — e.g. a `feedparser.parse()` CPU-bound parse on a malformed feed, or a DNS/SSL stall some `requests` versions don't cover via `timeout=`) permanently occupies one of only 6 worker slots until the bot's next restart. Cumulative across many consecutive hangs, this could eventually exhaust all 6 slots, silently degrading news coverage to zero without crashing run_cycle() (each `as_completed(timeout=12)` call just times out faster). Not capital-risk — this module is informational-only (SPY 5-min price action is the sole sizing trigger per its own MARKET-REACTION-FIRST architecture doc) — and mitigated today by the nightly restart cron, but a proper fix (periodic executor health-check + recreation if too many stale futures accumulate, or a fresh per-cycle executor instead of a persistent one) would close the gap. File: `events/news_monitor.py`. No board vote required (reliability/observability only, not sizing/scoring/execution logic) — implementation-only session.

**[2026-06-28 MTF FULL BOT AUDIT] Sizing-dial redesign — sustain-window + 3-bucket framework (Rafael mandate)**
`strategy/run_cycle.py`'s `size_multiplier` directly multiplies 6 independent stress factors (event/regime/TOD/SPY-risk/P&L/overnight) every cycle off a single momentary snapshot. Audit found this can compound to ~19% of intended size from individually-mild factors (none of which alone implied that severe a cut). Rafael's decision: this needs two structural changes, not a quick patch — (1) require risk dials to be **sustained across multiple consecutive 5-min scans** before they compound, rather than reacting to one momentary snapshot (exact scan-count threshold deferred to a dedicated board + Gro/GAI design session — not guessed); (2) split sizing logic into 3 buckets: **A) intraday** (current dial set, with the sustain-window added), **B) 1-2 week swing holds** (Rafael: needs an **entirely separate framework** built around higher-timeframe confluence indicators — do not reuse/filter the intraday dials), **C) quarterly QHM holds** (already structurally separate — confirmed during this audit that `qhm.maybe_enter_positions()` never receives `size_multiplier` at all, so this bucket already exists in practice). Requires: a dedicated design session (Feature Design Protocol gate first), board + Gro/GAI on the sustain-window threshold, full Open Question Protocol given the architectural scope. File: `strategy/run_cycle.py` (Bucket A), new module TBD for Bucket B.

**[2026-05-17 S25B] After-close STOD volume normalization for swing pre-filter (DS insight)**
Compute `today's total volume / 20-day avg` at 4:05 PM ET as an after-close computation.
Eliminates the partial bar problem entirely (full bar vs full bar, apples-to-apples).
This is the prerequisite before Option B volume scoring can work at all.
Requires: ≥30 samples of after-close data, dedicated board vote, STOD normalization design.
Target: after 5+ weeks of shadow data accumulation. File: `events/macro_risk_index.py` + new after-close hook.

**[2026-05-17 S25B] Volume additive scoring — Option B (+1pt when vol_ratio ≥ 1.5x, never subtractive)**
Additive only: +1pt when today's STOD-normalized volume clears the 1.5x threshold.
Never gates or blocks entries — never subtractive. Blocked until STOD normalization is built first.
Requires: ≥30 post-close samples, STOD design complete, full board vote.

**[2026-05-17 S25B] GEX levels from Alpaca option chain wired into scoring (GAP-I2)**
Gamma exposure (GEX) levels as a 1pt confluence modifier or MRI input.
`get_option_chain` MCP tool is already available. Need GEX computation formula + board vote on scoring impact.
Could use GEX-neutral zone as a "caution" signal and GEX magnet levels as target confirmation.

**[2026-05-17 S25B] Real-time VIX3M source investigation**
FMP `^VIX3M` = 402 Premium. CBOE free CSVs = end-of-day only. CBOE real-time API = requires investigation.
Finnhub VIX3M = null. yfinance VIX3M = works but subject to hanging (current T4 fallback).
Future: dedicated research session to find a free or low-cost real-time VIX3M source.
Options: CBOE paid tier, Quandl, Tiingo, or derive synthetic VIX3M from option chain data.

**[2026-05-17 S25B] ThreadPoolExecutor blocking-call pattern as reusable template**
The timeout wrapper built for yfinance (#14) — `concurrent.futures.ThreadPoolExecutor(max_workers=1)`
with 8s wall-clock timeout + `shutdown(wait=False)` + stale_since tracking — is a reusable template
for any future external API calls that might hang (Finnhub, Currents, Marketaux, FMP slow endpoints).
Future: extract into `utility/safe_fetch.py` as a shared decorator/wrapper once #14 is validated.

**[2026-05-17 S25B] TraderMonty breadth CSV integration (GAP-V8)**
TraderMonty breadth CSV not yet implemented — `data/breadth.py` stub exists. Free, no API key.
Contains: market breadth, sector uptrend ratios. Could serve as a VIX-alternative breadth component
for MRI scoring (GAP-V7: breadth as ±1pt confluence modifier). Board vote required before wiring into scoring.

**[2026-05-17 S25B] Dedicated debug session — RC-3 root cause elimination (22 instances)**
RC-3 (silent exception, count=22) is the highest recurring bug class. Instead of patching each instance
individually, a full debug session should map all 22 instances, find the structural cause (likely missing
logging standard in exception handlers + no linter rule), and eliminate the pattern permanently.
Candidate fix: add a custom ruff/flake8 rule that rejects bare `except: pass` and bare `except Exception: pass`.

**[2026-06-15 S59] TCA / Execution Quality Monitoring — DS+GAI consensus P0 whitespace (architecture audit)**
Bot runs completely blind to slippage, fill latency, and whether Alpaca executes at NBBO. Every trade since launch has unmeasured execution cost. DS estimates 5–15% of P&L silently lost. GAI cites O'Hara (1995) and Almgren & Chriss (2001). File: `execution/execution_quality.py` (new, ~600L). Requires: order-level fill data from Alpaca API, benchmark (arrival price, VWAP), slippage/latency alert. Board vote required (touches broker.py). Blocked by: paper trading makes slippage measurement artificial — prioritize at live launch.

**[2026-06-15 S59] Portfolio Correlation Aggregator — DS+GAI consensus P0 whitespace (architecture audit)**
Beta>0.7 check is insufficient. Two positions can have beta<0.7 to SPY but 0.9 to each other. In tail events, all correlations converge to 1. DS calls this "existential" (50% single-day loss scenario). GAI cites Markowitz (1952) and risk parity. File: `risk/correlation_matrix.py` (new). Requires: rolling 20-day pairwise correlation across all open positions, portfolio VaR/CVaR, reject entry if portfolio VaR >2.5% equity. Board vote required (touches risk_manager.py). P1 — implement before scaling beyond 3 concurrent positions.

**[2026-06-15 S59] Alpha Decay & Walk-Forward Validation — DS+GAI consensus P1 blind spot (architecture audit)**
12-point scoring weights are static. DS estimates 20–40% edge evaporation over 3–6 months in factor regime changes. GAI cites Aronson (2007). Shadow 16-point system logged but never analyzed — no pipeline to detect stale signals. File: `research/walk_forward_optimizer.py` (new). Requires: IC monitoring per factor, quarterly re-validation run, adaptive weight suggestions. Board vote required before any weight changes. Note: the 16-point shadow log is the raw material for this — start by analyzing it.

**[2026-06-15 S59] Bar-end adverse selection — DS-only P1 blind spot (architecture audit)**
50–70% of entries occur in the last 20% of the 5-min bar's range, paying 10–25bp adverse selection. At 250 trades/year with 2.5× ATR targets, DS estimates 500–1250bp of gross P&L lost annually to entry timing. Fix candidates: (a) mid-bar check at 2.5 min into bar with limit order, (b) sub-bar tick data confirmation. File: `execution/entry_logic.py`. Requires: 1-sec quote stream from data/alpaca_data.py. Board vote required. Prioritize after walk-forward validation confirms which factors drive the adverse selection.

**[2026-06-24 S63] ✅ COMPLETED — VIX stop widening → continuous curve (Rule 12 dynamization)**
Replaced static step-function (VIX>25→1.5x, VIX>30→2.0x) with a continuous linear curve.
Formula shipped: `scalar = min(1.0 + max(0, VIX - 20) × 0.1, 2.0)` (Round 2 — Simons' candidate
slope of 0.025 was rejected; 0.1 preserves all current policy anchors exactly: VIX=25→1.5x,
VIX=30→2.0x). Eliminates the VIX=24.9→1.0x / VIX=25.1→1.5x cliff. Board 4-0, Gro APPROVE, GAI
APPROVE. Commit `7e5c983`. File: `execution/risk_manager.py:get_stop_and_target()`.
**Documentation note (2026-06-28 AWP audit):** this entry sat un-updated for 4 days after
shipping — Architecture Invariant #12 also described the old step function as current the whole
time. Both corrected 2026-06-28. Separately found and fixed in the same session: the after-hours
GTC overnight-stop mirror in `strategy/run_cycle.py` (comment claimed "mirrors risk_manager.py
RTH logic") had not been updated to the continuous curve either — parity restored, commit
`3a7677b`.

**[2026-06-24 S63] Conviction thresholds → linear spline (Rule 9 dynamization — board vote required)**
Replace cliff (score<10=skip, score=10=half, score≥11=full) with linear spline:
`sizing_mult = max(0, (score - 9) / 3)` → 9=0x, 10=0.33x, 11=0.67x, 12=1.0x.
LdP: CPCV shows linear outperforms cliff in walk-forward tests. GAI: 0.35 Sharpe uplift / 2 sessions.
4/4 voices unanimous. Requires full board vote. Files: `execution/entry_logic.py`, `execution/kelly.py`.

**[2026-06-24 S63] Score-weighted Kelly pre-warm — DS-only P2 optimization (architecture audit)**
First 30 trades use flat fixed sizing. Replace with `min(flat_size × (score/12)², flat_size)`. Gives higher sizing to high-conviction entries before Kelly warms. DS estimates $240–450 additional EV over the 30-trade warmup. File: `execution/kelly.py` — add `score_weighted_prewarm()`, modify `get_position_size()`. No board vote required (sizing logic, bounded). 1-session implementation. Already in PENDING as "score-weighted warmup sizing deferred."

**[2026-06-15 S59] Adaptive MIN_SCORE floor — DS-only P2 optimization (architecture audit)**
Static 9/12 floor is suboptimal across regimes. DS proposes: `MIN_SCORE = 9 + floor(MRI_score / 25)` (MRI 0–100). High MRI → higher floor, reducing false entries in volatile regimes. Low MRI → floor stays at 9. DS estimates 6–8% P&L lift (20–25% fewer false entries in high-vol, 15–20% more entries in low-vol). Files: `execution/entry_logic.py`, `events/macro_risk_index.py`. Board vote required (changes entry gate). 2-session implementation.

---

## PENDING / DEFERRED

- `C-2`: `paper=True` hardcoded in `execution/broker.py:21` — change to `False` only when going live
- ~~T2~~ ✅ Kelly calibration complete (board vote Apr 2026): `KELLY_FRACTION` → 0.25 (paper), `KELLY_MIN_SAMPLE_SIZE` → 30, `KELLY_MIN_RISK_PCT` → 0.0075, `pstdev` → `stdev` in `kelly.py`. Score-weighted warmup sizing deferred (separate full board vote required).
- ~~T3~~ ✅ MRI STRESSED+ breakeven push — `_apply_mri_breakeven_push()` in `main.py`, wired into `run_cycle()` between `check_partial_exits` and `check_exits` (2026-04-18, full board vote)
- ~~DATA-4 phase 2~~ ✅ Economic calendar live — `get_economic_calendar()` in `data/fmp_client.py`, injected via `calendar.add_event_dynamic()` in `main.py` at startup (45-day lookahead, 6h cache, US high-impact only)
- ~~backtest_12pt~~ ✅ Already T1 compliant — `fetch_bars` throughout, zero `yf.download` calls. PENDING note was stale (confirmed 2026-04-18).

**Completed (Apr 18 2026):**
- ~~T3b~~ ✅ Macro risk window persisted to `data/state/hybrid_state.json` (`events/news_monitor.py` — `_persist_macro_risk_window` / `_restore_macro_risk_window`)
- ~~TB-1~~ ✅ `portfolio_tracker.py:644` — `STATIC_EVENTS` import fix (PDT holiday detection)
- ~~TB-2~~ ✅ `kelly.py:26` — absolute path anchor for `KELLY_STATS_FILE`
- ~~TB-3~~ ✅ `portfolio_tracker.py:91,157,607` — ET/PT timezone-aware datetimes
- ~~TB-5~~ ✅ `volatility_regime.py:70,181` — `.total_seconds()` fix

**Completed (Apr 15 2026):**
- ~~DATA-1~~ ✅ `signal_generator.py` weekly bias + intraday override → `fetch_bars` T1
- ~~DATA-2~~ ✅ All 10 yfinance equity/ETF calls in `main.py` → Alpaca Data T1
- ~~DATA-3~~ ✅ `macro_risk_index.py` ETF fetches (TLT/HYG/LQD/USO/GLD/EWJ/EWG) → `fetch_bars` T1
- ~~DATA-4~~ ✅ `data/fmp_client.py` built; earnings calendar → FMP T2
- ~~DATA-5~~ ✅ `logs/trade_events.jsonl` structured logging live (`trade_logger.py`)
- ~~DATA-6~~ ✅ VIX stop widening already implemented in `risk_manager.py:get_stop_and_target()` — config constants at `config.py:293–296`
- ~~TZ-1~~ ✅ All 9 PT timestamp fixes applied across dashboard, weekly review, scanner, audit scripts
