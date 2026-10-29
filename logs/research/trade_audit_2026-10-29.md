# Trade audit — 2026-10-29 (signed: Claude)

Owner request: "full audit report of trades from today understanding the why, what went wrong, and how we're going to
optimize and/or fix the bugs". Sources: Alpaca fills (activities API), OCI `logs/trade_events.jsonl`,
`logs/day_tier_events.jsonl`, `logs/mtf_bot.log`, `data/state/quarterly_holds.json`, `data/state/ownership_ledger.json`,
FMP `/stable/earnings` and `/stable/earnings-calendar`, Alpaca IEX 5-min and SIP daily bars.

## Realized today (fills)
| Tier | Symbol | Bought | Sold (PT) | P&L | Why it exited |
|---|---|---|---|---|---|
| Swing | NOW 2 sh | 157.59 on 10-26 (55-day breakout) | 117.60 @ 6:31, 116.02 @ 6:32 | −$81.56 | resting GTC stop 142.07; open 120.905 (SIP daily; −19.5% vs 150.13 close) |
| QHM | GOOGL 1 sh | 339.97 on 09-29 | 285.31 @ 6:30 | −$54.66 (−$39.58 of it from the earnings gap vs the 324.89 close) | resting GTC stop 288.97; open 286.89 after 10-28 earnings (EPS 2.12 vs 3.02 est.) |
| Day (Track A) | MSFT 3 sh | 537.99 @ 6:46 | 527.80 @ 6:52 | −$30.57 | protective stop 527.98 |
| **Total** | | | | **−$166.79** | |
Equity 2,655.86 → 2,554.44 (−$101.42) includes open positions marked to market.

## Why each happened
1. **NOW (Swing).** Bought by the 55-day breakout rule 2 sessions before the gap. `execution/swing_breakout_manager.py`
   has no earnings check (no reference to earnings anywhere in the file). Even with one, the data is missing: FMP's
   per-symbol endpoint returns HTTP 402 "not available under your current subscription" for NOW, and NOW is absent from
   the FMP calendar (48 rows for 10-20..11-05). The bot is blind to NOW's report date. [That the 10-29 gap is NOW's
   earnings is consistent with the gap and volume; no source in our tiers confirms the date.]
2. **GOOGL (QHM).** QHM knew the date: `QHM earnings sym=GOOGL next=2026-10-28 src=fmp` (10-28 19:23 UTC) and logged
   "stop KEPT through earnings (pre-earnings stop cancel disabled...)". `config.QHM_EARNINGS_STOP_CANCEL_ENABLED = False`
   since the 10-12 earnings-date fix; the config note says the stopless earnings window "needs the CEO's ratification".
   That decision was never brought back.
3. **MSFT (Day, Track A FADE).** Failed downside sweep of the 530 put wall in positive gamma, volume-confirmed, 2m/5m
   aligned, conviction 0.74 → 3 sh ($1,614 notional). The walls came from option positioning before MSFT's 10-28
   report. Stop room $10.01 (1.9%); the first 5-min IEX bar alone ranged 540.79–525.44 ($15.35). Same failure the Track A
   study found: stop inside that session's noise. The noise estimate uses the prior 5 sessions' time-of-day ATR, which
   does not see an earnings morning. Day tier code has no earnings awareness (`day_trade_manager.py`, `run_day_tier.py`).
4. **"Exiting at 6:31."** NOW and GOOGL were not bot decisions at 6:31 — they were resting broker stop orders that the
   overnight gap went straight through, so they filled at the first prints. Waiting would not have helped today:
   NOW 114.67 at 7:00 PT and ~111.9 at 8:35 (vs 116.81 sold); GOOGL 286.20 at 7:00 (vs 285.31). The loss came from
   holding into the print, not from the exit time.

## Bugs / defects found
- B1 Swing tier: no earnings check on entry or while holding. Still exposed: XOM 3 sh (FMP: earnings 2026-10-30),
  AAPL 1 sh (FMP: 2026-11-02).
- B2 Data: FMP plan does not cover NOW, AVGO, MU, SMCI, SNDK earnings; calendar endpoint is row-capped. Any earnings
  rule built on FMP alone is blind on those names.
- B3 `earnings_preflight.py` reads only the legacy `trade_log.json` (L32, L59) → "Total tracked symbols: 0" every week
  (10-25 log) while GOOGL, MSFT, NOW, XOM, AAPL were held. The weekly earnings warning never fires.
- B4 Ledger: `sync_ledger` paged CRITICAL "protected floor shrink GOOGL/qhm" at 6:40 and 7:00 PT (QHM recorded its own
  stop fill only at 7:07); "impossible negative protected replay GOOGL/qhm=-2" warning repeats every 20 minutes.
- Open decision (not a bug): QHM pre-earnings stop cancel awaiting CEO ratification since ~10-12.

