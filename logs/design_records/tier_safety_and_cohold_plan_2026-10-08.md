# Tier safety + co-hold plan — CEO decisions (2026-10-08, Claude)

## Audits (2026-10-07, two adversarial agents; key claims verified at source)
- Alpaca: several stops on one position are allowed while total open sell qty <= position qty (else 40310000).
  Wash-trade rule (docs user-protection; prod log "opposite side market/stop order exists"): a buy and a sell where
  either is market/stop is always rejected; OCO/bracket exempt. Both rejects use code 40310000.
- Our code is not tier-safe: on 40310000 the stop paths cancel EVERY tier's orders (broker.py:1001; partial closes
  only scope the day tier, broker.py:1599); several swing stops are sized to the whole netted position; swing exits
  close the whole symbol (OWNERSHIP_GUARD_ENFORCE=False, config.py:746); a wash-trade reject triggers the same
  cancel-everything recovery. Co-holding is prevented today only by entry-avoidance guards.
- Day tier flattens at 3:40 PM ET (DAYTRADE_FORCE_FLAT_MINUTES=20); swing overnight stops at 3:45 (PRECLOSE 15).
- Alpaca order rules (docs orders-at-alpaca): MOC/LOC (tif=cls) rejected after 3:50 PM ET; non-extended-hours
  orders submitted after 4:00 PM are queued for the next trading day.

## Decisions (board Harris + Taleb, Gro, GAI; CEO)
- D1 (CEO): Swing/QHM/F6 overnight stops placed AFTER the 4:00 close (their RTH stops protect until 4:00). Day tier
  exits with a 3:59 PM market order, its own stop live until then (MOC would need the stop cancelled by 3:50).
- D2 (CEO): 0-1 affordable shares -> 2x bull / inverse ETF for BOTH Day and Swing. Pending CEO confirm: no usable
  ETF + budget buys exactly 1 share -> trade the 1 share (board 4-0).
- D3: Track A follows the same 0-1-share ETF rule and 10/10 max size (3-1; ETF swap goes through the risk board).
- D4: tier-safety fixes ship BEFORE bot-watched stops / promotion (4-0); a replay must show a wash-trade reject
  leaves another tier's stop in place.
- D5 (CEO aligned): promotion of stock lots only (never 2x/inverse ETFs overnight); swing books at the take-over mark;
  day tier books its realized result at that mark; no double count (mechanical tie-out vs Alpaca fills); share
  count, cost average and order tag carried over; original basis kept on record; no stop loosening; a Slack message
  on every promotion (symbol, shares, original cost, take-over price, day-tier result, new swing stop).
- Tier names: only Day / Swing / QHM / F6 everywhere (CEO).

## Decisions round 2 (2026-10-07 evening PT; board Harris+Thorp+Taleb, Gro, GAI; CEO)
- D1 close sequence (CEO): day-tier stop stays live until 3:58 PM ET; at 3:58 cancel it and RETRY the cancel until
  confirmed (the stop stays live meanwhile — never naked without a confirmed cancel); the market exit goes out the
  moment the cancel confirms (board/Gro/GAI 4-0: no wait to 3:59); unfilled remainder retried up to 4:00. Any lot
  still open at 4:00 -> after-close stop + Slack CRITICAL + exit at next open.
- Promotion timing: decide at 3:56 on the closed 3:55 5-min bar; a promoted lot KEEPS the day-tier stop through the
  close; after 4:00 the swing GTC stop is placed first, then the day stop is removed (no unprotected gap).
- Leveraged ETF notional cap LEVERAGED_NOTIONAL_MAX_PCT 5% -> 10% of equity (CEO-approved; risk-path -> board gates
  the implementation). Plus the board's overnight-gap limit on swing 3x holds (CEO-approved): a stress index gap on one
  position (-5% index ~ -15% on a 3x ETF) costs < 2% of equity; overnight notional counted at 3x face vs the 1.75x cap.
- No 1-share leveraged ETF positions (CEO): an ETF route needs >= 2 shares, else no ETF trade. At $2,556 equity and the
  10% cap: SPXL 0 / UPRO 1 (no 3x S&P long until ~$3,110 equity); TQQQ 3, TNA 4, UDOW 4, SPXS 10, SPXU 7, SQQQ 7,
  TZA 5, SDOW 9 (Oct 7 close prices).
- ETFs come in ONLY when the stock/index is unaffordable (CEO). Shorts: 2x bear, else 1x bear (already the day-tier
  map); GOOGL/NFLX/META/AMD/COIN have no liquid bear — check for a 1x bear before building.
