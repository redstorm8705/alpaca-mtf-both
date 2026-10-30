# Day-tier deep search, round 5 — microstructure ("HFT-style") — 2026-10-30 (signed: Claude)

Owner task: test dynamic price monitoring the way a high-frequency fund would, a level below bars. Read-only research:
no bot code changed, no orders placed.

Rules as before:
- picks are made on the discovery period (before 2024-10-26) only;
- the test period is read once per finalist;
- the 2016–20 confirmation set is used only for a finalist;
- sessions after 2026-10-26 are untouched.

Costs are each symbol's **measured** median quoted spread from Alpaca SIP quotes (no commission), not a flat 5 bps.

Scripts (scratchpad `day_search/`): `tick_fetch.py`, `r5_micro.py`, `r5_bars.py`, `r5_volmgd.py`. Outputs:
`out/r5micro.csv`, `out/r5bars.csv`, `out/r5_volmgd.csv`.

## Plain-English summary

- **No finalist.** 1,198 new tick- and bar-level rules were tested. None reached the discovery bar (t ≥ 2.5 with
  enough trades and sessions). No test-period result was read. **Candidates tried, all rounds: 31,439.** (Measured.)
- **The price signals that high-frequency firms trade are real in this data, but too small to beat the bid-ask spread
  at our speed.**
  - Order-flow imbalance (changes in bid/ask size) and trade-sign imbalance predict the next 1–60 seconds of mid-price
    movement with R² up to about 2%.
  - The move they predict is 1–9 bps; crossing the spread both ways costs 0.3–3.6 bps.
  - The few rules positive after costs come from a handful of sessions (session-clustered t ≤ 2.3).
- **Volatility-managed sizing of the QQQ trend-night hold** (smaller on nights after volatile stretches) **does not
  raise Sharpe.** It does cut drawdown and the worst night by roughly a third to a half. It is a risk control, not a
  return booster.
- **Reachable at our latency: no microstructure edge was found.** Everything the literature monetizes at this level
  needs latency, queue position or passive fills that a REST paper account does not have (details below).

## Data

- **SIP trades and quotes:** 70 sessions sampled with a fixed seed, 10 per year, 2020-10..2026-10 (47 discovery,
  23 test). One 5-minute window per session, alternating 10:00 and 14:30 ET.
- **Symbols:** SPY, QQQ, QQQM, TQQQ, SPXL, AAPL, MSFT, NVDA. The rate limit (shared with the live bot) did not allow
  more; SPY alone posts about 225,000 quotes in 5 minutes.
- **Grid:** 1-second steps of the best bid and offer.
- **Latency rule:** a signal formed at the end of second t trades at second t+1's quote, as a marketable order (buy
  at the ask, sell at the bid), and exits h seconds later the same way.

**Measured median quoted spreads (bps):**

| SPY | QQQ | AAPL | MSFT | QQQM | SPXL | TQQQ | NVDA |
|---|---|---|---|---|---|---|---|
| 0.26 | 0.34 | 0.75 | 1.28 | 1.32 | 1.63 | 1.77 | 2.48 |

## Families and results (discovery)

| Family | Trials | Best predictability | Best net bps / trade after spread | Best session-clustered t |
|---|---|---|---|---|
| 1. Order-flow imbalance (Cont-Kukanov-Stoikov), windows 1/5/10/30 s → 1–60 s, both signs, 8 symbols | 320 | R² 1.5%, hit rate 51.6% (SPXL, 30 s → 30 s) | +3.8 (SPXL 5 s → 60 s; 552 trades, 31 sessions) | 2.26 |
| 1b. Trade-sign imbalance (Lee-Ready signed volume) | 320 | R² 1.9%, hit rate up to 52% | +6.2 (SPXL 30 s → 60 s; 16 sessions) | 1.60 |
| 2. Lead-lag at seconds: SPY → 7 followers; AAPL/MSFT/NVDA → QQQ | 400 | R² 2.8% (SPY → SPXL, 30 s → 60 s) | +11.8 (TQQQ, inverted: index momentum continuing over 30–60 s; 14 sessions) | 1.78 |
| 3. Tracking: TQQQ vs 3×QQQ, SPXL vs 3×SPY, QQQM/QQQ price ratio vs its trailing 60 s mean | 120 | ~0 | ≤ +1 | < 1 |
| 4a. Opening print vs first-bar price, fade/follow 30–60 min (5m bars, measured spread) | 12 | — | +2.9 | 1.02 |
| 4b. Late-day price pressure (15:00–15:45) ± volume surge → buy/sell at the close, exit next open | 16 | — | +11.2 (fade the late move, all days) | 1.85 |
| 6. Half-hour seasonality, time-series (same half-hour over the prior 5/10/20 days) | 6 | — | negative | < 0 |

- **Lead-lag:** constituents do not lead QQQ measurably at 1–30 s (NVDA → QQQ R² 1.3%, net +1.1 bps, t 0.8).
- **The "SPY leads the 3x ETFs" result** is really market momentum over 30–60 s showing up in the levered vehicle.
  Its positive versions rest on 14–22 sessions and their t is about 0. That is not robust.
