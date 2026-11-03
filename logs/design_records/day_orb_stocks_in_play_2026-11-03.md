# DAY-ORB-1103 — Day tier: stocks-in-play opening-range breakout (design draft, signed: Claude)

Approval-ID: DAY-ORB-1103. Status: draft; board + GAI + reviewer #2 design pass not yet run; owner approval required
(new Day-tier entry logic). Evidence: `logs/research/day_pillars_r8_r10_2026-11-02.md` (Round 11 and the 11-03
checks).

## Rule (as tested)
- Candidates known at 09:30 ET from daily bars: prior close > $5, prior 20-day average dollar volume > $20M, ranked
  by |open gap| / 14-day ATR%; top 40.
- Spread filter, known before the open: SIP quoted spread at 15:50 ET on the prior session <= 25 bps (chosen on
  2016-22 only). Real-time SIP is not on this plan; IEX spreads do not reproduce the filter.
- At 09:35 rank the remaining candidates by opening-bar relative volume (09:30 bar volume / (20-day average shares /
  78)); keep the top 5.
- Entry: buy-stop above / sell-stop below the 09:30 bar, first touch by 11:30 ET. Order type: stop-limit at the level
  held 30 s (best on discovery; test fill rate 94%). Shorts only where Alpaca marks the stock easy-to-borrow at order
  time.
- Stop: the other side of the 09:30 bar. Target: 2x the risk. Otherwise flat at 15:55 by the Day tier's existing EOD
  exit.

## Evidence summary (test 2022-07..2026-10)
- Exact trigger-time SIP filter: 2,302 trades, +28.9 bps net, book t 3.45, positive every year.
- Live-usable prior-day proxy (900-trade sample): +25.4 bps net (811 trades, t 1.59) vs +13.6 unfiltered.
- Test-year looks for this family: about 14. Confirmation owed: its first live sessions (data never consulted).

## Open design questions for the board pass
1. Sizing: Day tier's existing earned-sizing / Kelly path vs a fixed fraction until live fills exist (no exploration
   sizing per owner rule — size from the replay).
2. Integration: new trigger module beside `strategy/day_tier_entry_trigger.py`, run by `run_day_tier.py`; the
   candidate list built pre-open by a small cron from T1 daily bars + delayed SIP quotes.
3. Both-side stop orders on one symbol (Alpaca wash-trade rule): place only the side the 09:30 bar points to, or
   place the second only after the first is cancelled.
4. Interaction with existing Day tracks (A/B) — shared capital, gross cap, one direction per underlying.

## Rule B/E routing
New entries (frequency axis) → risk-path: full board + masked-loss seat + GAI + reviewer #2 on design and diff.
