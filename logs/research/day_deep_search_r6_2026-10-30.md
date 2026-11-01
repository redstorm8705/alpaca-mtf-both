# Deep search, round 6 — auction / overnight, deeper — 2026-10-30 (signed: Claude)

Read-only research: no bot code, no orders. Same rules as earlier rounds:
- picks are made on the discovery period (2020-10..2024-10) only;
- the sealed test (2024-10..2026-10) and the untouched 2016-01..2020-10 confirmation are each read once per finalist;
- portfolio weights use fixed slots (honest weighting);
- sessions after 2026-10-26 were not touched.

Timing: signals use data through 15:45 ET; entry at the 16:00 close (market-on-close); exit at the next 09:30 open
(market-on-open). Costs come from the measured spreads: 2 bps round trip for ETFs, 3 bps for stocks, with a +5 bps
stress column. No commission.

Scripts (scratchpad `day_search/`): `r6.py`, `r6_cond.py`, `r6_final.py`. Outputs: `out/fam_r6_all.csv`,
`out/r6_cond_disc.csv`, `out/r6_finalists_revealed.csv`.

## Plain-English summary

**No new edge beyond the market's overnight drift.**

- **Strongest result:** buy the stocks with the best overnight returns over the last 60 sessions and hold them
  overnight, on days SPY is in its uptrend. It made money in all three periods: +9.7 / +12.6 / +7.7 bps per
  stock-night, t 2.21 / 1.54 / 2.64. After correcting for the 6 finalists it is significant
  (p 0.007, test + confirmation combined).
- **But it is market exposure, not stock-picking.** It moves 1.4–2.3× SPY's overnight move. After removing that, the
  remaining edge is zero (alpha t −0.13 test, +0.33 confirmation). The ETF version simply picks the 3x ETFs
  (beta 2.6–3.9).
- **Conditioning filters on the QQQ trend night** (VIX, weekday, holidays, month end, Fed days, earnings season): none
  improves Sharpe consistently across all three periods.
- **Fading the day's move overnight** (Bogousslavsky reversal): no edge (best discovery t 0.84).
- **Shorting QQQ, SPY or IWM during the day on bear-regime days, or buying SQQQ/SPXS:** loses money. On SPY<200-day
  days the intraday drift is positive (round 3), so the short side bleeds.
- **Candidates tried this round: 94. All rounds: 31,533.**

## 1. Overnight momentum (Lou, Polk & Skouras)

Rank names by their trailing 20- or 60-night mean overnight return (point-in-time). Hold the top 10–20% close to open,
long-only or long-short (top minus bottom), all nights or trend-up nights only. 32 trials.

| Finalist (picked on discovery) | Discovery: bps / t | Test: bps / t / Sharpe | Confirmation: bps / t / Sharpe | Beta (test / confirmation) | Alpha t (test / confirmation) |
|---|---|---|---|---|---|
| Stocks, 60-night top 20%, long-only, trend-up | +9.7 / 2.21 | +12.6 / 1.54 / 1.09 | +7.7 / 2.64 / 1.21 | 2.31 / 1.39 | −0.13 / +0.33 |
| ETFs, 60-night top 10%, long-only, trend-up | +15.3 / 2.22 | +16.1 / 0.99 / 0.70 | +12.7 / 2.37 / 1.09 | 3.9 / 2.6 | −0.55 / −0.12 |
| ETFs, 60-night top 10%, long-short, trend-up | +11.3 / 1.81 | +10.9 / 0.78 | +9.5 / 2.25 | 13.2 / 8.4 * | −0.44 / +0.34 |
| ETFs, 60-night top 10%, long-only, all nights | +10.2 / 1.73 | +14.6 / 0.95 | +9.1 / 2.06 | 0.75 / 0.64 | +0.71 / +1.55 |

\* The long-short beta is large because the net exposure is small and noisy; the regression is ill-conditioned.

