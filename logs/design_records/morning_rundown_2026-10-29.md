# Morning rundown (daily Slack S/R + 0DTE call/put ideas) and weekend GEX report move — MORNING-1029 (signed: Claude, 2026-10-29) — DESIGN, not approved for build

Approval-ID: MORNING-1029
Covers: scripts/morning_rundown.py (new), scripts/gex_weekend_report.py (Monday wording + spot source), alerts.py (usage only: send_slack_blocks / send_slack; no change), tests/test_morning_rundown.py (new), server-ops: OCI crontab (move weekend report Sat 09:00 ET -> Mon 09:00 ET; add morning rundown lines)

Owner direction 2026-10-29 (verbatim): "we need to move the weekend gex levels report to Monday am at 6am pst. I also want a
morning run down sent to slack (passing readable and actionable UX) that gives me support and resistance levels for the day.
This needs to be sent everyday. It also needs to provide which 0dte strike price I should be looking to add if certain levels
break above or below. I need to know which spy or qqq strike I'm eyeing for the day. The cost per contract cannot exceed
$1.25. This means the levels needs to be defined such that high iv, low risk, high reward strikes are being identified. All
recommendations can't ever exceed the contract cap price and must always provide a call idea and a put idea. I'll be adding
more to the am report, but let's start there."

This is a report for the owner's manual decisions. It places no orders, imports nothing from `execution/`, and writes no
bot state. The bot's own trading is unchanged.

## 1. What data exists at 06:00 PT vs 06:35 PT (measured 2026-10-29 22:18 PT, read-only, Alpaca T1)

Probe scripts: scratchpad `morning/probe.py`, `morning/pre.py`, `morning/pre2.py`, `morning/select_demo.py`.

| Data | 06:00 PT (09:00 ET) | 06:35 PT (09:35 ET) |
|---|---|---|
| Prior-day daily bar (H/L/C) | yes (T1 bars) | yes |
| SPY/QQQ pre-market stock bars, SIP feed | yes, from 01:00 PT (08:00Z); 10/29 SPY 318 one-minute bars, 3.96M shares by 06:40 PT. Free plan SIP is 15-min delayed (memory note, not re-checked this turn) so the 06:00 run sees up to ~05:45 PT | yes (delayed ~15 min) |
| Same, IEX feed | thin: SPY 26 bars / 92K shares 05:01-06:40 PT; QQQ 24 bars | yes, real time |
| 0DTE contracts listed | yes. 10/30 expiry already listed at 22:18 PT the night before: SPY 498, QQQ 454 contracts | yes |
| 0DTE option TRADES | **yes, pre-market.** First print 04:30 PT (11:30Z, 07:30 ET) on every session checked (SPY 10/26, 10/28, 10/29; QQQ 10/29). SPY 770C on 10/29: 70 trades 04:30-05:00 PT, 300 in the 05:00 hour | yes, full volume |
| 0DTE option 1-minute bars | yes from 04:30 PT (bars endpoint) | yes |
| Snapshot `latestQuote` | at 22:18 PT it returns the prior session's 16:14 ET quote (20:14Z) for every near-spot contract. Whether it carries LIVE pre-market quotes at 06:00 PT is **unverified**: the historical option quotes endpoint returns 404, so it cannot be checked after the fact. Build step 0: read-only probe at 06:00 PT on the next trading day | yes |
| Greeks / IV in snapshots | **no** (indicative feed; `has_greeks=False`, `has_iv=False` verified). IV and delta are computed locally by Black-Scholes, as `data/gex.py` does | no, same |
| Open interest | yes, on the contract object (prior close OI) — GEX walls/flip computable at 06:00 | yes |

[hypothesis — unverified] Whether the indicative options feed is delayed. If it is, the 06:35 prices are ~15 min old. Build step 0 records
`latestQuote.t` against the wall clock during RTH and prints the lag in the message footer.

