# TRACKB-TREND-SWING-1028 — Track B signal as a trend-aligned 5-session Swing entry (design record, Claude 2026-10-28)

Status: DESIGN — not built, not approved. Evidence: `logs/research/track_b_6y_confirmation_exit_2026-10-28.md` Part 2.

## Frame
Paper account, $2.5K → $25K; data collection and edge evaluation first; 7% daily kill switch is the only daily brake.

## Proposal
When the live Track B screen (`strategy/day_tier_track_b.screen_mover` + `compute_momentum_trigger`) fires ENTER on a
`DAY_TIER_UNIVERSE` name, also offer the signal to the Swing tier as a separate entry:
1. Direction filter: SPY's prior close above its 200-day SMA → long signals only; below → short signals only; unknown → none.
2. Stop: entry ∓ 1.0 × the stock's 14-day daily ATR (known before the open). GTC stop.
3. Exit: the stop, or the close of the 5th session after entry (entry day = session 0).
4. Sizing: the existing Swing sizing (SWING-OWNER-1025: r = 0.25 × tier Kelly, ≤2% equity at the stop, K-limb ceiling,
   gap check). No new multiplier.
5. Selection when more signals than room: first signal of the session in clock order (tested: +0.224R / +0.122R).
The Day-tier Track B trade itself is unchanged (never-skip rule).

## Evidence (6-year replay, 18 names, 5 bps cost)
- Discovery 2020-10-26..2024-10-25: n 2,541, +0.183R, Sharpe 1.85, month-block 90% CI +0.019..+0.346.
- Test 2024-10-28..2026-10-26: n 1,422, +0.217R, Sharpe 2.14, CI +0.039..+0.402.
- vs the same rule on every session without a signal: +0.102R (p 0.035) / +0.151R (p 0.016).
- 614 rule sets tried; earlier batches' test results were seen before this one was designed.

## Rule C (front-loaded sim)
- Cases: 2020–2026, includes the 2022 bear (−0.039R) and 2020–21 / 2023–26 bull years.
- Expected effect: about +0.2R per trade before ranking; first-of-day selection +0.12R in test.
- Reversal criterion: mean R ≤ 0 over the first 40 closed trades, or a 90% CI wholly below +0.05R after 80.
- Size / frequency / concurrency delta: FREQUENCY non-zero (new entries) and CONCURRENCY non-zero (multi-day holds) →
  RISK-PATH; full board + masked-loss seat required. Size per trade: zero (existing Swing sizing and caps).

## Review round 1 (Codex + GAI, 2026-10-28)
Measured in response (`portfolio.py`):
- Entry at the OPEN of the bar after the signal bar (executable): +0.183R / +0.218R — unchanged.
- Portfolio replay, signals in clock order, one lot per symbol, slot held through its exit session:
  | cap | half | trades | mean R | Sharpe (session P&L) | max drawdown | months positive |
  |---|---|---|---|---|---|---|
  | 1 | disc / test | 198 / 98 | +0.235 / +0.295 | 0.82 / 1.22 | −13.8R / −12.8R | 27/49 / 14/25 |
  | 3 | disc / test | 560 / 289 | +0.193 / +0.210 | 1.19 / 1.24 | −33.1R / −21.5R | 29/50 / 15/24 |
  | 5 | disc / test | 854 / 454 | +0.167 / +0.183 | 1.15 / 1.17 | −44.4R / −23.5R | 27/50 / 13/25 |
- Drawdown in equity = R × risk per trade: at 0.5% risk, cap 3's −33R is −16.5%; at 2% it is −66%. Risk per trade must be
  set from this, not from the 2% Swing ceiling.
Not adopted: GAI's "kill switch liquidates multi-day holds" (CLAUDE.md: the 7% switch halts new entries only; changing it
is a separate owner decision).
- Alpha vs SPY beta (`beta.py`; each trade minus direction × 60-day beta × SPY's return over the same entry→exit window;
  trades with a 40+ day beta history: 2,526 / 1,406): +0.171R → +0.111R after removing SPY (discovery);
  +0.189R → +0.093R (test). Median beta 1.57 / 1.82. About half the return is market exposure; about half remains.
