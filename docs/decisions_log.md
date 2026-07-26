# Decisions Log — durable rationale (append-only)

**Purpose (BGG-endorsed 2026-07-26, unanimous: Gro + GAI + 2 board seats):** a thin, append-only
record of *non-obvious decision rationale* — the "why" behind choices that are otherwise only
reconstructable by re-litigating them. NOT a status log (git + `tb_audit_log.md` are that), NOT a
P&L record (Alpaca fills are the sole authority — **no event-log/`trade_events.jsonl` dollar figure
belongs here**). One entry per real decision: what + one-clause why + provenance.

**Append-only in practice:** never edit a prior entry; a correction is a NEW dated entry that
references the old. Git is the tamper-evidence layer.

**Provenance convention:** `source: <file> @ <git-sha>` — the sha the file had when its content was
folded in, so the pointer resolves in history forever even after the source file is deleted.

---

## 2026-07-26 — retention cleanup + carried-forward facts

- **Event-log P&L is NOT authoritative — Alpaca fills (FIFO) are.** Proven 2026-07-26 (PR #20): the
  weekly post-mortem's `trade_events.jsonl` path reported +$22.42 for the week of 2026-07-20 that
  authoritative Alpaca fills show was −$47.76 (missed 9 of 11 trades, flipped the sign). Any dollar
  figure sourced from `trade_events.jsonl` or a pre-#20 `wtp_*.md`/dashboard is suspect and must be
  re-derived from fills. _source: handoff.md ⏩ block + weekly_postmortem.py @ ba1c222._

- **yfinance-for-NEWS T4 violation (queued 2026-05-28) — RESOLVED, not open.** The May finding
  (`scan_to_html.py::_fetch_yfinance_news()` using `yf.Ticker().news`, an unapproved data tier) was
  verified against current code 2026-07-26: **the function no longer exists** in `scan_to_html.py`.
  Promoted as resolved, not carried forward as an open item. _source: logs/queued_for_review_2026-05-28.md @ 31b3e03; verified vs scan_to_html.py @ 6e24cb6._
- **CANDIDATE (separate, NOT the above):** `scan_to_html.py` still uses yfinance for ATM-straddle /
  SPY-options price (≈ lines 87, 1475) and `run_cycle.py` still imports `scan_to_html` (RTH-chain
  intact). Whether options-via-yfinance is an acceptable fallback or a T4 review item is UNVERIFIED —
  logged for a future data-tier audit, not asserted as a violation.

## May 2026 — carried-forward decision rationale (full detail in git session summaries)

- **Kelly sizing journey → settled at KELLY_FRACTION 0.25 (paper).** Was cut 0.25→0.15 (2026-05-11,
  DS flagged a thin/negative avg-R in the stat-building phase — *stats cited at the time, not
  re-verified here*), then restored 0.15→0.25 by unanimous full-board vote (2026-05-16) on the
  aggressive-paper mandate ($2.5K→$25K favors compounding frequency over defensiveness). Also that
  vote: KELLY_MAX_RISK 0.04→0.06, MAX_PORTFOLIO_RISK 0.03→0.04; **MIN_SCORE 10→9 REJECTED** (BoD 4/5,
  zero score-9 history in `trade_events.jsonl`). _source: session_summary_2026-05-11_2346 / _2026-05-16_1202._
- **Kelly A2 drawdown multiplier** — linear ramp 1.0×@DD=2% → 0.33×@DD=15% (scale-DOWN only); Derman
  correlation guard halves the CV penalty when `CV>0.20 AND a2_mult<0.80` (avoid compounding two
  independent risk signals); the `KELLY_MIN_RISK_PCT` floor applies AFTER A2 (unconditional min).
  Constants `KELLY_A2_DD_START/MAX/MULT_FLOOR`. _source: session_summary_2026-05-16_1202._
- **`_fetch_actual_fill_price()` returns `float`, never `None`** — 0.0 is the "no fill found"
  sentinel; callers must check `> 0`, not `is None`. _source: session_summary_2026-05-09_S12._
- **"Not a bug" clarifications** (so they aren't re-flagged): AMD $0 P&L = legitimate breakeven exit
  (stop moved to entry via breakeven_push); AAPL GTC 42210000 = broker.py deferred mechanism working;
  `_cancel_open_gtc_orders()` duplicate-cancel serves the `_sig_defer>=6` forced-fallthrough path
  (guard, don't remove). _source: session_summary_2026-05-04_S2 / _2026-05-10_1646._
- **reporting/metrics.py** is the shared P&L-computation module (weekly + monthly both use it;
  `_day_pnl()` ported from `reconcile_eod.py`). Monthly review is spawned by `weekly_review.py`, not
  a separate cron. _source: session_summary_2026-05-09_S12._
- **`paper_to_live_checklist.md`** (repo root) — the 8-section gate to revisit before live capital;
  $10K cumulative paper profit is the trigger to deploy small real capital. _source: session_summary_2026-05-16_1202._