## Fix plan (each through design → board + GAI + Codex → owner approval; entry/exit changes need the owner's yes)
- F1 Earnings risk for every tier from one source that covers every name: an event detector from the option chain
  (earnings-spanning expiry's implied move vs the next expiry), FMP date as confirmation. Feeds: Swing entry/hold,
  QHM, Day-tier noise estimate. Anti-silo: one signal, three consumers, unknown → treated as event.
- F2 Swing tier: size a new lot by the larger of the ATR stop and the expected earnings move when a report falls inside
  the expected hold (fewer shares, not fewer trades), and decide pre-report handling of open lots (owner decision).
- F3 Day tier on post-earnings mornings: noise estimate from today's realized opening range, not the prior 5 sessions.
- F4 `earnings_preflight.py`: read tracked symbols from the ownership ledger + QHM state.
- F5 Ledger: suppress the floor-shrink page when the shrink matches a filled protective-stop order of that tier.

## F1 probe — does the option chain see reports FMP misses? (2026-10-29 ~09:30 PT, read-only, `evdet.py`)
Measure: ATM straddle implied move (`data/gex.get_earnings_implied_move_pct`) for the 10-30, 11-06 and 11-13 expiries →
variance per trading day of the front week vs the following week ("front/next"), and of the following week vs the week
after ("next/after"). A report concentrates variance in the week that contains it.
| Group | Names (FMP date) | front/next | next/after |
|---|---|---|---|
| Report this week | AMZN (10-29) 11.88, COIN (10-29) 2.63, XOM (10-30) 1.52 | ≥1.52 | — |
| Report next week | AAPL (11-02) 4.12, PLTR (11-02) 4.35, UBER (11-04) 3.71, AMD (11-03) 2.42 | 0.13–0.37 | ≥2.42 |
| FMP-blind names | SNDK 2.54 (this week), SMCI 2.99 next/after (next week) | | |
| No report in window | SPY 0.62/0.93, QQQ 0.59/1.16, GE 0.64/1.11, JPM 0.52/1.10, META 0.61/0.94, NVDA (11-17) 0.51/0.97; already reported MSFT 1.03/1.23, GOOGL 1.08/1.34, NOW 1.04/1.47 | ≤1.08 | ≤1.47 |
- Separation is clean on this snapshot: every FMP-dated report week shows ≥1.52 (front) or ≥2.42 (next); no-report
  names stay ≤1.08 / ≤1.47. One snapshot, 24 names — the cut-off must be derived from a history of snapshots, not set
  from this table. TSLA, NFLX, AVGO, MU, BRK.B, LLY were cut off by HTTP 429 (rate limit) in this probe.
- Production note: the probe's 429s mean the detector must ride the existing GEX refresh cadence, not a separate burst.

## Outcome of the fix plan (same day)
F1/F2/F3 were replayed before build and withdrawn: holding Swing lots through reports at full size earned the most in
both halves (A +162.4R / +58.2R vs trim/close/size-down), and the Day-tier report-morning noise rule showed no
consistent gain. See `logs/design_records/earnings_risk_1029.md` "Review round 1". Open: B3 (F4), B4 (F5), QHM
ratification.

## B4 diagnosis (ledger false page after a QHM stop fill)
- GOOGL's QHM GTC stop (order 80cc0070, recorded as `stop_order_id` in `quarterly_holds.json`) filled at 13:30:42 UTC.
- QHM's first cycle of the day ran at 14:07 UTC (`QHM GOOGL broker position is flat`, then `stopped out (own GTC stop
  filled)`), so QHM recorded its own exit 37 minutes after the fill.
- In between, the 20-minute `ledger_sync` (13:40, 14:00 UTC) saw GOOGL/qhm 1 → 0 with net 0 and paged CRITICAL
  "protected floor shrink … need OPERATOR CONFIRMATION". It cleared at 14:20 after QHM's authorized reduction.
- Root cause: the ledger sync cannot tell a protected tier's OWN stop fill from an unexplained shrink until that tier
  runs. Candidate fix: treat a shrink as authorized when the broker shows the tier's recorded stop order filled for
  exactly that quantity. The fix belongs in `execution/ownership_guard.py` (1,824 lines, safety-critical), so it needs
  the full gate (full read, board with the masked-loss seat, GAI + Codex) before an approval package.
- Separate open item: every 20 minutes since 14:00 UTC the sync logs "reattributed impossible negative protected
  replay GOOGL/qhm=-2"; the fill replay attributes two more GOOGL sells to QHM than buys. Not yet root-caused.

## GE question (owner, 2026-10-29): +$30, no profit taken, stop looks old
- GE is a QHM hold: 3 sh, average 302.50 (first tranche 320.90 on 09-29, then the owner's 10-23 add lowered the
  average), stop 272.71 GTC (`quarterly_holds.json`). Last 313.20 (IEX, 10-29 ~13:00 PT) → +3.5%, ≈ +$32.
- QHM never sells for profit on a schedule; it holds for the quarter. Its only profit mechanism is the trailing lock
  (`quarterly_hold_manager.py:4316`, `_TRAIL_LOCK_MIN_GAIN_PCT = 0.05`): once a hold is ≥ +5% over its average it raises
  the stop to average + ½ of the gain. GE needs ≥ 317.62 to start; at +3.5% nothing moves.
- The stop was set from the first tranche and is not re-derived when the owner adds shares. Only bot tranche adds may
  raise it ("raise protection to the design stop for the new average", L3330), and never loosen it.
- Re-deriving GE's stop the way the post-earnings re-anchor does (`_resubmit_post_earnings_stop`, L2450: max(price −
  2.5 × 14-week ATR, price × 0.85)) at 313.20 gives 266.22 (ATR 20.74 from Alpaca weekly bars) — lower than today's
  272.71, so the old stop is the tighter one and a raise-only rule would leave it.
- Finding: that post-earnings re-anchor runs only from `PENDING_EARNINGS`, which is set only when the pre-earnings stop
  cancel runs (L2381); with `QHM_EARNINGS_STOP_CANCEL_ENABLED = False` no QHM stop is ever updated around a report.
  Owner direction 10-29: "stops should be updated around earnings but not removed" → design owed (QHM-EARN-STOP).