At 06:35 PT on 10/29 (bar closes, ±2% of spot):
- SPY: 61 contracts printed, 28 at or under $1.25. Under-cap calls 770-775 (delta 0.34 to 0.08, IV 24-25%); under-cap puts 761-766
  (delta -0.10 to -0.35, IV 23-26%). Spot 767.80.
- QQQ: 57 printed, 19 under $1.25. Calls 718-723 (delta 0.26 to 0.10, IV ~40%); puts 702-707 (delta -0.09 to -0.23, IV 42-44%). Spot 712.90.
- So the cap admits roughly the 0.08-0.35 delta band on SPY and 0.10-0.26 on QQQ that day. The band moves every day with IV, which is why
  the selection below never uses a fixed OTM distance or a fixed delta.

## 2. Levels (support / resistance for the day) — all derived, none static

Per symbol (SPY, QQQ), computed at run time and merged into one sorted ladder:
1. GEX call wall (resistance), put wall (support), gamma flip (pivot) — `data/gex.py` `_fetch_contracts` / `_fetch_snapshots` /
   `_compute_gex` with this-week expiries, same read-only path the weekend report uses (never the live `gex_snapshot.json` or its guard).
2. Prior-day high, low, close (T1 daily bar).
3. Overnight / pre-market high and low (SIP one-minute bars 01:00 PT to run time).
4. Expected move: ATM 0DTE straddle mid × 1.25 (the `get_earnings_implied_move_pct` conversion: straddle ≈ 0.798·S·σ·√T). Adds spot ± EM
   and spot ± EM/2 as outer and inner bands.
5. 06:40 run only: opening VWAP and VWAP ± EM/2.

Merge rule: two levels closer than the median one-minute true range of the last 20 sessions (measured from T1 bars) become one level,
labelled with both sources ("$769.24 prior-day low + put wall"). Levels with two or more sources are marked as stronger.
The first level above spot is the CALL trigger. The next level above it is the call target. The put side mirrors this.
Invalidation is the nearest level on the other side of spot, or VWAP at 06:40, whichever is closer.

## 3. Strike selection — one call idea and one put idea, always under the cap

For each side and each symbol, over every 0DTE strike on the correct side that has a market price (trade or quote) within the last 30 minutes:
1. Price now = quote mid when the spread is within the existing `_MAX_SPREAD_RATIO` (0.25), else the last trade. No market price means
   the strike is not eligible. A model price is never shown as a market price.
2. IV from that price by bisection (gex.py `_implied_vol`), delta by Black-Scholes.
3. Estimated entry = Black-Scholes value at spot = trigger, with the same IV and time left at the median time the trigger level is
   reached (measured in the replay, section 6; until the replay exists the run uses the halfway point of the remaining session and says so).
4. **Cap rule: eligible only if BOTH the price now AND the estimated entry at the trigger are ≤ the cap.** A call becomes more
   expensive as price rises to a call trigger, so checking only the 06:00 price would break the "never exceed" rule at the moment of entry.
5. Value at target (Black-Scholes at spot = target) and value at invalidation (spot = invalidation level).
6. Reward = value at target − entry; risk = entry − value at invalidation; payoff ratio = reward / risk.
7. Probability of reaching the target after the trigger breaks. Until the replay exists, the IV-implied touch probability
   2·(1 − N(|ln(target/trigger)| / (σ√t))). After the replay, the measured rate of "price reached the next level after first closing
   beyond the trigger", per symbol, from the last 250 sessions of one-minute bars.
8. Score = probability × payoff ratio. Pick the highest score per side. The idea shows the runner-up strike.

"High IV, low risk, high reward" maps to: IV sets how reachable the target is (step 7); risk is limited to the premium and measured to the
invalidation level, not to zero; reward is valued at the next level, not at a fixed percentage.

Honest limit: a pure Black-Scholes touch probability with the same IV makes every strike roughly fair, so the edge, if any, comes from
the conditional "after a level breaks" behaviour. That is why step 7 moves to a measured rate, and why every idea is graded (section 5).

