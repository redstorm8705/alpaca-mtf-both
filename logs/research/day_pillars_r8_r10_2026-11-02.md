# Day tier — foundational pillars re-tested, rounds 8-10 — 2026-11-02 (signed: Claude)

Owner, 2026-11-02: "re-evaluate every foundational pillar informing the logic tree... do not settle." Read-only
research; no bot code changed, no orders. Rounds 1-7 (about 31,000 rules) all shared four assumptions: we pay the
spread on entry; we trade stock or ETF shares; trades are directional; the universe is ~50 mega caps and ETFs. Each
round below drops one of them.

Shared method: Alpaca SIP 5-min bars. Selection uses discovery only; finalists are pre-declared by rule (≥ 200
trades, positive at stress cost, top 5 by discovery book t); test years and the 2016-2020 confirmation are each read
once. Book t uses a fixed capital slot per name known before the open.

## Round 8 — execution style: resting limit entries (1,440 variants × 2 universes)

- Rule: at a decision bar, rest a buy limit k × daily ATR below the price (shorts mirrored). A fill counts only when a
  later bar trades through the limit by 1 cent, so adverse selection stays in the result.
- Exits: the close, or a resting target / taker stop.
- Costs: passive legs cost 0; taker legs cost the measured half round trip.
- Discovery: best book t 2.27 (stocks) and 1.77 (ETFs). That is in line with what 1,440 tries produce by chance.
- Finalists, out of sample:

  | | Test 2024-26 | Confirmation 2016-20 |
  |---|---|---|
  | Stocks (5 finalists) | net −10 to −0.5 bps | net −1.8 to +10.9 bps |
  | Stocks, best (vid 270) | −9.0 bps, t −0.49 | +10.7 bps, t 1.20 |
  | ETFs (5 finalists) | net −10.6 to +2.4 bps | net −14.3 to −3.0 bps |

  - Stocks: no finalist has t > 1.2 in either period.
  - ETFs: every finalist loses in confirmation.
- **Why it loses:** resting limits remove the spread, but the orders that fill are the ones where the price keeps
  going. The fill is adversely selected, so post-fill drift is about 0.

## Round 9 — sell same-day option premium on SPY/QQQ (108 structures × 2)

- Structures: short straddles, strangles and iron condors on 0DTE options.
- Entry 09:35 / 10:00 / 12:00 / 14:00; stops at 2× or 3× credit, else out at 15:45.
- Data: 0DTE trade-print bars, 2024-02..2026-10. Discovery is the first 60% of sessions.
- Discovery: SPY naked strangles at 14:00 led, t 3.67 (+$15 per 1-lot per day, 72% wins).
- Test, one look:

  | Cost | Finalists' $/day | Test t | Worst day per 1-lot |
  |---|---|---|---|
  | Base | −$1 to +$5 | −0.30 to +0.86 | −$387 to −$696 |
  | 3× (full spread per side) | −$11 to −$3 | all negative | |

