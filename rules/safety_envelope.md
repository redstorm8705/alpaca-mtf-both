# SAFETY ENVELOPE — full text (canonical definition site for S01–S07)
<!-- Consolidation (Rafael 2026-08-22, Hybrid C+). The envelope is NEVER widened — Profitable>Perfect makes the
     bot more aggressive WITHIN it, never beyond it. CORE.md carries the one-line pointers; THIS file is
     authoritative for the full text. The anti-drift lint forbids re-defining any S-rule outside this file. -->

## S01 — paper=True is hardcoded and locked
`paper=True` is hardcoded in execution/broker.py and locked until a full board vote approves going live. Never
read `TRADING_ENV` inside broker.py — the paper flag is a manual, auditable constant, not a runtime switch.
`TRADING_ENV` controls analysis behaviour only; it never overrides the broker hardcode.
_Source: CLAUDE.md §Guardrail 6 (Environment Management) + §Arch-Inv #8._

## S02 — 7% kill switch for paper; tiered upgrades need a board vote
Kill switch = 7% for the paper account (board vote 2026-04-22 25-1; confirmed S50 13-0). config.py paper
profile is the single source of truth. Tiered upgrade path — $10K→10% | $20K→12% | $25K→15% — each requires its
own board vote. No hidden override elsewhere in the codebase.
_Source: CLAUDE.md §Arch-Inv #6._

## S03 — never-mask-a-loss
A risk-path guard may override the raw reading ONLY on a positively-confirmed fault; on ANY ambiguity it returns
the raw (worse) reading so the kill switch stays sensitive. The correct fault detector is explained-P&L, not
`equity == cash + market_value`. Any risk-path diff that could mask a loss takes the mandatory cold board +
masked-loss seat — Gro and GAI approving is NOT sufficient (the cold board has caught masked-loss guards both
approved).
_Source: CLAUDE.md §Build-Don't-Fix E + memory feedback_safety_control_never_mask_loss._

## S04 — data-source tiers; never yfinance for equities
Every data call uses the highest available tier: T1 Alpaca Data (all intraday/historical OHLCV, real-time
quotes for US equities/ETFs — data/fetcher.py bars, data/alpaca_data.py quotes) · T2 FMP (fundamentals,
earnings, economic calendar) · T3 TraderMonty CSV (breadth) · T4 yfinance (ONLY ^VIX, ^VIX3M, JPY=X — never a
US equity/ETF, never a real-time quote). Never a raw `requests.get()` to a market-data endpoint. When T4 is
used as a fallback for something that should be T1/T2, log a WARNING + Slack-alert.
_Source: CLAUDE.md §Guardrail 1 (Data Source Hierarchy)._

## S05 — execution isolation (one trade client, one data client)
Two Alpaca SDK clients, each locked to exactly one module: `TradingClient` → execution/broker.py ONLY (orders,
position queries); `StockHistoricalDataClient` → data/fetcher.py ONLY (bars/historical). No other file
instantiates either. data/alpaca_data.py uses the Alpaca Data REST API via `requests` (no SDK) for real-time
quotes. Backtest scripts are self-contained (no execution imports); audit/diagnostic scripts import
strategy/events/indicators read-only.
_Source: CLAUDE.md §Guardrail 2 (Execution Isolation)._

## S06 — risk-path diff → mandatory cold board + masked-loss + Gro + GAI
A diff is RISK-PATH iff it can increase (a) per-trade or aggregate position SIZE, (b) trade FREQUENCY / entry
rate (e.g. lowering MIN_LONG/SHORT_SCORE, or raising conviction so more trades clear the size floor), or (c)
CONCURRENCY / gross or overnight EXPOSURE — INCLUDING indirectly via any multiplier applied UPSTREAM of the
Kelly / gross caps (a regime up-size multiplier, the GEX book-wide edge multiplier). A change that only alters
WHICH trades fire (pure selectivity, still bounded by the existing caps) is NOT risk-path. Every risk-path diff
takes the full mandatory cold board + the masked-loss/risk-asymmetry seat + Gro + GAI, on BOTH the design and
the diff. The same-day / all-zero fast path is NOT available to a risk-path diff. Default-to-risk-path on any
ambiguity — the same-day path requires a positive all-clear, never the absence of a flag.
_Source: CLAUDE.md §Build-Don't-Fix E (risk-path DEFINITION) + §B routing screen._

## S07 — no .env secrets in tracked files
API keys / tokens (GROQ_API_KEY, GEMINI_API_KEY, GEMINI_PAID_API_KEY, ALPACA_*, FMP_API_KEY, etc.) live ONLY in
`.env` and are never hardcoded in CLAUDE.md, any tracked .py/.md/.sh, or a commit. Read them via
`source .env` / `os.getenv`. (A prior key leak via a session transcript is why the Gemini key was rotated.)
_Source: CLAUDE.md §Gro/GAI Direct API Protocol (API Keys note)._
