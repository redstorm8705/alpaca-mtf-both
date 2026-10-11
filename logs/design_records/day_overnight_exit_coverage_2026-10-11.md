# Day lots never held over — after-hours exit coverage (design, 2026-10-11, Claude)

**CEO (2026-10-11):** "If the end of day flatten is unsuccessful, the shares can still be sold after hours using a
limit order ... there should never be accidental day tier shares held over."

## Today (verified at source)
- `execution/day_trade_manager.py` `after_hours_exit()` (CEO 2026-10-07): a Day lot still open after 4:00 PM gets an
  extended-hours GTC LIMIT at the live bid (sell) / ask (buy), from `_ah_touch_price` (IEX quote; else the latest trade
  -/+ `DAYTRADE_AH_FALLBACK_PCT` 1%).
  - The runner re-prices each tick when the touch moves away, or after 2 unfilled ticks, stepping 0.5% further through
    the touch per unfilled re-price (cap 2%).
  - It refuses to trade another tier's shares, waits for the Day stop's confirmed cancel, and books partial fills.
- **Runner schedule:** OCI cron `*/2 13-23 * * 1-5` UTC = 9:00 AM–7:58 PM EDT (8:00 AM–6:58 PM EST). The exit is
  therefore worked from 4:00 PM until about 8 PM EDT (only to about 7 PM in winter, EST). Nothing works it overnight,
  or in pre-market before 9:00 AM ET.
- **Alpaca 24/5** (docs us/245-trading-for-trading-api, read 2026-10-11): an overnight session runs 8:00 PM–4:00 AM
  ET (BOATS ATS), Sunday evening to Thursday night. Limit orders only; TIF day or gtc. Trading API accounts are enabled
  by default.
  - Free-plan overnight data: `feed=overnight` latest quotes (real-time indicative) and latest trades (15-minute
    delayed).
  - Assets carry `overnight_tradable`.
  - Overnight buying power is 2x max; DTBP does not apply.
- **No Day lot has been held over unintentionally yet.** AAPL/EWY 2026-10-07 were adopted by the Swing tier.

## Gaps
1. **Winter:** cron hour 23 UTC is 6 PM EST, so the 7–8 PM EST after-hours window is not covered at all.
2. An exit unfilled at about 8 PM is not worked overnight (8 PM–4 AM) or in pre-market (4:00–9:00 AM).
3. The step cap (2%) may never reach a fill on a thin evening book. Nothing escalates or pages before 8 PM.
4. Overnight quotes come from a different feed (`overnight`). The IEX quote the touch price reads has nothing overnight,
   so the fallback (latest trade -1%) would price off a stale after-hours print.

## Proposed design
- **D1 — schedule:** run the Day runner's closed-market exit every 2 minutes through every extended and overnight
  session (Sunday 8 PM ET to Friday 8 PM ET). Closed-market ticks do the exit work only when a Day lot is open; they
  already do nothing else that trades.
- **D2 — escalation before 8 PM ET:** from 7:30 PM ET the step rises to the cap faster (every unfilled tick), and an
  alert fires once if the lot is still open at 7:30 PM.
- **D3 — overnight pricing:** during 8 PM–4 AM ET, read the touch from `feed=overnight` (T1 Alpaca, real-time
  indicative quote). With no usable quote, use the latest trade -/+ the fallback percentage and keep stepping. Check
  `overnight_tradable` before placing; if it is false, page and wait for 4:00 AM pre-market.
- **D4 — guarantee:** if a lot is still open at 9:25 AM ET it is flattened in the first regular minute through the
  normal regular-hours path. Every tier keeps counting the shares as the Day tier's until they are gone (backstop).

## Open forks for BGG
1. D1: a 24/5 cron, or a separate lighter "exit-only" job that runs only while a Day lot is open after hours?
2. D2: is a faster step plus a cap rise (e.g. to 4%) before 8 PM acceptable, or keep the 2% cap and rely on the
   overnight session?
3. D3: a new `feed` parameter on the T1 quote module (`data/alpaca_data.py`), or a dedicated overnight-quote helper?
4. Any reason NOT to work the exit overnight on a paper account (thin BOATS liquidity, wider spreads)?

## BGG consensus (2026-10-11; board Harris+Peterffy 2-0, GAI; Gro unavailable — daily cap, 2026-07-07 rule)
1. **D1:** ONE runner. Cron every 2 minutes every day; the ET clock decides in code (the UTC-hour crontab is what
   breaks in winter). On a closed-market tick, return early (heartbeat only) when the Day state has no open target,
   pending exit or pending promotion hand-off — before the heavy imports and the account read. No second job.
2. **D2:** from 7:30 PM ET, re-price on every unfilled tick, re-anchored to a fresh bid/ask, cap 4%. Page at 7:30 PM,
   when the cap is reached unfilled, and at 8 PM if still open. Dedupe pages by trade_id, not calendar day (the
   overnight session crosses midnight).
3. **D3:** an optional `feed=None` keyword on `data/alpaca_data.get_latest_quote` (no change for existing callers),
   returning the quote timestamp. Overnight: `feed=overnight`, and reject a quote older than about 5 minutes. Fall
   back to the delayed trade -/+1% (log every fallback).
4. **D4:** work the exit overnight. Before building, VERIFY whether an extended-hours GTC limit placed after 4 PM
   stays eligible in the 8 PM–4 AM session, or must be re-placed. Build the deadline action: at 9:25 AM ET confirm the
   after-hours limit cancelled, then flatten (market-on-open if the OPG cutoff verifies, else a regular-hours market
   close at 9:30). Tag overnight fills as paper-simulated in the decision log.
Not risk-path on its face (exit coverage only); the cold-2nd confirms independently.

## Build (2026-10-11, branch claude/day-overnight-exit)
- **data/alpaca_data.get_latest_quote(symbol, feed=None):** passes `feed` as a query parameter and returns the quote's
  own `ts`. Verified live on OCI 2026-10-11: `feed=overnight` returns a BOATS quote with a timestamp.
- **execution/day_trade_manager.py:**
  - `_utc_ts` (Alpaca nanosecond timestamps on py3.10), `_overnight_session`;
  - `_ah_touch_price` uses the overnight feed 8 PM-4 AM ET and rejects a quote older than 300 s (trade fallback);
  - `_ah_exit_price(cap=)`;
  - `after_hours_exit`: from 7:30 PM ET and overnight, one-tick re-price and a 4% cap; pages keyed by trade_id
    (7:30 PM, 8 PM, cap reached); overnight orders logged with `[day-overnight-exit]` (paper-simulated); at the regular
    open a lot with a pending after-hours limit is cancelled (confirmed, fills booked) then flattened at market.
- **run_day_tier.py:** `_day_tier_idle` early return on closed-market ticks (local files only; stale records ignored).
- **OCI crontab (after merge):** the Day runner line changes from `*/2 13-23 * * 1-5` to `*/2 * * * *` (the ET clock in
  code decides; idle ticks return early).
- **Review fixes (cold-2nd + adversarial, 2026-10-11):** no session Fri 8 PM-Sun 8 PM ET (a resting exit is left alone;
  no 7:30 PM escalation on weekends); the cap page is keyed by cap level and trade and pages once (the 4% cap pages even
  after the 2% one did); at the regular open an after-hours limit already terminal goes straight to the market close.
- **Known limits (paper; revisit before real money):** the overnight fallback trade is 15-minute delayed on the free
  plan and its age is not checked; `overnight_tradable` is not checked (an order Alpaca rejects overnight pages through
  the submit-failed path and the regular-open deadline still flattens); NYSE holidays and half days are not modelled.