- Index 3x (CEO): signal on SPY/QQQ/IWM/DIA bars, execute in the 3x ETF (SPXL|UPRO / SPXS|SPXU by live liquidity,
  TQQQ/SQQQ, TNA/TZA, UDOW/SDOW) for Day and Swing. TQQQ/SQQQ retired as standalone scored tickers (CEO agreed).
  Build notes: rewrite the FORCE_FLAT > SWEEP validation (config.py:1160) for the new timing; add every 3x name to
  LEVERAGED_3X_TICKERS; LEVERAGED_MIN_HOLD_DAYS must not block a day-tier 3:59 exit or any stop exit; day-tier 3x lots
  not exempt from news-halt closes (Invariant #7 Bucket A exemption) — confirm in build.
- Rejected reviewer suggestions (masked loss): excluding overnight-gap P&L from the kill switch (GAI); counting only
  the planned risk of a gap loss toward P&L (Gro).
- CEO 2026-10-07 (answered): YES to the 1-share fallback — no usable ETF and the budget buys exactly 1 share of the
  stock -> trade that 1 share (CEO wording "Yes to one share etf", recorded as the stock fallback; leveraged ETF
  positions still need >= 2 shares).
- CEO 2026-10-07: a day-tier lot still open at 4:00 PM is NOT left for the next open: an extended-hours GTC LIMIT at
  the bid (sell) / ask (buy), re-priced until filled — all non-promoted day-tier trades closed out by end of day.
  (Alpaca docs: extended-hours orders must be limit, TIF day or gtc; non-extended orders after 4:00 queue to the
  next session.) Code fact: the swing tier already places its overnight GTC stops after the close (4:05 AH block);
  its 3:45 sweep places DAY stops only, on swing-tracker trades only.

## Build order
1 tier safety; 2 close timing; 3 tier names (display, then internal IN->SW migration); 4 5-min ownership refresh +
polling inventory; 5 0-1-share ETF routing for Track A and Swing; 6 bot-watched stops on shared stocks; 7 promotion;
8 hourly balance + watch-day reporting.

## Stage 1 shipped scope + stage-2 carry-overs (2026-10-07 PT, Claude)
- Shipped: tier-scoped cancels in the GTC/DAY stop recoveries and partial close (every tier); wash-trade rejects
  cancel nothing and page; wash-trade no longer cached as a short block; free-share stop when another tier's STOP
  orders cover the held shares, else page. Out of scope: close_position / close_all blanket cancel (Invariant #7).
- Stage 2: (a) a free-share stop + later full-qty resubmit can churn (own-cancel -> futile 63s poll) — check own+foreign
  cover before cancelling [hypothesis — unverified]; (b) a free-share stop relies on another tier's stop that can be
  replaced/cancelled — the tier tracker records a full-qty stop id; (c) swing and swing_breakout share the IN- tag;
  (d) day-tier OCO child legs are untagged (own legs read as foreign); (e) dynamic cover = position qty minus all live
  unfilled reducing-stop qty instead of parsing the error text.

## Item 2 (close timing) shipped scope + carry-overs (2026-10-07 PT, Claude)
- Shipped: entries stop 3:40; stops live to 3:58; confirmed cancel -> market exit (3 tries, none < 2 s before the
  close); any lot left open -> extended-hours GTC limit at bid/ask, repriced (touch moved / 2 unfilled ticks, 0.5% per
  step, cap 2%), managed next morning if still open; split fills booked at their own price. Board Harris+Taleb, 7
  cold-2nd rounds, GAI preship (Gro daily token cap hit -> waived per the 2026-07-07 rule), adversarial PASS.
- Deploy: OCI cron for run_day_tier.py widened */2 13-21 -> 13-23 UTC (repricing to 7:58 PM EDT / 6:58 PM EST).
- Unverified: whether an extended-hours GTC limit can fill in Alpaca's overnight session (8 PM-4 AM).
- Carry-overs: adaptive reprice step from live spread + after-hours volatility, log slippage vs touch (adversarial);
  stale comment strategy/day_tier_sizing.py:146 ("buying power returned before the close"); _ah_account docstring
  wording; unknown clock on a market holiday inside host hours still flattens at market (queued to the open).
- Follow-up: after_hours_exit has no foreign-stop/transfer check (a lot adopted by another tier after the close
  could get an exit order); #513 prevents same-day adoption, so low likelihood — add the check in the next pass.
