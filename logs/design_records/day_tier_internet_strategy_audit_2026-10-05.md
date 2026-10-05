# Day-tier internet strategy audit — 2026-10-05

**Owner/signature:** ChatGPT/Codex (`research/day-tier-strategy-audit`)

**Scope:** Research and test specification only. No trading behavior changed.
**Terminology:** Day tier means same-day flat. Swing means multi-day. PDT is not a constraint; Alpaca removed its PDT/DTBP fields and checks on 2026-07-06.

## Decision

Do not build a nominal "HFT" strategy on the current stack. The live runner wakes every two minutes, consumes one- and five-minute bars, and routes through Alpaca. That can run systematic intraday strategies, but it cannot compete for queue position or sub-second order-flow alpha. Calling it HFT would hide a latency and data mismatch.

Do not expand the present opening-range strategy merely because published backtests show large returns. The bot already has a 15-minute, volume-confirmed, VWAP-filtered ORB implementation in Track B. Claude independently tested stocks-in-play ORB on 3,655 name-days and found only +0.02R in discovery and +0.04R in confirmation; gap continuation changed sign. A 2026 pre-registered study of 225 futures ORB variants found zero cells that survived its cost and stability bar. The older high-frequency pairs result is equally unsuitable: 15 bp costs cut returns by more than half and a one-period execution delay erased them.

The next research candidate is **late-day hedging-demand momentum** on SPY/QQQ. It is not the Gao first-half-hour signal that has already failed locally. Baltussen, Da, Lammers and Martens (JFE 2021) use the return from the prior close through the start of the final half-hour to predict the final half-hour. They report the effect across more than 60 liquid futures and four asset classes over 1974–2020 and tie its strength to dealer/leveraged-ETF hedging demand. This is a credible mechanism and maps to liquid ETFs, where the account can obtain clean fills.

It still must earn admission on our data. Recent evidence warns against importing a published equity curve: an independent 2026 replication of the related SPY noise-boundary model reproduced its historical result but found Sharpe 0.39 out of sample through March 2026; a longer audit found a -8.1% period and Sharpe -0.461 from September 2025 through August 2026. The bot's own noise-boundary test also decayed in 2025–2026.

## Evidence ranking

| Rank | Family | Evidence | Fit to this bot | Action |
|---|---|---|---|---|
| 1 | Late-day hedging-demand momentum | Peer-reviewed; broad asset/time sample; economic mechanism; costs discussed | High: SPY/QQQ SIP bars, one entry, liquid execution, flat by close | **Test next** |
| 2 | Dynamic noise-boundary momentum | Reproducible historical result; recent independent OOS degradation | High technically, but already failed the bot's DSR/decay gates | Retain as benchmark only |
| 3 | Market first-to-last-half-hour momentum | Peer-reviewed and historically OOS significant | Already failed locally: -1.32 bp/trade, permutation p=.806 | Reject |
| 4 | Stocks-in-play ORB | Some positive published studies | Already implemented and locally weak; sensitive to fills/costs | Do not scale |
| 5 | Cross-sectional residual reversal / order-flow imbalance | Strong microstructure rationale; recent papers report large gross effects | Requires broad universe, factor residuals, trade/quote data and strict cost modeling | Research feature layer after data QA |
| 6 | High-frequency pairs / queue imbalance | Known academic family | Current latency, feed and routing are structurally inadequate | Reject on current stack |

## Pre-registered Test 3 — late-day hedging-demand momentum

Write this declaration to the trial ledger before looking at results.

### Data and sample

- Alpaca SIP one-minute RTH bars, split-adjusted and session-calendar aligned.
- SPY and QQQ are separate strategies; do not stack correlated symbol-days as independent observations.
- Primary sample: 2022-01-03 through 2026-10-02, with fixed yearly folds. Preserve the newest six months as the final untouched reserve if they have not already been inspected for this exact rule.
- Half-days are excluded from the primary test and reported separately.

### Signal and fills

