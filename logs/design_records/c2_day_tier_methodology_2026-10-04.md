# Confluence 2.0 day-tier methodology — structural-flow research program (2026-10-04, Claude-signed) — RESEARCH

## Owner direction (Rafael 2026-10-04)
Act as a professional fund: find documented, structurally-caused intraday edges; use real (post-2020, incl. 2022 bear)
data; consider index / leveraged / commodity ETFs instead of concentrated single names; overnight edge belongs to swing.

## BGGN consolidation (board Simons/Shaw, López de Prado, Harris, Sosnoff/Sinclair — sources web-verified; Gro + GAI)
Converged: stop mining chart patterns on fixed mega-caps; trade FORCED-FLOW effects at the index level.
Verified documented families: (1) market intraday momentum — Gao, Han, Li & Zhou, JFE 2018 (prior close->10:00 return
predicts the 15:30-16:00 return on SPY); (2) hedging-demand intraday momentum — Baltussen, Da, Lammers & Martens, JFE 2021
(rest-of-day return predicts last 30 min, stronger when dealers are short gamma); (3) SPY noise-area breakout — Zarattini,
Aziz & Barbon, SFI RP 24-97, 2024 (working paper; deflate); (4) stocks-in-play opening-range breakout — Zarattini, Barbon &
Aziz (SSRN; relative-volume filter); (5) leveraged-ETF rebalancing — Cheng & Madhavan 2009 vs Ivanov & Lenkey 2018 (weak);
(6) overnight vs intraday — Lou, Polk & Skouras JFE 2019 (-> swing tier). Gro/GAI citations that could not be verified
(e.g. "Bali & Hens 2019", "Boudoukh et al. 2020 Intraday Gamma Effect", a HF "intraday-stock-data" set) are NOT used.
Universe: SPY/QQQ/IWM core; leveraged ETFs only as an execution vehicle for an index signal (Rule E risk-path -> board);
single names only via an in-play filter; commodity ETFs last. GEX: gamma conditions trending strength (Ni et al. RFS 2021),
not level pinning — consistent with our 40% vs 38% pin result.
Data: Alpaca SIP history verified on OUR account (SPY 2022-06-13: SIP 143.5M sh vs IEX 3.8M); DoltHub post-no-preference
options (from 2019, coverage unverified); FNSPID (HF Zihan1004/FNSPID, news to 2023, daily stamps); Chronos/TimesFM/Kronos
exist — volatility use only.