- **Tracking:** TQQQ, SPXL and QQQM stay within their spread of fair value at a one-second view. The arbitrage is done
  faster than we can see it.
- **Information share:** a full Hasbrouck VAR was not estimated. The cross-predictability above (SPY → others R²
  ≤ 2.8%, constituents → QQQ ≤ 1.3%) is the simplified evidence. [simplified, not the formal information share]

**Finalist screen** (at least 200 trades, at least 20 sessions, session-clustered t ≥ 2.5): 0 of 1,198. Closest:
order-flow imbalance → SPXL, 5 s → 60 s, +3.8 bps net after a 2.7 bps spread, t 2.26 over 31 sessions. Its
significance would not survive about 1,200 trials (Bonferroni requires roughly t 4.3).

## Volatility-managed trend-night sizing (Moreira & Muir 2017) — for SWING-OVERNIGHT-1029

**Base rule:** the QQQ trend night (V1: SPY 15:40 price > mean of the prior 200 settled closes; buy at the 16:00 close,
sell at the next 09:30 open, 2 bps).

**Sizing variants:**
- Base size 0.5x, scaled by target/σ² (inverse variance) or √(target/σ²) (inverse volatility).
- σ² = variance of QQQ's overnight returns over the prior 20 or 60 nights.
- target = expanding median, so it is point-in-time.
- Capped at 1.0x.

4 counted candidates, compared with constant 0.5x:

| Period | Sizing | Bps / night | t | Sharpe | Annual % | Max drawdown % | Worst night % |
|---|---|---|---|---|---|---|---|
| Discovery | constant 0.5x | 1.58 | 1.83 | **0.92** | 4.0 | −9.2 | −2.49 |
| | inv-var 20 | 1.25 | 1.64 | 0.82 | 3.1 | **−3.5** | **−1.55** |
| | inv-vol 20 | 1.20 | 1.64 | 0.82 | 3.0 | −5.5 | −1.52 |
| Test | constant | 2.10 | 1.78 | 1.27 | 5.4 | −4.3 | −1.41 |
| | inv-var 20 | 1.22 | 1.11 | 0.79 | 3.0 | −5.1 | −1.36 |
| | inv-vol 20 | 1.96 | 1.85 | 1.31 | 5.0 | −3.6 | −1.02 |
| Confirmation 2016-20 | constant | 1.74 | 2.45 | 1.12 | 4.4 | −5.4 | −1.77 |
| | inv-var 20 | 1.14 | 2.34 | 1.08 | 2.9 | −2.4 | −1.11 |
| | inv-vol 20 | 1.34 | 2.58 | 1.18 | 3.4 | −3.3 | −1.45 |

The 60-night variants are similar or worse; see the CSV.

**Reading:**
- Picked on discovery Sharpe, constant size wins (0.92 vs 0.82), so no variant is a Sharpe finalist.
- Inverse-variance sizing cuts the discovery drawdown from −9.2% to −3.5% and the worst night from −2.5% to −1.6%, but
  the average size falls, so annual return drops by about a quarter.
- Inverse-volatility (20 nights) roughly matches constant Sharpe in test and confirmation, with 15–40% smaller
  drawdowns.
- Recommendation for the design: use σ-scaling as a **risk cap** (shrink after volatile stretches; never lever above
  0.5x), not as a return lever.

## What is reachable at our latency, and what is not

| Edge type | Needs | Reachable here? |
|---|---|---|
| Queue-position market making, rebate capture, passive fills at the touch | co-location, microsecond order handling, exchange rebates, a real fill model | No. Paper fills are simulated and give no queue priority. |
| Latency arbitrage (SIP vs direct feeds, ETF vs futures, cross-venue) | direct exchange feeds, sub-millisecond reaction | No. SIP plus REST at about 1 s is slower than the arbitrage. |
| Order-flow imbalance / trade-sign at sub-second horizons | the same | No. The predictable move is mostly gone within 1 s. |
| Order-flow imbalance / lead-lag at 1–60 s with marketable orders | 1 s polling, quote stream | Reachable. Tested: predictability is real but smaller than the spread. |
| ETF and leveraged-ETF tracking stat-arb | sub-second | Not at 1 s; deviations are inside the spread. |
| Auction (open/close) and overnight effects | MOC/MOO orders | Reachable. The trend-night hold is the measured edge here (rounds 4–5). |

## Limits

- 70 sessions × 5 minutes is a thin sample: about 35,000 one-second observations per symbol. It cannot rule out a
  weak edge, but it bounds the cost-adjusted one; nothing came close at t ≥ 2.5.
- No ES futures data, so SPY stands in for the index leader.
- No Hasbrouck VAR.
- Spreads are medians over the two sampled windows, not the full day.
- The confirmation backfill was not used, since there was no finalist.
- **Trials this round:** 1,160 tick + 34 bar + 4 sizing = 1,198. **Cumulative 31,439.**