- Iron condors (defined risk, what Alpaca's option levels allow) were weaker in discovery: best t 1.49.
- **Why it loses:** the premium was there in 2024-25 and shrank in the test months. Costs on at-the-money 0DTE
  options are not measured (no historical quotes), and the result flips sign between 1× and 3× the assumed spread.
- No 2016-20 confirmation exists (0DTE dailies began in 2022).

## Round 10 — market-neutral residual reversion at measured live cost (1,386 variants)

- Rule: every 30 minutes, rank the 50 stocks by their return net of beta × SPY over the last 15-60 minutes. Buy the
  laggards and/or sell the leaders, hedged with SPY, and hold 15 minutes to the close.
- Cost: measured live, 2.4 bps median round trip per stock leg, plus 0.5 bps for the SPY hedge.
- Discovery: best t 1.27; gross edge 3-6 bps. Not carried to a test.
- Bug noted: the first 200 sessions had no regime value and were counted as "down". It does not change the verdict.

## Running total
- About 34,000 same-day candidates; 0 survive out of sample.
- Test-year looks this session: 10 finalists in round 8, 5 in round 9, 0 in round 10.

## Round 11 — universe pillar: whole-market "stocks in play" (1,728 variants)

Data:
- 14,506 US symbols, active and delisted, 2015-06 to 2026-10. SIP daily bars and 5-min bars.
- Candidates each day (known at 09:30): prior close > $5, 20-day dollar volume > $20M, top 40 by |gap| / ATR%.
- At 09:35, rank the candidates by opening-bar relative volume.

Families tested: opening-bar direction, opening-range breakout (5/15/30 min), gap-and-go, gap fade. Each combined
with N, stop, target, a SPY-direction filter, a daily-trend filter and a relative-volume floor.

**Discovery (2016-01..2022-06, 1,620 sessions):**
- The 5-min opening-range breakout dominates.
- Rule: top 5 by relative volume. Buy-stop above / sell-stop below the 09:30 bar, first touch by 11:30. Stop at the
  other side of the bar, target 2R, else out at 15:55.
- Result: 7,539 trades, +35 bps gross, +25 net at 10 bps, t 4.64.
- With 1,728 variants, a Bonferroni-adjusted p is about 0.006.
- Every other family is below t 2.8.

**Test (2022-07..2026-10), one look at the 5 pre-declared finalists:**

| Finalist | Gross | Net at 10 bps | Net at 20 bps |
|---|---|---|---|
| Best | +26 bps | +16, t 2.1 | +6, t 0.8 |

- Net by year at 10 bps: 2022 +19, 2023 −9, 2024 +8, 2025 +22, 2026 +49.

**Measured cost:**
- Method: SIP quotes in the first 3 seconds of the breakout minute and at 15:55, priced at half spread + half
  spread.
- Test sample (300 trades, 2024-26): round trip 20 bps mean → +4 bps net. The flat-cost replay overstated the edge.

**Diagnosis — the edge lives in cheap-to-trade names, and that changed between periods:**

| Period | Pattern by entry-spread quartile |
|---|---|
| Discovery, 751 measured trades | Net positive in every quartile; the widest quartile carried the most gross |
| Test, 300-trade sample | Tightest two quartiles +70 bps gross; widest two −11 and −33 gross |

- A dollar-volume floor is a poor stand-in for spread. On discovery it removes the edge rather than isolating it.
- A spread cutoff can be read from the live quote at the trigger.
  - Chosen on discovery only: entry spread ≤ 16 bps had the best discovery t (2.32, net +33 bps on 462 trades).
  - Applied to the test sample: 133 trades, +72 gross, 7 cost, **+65 net, t 1.94**.
  - Positive each year: 2024 +86, 2025 +55, 2026 +50.

**Looks at the test years for this family:** 5 finalists (one look), then the 300-trade cost sample, then the
discovery-chosen spread rule on that sample.

**Full test set, measured spread on every trade (4,522 of 5,051 had quotes; one more look):**

| Rule | Trades | Gross | Cost | Net | Book t | Trades/day |
|---|---|---|---|---|---|---|
| No filter | 4,522 | +27.1 | 17.6 | +9.5 | 1.19 | 4.2 |
| **Entry spread ≤ 16 bps (cutoff from discovery)** | **2,302** | **+36.0** | **7.0** | **+28.9** | **3.45** | **2.1** |

- Spread ≤ 16, net by year: 2022 +18.6, 2023 +30.5, 2024 +35.5, 2025 +28.3, 2026 +26.7. Longs +27.9, shorts +30.0.
- Cutoff sensitivity (read after, not used to choose): ≤ 12 +23.3 (t 2.73), ≤ 20 +28.2 (t 3.48), ≤ 25 +27.8
  (t 3.49). The result does not hinge on the exact cutoff; ≤ 10 is weaker (+20.0, t 2.24).
- Multiple testing: test-year looks for this family total about 10 (5 finalists, the cost sample, the filter on the
  sample, the full set, 4 sensitivity cutoffs). Bonferroni over 10 looks on book t 3.45 gives p ≈ 0.006.
- 529 trades had no SIP quote in the window and are excluded; their gross is not known to differ.

**Short borrow (2026-11-03):** Alpaca's current asset flags (not point-in-time) mark 1,053 of 1,172 filtered short
trades' symbols easy-to-borrow. Keeping only borrowable shorts: 2,196 trades, net +24.7 bps, book t 2.94, positive
every year (2022 +13.5, 2023 +25.4, 2024 +35.1, 2025 +16.9, 2026 +28.1).

**Entry fills from SIP prints (2026-11-03):** for a sample of filtered trades (discovery 253, test 599), every SIP
print from the first print through the level onward. Level converted to raw prices with the ratio of the raw and
adjusted 09:30 bar.
- A plain stop order (size-weighted prints in the next second) fills within about 0.4 bps of the replay's price
  plus half spread: test +19.2 vs +19.6 bps per trade. A first pass that converted prices with a later bar's close
  ratio showed about 20 bps of extra slippage; that was a price-adjustment error, not slippage.
- Stop-limit at the level, held 30 s (best on discovery, t 1.45 among 21 cap/window variants): test fill rate 94%,
  +27.2 bps per signal vs +19.2 for the stop order. Caps 5 bps and wider are worse on both periods.
- These samples' averages (+19 to +20 bps) sit below the full-set +28.9; the sample t is about 1.2-1.8.

**Live data blocker and a usable proxy (2026-11-03):**
- The paper plan's real-time quotes are IEX only. On 300 test trades (2025+) at the breakout minute, IEX had no quote
  on 99, and where it quoted its spread was a median 3.6x the SIP spread. Filtering on IEX spread does not select the
  same trades (64% agreement) and the IEX-selected trades were not profitable. The SIP filter cannot run live as is.
- Proxy known before the open: the SIP spread at 15:50 ET on the prior session (SIP older than 15 minutes is
  available on this plan). Cutoff chosen on discovery (700 trades): prior-day spread <= 25 bps (best discovery t 2.06).
- Test (900 trades): unfiltered net +13.6 bps (t 0.81); prior-day spread <= 25 bps net +25.4 (811 trades, t 1.59);
  the 80 excluded trades net -106 bps. The exact trigger-time SIP filter on the same sample: +50.0 (469, t 2.86). The
  proxy recovers about half the filter's benefit.
- Option not tested: Alpaca's paid real-time SIP plan would allow the exact filter (owner decision: cost).

**Test-year looks for this family:** now about 14 (adds the borrow filter, the fill-model comparison, the IEX check
and the delayed-SIP proxy).

Still owed before it can be proposed: confirmation on its first live sessions (data never consulted); live
locate/borrow at order time (Alpaca's flag can change daily).

Scripts: session scratchpad `dn/r8_passive.py`, `dn/r9_vrp.py`, `dn/r10_resid.py`, `dn/sip_universe.py`,
`dn/r11_sip.py`, on the round-1 harness `day_search/`.
