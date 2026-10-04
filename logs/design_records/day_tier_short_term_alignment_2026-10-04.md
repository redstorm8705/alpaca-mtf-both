# Day tier — short-term alignment gate + counter-trend fades (2026-10-04, Claude-signed)

## Owner direction (Rafael)
- 2026-10-03: "Trades shouldn't be taken against another signal. Period." (shipped PR #464: trade must match the Layer-A
  daily trend side; TWO_SIDED/UNKNOWN = no trade.)
- 2026-10-04: "I do want counter-trend fades, but short-term indicators must align."

## Evidence (all 19 live day-tier trades since 2026-09-15; indicators on bars closed before entry)
"Short-term aligned" = 5m and 15m EMA13-vs-EMA30 and 5m close-vs-VWAP agree with the trade direction:
aligned + with daily trend 2 trades +$4.46 (2 wins); aligned counter-trend fade 2 trades -$3.10 (0 wins); aligned on a
TWO_SIDED day 4 trades -$16.19 (0 wins, all AMZN); not aligned 11 trades -$21.16 (2 wins). n is tiny — directional only.

## BGG alignment (board Thorp/Taleb + Simons/Harris, Gro, GAI)
- Rule (every Track A and Track B entry): ALL of — 5m EMA13 vs EMA30, 15m EMA13 vs EMA30, 5m close vs session VWAP,
  5m MACD-FAST histogram sign — agree with the trade direction (MACD-FAST: Gro + GAI 2-1 over the board; RSI not
  required: GAI only). Computed with strategy.confluence-family helpers (indicators/*), closed bars only (T1).
- Counter-trend (trade direction opposite the Layer-A side): allowed ONLY for FADE mode and only when aligned.
  RIDE / Track B counter-trend stays blocked. TWO_SIDED / UNKNOWN stays blocked (board + owner rule).
- Fail-safe: any missing/insufficient bar data or indicator -> not aligned -> no trade.
- Reversal (live evidence, no waiting period): counter-trend fades auto-disable when their cumulative realized P&L
  <= -$25 (~1% equity) or they lose 3 in a row (board). Kill flag DAYTRADE_COUNTER_TREND_FADES_ENABLED.
- Every counter-trend entry is tagged (trigger.counter_trend=True, trigger.alignment=checks) for measurement; every
  skip is logged with its reason (Rule D).
- Rule E: requiring alignment on all entries lowers frequency; allowing aligned counter-trend fades raises it versus
  the 2026-10-03 rule (still below the pre-10-03 baseline) -> risk-path (frequency) -> board gate (above).

## REVISION 2 (2026-10-04, later the same day) — supersedes the rule above where they differ
### Owner direction (Rafael)
- "I want the 2-minute and 5-min scans aligned. The 15-min for the day tier can be an inform but shouldn't be blocking."
- "For counter trend, I think 15-min should lead and then ladder into a 30-min bar. We don't want to fade rallies. We
  need to be sure the intraday (day tier) trend has failed/rejected and isn't just a bull flag. This is critical."
- The runner already scans every 2 minutes (cron */2).

