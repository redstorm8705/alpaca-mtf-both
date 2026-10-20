# GEX spot guard: stop going blind on wide-quote stocks — 2026-10-19 (Claude)

Approval-ID: GEX-SPOT-1019
Covers: data/gex.py, tests/test_gex_spot_guard.py

## Why (post-live audit of 2026-10-19, anomaly rule)
There was one Day-tier trade today. Most setups stood down on the options-market (GEX) reading, and SNDK was
blind for the whole session.

What happened (logs/day_tier_runner_cron.log, logs/live_writer.log, logs/gex_snapshot.json):
- SNDK trades near $1,385. Its free IEX quote had a $29 spread (2.09% of mid).
- The spot guard in data/gex.py (`_spot_is_suspect`) treats any quote wider than GEX_SPOT_SPREAD_SANITY_PCT (2%)
  as broken.
- As a result, SNDK's GEX label stayed frozen at 07:47 AM PT and aged into STALE. The Day tier stood down on SNDK
  141 times between 14:00 and 19:59 UTC.

This repeats on recent trading days. Rejected readings, per day and symbol (live_writer.log):

| Date | Rejections |
|---|---|
| 10/08 | SNDK 15, MU 4 |
| 10/09 | SNDK 17, EWY 5 |
| 10/12 | EWY 9, MU 3 |
| 10/13 | EWY 7, SNDK 3 |
| 10/14 | MU 4, SNDK 3 |
| 10/15 | SNDK 5, EWY 4, TSLA 3 |
| 10/16 | SNDK 7, META 5, EWY 5, TSLA 5 |
| 10/19 | SNDK 23, TSLA 4 |

The guard exists to stop a bad last-trade price from flipping the label (board 2026-07-26). A wide IEX quote is not
evidence of a bad trade price. On this data plan, IEX is a thin slice of the market, and high-priced or thin names
routinely quote wide there.

## The change (revised after review: board Harris + Taleb, Gro, GAI — 2026-10-19)
When the IEX quote is too wide to validate the last trade, validate the trade against the recent tape instead of
rejecting it outright.

1. **Time-ordered reference.** Use the most recent completed real-time 5-minute IEX bar that ended at or before the
   trade's own timestamp. The bars come from `fetch_bars_ref`, which already guards freshness. Because the bar ends
   before the trade, the trade can never sit inside its own reference (fixes the circularity the board found).
2. **Expected-move band.** Accept the trade if it lies within [bar low − ATR, bar high + ATR]. ATR is the median true
   range of that session's completed bars: measured, not fixed.
3. **Trade age.** Reject a trade older than the existing bar-freshness limit (`_MAX_LAST_BAR_AGE`).
4. **Regular-way prints only.** Reject a trade whose condition codes say it would not set a bar's high or low:
   extended hours, out of sequence, or derivatively priced.
5. **Fail-safe.** No bar, a stale bar, no ATR, a stale or off-condition trade, or a trade outside the band keeps the
   spot suspect, exactly as today: the previous label is carried forward, then STALE / UNKNOWN.
