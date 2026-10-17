# Alerts that stated something false — wording fixes (2026-10-17, Claude)

**Trigger:** the owner's Slack review (2026-10-16): alerts must be intentional and in plain English. The trading-side inventory found three alerts in `alerts.py` whose text was false.

## Facts read at source
1. **`alert_stop_breach`.** Its only caller is `execution/exit_logic.py:1803`, which calls it right after "Hard stop close_position() FAILED — position may be naked". It does not pass `gtc_submitted`, and the default was `True`. So the page said "GTC stop placed — fires next RTH open." when the close had failed and no stop had been confirmed.
2. **`alert_systemic_stale_feed`.** The text said "yfinance feed degraded". The caller, `execution/entry_logic.py:1860-1879`, measures bars from Alpaca Data (T1), and its own log line says "Alpaca Data T1 feed degraded".
3. **`_JARGON` mapped "RC-4" to "Position-Mismatch check".** Every RC-4 page (`exit_logic.py:1619/2096/2189/2379`, `fill_reconciler.py:100`) is about an exit recorded without a verified fill price, not a position mismatch.

## Change (alerts.py only; no caller changed)
- **`alert_stop_breach`.**
  - The default is now `gtc_submitted=False`.
  - The title reads "STOP HIT, CLOSE FAILED".
  - The body reads "SYM (long) traded at $X, past its stop $Y. The close order failed, so SYM may still be open with no protection. Check SYM in the Alpaca app now and close it or place a stop by hand."
  - The `True` branch is kept for a future caller that does place a GTC.
- **`alert_systemic_stale_feed`.**
  - The title reads "MARKET DATA RUNNING LATE".
  - The body reads "The price bars the bot trades on are typically N minutes old, for K scans in a row. Any stock whose own bars are past the stale limit is skipped; the rest still trade. Check the Alpaca data status page."
  - The page no longer repeats the caller's fixed 20-minute number. That limit lives in entry_logic and is not changed here.
- **`_JARGON` "RC-4"** now maps to "Fill-price check".

## Risk path
Size, frequency and concurrency are all zero. These are text changes only; the paging cadence and priorities are unchanged.

## Not changed
The 20-minute stale-bar limit in entry_logic is a static number. It belongs to a separate, dynamic-threshold change, not this text fix.

## Review
Cold second-agent + adversarial: round 1 PASS (one wording nit: the stale-feed page implied it blocked entries; fixed), round 2 PASS on the exact bytes.
