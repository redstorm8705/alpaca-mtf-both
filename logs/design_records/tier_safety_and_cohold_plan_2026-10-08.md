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

## Build order
1 tier safety; 2 close timing; 3 tier names (display, then internal IN->SW migration); 4 5-min ownership refresh +
polling inventory; 5 0-1-share ETF routing for Track A and Swing; 6 bot-watched stops on shared stocks; 7 promotion;
8 hourly balance + watch-day reporting.