A call idea and a put idea are always produced when a chain exists, because a far-OTM strike under $1.25 always exists. When the
best one scores poorly (payoff ratio < 1 or probability under the measured base rate) it is still shown, labelled "weak", never hidden.

Worked example, 2026-10-29 at 06:35 PT (scratchpad `morning/select_out.json`; GEX walls not included because past open interest cannot be
re-fetched):
- SPY call: trigger 769.24 (prior-day low), target 770.86, invalidation 768.37 (VWAP). 770C last $1.17, est. entry $1.17, worth ~$1.56 at
  target, ~$0.77 at invalidation, payoff 0.98, touch ~0.67.
- SPY put: trigger 768.06 (overnight low), target 765.89. 767P scored highest but was $1.48 at 06:35, so the cap rule removes it;
  pick 766P ($1.14 now, est. entry ~$0.75, ~$1.17 at target, payoff 2.6).
- QQQ call 718C (trigger 714.98, $1.20 now); QQQ put 707P (trigger 710.45, est. entry $1.14, payoff 2.0, touch ~0.40).
- What happened (IEX daily bar, illustration only, not evidence): SPY high 770.76 and low 766.62, so both triggers broke and both
  targets were missed by $0.10 and $0.73. One day proves nothing; the grader in section 5 does this every day.

## 4. Slack message mock (phone first; Block Kit via `send_slack_blocks`, fallback text carries the ideas)

```
☀️ Morning Rundown · Thu Oct 29 · 06:00 PT
SPY 770.02 pre-mkt · QQQ 718.10 pre-mkt · expected move today: SPY ±$5.0 · QQQ ±$7.7

━ SPY ━  🧲 calm/mean-revert (GEX)
🔴 773.36  prior-day high
🔴 771.34  overnight high + call wall
⚖️ 769.24  prior-day low (pivot)
🟢 768.06  overnight low + put wall
🟢 765.89  lower band

📈 CALL IDEA — if SPY breaks ABOVE 769.24
   eye the 770C · ~$1.17 (cap $1.25)  · target 770.86 → ~$1.56
   out if back below 768.37 → ~$0.77 · payoff 1.0 : 1 · reach ~67%
📉 PUT IDEA — if SPY breaks BELOW 768.06
   eye the 766P · ~$0.75 (cap $1.25)  · target 765.89 → ~$1.17
   out if back above 768.37 → ~$0.60 · payoff 2.6 : 1 · reach ~58%
   runner-up: 765P ~$0.50

━ QQQ ━  (same layout)

⭐ Best of both: CALL = SPY 770C · PUT = QQQ 707P
🚫 Don't: pay over $1.25 · chase after the target prints · hold after the out level · buy before the trigger breaks
_Prices: last 0DTE print/quote, ages shown. Pre-market option prices are thin; 06:40 update re-prices after the open._
```

06:40 PT update (short): "Ideas confirmed" or the changed strike/price per side, plus VWAP. If nothing changed, one line.

Failure text (never a made-up idea):
- No 0DTE price for a symbol: "SPY — no 0DTE prices yet; ideas at 06:40." Levels still sent.
- GEX unavailable: the wall rows show "GEX n/a" and the other levels are still sent.
- Market closed (Alpaca calendar): no rundown is sent.
- Build failure: a one-line Slack warning, heartbeat `ok=false`.

## 5. Logging and the outcome grader

- `logs/morning_rundown_YYYY-MM-DD.md` (atomic tmp→replace) — archive of the message.
- `logs/morning_rundown_recs.jsonl` (append) — one row per side per symbol per send: every level and its source, every candidate strike
  with price, price age, IV, delta, estimated entry/target/invalidation values, probability, score, the pick, and the reason each
  rejected strike was rejected (over cap now / over cap at trigger / no price / wide spread). Timestamps in PT.
- `logs/morning_rundown_heartbeat.json` — last run, ok flag, Slack and file status.
- Grader (same script, `--grade`, 13:20 PT after the 0DTE close): from T1 one-minute stock and option bars, for each idea — did the
  trigger break, when, did the target print, the option's price at trigger, its maximum afterward, its price at the out level. Appends
  graded rows to the same JSONL. This is the independent evidence (Q44) and feeds the measured probability in step 7.