Bonferroni (k = 6), test + confirmation combined: 0.007, 0.035, 0.062, 0.075.

**Reading:** the overnight premium is concentrated in names that have been earning it lately. They are high-beta
names, so the selection adds beta, not alpha. The 20-night versions are weaker.

## 2. Conditioners on the QQQ trend night

Base rule (V1): SPY's 15:40 price above the mean of the prior 200 settled closes → buy QQQ at the close, sell at the
open, 0.5x, 2 bps. Each conditioner was tried as keep-only and as exclude (28 trials). The discovery screen required
Sharpe above the base with at least 50% of nights kept.

Discovery Sharpe, base 0.86:
- Exclude earnings-dense weeks: **1.34**.
- Exclude Friday nights: 1.06.
- Exclude Monday, Tuesday or Thursday nights, or pre-holiday nights: 0.88–0.96.
- Wednesday nights only: 1.58, but only 20% of nights, so it fails the screen.
- Low VIX hurts: Sharpe −0.05 on low-VIX nights; excluding them gives 1.17 but keeps only 44% of nights.

The two best were revealed once:

| Conditioner | Discovery Sharpe (base) | Test Sharpe (base) | Confirmation Sharpe (base) | Nights kept | Worst night (test / confirmation) | Alpha t (test / confirmation) |
|---|---|---|---|---|---|---|
| Exclude nights when many large caps report in the next 5 sessions (SEC 8-K 2.02 dates) | 1.34 (0.86) | 1.24 (1.01) | 0.94 (1.11) | 60% | −1.5% / −2.0% | −0.63 / −2.61 |
| Exclude Friday nights (weekend hold) | 1.06 (0.86) | 0.87 (1.01) | 1.20 (1.11) | 80% | −1.5% / −1.3% | −1.76 / −1.01 |

Each conditioner helps in 2 of 3 periods and hurts in the third. Not adopted.
- CPI-eve was not testable: the FMP calendar is plan-blocked.
- FOMC eve / FOMC night have too few nights (23 in discovery).

## 3. Intraday → overnight reversal

Fade the 09:30 → 15:40 move overnight, time-series (|z| ≥ 0.5 / 1.0) or cross-sectional (buy the bottom quintile,
or bottom minus top), across all / up / down regimes, in ETFs and stocks. 24 trials.

Best discovery result: cross-sectional ETF long-only on trend-up days, +3.4 bps, t 0.84. **No edge.**

## 4. Day-tier bear-day leg (same-day 09:35 → 15:55)

10 trials, all negative:

| Trade on SPY<200-day days | Net bps | t |
|---|---|---|
| Short QQQ | −9.2 | −0.86 |
| Short SPY | −7.5 | −0.93 |
| Short IWM | −6.0 | −0.64 |
| Long SQQQ | −24.9 | −0.78 |
| Long SPXS | −18.2 | −0.76 |

**Do not add.**

## Recommendation

1. **SWING-OVERNIGHT-1029: keep the plain QQQ(M) trend night.**
   - V1 signal, market-on-close entry and market-on-open exit.
   - 0.5x, with the inverse-volatility risk cap from round 5.
   - Add no calendar or VIX conditioner. None improved Sharpe in all three periods.
2. **Optional exposure choice, not alpha.** The overnight-momentum basket (top 20% of stocks by 60-night overnight
   return) earns more per night because it carries about 1.4–2.3x the market beta. Taking more overnight beta through
   QQQ sizing is simpler and does the same thing. If preferred, size the QQQ leg up within the K-limb. That is a risk
   decision for the owner.
3. **Day tier: add nothing from this round.** The same-day bear-regime short loses money.

## Limits

- Costs are per-class proxies from measured spreads (ETFs 2 bps, stocks 3 bps), not per-name, per-minute.
- The 2016–20 confirmation for stocks uses 30 non-Day large caps.
- The earnings-density conditioner uses SEC 8-K 2.02 dates for 49 names; coverage is thinner before 2019 for names
  that listed later.
