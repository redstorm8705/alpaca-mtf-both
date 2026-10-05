# C2 day-tier — evidence ledger for the ten-mechanism foundation — 2026-10-05

**Owner/signature:** Claude. Companion to ChatGPT/Codex `day_tier_ten_mechanism_foundation_2026-10-05.md`
(branch `docs/day-tier-mechanism-foundation`).

**Division of labor (Rafael 2026-10-05):** ChatGPT builds and connects the ten mechanisms and the shared
architecture (point-in-time snapshot, family tags, implementation-shortfall attribution, router that can allocate zero).
Claude owns the ADMISSION evidence: every mechanism family goes through the fixed gate below and an independent
adversarial auditor before the router may allocate to it. Claude also builds Track M (approved 2026-10-05).

## The admission gate (fixed; candidates may not change it)
Script: `logs/lab/c2_claude_scripts/c2_harness.py` (on OCI). Hedged (QQQ-beta or cross-sectional-mean) daily book,
equal weight per trade per day, $0.04/share round trip always. Pass requires ALL of:
P1 mean > 0 in DISC (2024-11-05..2025-12-31) AND CONF (2026-01-02..2026-10-02); P2 date-level t >= 2.0;
P3 > 0 after +5bp/side slippage; P4 > 0 without the 5 best days; P5 sign-permutation p < 0.01; P6 >= 60% positive months.
A PASS then goes to a cold adversarial auditor (split-adjustment, leakage, beta decomposition, concentration,
parameter-grid dispersion, empirical null, slippage). Lessons already learned the hard way:
`1Min_sip_r` is RAW (unadjusted) — apply `logs/lab/split_factors.csv`; never let leveraged/inverse ETFs into the
tradable set unhedged; the within-day shuffle placebo does not test market-timing models.

## Evidence per mechanism (post-election sample, Alpaca SIP)
| # | Mechanism | Tested so far | Result |
|---|---|---|---|
| 1 | Order-flow imbalance | Tick-rule signed volume + $100k blocks, 09:31-09:59, 27 names, 2025-10 -> (download in progress, `logs/lab/ofi/`); test `c2_ofi.py` (cross-sectional ofi, block flow, own-surprise; exits 30m/1h/2h/close) | PENDING (insufficient overlapping names yet) |
| 2 | Residual vs market/sector | `c2_candidates.py` C1 (z>=2 reversal/momentum, 30m hold) and `c2_multi.py` F2/F3 at H=5/15/30/60/120m, both directions, 15 large caps vs sector ETFs | FAIL every config; gross ~0, cost-negative (e.g. F2 reversal 30m -5.7bp/day hedged) |
| 3 | Liquidity / implementation shortfall | infrastructure | build (no edge claim) |
| 4 | VWAP displacement | VWAP distance as a feature in walk-forward ridge v1/v2; in-play 10:00 VWAP state | FAIL as a feature; stateful path version UNTESTED |
| 5 | Catalyst + abnormal participation | Benzinga news counts + opening-auction size + RVOL in `wf_v2.py` (hedged +1.9bp/day t=0.44); stocks-in-play (3,655 name-days, gap>=3%) ORB +0.02R/+0.04R | FAIL |
| 6 | Overnight gap resolution | generic gap-down buy DISC +17bp / CONF -4bp; in-play gap continuation flips sign between windows | FAIL, EXCEPT Monday weekend-gap-down QQQ (Track M): audit WEAK-BUT-REAL |
| 7 | Volatility normalization | noise-area breakout (Zarattini 2024) refuted earlier | infrastructure only |
| 8 | Breakout / failed-break | first-5-min ORB on in-play names FAIL; failed-break trap UNTESTED | open |
| 9 | Dealer / leveraged-product hedging | GEX labels always POSITIVE (data defect); late-day leveraged-ETF continuation +3bp with placebo +2bp | BLOCKED (fix gamma sign first) |
| 10 | Time-of-day / auction / events | intraday periodicity (Heston-Korajczyk-Sadka) FAIL (-3.0bp/day); megacap->QQQ lead-lag FAIL; pre-FOMC drift absent; post-FOMC-statement 14:05-15:50 index decline 11/16 days, mean -40.9bp (lead, post hoc, not audited); Monday weekend dip (Track M) | leads only |
| + | Volatility risk premium (not in the ten) | 0DTE SPY/QQQ iron condor 10:00 -> 15:45, 478 sessions each of option 5-min bars in `logs/lab/0dte/`; test `c2_vrp.py` | RUNNING |
| + | Rafael 50/150 SMA cross (12h/6h/4h, 2016->) | cross-up modest bullish (+0.2..0.9% over 20 bars); waiting for the pullback worse than entering at the cross; fixed-target dip-buy ~breakeven; shorts lose | swing-horizon trend filter, not a day-tier entry |

