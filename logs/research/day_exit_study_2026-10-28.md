# Day-tier exit study — Track A + Track B (2026-10-28, Claude)

Owner request 2026-10-28 ("Start exit study"). Read-only research; no code changed.

## Track B, 6 years (2020-10-26..2026-10-26, 7,613 live-rule signals, 5-min SIP bars)
Scripts (session scratchpad): `exitgrid.py`, `exitgrid_sim.py`; output `exitgrid.csv`.
Entry = signal bar close; out by the 15:55 ET bar; stop before target inside a bar; 5 bps round trip; R = each rule's
own initial risk. 99 exit rule sets (stop: structural / 0.25 / 0.5 x daily ATR; fixed target 1-3R or none; trailing
stop after +0.5-1.5R at 0.25-1R behind; half off at 0.5-1R with breakeven stop on the rest; time exit 30/60/120 min).
Chosen on 2020-24 only; 2024-26 shown for each.

- Live rule (structural stop, 2R): -0.061R (2020-24, t -3.48) / -0.023R (2024-26, t -0.96) — matches the earlier study.
- Best by 2020-24 t: 0.5 x ATR stop, 1.5R target: +0.012R (t 1.08) / -0.008R (t -0.53).
- No rule set has t > 2 in 2020-24; 4 of 99 are positive in both halves, all under +0.006R (t < 0.5).
- Reading: the exits only reshuffle a near-zero same-day edge (before costs +0.02R / +0.06R). A wider stop removes
  most of the cost drag (back to ~0R); no exit turns it into a profit.

## Live trades, both tracks (2026-09-15..10-28, 1-minute SIP bars, real fills and real first stops)
Script: `live_exits.py`. 33 Track A and 17 Track B trades. Mean R per trade:

| exit | Track A | Track B |
|---|---|---|
| live: 2R target | -0.505 | +0.226 |
| 1R target | -0.535 | -0.092 |
| hold to close | -0.506 | +0.119 |
| half off at 0.75R, BE stop, 2R | -0.506 | +0.185 |
| trail after +0.75R, 0.5R behind | -0.530 | +0.076 |
| time exit 60 min | -0.372 | -0.079 |

- Track A loses about half its risk per trade under every exit tested: the loss comes from the entry signal, not the
  exit. (The TRACK-A-STOP-1026 wider noise stop, live since 2026-10-27, is not in these logged stops.)
- Live Track B's current 2R rule is the best of the set on 17 trades (small sample).

## What would win
- Track B: the only tested version that makes money in both periods is the 5-session hold in the SPY 200-day trend
  direction with a 1 x ATR stop (+0.24R / +0.23R; `logs/design_records/trackb_trend_swing_2026-10-28.md`, parked by
  the owner 2026-10-28). Same-day exits are exhausted (99 sets here + 144 re-pricings and 454 opening-range rules earlier).
- Track A: next test is the ENTRY — replay the GEX wall-fade signal on the stored option-wall history
  (`logs/gex_history.jsonl`, from 2026-07-20) and test which wall/conviction conditions separate its winners from losers.