## 6. Replay before build (Rule C)

Alpaca option one-minute bars exist from 2024-02. Replay every trading day 2024-02-01 → 2026-06-30 (discovery) and 2026-07-01 → 2026-10-29
(holdout, looked at once): build the 06:40 levels and ideas from data available at 06:40, then grade them from later bars. Report: trigger
hit rate, target-after-trigger rate, average option return at target / out / close, by symbol, side, VIX band and GEX regime. Costs: entry at
the bar close plus half the measured spread. Expected effect: none on the bot (report only). What would prove the levels wrong: a
target-after-trigger rate no better than the IV touch probability on the holdout.

## 7. Weekend GEX report → Monday 06:00 PT

- Cron (OCI, server-ops): `0 13,14 * * 6 ... cron_tz_wrapper.py 09:00 ... gex_weekend_report.py` becomes the same line with day `1`.
  09:00 ET = 06:00 PT all year (both zones change on the same dates), and the wrapper already picks the right UTC hour.
- Code: on Monday at 06:00 PT `bars/latest` (IEX) returns a pre-market bar, not Friday's close (IEX pre-market bars verified on 10/29).
  Change the wording "Friday close as spot" to state the spot actually used and its time. Recommended spot: Monday pre-market price
  (newer). `_expiry_range()` on Monday gives Monday → Friday, which already includes Monday's 0DTE.
- If Monday is a market holiday: run on the next trading day at 06:00 PT (calendar check) — owner question 6.
- Morning rundown crons: `0 13,14 * * 1-5 ... cron_tz_wrapper.py 09:00 ... morning_rundown.py` and `40 13,14 * * 1-5 ... 09:40 ... --update`,
  and `20 16,17 * * 1-5 ... 12:20 ... --grade`. The Monday rundown and the Monday GEX report send as two messages.

## 8. Static numbers (each flagged)

| Number | Status |
|---|---|
| $1.25 cap | owner number (question 1) |
| 1.25 straddle → 1-sigma factor | math constant (√(2π)/2), existing gex.py convention |
| r = 0.045, q = 0.012 | existing gex.py constants, grandfathered; replace with the T-bill rate later |
| 0.25 spread ratio | existing `_MAX_SPREAD_RATIO`, reused |
| 30-min price-age limit | STATIC — derive from the median gap between prints on near-ATM 0DTE strikes (replay) |
| ±2% strike window in the probe | STATIC in the probe only; the build uses spot ± 2 × expected move |
| Level-merge distance | derived: median one-minute true range, 20 sessions (window length is an estimator argument) |
| Time-to-trigger | STATIC halfway point until the replay; then measured median |
| Touch probability | IV formula until the replay; then measured rate |
| "weak" label threshold (payoff < 1) | STATIC — replace with the measured break-even payoff from graded rows |
| Send times 06:00 / 06:40 / 13:20 PT | owner schedule (plumbing) |

## 9. Required questions (RQ-4)

Q1: Collects a daily, graded record of level-break behaviour on SPY/QQQ 0DTE: edge evaluation for the 0DTE program; no capital used by the bot.
Q2: Bot trade count unchanged (report only).
Q3: Simplest form: one script reusing gex.py internals and alerts; no new data source, no bot wiring.
Q4: options_scanner.py already makes 0DTE recs with a fixed ~0.35 delta and $75 cap (Public.com greeks); this design is dynamic and
    capped at the owner's number; the options_scanner static delta is a forward-pass target (Q6). gex_weekend_report.py reused. No open PR
    found touching these files (origin/main log checked 2026-10-29).
Q5: See section 8; every decision number is derived or flagged.
Q6: Forward pass: replace options_scanner.py's fixed 0.35 delta with this selection; feed graded level-break rates to the Day tier as a
    confirmation input (separate diff, own gate).
