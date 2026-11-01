# Day-tier deep search, round 3 — 2026-10-29 (signed: Claude)

Read-only research; no bot code, no orders. Follows rounds 1–2. Same rules as before:
- choose on discovery years only (2020-10..2024-10);
- weight capital the honest way (a fixed slot per name, decided before the open);
- sessions after 2026-10-26 stay the untouched holdout.

Scripts are in the session scratchpad `day_search/`: `fam_r3.py`, `gex_r3.py`, output `out/r3_overnight.txt`.

## Plain-English summary

**No finalist this round.** None of the 116 new rule variants passed the discovery screen used in every round
(t ≥ 2.5 and still profitable at 10 bps). So no test-year result was read and the 2016–2020 confirmation set was not
used. **Total candidates tried: 30,063. Survivors: 0.** (Measured.)

**The main finding explains why.** The one edge measured on these names, the 5-session hold taken while SPY is
above its 200-day average, earns its money **overnight, not during the trading day**. On those trend-up days in the
discovery years:

| Universe | Overnight (prior close → open), per session | Open → close |
|---|---|---|
| 51 stocks | +6.7 bps | +1.0 bps |
| 20 ETFs | +8.9 bps | −0.7 bps |

A same-day trade that is flat by the close cannot collect that drift, whichever entry it uses. Every intraday entry
on trend-up days captured 0–1.6 bps before costs and lost money after 5 bps.

On trend-down days the pattern flips: overnight −5.5 bps, intraday +12.5 bps. But buying the open on those days is
pure noise after costs (t 0.62 for stocks, 0.30 for ETFs).

**Option-flow (GEX) data is far too thin to judge.**
- About 70 trading days and 13 symbols; only 140 usable symbol-days fall in the internal discovery window.
- No rule came near the bar (best t 1.01 on 58 trades).

## 1. Intraday entries on the trend days (the rule in `trackb_trend_swing_2026-10-28.md`)

The trend label is SPY's prior close above (long-only day) or below (short-only day) its 200-day simple average.
It is known before the open and built from SIP daily closes going back to 2016.

Long entries on trend-up days, exit at 15:55, discovery:

| Entry | Stocks: trades / gross bps / t at 5 bps | ETFs: trades / gross bps / t at 5 bps |
|---|---|---|
| 09:30 open (benchmark: hold open → close) | 36,234 / +0.7 / −1.39 | 15,245 / −0.8 / −1.92 |
| 10:00 | 36,234 / −1.7 / −2.38 | 15,245 / −1.4 / −2.26 |
| First dip to VWAP after 10:00 | 31,601 / −0.6 / −2.23 | 13,515 / −0.6 / −2.16 |
| 15-minute opening-range break | 20,698 / +0.3 / −1.79 | 9,856 / +0.8 / −1.50 |
| 30-minute opening-range break | 18,266 / +0.6 / −1.74 | 8,851 / +0.8 / −1.51 |
| 5-minute RSI(14) < 30 | 14,353 / −0.9 / −1.96 | 6,224 / −4.7 / −2.87 |
| 60-minute RSI(14) < 30 | 4,082 / +5.3 / +0.10 | 1,421 / +9.5 / +0.59 |

- A 1×ATR stop and a 60-minute exit were also tested. The best of all of these is the ETF 60-minute RSI dip held 60
  minutes: t 1.31, still losing at 10 bps.
- The short mirror on trend-down days lost in every version, which matches the design record's decision to drop
  shorts.
- **Drift captured:** none. Holding open → close on trend days earns about what any entry earns, about zero.

## 2. One-leg cross-sectional rank (long only, no short leg; 51 stocks)

| Rank used | Best version | Gross bps | Net 5 bps | t |
|---|---|---|---|---|
| Round-1 ML out-of-fold prediction at 10:00, top decile, to 15:55 | all days | +6.4 | +1.4 | 0.40 |
| Same, trend-up days only | | +4.6 | −0.4 | −0.10 |
| Prior-day return: buy the biggest losers at the open (top quintile), trend-up days | | +4.5 | −0.5 | −0.13 |

- 24 rank variants in all.
- Net of 2 bps, the best is Sharpe 0.53 (not significant). Net of 5 bps, none is positive with t > 0.4.
- Dropping the short leg halves the cost but also removes half of the relative signal. What remains is mostly
  market drift.

## 3. Option-flow features (GEX history)

Data: the bot's `logs/gex_history.jsonl` (copy from OCI), 2026-07-20..10-26.
- 549 symbol-days had a usable 09:45 ET snapshot: 13 symbols, 70 days.
- Labels: positive gamma 306, negative 148, near-flip 95.
- The whole window lies inside the round-1 test years, so I split it internally: discovery 07-20..09-10
  (140 symbol-days), test 09-11..10-26.

**Range as a filter.** Rest-of-day range (bps of price) by label: positive 160, near-flip 120, negative 91. This is
the opposite of the usual gamma theory, that negative gamma means bigger moves.
[hypothesis — unverified: the mix differs, e.g. SPY/QQQ carry more negative labels than single stocks.]

**Trades.** 24 rules:
- follow the first 30 minutes in negative gamma / fade it in positive gamma;
- each half separately;
- pull toward the pin centroid;
- fade within 0.25 ATR of the call or put wall;
- side of the gamma-flip strike;
- each inverted, exits 60 minutes and 15:55.

Best discovery result: wall fade to the close, +10 bps on 58 trades, t 1.01. "Follow in negative / fade in positive"
lost (−13.6 bps, t −2.73 at 60 minutes). No rule had at least 100 trades with t ≥ 2.5, so the test rows were not
read.

**Honest sample statement.** About 1–2 years of daily snapshots would be needed to detect a +10 bps per-trade edge at
t 2.5, given ~150 bps daily range noise. [inferred]

## 4. Confirmation set and beta

- The 2016–2020 SIP backfill is now complete: 20 ETFs, SPY, TQQQ and 30 large caps (2 ETFs and GS/WFC were not in the
  backfill list).
- It was **not used** this round because there was no finalist. It stays untouched for the next one.
- Beta diagnosis: no round-3 finalist to diagnose. Round 2's diagnosis of the closest-ever candidate (ETF hourly-dip
  long) still stands: beta 1.14–1.47 to SPY over the same bars; 75–94% of daily P&L explained; alpha t −1.98 / +1.28 /
  −1.29 across the three periods.

## Trials

Round 3:
- trend-entry family: stocks 57, ETFs 33;
- GEX: 24;
- long-open on trend-down days: 2.

Round 3 total 116. **Cumulative 30,063.**

## Diagnosis and next test

**Where the money is.** On these names, in the regime where the measured multi-day edge works, returns accrue
overnight. A flat-by-close tier is structurally shut out of it. Within the session, after 30,063 tries, the
cost-adjusted edge is zero.

**What would make a same-day edge possible:**
- new information the bot does not have yet: option-flow history of at least a year, or news timing; or
- a cost near 1 bp.

**What the Day tier could do instead** [proposal for the owner, not a measured result]: act as the entry-timing arm
of the multi-day hold.

**Next test (all on discovery first, confirmation on the untouched 2016–2020 stock set):** whether entering the
5-session trend hold at a Day-tier intraday price (VWAP dip / 60-minute RSI dip) beats entering at the open or at the
next bar after the Track B signal. Measure in R and in bps for the full 5-session hold.
- Run daily `daily.py` on post-holdout sessions only after a finalist exists.
- Re-run the GEX tests once `gex_history.jsonl` holds at least 250 sessions.