## Data on OCI (research only, read-only pulls)
`data/cache/lab/bars/1Min_sip/` (SPY/QQQ/IWM/TQQQ/SQQQ 2022->), `1Min_sip_r/` (39 names 2024-10->, RAW),
`{SPY,QQQ,IWM,DIA}_1h_sip_2016.csv.gz`, `inplay_1m/` (3,697 name-days incl. premarket), `logs/lab/alt/` (Benzinga
news + auctions, 39 names), `logs/lab/ofi/` (order flow, in progress), `logs/lab/0dte/` (0DTE option bars),
`logs/lab/split_factors.csv`. Scripts + audit scripts: `logs/lab/c2_claude_scripts/`.

## Next (Claude lane)
1. VRP and order-flow results through the gate -> adversarial audit. 2. Untested families: VWAP path/state (#4),
failed-break trap (#8). 3. GEX gamma-sign root cause (#9). 4. Track M build through the full patch gate.
Every candidate result is appended here with the verbatim gate line.

## RESULTS UPDATE (2026-10-05, Claude) — all ten mechanisms now tested (except #9, blocked on gamma data)
- #1 order flow (24 names, 251 days, cross-sectional ofi / block flow / own-surprise, exits 30m/1h/2h/close): 12 configs FAIL
  (best O2 -> close -0.2bp/day). #8 failed-break trap / confirmed break N=5/15/30: FAIL. #4 VWAP reclaim/reject after
  60/120 one-sided minutes: FAIL (best REJECT-60 +3.2bp/day t=1.68, fails slippage + ex-top5).
- Multi-timeframe sweep 5/15/30/60/120m x {own, sector-residual, cross-sectional, megacap->QQQ} x {cont, rev}: 48 FAIL.
- Regime check (GAI): the same families split by prior-5-day QQQ range tercile — FAIL in HIGH-vol and LOW-vol alike.
- Options-as-signal (implied-move fade/breakout on SPY/QQQ from the 10:00 0DTE straddle): 12 FAIL.
- 0DTE iron condor (outside the day tier; Rafael: day tier = stocks/ETFs only): significant GROSS premium, survives
  $0.01/leg/side, fails $0.02; max loss ~18% of equity per condor at this account size. Historical option QUOTES are not
  available; OPRA agreement signed by Rafael 2026-10-05 (pending Alpaca activation) enables live quotes.
- Crypto BTC/ETH/SOL momentum/reversal 1h-24h: gross ~0.
## BGG ALIGNMENT 4/4 (board Simons + Thorp seats, Gro, GAI) — day-tier role
Day tier = small data-collection sleeve (~10-15% of risk budget, capped by the 5% tier kill) with a router whose DEFAULT
allocation is ZERO until a family passes admission; ship Track M (approved) + ChatGPT's point-in-time snapshot +
implementation-shortfall attribution; put research/growth weight on overnight / multi-day edges (swing tier) where the
evidence is strongest. Next research (pre-registered, no window shopping): overnight decomposition on index ETFs and
megacaps (Lou-Polk-Skouras 2019), turn-of-month/holiday-eve close->open (Lakonishok-Smidt 1988), CPI/NFP/FOMC event
windows as a single pre-registered set. Reviewer caveat: the result we trust is "no market-neutral intraday alpha at
retail costs in this sample", not "no intraday profit".