Q7: Interconnect later: GEX walls ↔ triggers (done here), levels ↔ Day tier entries, graded rates ↔ options_scanner. Report-only for now:
    each later connection needs its own fallback.
Q8: Data availability claims in section 1 are measured this session; the live pre-market quote and the feed delay are labelled hypotheses.
Q9: Section 6.
Q10: No.
Q11: No size, frequency or exposure change for the bot.
Q12: Safety envelope untouched; no broker calls.
Q13: Section 4 failure text; per-symbol isolation; never a fake idea.
Q14: Not a shadow: the report is the product and is acted on by the owner; every idea is graded.
Q15: Section 5 JSONL carries every candidate and rejection reason.
Q16: Heartbeat + `morning_rundown: Slack blocks sent` log line; first-session audit via the grader rows.
Q17: Step 0 read-only probe at 06:00 and 06:40 PT on OCI before the cron is armed.
Q18: N/A.
Q19: No bot entry/exit/sizing change; owner approves the report design and the cap reading.
Q20: Review prompts neutral; claims limited to measured data.
Q21: No broker calls; tests mock all HTTP.
Q22: Not a fail-closed control.
Q23: A full dry run (build message, no send) on OCI before arming.
Q24: "Options trade pre-market" checked against trades data this turn (4 sessions).
Q25: IV/delta numbers measured from 10/29 bars.
Q26: T1 Alpaca only (stock bars SIP/IEX, option contracts, snapshots, bars, calendar). No yfinance, no FMP.
Q27: logs/ only; md archive atomic; JSONL append.
Q28: Separate cron process at 06:00/06:40/13:20 PT; never on the run_cycle thread.
Q29: Owner request; one session of work; precondition for evaluating the 0DTE program.
Q30: Missing in standard practice: the price age and spread on every idea (included), and an outcome grader (included).
Q31: Replay discovery/holdout split in section 6; candidates counted.
Q32: Replay uses only bars closed before 06:40; contracts listed that day (no survivorship).
Q33: Half the spread is charged on entry and exit in the replay; pre-market spreads unknown until step 0.
Q34: Ideas are directional on the index itself; no claim of alpha over the index move.
Q35: Replay is split by VIX band and GEX regime.
Q36: SPY/QQQ 0DTE near-spot strikes trade thousands of contracts daily; one contract is far inside capacity.
Q37: ~250 graded ideas per side per year; the replay gives ~650 days.
Q38: Measured by the grader's rolling target-after-trigger rate versus the IV touch probability.
Q39: N/A (no bot sizing).
Q40: Outcome check: grader rows exist for every sent idea each day and a weekly hit-rate summary; a missing graded row is an alert.
Q41: An expert desk would show the price age and spread, mark the cap at entry not at publication, and grade every call.
Q42: Raw one-minute option and stock bars, contract open interest.
Q43: Level-break trades priced off a vol surface with defined risk to an invalidation level; this design follows that.
Q44: The grader, using bars after the send that the selection never saw.

## 10. Owner questions (required before build)

1. "$1.25 per contract" — recommended reading: $1.25 per share of option premium, i.e. $125 per contract (how prices are quoted). The
   other reading ($1.25 total) means $0.01 options only, which are lottery tickets with no usable reward/risk.
2. The cap holds at the price you would pay when the level breaks, not only at 06:00 — recommended yes (section 3 step 4).
3. Two sends (06:00 levels + provisional ideas, 06:40 re-priced ideas) or one at 06:35? Recommended two: options do trade from
   04:30 PT, but pre-market prices are thin and the open resets IV.
4. One best call and one best put across SPY and QQQ, with both symbols' levels shown — or a call and put for each? Recommended:
   both symbols, plus a "best of both" line.
5. "High IV": recommended meaning is "the expected move is large enough to reach the target", not "prefer expensive options".
6. Monday holiday: send the weekly GEX report on the next trading day at 06:00 PT? Recommended yes. Stop the Saturday send (move,
   not copy)? Recommended yes.