### Data (verified on OCI 2026-10-04)
- Bars from data.fetcher include extended hours (to 20:00 ET) and indicators/vwap.add_vwap resets on the index's
  date — UTC for raw bars. The gate therefore fetches ONE IEX 1m window (fetch_bars_window, feed="iex", 8 calendar
  days), keeps 09:30-16:00 ET only, re-indexes to ET (VWAP resets per ET session) and resamples to 2/5/15/30m
  anchored at 09:30. A bucket counts only when it has ended AND the 1m data reaches its end. Newest 1m bar older than
  15 min -> fail closed (data contract #1). < 40 closed bars on any timeframe -> fail closed. One fetch per symbol
  per minute (cached), only for candidates that already have an ENTER trigger.
- fetch_bars/fetch_closed_bars were NOT used: their lookback formula (days_back = n_bars x 2 for 1Min) would request
  years of 1m bars for the history this gate needs.

### With-trend gate — 2m + 5m (board LdP/Asness + Gro + GAI: unanimous option B)
- Blocking on BOTH 2m and 5m: EMA13 vs EMA30 and close vs session VWAP agree with the trade direction.
- Logged, not blocking: 2m/5m MACD-fast sign, all 15m checks (config.DAYTRADE_ALIGN_BLOCKING_CHECKS = ("ema","vwap");
  adding "macd_fast" restores momentum blocking).
- Why not MACD blocking (option A): replay of all 19 live trades allowed 0/19 and blocked all 3 winners, each of which
  failed ONLY on MACD-fast; a with-trend FADE buys a dip, so 2m/5m momentum is wrong-sided by construction. Sweep
  (11 symbols x 10-min samples x 5 sessions x 2 directions, 2,332 samples): A aligned 9.8%, B 30.9%.
- Replay with B: 4/19 allowed (GOOGL 9/23 +$2.53, AAPL 9/25 +$3.10, EWY 9/25 +$1.93, MSFT 9/21 -$0.63); the other 15
  (net -$43.0) blocked. n=4 is NOT evidence of edge (Wilson ~30-95%); B is the least-parameterized option that
  honours the owner rule, shipped as a live hypothesis.

### Counter-trend fade — trend-failure test (board Brandt/Harris + Gro + GAI, 2026-10-04)
Short fade of an up-trend shown; a long fade of a down-trend is the exact mirror. ALL required, closed bars only:
- B1: the with-trend gate above, in the fade direction (2m + 5m).
- B2 (15m leads): last closed 15m EMA13 < EMA30, close < VWAP, MACD-fast histogram < 0.
- B3 (15m structure, TODAY's session bars only; W = last 16 session 15m bars; >= 6 required, i.e. no fade before
  11:00 ET): H = highest high in W at index h with >= 2 closed bars after it and H >= session high - 0.10 x ATR15;
  S = lowest low in W up to h; HL = lowest low of the 4 bars before h.
  (i) last close < HL - 0.10 x ATR15 (body break of the last higher low);
  (ii) (H - last close)/(H - S) >= 0.50 (deeper than a flag);
  (iii) highest high after h < H - 0.25 x ATR15 AND no bar after h+1 closed above its 15m EMA13 (the bounce failed).
- B4 (30m ladder): the last closed 30m bar of today's session closes below its EMA13 AND below the prior session 30m
  bar's low (AND — all three reviewers; the 30m EMA13<EMA30 OR-leg was dropped as a stale multi-day state).
- EMAs warm up across prior sessions; VWAP and all structure use today's session only.
- Replay: all 7 live counter-trend fades (0 wins) are blocked.
- Existing guards verified (no new code): same-symbol day-tier re-entry blocked and opposite-side cross-tier netting
  blocked (execution/day_trade_manager.place_entry), shared per-name caps, concurrency cap 3, no new size multiplier.

### Rule E
Versus the live baseline (PR #464 blocks every counter-trend trade), re-admitting fades RAISES frequency -> risk-path
-> board gate incl. the masked-loss seat. The with-trend change only removes entries.

### Reversal criteria (no waiting period; per trigger class, FADE and RIDE separately)
- Counter-trend fades auto-disable at cumulative realized P&L <= -$25 (partials included) or 3 consecutive losses.
- After 30 trades through the new gate or 6 weeks, whichever first: (1) expectancy <= 0 with the bootstrap 80% CI wholly
  below zero -> rebuild the gate; (2) MACD-misaligned trades negative and MACD-aligned positive with a CI excluding
  zero -> add "macd_fast" blocking for RIDE only; (3) fewer than ~1 trade per 5 sessions -> too tight, loosen the 5m
  check first. Trend-failure test is wrong if post-break price returns above HL within 4 bars in more than half of
  fades.