## PRE-REGISTERED TEST 1 (written before any result): market intraday momentum
- Data: Alpaca SIP 1-minute RTH bars, 2022-01-03 .. 2026-10-02 (data/cache/lab/bars/1Min_sip). Universe: SPY, QQQ, IWM.
- Signal (primary): s = sign(close of the 15:29 bar / prior session's last RTH close - 1). Enter at the 15:30 bar open,
  exit at the 15:59 bar close, direction s. Variant (pre-declared, not a re-tune): s from prior close -> 10:00 (close of the
  09:59 bar).
- Costs: $0.01/share each way + half the spread (assume $0.01) -> $0.03/share round trip, expressed in bps of entry price.
- Nulls: (a) 5,000-draw sign permutation of s; (b) always-long 15:30-15:59.
- Pass bar (pooled over the 3 ETFs, primary signal): permutation p < 0.05 AND Deflated Sharpe probability > 0.95 with the
  trial count = 13 prior day-tier patterns + 2 momentum/exit families + this test's 2 signals = 17 AND positive mean net
  return in >= 3 of 5 calendar years including 2022.
- Secondary (reported, not gating): split by gamma-regime proxy VIX/VIX3M > 1 (backwardation) vs <= 1 (T4 yfinance daily,
  prior-day value); also TQQQ/SQQQ as execution-vehicle P&L (informational only).
- One shot: no parameter changes after results.

## TEST 1 RESULT (SIP 1m, 2022-01-03..2026-10-02, 3,553 symbol-days) — FAIL
Primary mean net -1.32 bp/trade (gross -0.41), win 47.6%, annualized SR -0.74, permutation p 0.806, DSR prob 0.000.
By year (bp): 2022 +2.71, 2023 -0.80, 2024 -2.99, 2025 -4.11, 2026 -1.49. SPY -0.92, QQQ -0.89, IWM -2.17. Variant (10:00
signal) -1.58. Always-long -0.85. Gamma proxy: VIX/VIX3M>1 days -3.48 bp (n=189) vs -1.23 (n=3,358). Not shipped.

## PRE-REGISTERED TEST 2 (written before any result): noise-area intraday momentum (Zarattini, Aziz & Barbon 2024)
- Data/universe: SIP 1m RTH 2022-01-03..2026-10-02; SPY and QQQ.
- Noise band for minute m of day d: sigma_m = mean over the prior 14 sessions of |close_m / open_session - 1|.
  UB_m = max(open_d, prev_close) x (1 + sigma_m); LB_m = min(open_d, prev_close) x (1 - sigma_m).
- Decisions only at half-hour marks 10:00..15:30 (bar close at HH:00 / HH:30 minus one minute). Flat: enter LONG if close > UB,
  SHORT if close < LB. Long: exit (or reverse if close < LB) when close < max(UB, VWAP); short mirror with min(LB, VWAP).
  Flat at the 15:59 close. Unit size (sizing does not change the sign/Sharpe test).
- Costs: $0.03/share per round trip (entry+exit), charged per position change.
- Nulls: (a) 1,000 random-time draws matched per day on trade count, direction and holding length; (b) intraday buy-and-hold.
- Pass: net daily Sharpe > 95th percentile of the random draws AND DSR prob > 0.95 (N = 18 trials) AND 2022 net >= -10%.
- One shot.

## TEST 2 RESULT — FAIL as pre-registered; NULL FOUND INVALID
SPY mean +2.35 bp/day, ann SR +0.90 (by year %: 2022 +18.4, 2023 +9.7, 2024 +2.8, 2025 +1.8, 2026 -5.2); QQQ +4.80 bp/day,
ann SR +1.33 (2022 +17.1, 2023 +15.3, 2024 +9.1, 2025 +10.7, 2026 +3.9). Pooled SR +1.13, DSR prob 0.962, 2022 +17.7%.
Pre-registered null (random times, SAME direction & holding length) gave SR median +4.90 / p95 +5.31 -> FAIL. Diagnosis: that
null leaks look-ahead (each trade's direction is set by the day's own move; random starts include times BEFORE the breakout).
## TEST 2b — CORRECTED NULL (declared before running; post-hoc correction, reported as such)
Same trades (actual entry/exit times), direction replaced by a random sign: 2,000 permutations. Pass: observed pooled ann SR >
95th percentile of the sign-permuted SRs AND the original DSR > 0.95 (0.962 already) AND 2022 >= -10% (already +17.7%).
Trial count N = 19 (one more for the correction).

## TEST 2b RESULT + ADVERSARIAL AUDIT — EDGE REFUTED (not shipped)
2b sign-permutation: observed pooled SR +1.13 vs permuted median -0.20 / p95 +0.32 (p < 0.0005) -> passed the corrected null.
Adversarial audit (independent re-implementation matches: SPY 0.87, QQQ 1.32, stacked 1.12; no look-ahead; next-bar fill
costs ~0.02 SR): (1) DSR was inflated by stacking correlated SPY/QQQ days (corr 0.755); equal-weight portfolio n=1,171 SR 1.21
-> DSR 0.789 at N=19 (< 0.95 bar) — FAIL; (2) decay: equal-weight SR by year 1.72/2.53/1.15/0.74/-0.17 (2022..2026); 2025-26
stacked p=0.15; SPY 2025-26 SR -0.32; (3) fragility: top 20 of 1,171 days = all P&L; IWM SR -0.91 every year; check-grid shifts
+-5..15 min give SR 0.18-1.00; costs $0.10/sh -> SR 0.70 (SPY negative 2024-26). QQQ alone: 2024-26 p=0.02, 2025-26 SR 0.89
(p=0.099); TQQQ vehicle (info): SR 0.89, ~-29% drawdown, 2025 +27.3%, 2026 +4.6%. Possible NEW hypothesis (QQQ-only, N=20,
fresh data only) — owner decision pending. Next pre-registered: TEST 3 stocks-in-play ORB (needs Russell-1000 SIP 1m).