- Tails and sides (`tail.py`, next-bar entry, all trend-aligned signals): R std 1.76 / 1.88; worst −4.68R / −3.76R;
  1st percentile −1.97R / −2.35R; trades below −2R 1.0% / 1.8%. LONGS +0.262R (n 1,917) / +0.289R (n 1,258);
  SHORTS −0.061R (n 624) / −0.331R (n 164) — shorts lose in both halves, so the discovery years alone already reject them.
  The five worst trades (AAPL 2024-08-02 ×2, META 2024-04-23, MSFT 2026-01-27, AMZN 2024-07-31) are longs held through
  an earnings date [earnings dates from memory — to be confirmed against FMP before the block is built].
Board seat Thorp/Taleb (cold, 2026-10-28) required changes: 0.5% equity risk per trade (ceiling 0.75%, tier Kelly
from live fills inside that band), cap 3, ownership fix with the Day tier on the same symbol (wash-trade rejection and
whole-symbol close), earnings-window block, borrow check for shorts, step-down (not switch-off) reversal measured vs the
no-signal control.

## Revised proposal (after review round 1)
- LONG ONLY, taken only while SPY's prior close is above its 200-day average; no entries below it (shorts dropped:
  negative in discovery and test).
- Stop 1.0 × 14-day daily ATR (GTC); exit at the stop or the 5th session's close.
- No entry when the stock reports earnings inside the 5-session window (T2 FMP calendar; unreadable → no entry).
- Risk 0.5% of equity per trade at the stop; at most 3 open lots; one lot per symbol; signals in clock order.
- Same-symbol ownership with the Day tier must be resolved before go-live (qty-bounded close, owner tags,
  cancel-stop → buy → re-place-stop on adds).
Long-only portfolio replay (`portfolio_long.py`, next-bar entry, no earnings block yet):
| cap | trades disc / test | mean R | Sharpe | max drawdown | total R |
|---|---|---|---|---|---|
| 3 | 444 / 260 | +0.231 / +0.226 | 1.24 / 1.26 | −24.1R / −12.5R | +102.8 (4 yr) / +58.6 (2 yr) |
At 0.5% risk per trade: max drawdown about −12% / −6%; about +29R a year ≈ +14.6% a year simple (before compounding,
before the earnings block). [Projection from replay — not live evidence.]
Board seat LdP/Harris (cold, 2026-10-28) corrections, accepted:
- The entry-day Sharpe/t overstate significance (overlapping 5-session holds). Re-computed for the selected rule:
  t by entry day 2.72 / 1.56; Newey-West lag 5: 1.86 / 1.16; by week 1.00 / 0.74; by month 1.54 / 1.43. Deflated for
  ~100 effective trials (discovery) / ~5 (test): deflated-Sharpe probability about 0.6. Realistic edge +0.05 to +0.10R per
  trade, not +0.2R. The Sharpe 1.85 / 2.14 figures above are entry-day figures and are withdrawn as performance claims.
- Daily bars are RTH-only (high/low match the 5m bars ~99.9%): resolves the extended-hours stop question.
- Code fixes applied (`portfolio_long_mtm.py`): the 15:55 bar is now checked for the stop; P&L is marked to each daily
  close. Long-only, next-bar entry:
  | cap | trades disc / test | mean R | Sharpe (daily marked) | max drawdown | worst day |
  |---|---|---|---|---|---|
  | 1 | 157 / 87 | +0.289 / +0.296 | 1.01 / 1.21 | −10.1R / −9.1R | −4.3R / −3.1R |
  | 3 | 444 / 259 | +0.240 / +0.231 | 1.22 / 1.24 | −25.6R / −15.8R | −5.9R / −4.9R |
  | 5 | 672 / 408 | +0.198 / +0.218 | 1.09 / 1.35 | −38.1R / −19.3R | −7.9R / −6.7R |
  At 0.5% risk per trade, cap 3: max drawdown −12.8% / −7.9%, worst day −3.0% / −2.5%.
