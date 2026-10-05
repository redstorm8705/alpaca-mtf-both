# Day-tier ten-mechanism foundation — 2026-10-05

**Owner/signature:** ChatGPT/Codex (`docs/day-tier-zero-commission`)

**Status:** Architecture and research queue. No live behavior changed.

## Governing rule

These are independent market mechanisms, not ten points in a static confluence score. Each mechanism must have a versioned hypothesis, observable inputs, falsifiable rule, separate trial ledger, execution assumptions, and retirement criteria. A dynamic family router may allocate among mechanisms that have positive walk-forward evidence. It may allocate zero. It may not combine rejected mechanisms until their sum looks attractive.

## Ranked foundation

### 1. Trade and quote order-flow imbalance

**Mechanism:** Persistent aggressive buying or selling consumes displayed liquidity. Continuation is plausible while imbalance persists; exhaustion or reversal is plausible when price stops responding to continued flow.

**Inputs:** SIP trades and quotes, tick-rule signed volume, bid/ask changes, block-trade share, price response per signed dollar, spread, and quote age.

**Trade families:** imbalance continuation; absorption reversal.

**Current state:** Data collection is pending on OCI. This is the highest-priority net-new information because present entries mostly infer demand from bars after it has already moved price.

### 2. Market- and sector-relative residual return

**Mechanism:** A stock move explained by SPY, QQQ, or its sector is beta. The residual isolates idiosyncratic pressure. Large residuals may continue when information-driven or reverse when liquidity-driven.

**Inputs:** contemporaneous SPY/QQQ/sector returns, expanding-window beta, residual return, residual volume, and residual volatility.

**Trade families:** residual continuation with catalyst/flow confirmation; residual reversal with absorption confirmation.

**Current state:** Missing as a first-class signal. The rejected cross-sectional ridge experiment showed why unhedged raw returns cannot substitute for residuals.

### 3. Liquidity state and implementation shortfall

**Mechanism:** The same forecast can be profitable in a tight, stable book and untradeable in a wide or rapidly retreating book. Spread, depth and fill response determine whether theoretical alpha is capturable.

**Inputs:** arrival midpoint, spread in basis points, quote stability, trade size relative to displayed size and recent volume, decision-to-fill slippage, and fill latency.

**Trade families:** This is an admission and sizing layer for every family, not a directional strategy.

**Current state:** The bot has marketable-limit protection and some spread guards, but does not yet maintain a complete family-level implementation-shortfall history. Alpaca commission is zero; spread and slippage are execution measurements, not commissions.

### 4. VWAP displacement, acceptance and rejection

**Mechanism:** VWAP approximates the session inventory benchmark. A move away from VWAP that holds with flow can continue; a move that cannot attract volume and re-enters value can revert.

**Inputs:** distance from session VWAP in volatility units, VWAP slope, time spent above/below, crossing count, signed volume, and price response near VWAP.

**Trade families:** VWAP acceptance continuation; failed-displacement reclaim/rejection.

**Current state:** Track B uses VWAP as a binary filter. The missing improvement is to model state and path rather than only which side price occupies.

### 5. Catalyst plus abnormal participation

**Mechanism:** News, earnings, analyst changes, macro releases and unusual attention introduce informed or forced trading. Price movement with truly abnormal participation is more likely to contain information than the same chart pattern on routine volume.

**Inputs:** point-in-time catalyst timestamp and type, premarket gap, float-adjusted relative volume, dollar volume, opening-auction volume, and news novelty.

**Trade families:** catalyst continuation after acceptance; overreaction reversal after failed follow-through.

**Current state:** Track B screens gaps and relative volume, but catalyst identity and point-in-time news novelty are not yet reliable first-class features.

### 6. Opening inventory and overnight-gap resolution

**Mechanism:** The open clears overnight information and inventory. Gaps may continue when new information attracts same-direction flow or reverse when the auction overshoots and liquidity returns.

**Inputs:** prior close, overnight high/low, premarket VWAP and volume, official opening print, opening-auction size/imbalance when available, first-window return, and gap fill progress.

**Trade families:** gap acceptance; gap rejection; overnight high/low break and retest.

**Current state:** Pieces exist, but the live ORB path does not model the overnight auction as a complete state object.

### 7. Volatility state and noise-normalized boundaries

**Mechanism:** A fixed price move has different meaning in calm and stressed markets. Entry distance, stop distance and expected holding range must scale with current volatility and the instrument's time-of-day distribution.

