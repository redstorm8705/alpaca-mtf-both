# Day-tier higher-timeframe study — 2026-10-29 (signed: Claude)

Owner request: "maybe it's higher timeframes within the trading session? 30m, 45m, 1H, 2h". Read-only.

## Method
Same 6-year lab as the Track B and Track A studies (`lab.py`): SIP 5-min RTH bars for the 18 Day-tier names + SPY,
2020-10-26..2026-10-26. Discovery before 2024-10-26, test after. 5-min bars resampled to 30 / 45 / 60 / 120-minute
bars from 09:30. A signal uses only a COMPLETED higher-timeframe bar; entry at the next 5-min open; stop checked before
target inside a bar; flat at 15:55; 5 bps cost; first signal per name-day-family-direction. Script `htf.py` (scratchpad).
Families, long and short:
- **brk**: the bar closes beyond every earlier bar's high/low that session → continuation.
- **orb**: first bar = opening range; a later close beyond it (within the first 2 or 4 bars) → continuation.
- **trend**: close above/below VWAP with EMA3 over/under EMA8 on bar closes, two bars in a row → continuation.
- **fade**: close ≥ 1.0 or 1.5 × 14-day ATR from VWAP → fade toward VWAP.
Exits: stop at the bar's structure, 0.5 × or 1.0 × daily ATR (never tighter than structure) × target 1R, 2R, close
(fade also: VWAP). 548 variants with ≥100 discovery trades.

## Result — no winner
- Variants positive in BOTH halves: **0 of 548**. Discovery t > 2: 0.
- Best per timeframe (picked on discovery):
  | TF | Rule | Discovery R | Test R |
  |---|---|---|---|
  | 30m | trend, 1.0 ATR stop, 1R target | −0.008 | −0.019 |
  | 45m | trend, 1.0 ATR stop, 1R target | −0.008 | −0.021 |
  | 1h | trend, 1.0 ATR stop, 2R target | −0.012 | −0.021 |
  | 2h | breakout short, hold to close | −0.011 | −0.045 |
- Before costs the best families earn +0.01 to +0.07R in discovery and about zero in test (e.g. 30m opening range
  long: +0.056R gross / +0.022R net discovery, +0.013R gross / −0.017R net test).
- Fade-to-VWAP on large stretches is rare (116 discovery trades at 30m) and loses in both halves.

## Diagnosis
Longer intraday bars do not add direction. Intraday continuation and mean-reversion on these 18 names carry about zero
edge before costs at every bar size from 5 minutes to 2 hours, matching the Track A (741 variants) and Track B (614 +
99) studies. The one measured edge on these names is multi-session (the 5-session SPY-trend hold, design reopened).
**What would have been profitable:** a setup whose post-signal drift exceeds 5 bps on a stop at least ~1 daily ATR;
none of the 548 does. Next candidates outside this family: overnight/close-to-open drift and post-earnings drift
(the option event detector from the 10-29 audit identifies those days for every name).

## Limits
18 names (survivorship); one bar alignment (09:30 anchor); test half looked at once per family after the discovery pick.