- At 15:30 ET, compute `r_rod = close(15:29) / prior_session_close - 1`.
- Primary direction is `sign(r_rod)`. The signal uses only information known before entry.
- Enter at the 15:30 bar **next available executable price**. Primary simulation uses the adverse side of the observed quote when quotes exist; bar-only fallback uses the 15:30 open plus an explicit adverse slippage model.
- Exit at 15:49 ET so the existing 20-minute force-flat window remains authoritative. A published 15:59 exit is a separate benchmark, not an executable assumption for this bot.
- No fixed return threshold is tuned on the full sample. A continuous conviction score may be computed from an expanding-window z-score of `r_rod`, realized volatility and volume, with every estimator lagged one session.

### Dynamic conditioning

- Primary strategy is unconditional so the core anomaly can fail honestly.
- Pre-declared interaction tests: prior-day VIX/VIX3M, realized-volatility tercile, intraday relative volume, day of week, and a **validated** dealer-gamma state.
- The current GEX feed must not be used until its all-POSITIVE-sign defect is resolved. An invalid gamma label must never become a profitable-looking filter.
- Interactions adjust conviction only after walk-forward estimation. They cannot flip a losing base rule into production through full-sample selection.

### Costs and statistical gates

- Charge observed spread where available, SEC/TAF fees where applicable, and slippage scenarios of 1, 3, 5 and 10 bp per side.
- Report gross and net return, trade count, win rate, payoff ratio, profit factor, Sharpe, Sortino, maximum drawdown, expected shortfall, exposure, turnover and fill sensitivity.
- Required admission: positive net expectancy at 5 bp/side; positive in at least four of five yearly folds including 2022 and 2026; Deflated Sharpe probability above .95 using the complete day-tier trial ledger; stationary-block-bootstrap p below .05; no single 20 trading days contributing more than 50% of total P&L; and positive SPY/QQQ equal-weight portfolio results after correlation is handled at the date level.
- Required falsifications: random-sign permutation, random entry window matched on holding duration, always-long final-half-hour control, and a 15:15/15:45 timestamp perturbation.
- A strategy that passes only with TQQQ leverage fails. Leverage may scale a verified edge; it cannot create one.

## Architecture required if Test 3 passes

Build it as a new day-tier family, not as another score inside the current ORB trigger:

1. `day_tier_family_router` selects among independently validated families by current regime and their trailing, shrinkage-adjusted expectancy.
2. Each family owns a versioned hypothesis ID, entry tag, exit tag, feature snapshot, expected holding window and cost model.
3. The router may allocate zero. It cannot force a trade or combine several rejected signals into a high score.
4. Sizing uses predicted loss at the structural stop and the shared daily dollar-risk budget. Family weights use capped, shrunk evidence rather than raw recent P&L.
5. Weekly Confluence 2.0 evaluation updates evidence; it does not rewrite thresholds from the same observations used for scoring.
6. Every fill records arrival midpoint, spread, decision price, fill price and exit price so realized implementation shortfall becomes part of admission and retirement decisions.

## What professional-grade means here

The meaningful upgrade is an evidence-controlled intraday portfolio, not faster polling. It needs a strategy trial ledger, clean SIP/quote data, point-in-time universes, next-bar/adverse-side fills, execution-cost attribution, purged walk-forward folds, multiple-testing correction, and automatic family retirement. The current code has good order protection and ownership controls, but its alpha layer remains two provisional families: GEX wall fade/ride and ORB movers.

True HFT would require a different system: direct or colocated market access, event-driven order-book processing, nanosecond/microsecond timestamps, queue-position models, exchange-specific fees/rebates, and deterministic low-latency execution. Alpaca plus a cloud-hosted two-minute process is the wrong substrate.

## North Star reality check

Growing $2,500 to $25,000 requires a 900% cumulative return. Even the original noise-boundary paper's reported 19.6% annual return would take roughly 12.7 years to compound tenfold before taxes and withdrawals. The $25,000 goal should set the desired growth rate and risk budget; it cannot be an admission criterion that pressures the research process into accepting an overfit strategy.

