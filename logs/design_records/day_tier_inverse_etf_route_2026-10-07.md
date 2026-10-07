# Day tier: short signals route to a bought inverse ETF — BGG alignment (2026-10-07, Claude) — AWAITING CEO

## Why
Since PR #503 the day tier never opens on a symbol another tier holds; co-held SHORTS are skipped (Alpaca nets a
short against another tier's long). Today other tiers hold GOOGL, MSFT, NFLX, NVDA, PLTR. Recent short trigger log
lines on those: 10/05 GOOGL 8; 10/06 NVDA 12, MSFT 2 (log lines, not distinct setups).

## Verified facts
Inverse ETFs ACTIVE + tradable on Alpaca (10/06 IEX-only volume): NVD 2x 17.9M (~$3.30), NVDQ 2x 441K, PLTD 1x 3.9M,
PLTZ 2x 15.5K, AVS 1x 2.5M, AMZD 1x 1.5M, AAPD 1x 426K, MUD 1x 309K, MSFD 1x 101K, TSLQ 2x 54K, TSLZ 2x 26K.
Too thin: GGLS (GOOGL), NFXS (NFLX), METD (META), AMDD (AMD), CONI (COIN).

## Alignment — board Harris + Taleb, Gro, GAI: all APPROVE-WITH-CHANGES
GAI's first answer used a stop ABOVE a long ETF entry and a 0.15% spread gate (impossible for a $3.30 product with a
$0.01 tick); one evidence counter-prompt -> GAI agreed with the below-entry formula and a 0.5% gate.
Aligned design (all new checks fail safe = skip):
1. A short signal BUYS the inverse ETF (order side long, stop below entry); record signal_direction=short,
   instrument=inverse_etf, underlying, k. Same trigger rule as the bull pivot: stock co-held by another tier, or the
   budget buys 0-1 shares. Map: NVDA->NVD (alt NVDQ), PLTR->PLTD, AVGO->AVS, AMZN->AMZD, AAPL->AAPD, MU->MUD,
   MSFT->MSFD, TSLA->TSLQ (alt TSLZ). Kill flag DAYTRADE_INVERSE_PIVOT.
2. Exact daily-reset stop translation (Harris k_eff): inverse E_stop = E*(1 - [k*(L-U)/C] / (1 - k*(U-C)/C));
   bull E_stop = E*(1 + [k*(L-U)/C] / (1 + k*(U-C)/C)) (C = stock prior close; derived from fund value 1 +/- k(P/C-1)
   since the close; Taleb seat's printed form misplaced the division — corrected here). Check: k=2, C=100, U=95, L=96.9
   -> inverse stop E*(1 - 0.038/1.10) = E*0.9655 (-3.45%, matches Harris). Fallback to first-order if C missing. Also fixes
   the live bull pivot (with NVDA -5% on the day the first-order stop sits ~0.3% past the structural level).
3. Live liquidity gate on the ETF at decision time: fresh uncrossed quote; spread <= min(0.5% of mid, 25% of stop
   distance); today's IEX volume >= 50K shares and >= 100x order qty; stop distance >= 5 ticks.
4. Tracking check: skip if the ETF's move since prior close differs from the implied -k x stock move by more than
   max(0.5%, 3 ticks) (proxy for premium/stale quote; no iNAV feed exists in our data tiers).
5. Limit price for pivoted ETFs = live ask + 1 tick (Harris: last-trade-based limit is stale on thin ETFs).
6. One direction per underlying: no inverse while a day-tier long on the same stock/bull ETF is open, and reverse.
7. Research scores these as SHORT signals on the underlying (keeps watch-day stats clean); log quoted spread.
Rule E: frequency up (bounded: 8 names, co-hold/budget only, daily dollar budget, 7% kill); per-trade dollar risk
unchanged (1% at the ETF's stop). Declined: notional-beta caps (CEO declined leverage-weighted caps 2026-10-06).
