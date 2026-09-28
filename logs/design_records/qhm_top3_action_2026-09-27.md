# QHM weekly report -> top-3 queued buys at support (design record, 2026-09-27)

Builds on logs/design_records/qhm_research_execution_pipeline_2026-08-23.md (fully automatic, routes
through the existing safety envelope, fail-closed to NO ORDER, risk-path).

## Rafael decision (2026-09-27, APPROVED)
- The weekly report ends in action: the TOP 3 ideas only (not a running list), chosen after checking
  portfolio balance, capital already allocated, share affordability, price and thesis.
- Queued buy orders at support levels (resistance for shorts) "unless the BGGN has a better way".
- New buys get their own budget so LLY's grandfathered size no longer blocks them; the 20% per-stock
  limit still applies; LLY is never auto-trimmed.
- GEV: Rafael sells it himself (Claude cannot place trades).
- Report: plain-English takeaways (business, valuation with class-A P/E vs peers, 1-2 sentence news).

## BGGN consensus (2026-09-27)
- Board selection seat (Thorp/Dalio/Asness/Taleb) 4/4; execution seat (Harris/Brandt/Beck/Peterffy):
  reuse the existing QHM pipeline (thesis config -> add_candidate -> tranches); no new order path.
- Ranking (selection seat): 0.20 fundamentals + 0.15 valuation vs peers + 0.15 dip/catalyst
  + 0.20 diversification + 0.30 affordability. This week: UBER, SOFI, NFLX (matches Rafael).
- Grandfathered notional must not count against new-buy headroom (all seats; Gro; GAI).
- Risk-path: YES for the budget/cap change (Rule E: raises size/concurrency vs today's zero headroom).
  Gro and GAI called it not risk-path; overruled by the Rule E definition (reviewer applies the
  definition, default-to-risk-path).
- Order type: DAY limits (not GTC); drop a pick whose earnings pass before it fills.
- Presentation: deterministic templates + a validator that rejects invented numbers.

## Verified facts that shape the build
1. Alpaca rejects a new limit BUY whenever a sell STOP rests on the same symbol ("stop sell | limit
   buy | always rejected", docs.alpaca.markets/us/docs/user-protection, fetched 2026-09-27). So chunks
   2 and 3 cannot be resting buy limits once chunk 1 has its protective stop.
2. The existing ladder never reaches chunk 2: _check_fill_and_advance -> _compute_and_submit_stop sets
   state ACTIVE after chunk 1 (quarterly_hold_manager.py ~L3022), and maybe_enter_positions only
   handles PENDING_ENTRY / AWAITING_FILL (~L840). This matches the 2026-08-23 root-cause flag
   ("NVDA at 1 share vs 20% target is likely the entry ladder under-filling").
3. Current sizing gives new names ~0: available_equity = equity - other QHM notional
   ($2,541 - $3,325 -> 0) and the aggregate cap room is negative.

## Build (risk-path; full gate on each diff)
- Report script (qhm_thesis.py): deterministic top-3 scorer over the shortlist; excludes current
  holds, names whose earnings are within the pre-earnings window, and names where one chunk
  (budget/3) buys < 1 share. Writes data/state/qhm_new_picks.json (atomic): week, symbol, budget,
  earnings date. The report shows the top 3, share counts and the support levels.
- Bot (quarterly_hold_manager.py):
  - ingest the picks file each RTH cycle (idempotent per week+symbol; skip tracked symbols);
    add to thesis config + add_candidate with budget_usd and ladder=True.
  - chunk 1: DAY limit at the nearest support BELOW the live price (20-day MA, 50-day MA, 10-day low,
    20-day low); re-placed each day until filled; the Day-1 gate still applies.
  - chunks 2-3 (days 3 and 5, ACTIVE with a stop): when the live price trades at/below that chunk's
    support level, buy via the existing stop-safe add sequence (cancel stop -> marketable limit ->
    poll -> re-place the stop for the full held qty; never naked). Day-3 reconfirm still applies.
  - new-money cap: 40% of equity across NON-grandfathered QHM holds + the 20% per-name cap; RegT
    buying-power check; fail closed on any unreadable input.
  - expiry: a pick with no fill whose earnings date passes is dropped (CLOSED, never bought).
- Tidy-first: extract the dip-add stop-safe add block into a helper as its own behavior-preserving
  diff before the feature diff.

## Rule B/C/E routing
Size/frequency/concurrency delta: NON-ZERO (new QHM buys where today there are none) -> risk-path ->
cold board + masked-loss seat + Gro + GAI on the diff, Rafael approval given 2026-09-27 for this scope.
Expected effect: ~3 new QHM positions per week at ~10% of equity each, bounded by 40% new-money
aggregate. Reversal criterion: new-pick fills show negative average return after 8 weeks, or any
naked-position incident in the stop-safe add path.
