# 🚀 BOT IMPROVEMENT QUEUE (non-bug) — Rafael-owned

**Purpose:** dedicated home for NON-BUG, bot-improvement work — distinct from the bug/patch log
(`tb_audit_log.md`) and the mid-flight build sequence (`handoff.md`). Each item runs its own
Feature-Design-Protocol gate (board + Gro + GAI) when picked up. Ordered roughly by value.
Established 2026-07-11 at Rafael's request.

---

## A. WIN-RATE IMPROVEMENT — dedicated NON-BUG investigation session (Rafael 2026-07-11)
Deep investigative session (NOT a bug fix) to find the **5 biggest ways to improve the win rate**
(currently honest entry-level ~40%, winners > losers, +$1.73/trade). Inputs to gather + synthesize:
- **All resources Rafael has shared** — specifically the **tweets on "delta change"** (trade-the-delta /
  bar-over-bar signal change) he shared. Pull them together into the analysis.
- **Board audit** (BoD + AB seats — signal construction, factor/momentum, entry mechanics) on win-rate levers.
- **Gro + GAI architecture/whitespace audit** (MODE 2) on the 5 highest-EV win-rate improvements.
- Cross-reference the existing shadow signals (delta-of-signal, volume confirmation, 16-pt) already logging.
Deliverable: ranked top-5 with mechanism + expected lift + implementation file, for Rafael to greenlight.

## B. FOREVER-6 — crash-entry design + behavior simulation (Rafael 2026-07-11)
The Forever-6 long-term holds must be **included in overall P&L** but **EXEMPT from regime gates, MRI,
mass-liquidations, kill-switch, and safe_close_all**. Open design questions to resolve WITH the board:
- **Entry threshold** = crash-like scenarios only? Proposed rule: open/add when a name is having its
  **worst one-day drawdown since 2022** (per-stock, individually tailored — e.g. if TSLA has its worst
  trading day since 2022, we open). Need the per-stock worst-day distribution (since 2022) as the trigger.
- **Intraday drawdown** included per stock (not just the daily close — catch the worst intraday move).
- Requires: historical worst-day analysis per Forever-6 name (2022→now), a rung/ladder entry design, and
  **SIMULATIONS** to monitor how the bot behaves before it goes live. BoD + AB lead (pick selection);
  TB/execution handle integration + exemption plumbing.
- BLOCKED ON: the safety-hardening + per-strategy-ownership work must be built/tested/functional first
  (Rafael 2026-07-11: hardenings before Forever-6).

## C. CORRELATION-RISK FRAMEWORK build-out (Rafael 2026-07-11)
**Current state (verified 2026-07-11):** there IS a real entry gate — `entry_logic._check_portfolio_
correlation()` blocks a new entry if its 20-day daily-return correlation (rho) > **0.70** with ANY open
position (fail-closed on data errors). That is pairwise + at-entry only.
**Not yet built:** a **portfolio-wide, continuous correlation + VaR aggregator** (rolling pairwise matrix
across ALL open positions, portfolio VaR/CVaR, tail-convergence detection — in a crash all correlations
→ 1). Diversifying the universe (momentum swings + QHM + Forever-6) reduces correlation but is not
correlation *measurement*. Build the aggregator, tested (board + Gro + GAI). Roadmap `risk/correlation_matrix.py`.

## D. HIGH-VALUE ADAPTATIONS — Rafael aligned, START (2026-07-11)
1. **Adaptive MIN_SCORE floor** — raise the entry quality bar in stressed regimes, lower it in calm
   (`MIN_SCORE = 9 + floor(MRI/25)`); ~6-8% P&L lift est.
2. **Conviction spline** — replace the score cliff (10=half, 11=full) with a smooth ramp (9→0x … 12→1x).
3. **Walk-forward validation** — detect edge decay + re-tune weights (the 16-pt shadow is the raw material).
4. **TCA / execution-quality monitoring** — measure slippage/fill-latency vs NBBO (est. 5-15% of P&L
   leaking, currently invisible). `execution/execution_quality.py`.

## E. CASH-ACCOUNT DEPOSIT PLUMBING (Rafael 2026-07-11)
This is a paper account that will eventually convert to a **cash account**. The P&L math must correctly
**exclude future deposits from "gains"** (total P&L = equity − net_deposits). Today `pnl_ledger` uses
`fetch_net_deposits() or 2500` which can mis-sum a real deposit. Build proper deposit/withdrawal tracking
so a capital add never shows as a phantom gain — REQUIRED before the account converts / capital is added.

## F. UX REDESIGN — all 5 web pages (Rafael, prior) — presentation-layer, Luke Wroblewski leads.

---
*(Bug-class + mid-flight build items live in `tb_audit_log.md` / `handoff.md`, not here.)*