**Inputs:** realized volatility, ATR, opening-range width, implied-volatility regime where valid, minute-of-day historical move distribution, and volatility-of-volatility.

**Trade families:** volatility expansion; post-shock compression; dynamic thresholds for all other families.

**Current state:** ATR and opening-range scaling exist. The noise-boundary strategy failed as standalone alpha, but volatility normalization remains essential infrastructure.

### 8. Structural breakout quality and failed-break reversal

**Mechanism:** A level break that receives volume, holds outside value and survives a retest may continue. A break that immediately loses the level exposes trapped participants and can reverse.

**Inputs:** overnight/opening-range/prior-day levels, break distance in volatility units, volume at break, retest depth, wick structure, time outside value, and post-break order flow.

**Trade families:** confirmed break-and-hold continuation; failed-break trap reversal.

**Current state:** Track B implements a provisional break-and-hold rule. The failed-break family and empirical calibration are incomplete.

### 9. Dealer and leveraged-product hedging pressure

**Mechanism:** Short-gamma dealers and leveraged-product rebalancing may trade with price moves; long-gamma dealers may trade against them. The sign and magnitude can change momentum/reversal odds, especially near the close.

**Inputs:** validated index gamma sign/magnitude, distance to high-open-interest strikes, spot consistency, options freshness, leveraged-ETF rebalance proxy, and time to close.

**Trade families:** gamma-conditioned continuation or fade. Gamma is a conditioner, not an automatic entry.

**Current state:** GEX exists, but the recent all-POSITIVE-sign defect must be resolved before this mechanism can influence decisions. The unconditioned late-day hedging strategy failed locally.

### 10. Time-of-day and auction flow

**Mechanism:** The open, midday and close have different participants, liquidity and forced flows. Institutional benchmarking and closing-auction demand can produce effects that are invisible in a session-wide score.

**Inputs:** minute-of-day, time-to-close, scheduled macro events, index rebalance dates, options expiration, closing-auction imbalance where available, and historical conditional distributions.

**Trade families:** opening discovery, midday compression/reversal, closing flow continuation or imbalance mean reversion.

**Current state:** The runner has time windows, but time currently acts mostly as a static gate rather than a learned conditional distribution.

## Shared architecture above the mechanisms

1. A point-in-time feature snapshot is frozen at every decision.
2. Each entry carries `family_id`, `hypothesis_version`, regime, expected holding window and feature tags.
3. Each exit carries reason, MFE, MAE, realized slippage and whether the original mechanism remained valid.
4. Family evidence is updated weekly with shrinkage and minimum-sample rules.
5. The router considers only admitted families and can select no trade.
6. Capital follows capped expected edge, correlation and drawdown state; recent raw P&L alone cannot control allocation.
7. New or materially changed families require the full BGGN risk-path gate before live paper activation.

## Build order

1. Finish order-flow data QA and define continuation versus absorption labels.
2. Build the shared point-in-time mechanism snapshot and entry/exit tags.
3. Add residual-return and complete VWAP-state features.
4. Add liquidity and implementation-shortfall attribution to every day-tier fill.
5. Build catalyst and overnight-auction state.
6. Test failed-break reversal independently from existing ORB continuation.
7. Repair and validate GEX before using gamma conditioning.
8. Admit only families that pass purged walk-forward, zero-friction significance, execution-friction stress, multiple-testing correction and concentration gates.

## Evidence anchors

- Chordia and Subrahmanyam, *Order Imbalance and Individual Stock Returns*: https://ssrn.com/abstract=354122
- Heston, Korajczyk and Sadka, *Intraday Patterns in the Cross-Section of Stock Returns*: https://arxiv.org/abs/1005.3535
- Brogaard, Han and Kim, *Intraday Residual Reversal in the U.S. Stock Market*: https://ssrn.com/abstract=4731947
- Challet and Gourianov, *Dynamical Regularities of U.S. Equities Opening and Closing Auctions*: https://ssrn.com/abstract=3119537
- Baltussen, Da, Lammers and Martens, *Hedging Demand and Market Intraday Momentum*: https://doi.org/10.1016/j.jfineco.2021.04.029
- Gao, Han, Li and Zhou, *Market Intraday Momentum*: https://doi.org/10.1016/j.jfineco.2018.05.009
- Alpaca paper-trading fill limitations: https://docs.alpaca.markets/us/v1.4.2/docs/paper-trading