## Primary sources

- Gao, Han, Li and Zhou, *Market Intraday Momentum*, Journal of Financial Economics 129 (2018): https://ssrn.com/abstract=2440866
- Baltussen, Da, Lammers and Martens, *Hedging Demand and Market Intraday Momentum*, Journal of Financial Economics 142 (2021): https://doi.org/10.1016/j.jfineco.2021.04.029
- Heston, Korajczyk and Sadka, *Intraday Patterns in the Cross-Section of Stock Returns*, Journal of Finance 65 (2010): https://arxiv.org/abs/1005.3535
- Bowen, Hutchinson and O'Sullivan, *High Frequency Equity Pairs Trading: Transaction Costs, Speed of Execution and Patterns in Returns* (2010): https://ssrn.com/abstract=1611623
- Zarattini, Aziz and Barbon, *Beat the Market: An Effective Intraday Momentum Strategy for SPY* (revised 2025): https://ssrn.com/abstract=4824172
- Paz, *Out-of-Sample Evaluation of an Intraday Momentum Strategy for SPY* (2026): https://ssrn.com/abstract=7290621
- Delgado, *Beat the Market Revisited: An Independent Replication and Statistical Audit* (2026): https://ssrn.com/abstract=7323419
- Fetna, *Opening-Range Breakout Does Not Survive Trading Costs* (pre-registered study, 2026): https://ssrn.com/abstract=7428398
- SEC approval of FINRA's replacement of PDT provisions: https://www.sec.gov/files/rules/sro/finra/2026/34-105226.pdf
- Alpaca PDT/DTBP deprecation and removal: https://docs.alpaca.markets/us/changelog/2026-06-03-pdt-651df23

## Handoff to Claude

This audit accepts Claude's 2026-10-04 negative results and does not duplicate Confluence 2.0. The specific uncovered test is the Baltussen rest-of-day-to-final-half-hour formulation. Claude should review this pre-registration before any result is generated, then decide how much to incorporate. ChatGPT/Codex owns this research specification; Claude remains the owner of Confluence 2.0 integration.

## Test 3 result — FAIL (2026-10-05, one shot)

The pre-registration above was merged in PR #475 before execution. `research/day_tier_late_day_momentum.py` then ran against the existing Alpaca SIP one-minute cache on OCI (SPY and QQQ, 1,183 common full trading days, 2022-01-03 through 2026-10-02).

The equal-weight, date-level SPY/QQQ portfolio had no gross edge. Net results were:

| Cost | Mean/trade | Annualized Sharpe | DSR probability | Circular-block p |
|---|---:|---:|---:|---:|
| 1 bp/side | -1.98 bp | -1.70 | 0.000 | 0.9998 |
| 3 bp/side | -5.98 bp | -5.14 | 0.000 | 1.0000 |
| 5 bp/side | -9.98 bp | -8.58 | 0.000 | 1.0000 |
| 10 bp/side | -19.98 bp | -17.17 | 0.000 | 1.0000 |

At the required 5 bp/side, every calendar year was negative: 2022 -7.17 bp/trade, 2023 -10.09, 2024 -12.61, 2025 -11.02, and 2026 -8.72. SPY and QQQ both failed separately. Extending the exit from the bot-compatible 15:49 to the paper-style 15:59 did not rescue either instrument. The random-sign falsification produced p=0.4737.

The implementation used a fixed-length circular block bootstrap rather than the pre-declared stationary bootstrap. That deviation cannot affect the decision: mean returns were negative before realistic costs, DSR was zero, every yearly fold failed at the required cost, and the independent random-sign null also failed.

**Verdict:** reject. Do not implement, tune a threshold, add leverage, or send this family through the live risk gate. The published effect is absent in the bot's available period and execution window. The next useful work is the already-running SIP order-flow-imbalance data audit; it must be evaluated as an execution/conditioning feature first, not presumed to be standalone alpha.