- Required before build: replay the LIVE-gated signal (Track B's trend gate, watch-day rule, min-stop skip) or tap the
  raw screen upstream of them; tag lots with their own source and keep their fills out of the existing Swing Kelly
  statistics; reversal = weekly-clustered CUSUM (+0.1R vs 0) plus a path bound (cumulative R below the replay's
  5th-percentile path at the same trade count) → size steps down; owner defines "non-working".
- Survivorship: today's 18 names; per-symbol signs flip between halves. Not removable with this data.
Still owed: live-gated replay; earnings block in the replay; sector residual; Q1–Q5 build answers.

## Review round 2 (Gro as reviewer #2 — Codex at its usage limit — and GAI, 2026-10-28)
Both name the same open items as the board: same-symbol ownership with the Day tier (GAI's failing case: a stop
re-placed on a combined NVDA position while the Day lot exits leaves the Swing lot without a correct stop), the earnings
block with POINT-IN-TIME dates, and the live-gated replay. GAI also asks for an explicit "non-working" state that halts
this source's entries until the owner re-enables it.

## Revised design v3 — PROMOTION instead of a second entry (closes the ownership conflict)
The Swing tier never opens its own lot on the symbol. The Day-tier Track B LONG trades exactly as today on the entry day
(same signal, same size, same structural stop; never skipped). Two changes:
1. Track B longs drop the 2R same-day profit target (shorts unchanged).
2. At 3:56 PM ET a Track B long still open is handed to the Swing tier through the EXISTING Day→Swing promotion path
   (`execution/day_promotion.py`: decide → hand off → adopt, ledger transfer, restart-safe) when SPY's prior close is
   above its 200-day average and the stock has no earnings inside the next 5 sessions. Swing stop = entry − 1.0 × 14-day
   daily ATR (never tighter than the Day stop); out at the stop or the 5th session's close. The carried size is cut so
   shares × (mark − Swing stop) ≤ 0.5% of equity (the excess sells with the Day tier's 3:58 exit) — a partial
   promotion, which the module does not do today.
Replay (`promo.py`, `promo2.py`; same 7,624 signals, longs only changed):
| Track B longs | discovery 2020–24 (n 2,532) | test 2024–26 (n 1,387–1,388) |
|---|---|---|
| today (2R target, flat at close) | −0.069R, −2.2 bps | −0.040R, +3.8 bps |
| hold to close, no target | −0.048R, −0.2 bps | +0.013R, +6.7 bps |
| v3: no target + promote in SPY uptrend | +0.325R, +32.6 bps | +0.273R, +43.8 bps |
- Promoted: 871 / 603 lots, +2.33R / +1.83R each in Day-stop units; +192 bps mean, median +18 bps, 51% winners;
  worst −2,424 bps (one gap), 1st percentile −1,002 bps, 5th −661 bps.
- By year, bps per Track B long, today → v3: 2020 −7.9 → +50.1; 2021 −0.8 → +63.0; 2022 −2.6 → −25.7 (29 promoted);
  2023 +3.2 → +39.9; 2024 +4.6 → +31.7; 2025 +6.7 → +56.7; 2026 +3.3 → +45.4.
- Not yet run for v3: daily-marked drawdown at 0.5% carried risk, earnings block (point-in-time), sector residual,
  live-gated signal. The 2022 loss says the SPY 200-day switch lags at turns.
- The existing promotion rule P2 (lot ≥ 0.5R in profit at 3:56) lowers the result in both halves (`promo3.py`):
  no profit test +0.325R / +0.273R (871 / 603 promoted); ≥ 0R +0.284 / +0.180; ≥ 0.5R +0.277 / +0.083; ≥ 1R +0.217 /
  +0.106. v3 therefore drops P2 for Track B longs; the stock-trend test P3 is replaced by the SPY 200-day test.
- Round 3 (GAI; Gro returned HTTP 413, Codex at its usage limit): the promotion model closes the ownership conflict;
  requires the earnings-block replay, gap handling at the open (the replay already fills a gapped stop at the open), and
  a VIX>30 override [a fixed threshold — conflicts with the no-static rule; not adopted as written].
- Earnings block (`promo4.py`; FMP T2 `/stable/earnings` history; AVGO, MU, SMCI, SNDK return HTTP 402 on this plan,
  DRAM/EWY are ETFs): the five worst trades are confirmed earnings holds (AAPL 2024-08-01, META 2024-04-24,
  MSFT 2026-01-28 report dates). Fail-closed (no promotion when the calendar is unreadable): all Track B longs
  +0.151R / +19.5 bps (disc), +0.300R / +27.6 bps (test); promoted lots +169 / +160 bps; worst −1,534 / −1,164 bps.
- CORRECTION — sizing-consistent book (`promo7.py`): the R figures above are in Day-stop units, which overweight the
  carried leg. With every trade at 1 unit of risk (0.5% equity) and the carried lot cut to 1 unit at the Swing stop
  (the excess sold at the close), v3 improves the Track B book but does not make it profitable:
  | whole Track B book (longs + shorts), daily-marked | disc total / Sharpe | test total / Sharpe |
  |---|---|---|
  | today | −299.7R / −1.40 | −61.6R / −0.56 |
  | v3, cap 3 carried | −196.6R / −0.65 | +43.1R / +0.27 |
  | v3, no cap | −119.5R / −0.38 | +107.0R / +0.60 |
- v4 (`promo8.py`): a qualifying Track B long (SPY > 200d, earnings clear, free slot) takes the WIDE stop from entry
  (entry − 1.0 × ATR, 1 unit of risk there, no target) and is promoted whole; every other Track B trade as today:
  | whole Track B book | disc total / Sharpe | test total / Sharpe | worst day |
  |---|---|---|---|
  | v4, cap 3 | −177.8R / −0.81 | +10.2R / +0.09 | −13.8R / −11.0R |
  | v4, cap 5 | −120.5R / −0.52 | +62.7R / +0.50 | −13.8R / −11.7R |
  | v4, no cap | +127.3R / +0.42 | +186.4R / +1.05 | −24.5R / −20.1R |
- What this shows: the edge is the wide-stop, multi-session LONG leg (v2 long-only cap 3: +0.240R / +0.231R per trade,
  Sharpe 1.22 / 1.24, daily-marked). The existing Track B Day trades at their tight stops lose −299.7R / −61.6R at equal
  risk, and they stay in the book under the never-skip rule; shorts are part of that loss (−0.061R / −0.331R per trade
  on the trend-aligned subset). Whether the losing Day leg keeps its size is an owner decision (Q2: fewer trades needs
  owner approval); the never-skip rule allows changing SIZE, not count.
Why v3: no new entries and no second lot on the symbol (no co-hold, no wash-trade conflict); it uses the restart-safe,
ledger-aware promotion code that already exists; every Track B trade is still taken.

## Open questions for the review
Q1 ownership/co-hold with the Day-tier Track B lot on the same symbol; Q2 shorts overnight (borrow, Swing short support);
Q3 SPY 200-day source (T1 daily bars) and fail-closed when unreadable; Q4 interaction with the overnight K cap;
Q5 daily-bar stop check vs the live GTC stop (replay assumption).

## REOPENED 2026-10-29 as a Swing-tier design (owner: "We can open the 5-day as swing tier design")
Status: DESIGN — reopened, not built, not approved. Scope: the v2 LONG-ONLY rule as a Swing-tier entry source
(SPY prior close > 200-day SMA; 1.0 × 14-day ATR GTC stop; out at the stop or the 5th session's close; 0.5% equity
risk per lot; cap 3; one lot per symbol; earnings block), with v3 promotion kept as the ownership answer when the Day
tier already holds the symbol.
Owed before an approval package (in order):
1. **Earnings coverage on every name.** 2026-10-29 audit: FMP's plan returns HTTP 402 for NOW (and AVGO, MU, SMCI, SNDK
   per round 2), and the calendar endpoint returns only 28–48 rows. A fail-closed block on FMP alone silently removes
   those names from the source. The block needs a date (or an event detector) for every universe name; candidate: the
   option chain's earnings-spanning expiry (implied move vs the next expiry, `data/gex.py` already prices ATM straddles).
2. Point-in-time earnings block inside the v2 portfolio replay (cap 3, daily-marked).
3. Live-gated signal replay (Track B trend gate, watch-day rule, min-stop skip).
4. Confirmation on sessions never consulted (after 2026-10-26); first 40 closed live lots carry the reversal rule.
5. Board (full + masked-loss seat) → GAI + Codex → owner approval package with the Required-questions section.