6. **Unchanged when the quote is usable.** With a spread within 2%, the existing divergence band still applies.
7. **Logged and tagged.** Every guard decision logs the trade price, time and conditions, the bar start and end, the
   high, low and ATR, the quote spread, and the outcome with its reason (change=gex-spot-1019). A label from a
   bar-validated spot carries `spot_validated_by: "bar_atr"` (and the guard's diagnostics as `spot_guard`) in the GEX
   snapshot, and `get_gex_regime` returns it. Trades opened on such labels can be separated in fills and P&L by
   joining symbol and time to that snapshot and log line. Copying the tag into the Day-tier decision record itself
   (`strategy/day_tier_gex_action.py`) is a follow-up outside this change's approved files.

Honest description: the reference is a different aggregate of the same IEX tape, not a separate venue. That is
enough to catch a lone bad print. It does not prove the IEX tape matches the whole market.

Measured on SNDK 10/19 (board seat, proxy = the next bar's close), the share of readings that would stay suspect:

| Reference | Stays suspect |
|---|---|
| Latest completed 5-minute bar | 68% |
| 15-minute window | 37% |
| Bar ended before the trade, widened by one ATR | 3% |

Replay on the real logged trades (2026-10-19, read-only on the server; /tmp/gexspot_replay2.py). It covers all 143
quote_unusable rejections from 10/08 to 10/19 (SNDK, EWY, MU, TSLA, META, MSFT, AMZN). Only regular-way IEX trades and
bars ending before each trade are used, with the session ATR:
- 129 accepted. All 129 lie within the range traded in the following 15 minutes ± ATR; none was off the later tape.
- 13 stay suspect for data reasons (no regular-way trade, or fewer than two completed bars, mostly at the open).
- 1 rejected as outside the band (EWY 10/16 17:42 UTC).

The condition-code list used in the replay is [hypothesis — unverified]. The build checks it against Alpaca's
documented trade conditions before use.

Round 2 (Gro and GAI on the corrected design; both returned APPROVE-WITH-CHANGES). Adopted as build requirements:
- The condition-code filter is an explicit set built from Alpaca's documented trade conditions, not the replay's guess.
- Bars are confirmed completed (`fetch_bars_ref` returns completed bars only).
- An ATR of zero leaves the band equal to the bar's own range, which can only reject more (fail-safe; no division).

Rejected in round 2, with reasons:
- Gro's bar-volume minimum, its symbol-adaptive age limits and its fixed thresholds: new static numbers.
- Gro's NBBO cross-venue check: the data plan's real-time feed is IEX only.
- Gro's cited "risk team Docker replay" and "QuantCo" result: no source given, so not relied on.

Rejected review items, with reasons:
- A 48-hour shadow canary (Gro): the no-shadow rule; this ships live after the replay and the gate.
- New fixed thresholds (Gro C3, C4 and C9; GAI's fixed epsilon): the no-static rule. ATR is measured instead.
- A per-symbol exposure cap (Gro C6): out of scope. Sizing is unchanged, and the Kelly GEX multiplier reads SPY only
  (kelly.py:368).
- An outside "peer firm" benchmark (Gro): no source given.

## Expected effect
SNDK, EWY, MU, TSLA and META stop going stale on wide-quote days, so the Day tier evaluates them instead of standing
down on missing data.

This is not a looser gate:
- every other GEX gate still applies: strike quality, admission per expiry, STALE ageing, the label thresholds;
- a trade outside the recent traded range is still rejected.

## Required questions
Q1: Data collection improves: the Day tier stops standing down on stocks whose GEX reading went stale only because
the free quote was wide (SNDK alone stood down 141 times today).
Q2: Day-tier trade count can go up, since symbols blocked by stale data become evaluable; nothing becomes fewer.
Q3: It should exist; the simpler option (raise the 2% spread number) would be a static guess, while a second
independent price reference keeps the bad-tick protection.
Q4: Builds on the 2026-07-26 spot-consistency guard and GEX-1017 (#693); no other open work changes the guard.
Q5: No new decision number: the band is the last completed 5-minute bar's range widened by that session's median
true range, the trade-age limit reuses the trigger's existing bar-freshness limit, and "at least 2 bars" is a
data-sufficiency check.
Q6: Static assumption exposed: "a wide quote means a bad price". Forward: measure each symbol's typical IEX spread
and use the full SIP quote if the data plan is upgraded.
Q7: Feeds the GEX label, which feeds Day-tier act/stand-down, the kelly SPY multiplier and the walls; each keeps its
STALE/UNKNOWN fail-safe.
Q8: Counts are measured from live_writer.log and day_tier_runner_cron.log on the server this session.
Q9: Replayed on all 143 rejections 10/08-10/19: 129 accepted, all consistent with the next 15 minutes' tape; wrong if live accepted spots fall outside the later traded range or bar-validated trades show worse fills.
Q10: Not an edge decision; it restores data for an existing gate.
Q11: Frequency can rise (more Day evaluations), so it is routed as risk-path: board plus the masked-loss seat.
Q12: Safety envelope untouched: stops, the 7% kill, never-mask-a-loss and paper=True are unchanged.
Q13: Bars unreadable or empty -> stays suspect, exactly as today (fail-safe).
Q14: Not a shadow: accepted spots compute live labels.
Q15: Each acceptance logs the trade, the bar range and the reason (change=gex-spot-1019).
Q16: Proof: live_writer.log lines "change=gex-spot-1019 accepted" for SNDK/EWY on the first session.
Q17: Read-only run on the server against today's SNDK bars before ship.
Q18: The all-day SNDK stand-down was investigated, not explained away as "by design".
Q19: It changes when the Day tier can enter (through the GEX gate); the owner approves this design.
Q20: Review prompts carry facts only.
Q21: Tests mock every HTTP read; nothing reaches the broker.
Q22: The new acceptance path runs read-only on the server on today's data before ship.
Q23: Tests cover the guard function on every branch (tests/test_gex_spot_guard.py); there is no automated test of the
refresh_gex wiring itself. That wiring is proven by the first live session's change=gex-spot-1019 lines and the
post-live replay.
Q24: The design makes no claim that a symbol or data feed is unavailable beyond the observed IEX spreads in the logs.
Q25: The spread figures are measured from today's logged quote (SNDK mid 1385.5, spread 29.0), not quoted from memory.
Q26: T1 Alpaca data: latest trade, latest quote, 5-minute IEX bars; any failure keeps the reading suspect.
Q27: Writes nothing new beyond the existing atomic gex_snapshot.json and a log line.
Q28: data.gex.refresh_gex on the live_data_writer thread, not the trading thread.
Q29: High: it restores trading on the stocks the Day tier stands down on most often.
Q30: Blind spot: data-quality gates were validated on liquid names only; thin and high-priced names fail them by
default.
Q31: Not a fitted model; deterministic check of a trade against a traded range.
Q32: The bar range uses completed bars before the refresh time only (no look-ahead).
Q33: No trading cost change; one extra bar read per wide-quote symbol per refresh.
Q34: Adds no exposure of its own.
Q35: High-volatility days widen the traded range, which accepts more trades as valid; a bad tick outside the range
is still rejected.
Q36: Works at $25K and for thin names; that is its purpose.
Q37: Not a statistical claim.
Q38: Nothing decays; the post-live check counts accepted vs rejected per symbol each week.
Q39: Sizing unchanged; the kelly SPY multiplier only fires on SPY, whose quote is rarely wide.
Q40: Outcome: no Day-tier symbol goes STALE in RTH only because its quote was wide; check: weekly count of
quote_unusable rejections that the bar range would have accepted.
Q41: An expert would also take the full SIP quote when available.
Q42: Rawest data: trades, quotes and 5-minute IEX bars from Alpaca.
Q43: Professional method: validate a price against more than one independent reference.
Q44: Independent evidence: the replay of 10/08-10/19 rejections against their bars.
