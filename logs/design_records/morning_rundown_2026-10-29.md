# Morning rundown (daily Slack S/R + 0DTE call/put ideas) and weekly GEX report move — MORNING-1029 v7 (signed: Claude, 2026-10-30) — DESIGN, not approved for build

Approval-ID: MORNING-1029
Covers: scripts/morning_rundown.py (new), scripts/morning_watch.py (new, trigger-time watcher), scripts/gex_weekend_report.py (first-trading-day-of-week gate, Monday wording, spot source), alerts.py (usage only: send_slack_blocks / send_slack; no change), tests/test_morning_rundown.py (new), tests/test_morning_watch.py (new), tests/test_gex_weekend_report_schedule.py (new), server-ops: OCI crontab (weekly GEX report Sat -> first trading day of the week 09:00 ET; morning rundown lines; watcher line)

Owner direction 2026-10-29 (verbatim): "we need to move the weekend gex levels report to Monday am at 6am pst. I also want a
morning run down sent to slack (passing readable and actionable UX) that gives me support and resistance levels for the day.
This needs to be sent everyday. It also needs to provide which 0dte strike price I should be looking to add if certain levels
break above or below. I need to know which spy or qqq strike I'm eyeing for the day. The cost per contract cannot exceed
$1.25. This means the levels needs to be defined such that high iv, low risk, high reward strikes are being identified. All
recommendations can't ever exceed the contract cap price and must always provide a call idea and a put idea. I'll be adding
more to the am report, but let's start there."

This is a report for the owner's manual decisions. It places no orders, imports nothing from `execution/`, and writes no
bot state. The bot's own trading is unchanged.

**Scope (v2, kept in v3):** phase 1 ships levels + one CALL and one PUT section per symbol, each built from a FRESH
EXECUTABLE ASK checked against the $1.25 cap at send time, a limit price at or under $1.25, the trigger and invalidation levels,
and only facts that need no model (bid/ask, quote time and venue, intrinsic value at the target). No modeled payoff, probability
or future option price appears anywhere in phase 1. The replay and outcome grader are phase 2 (section 8), gated on data that
phase 1 starts archiving.

## 1. What data exists at 06:00 PT vs 06:40 PT (measured, read-only, Alpaca T1)

Probe scripts in scratchpad `morning/`: `probe.py`, `pre.py`, `pre2.py`, `gth.py`, `gth2.py`, `select_demo.py`.

| Data | 06:00 PT (09:00 ET) | 06:40 PT (09:40 ET) |
|---|---|---|
| Prior-day daily bar (H/L/C) | yes | yes |
| SPY/QQQ pre-market stock bars, SIP | yes from 01:00 PT; 10/29 SPY 318 one-minute bars. Free-plan SIP is 15-min delayed (memory note; step 0 records the lag). Used for LEVELS only, never for trigger state | yes, delayed |
| Same, IEX | thin: SPY 26 bars 05:01–06:40 PT | yes, real time |
| 0DTE contracts listed | yes, the night before (10/30: SPY 498, QQQ 454) | yes |
| 0DTE option trades before 09:25 ET | **yes since 2026-10-19 only.** Six near-ATM SPY 0DTE contracts: 0 pre-open trades on 10/14 and 10/16; 63 on 10/19; 127 on 10/20. 10/29: all pre-09:25 ET prints on venues C (Cboe) and A, condition `v`, first print 07:30 ET. This matches Cboe Global Trading Hours for SPY/QQQ options from 2026-10-19, 07:30–09:25 ET (notice cited by reviewer; dates confirmed by our trade data) | yes |
| Snapshot `latestQuote` | late night: the prior session's 16:14 ET quote. **Live pre-market bid/ask at 06:00 PT is unverified** (historical option quotes endpoint returns 404) | yes |
| Greeks / IV in snapshots | no (`has_greeks=False`, `has_iv=False`) | no |
| Open interest | prior-close OI on the contract object | same |

Not established, and checked in build step 0 (section 9) before the 06:00 send is armed: live bid/ask at 06:00 PT, quote
venue (Cboe-only vs consolidated), the indicative feed's delay (Alpaca documents indicative trades as delayed; the lag is measured
by comparing `latestQuote.t` to the wall clock), and whether the owner's broker routes Cboe GTH orders.

At 06:36 PT on 10/29 (bar closes, ±2% of spot — bar closes, NOT asks): SPY 28 of 61 printed strikes ≤ $1.25 (calls 770–775,
puts 761–766); QQQ 19 of 57 (calls 718–723, puts 702–707). The under-cap set moves daily, so no fixed OTM distance or delta is used.

## 2. Levels (support / resistance) and the setup on each side

### 2a. Two prices, two jobs, and the trigger history
- **Level price** builds the ladder. Sources: SIP one-minute bars, 15-min delayed on the free plan, and the prior-day bar. The SIP cutoff is
  the timestamp of the latest SIP bar the run receives; it is printed with the message.
- **Current price** decides whether a trigger is still unbroken. Source: the IEX latest trade (real time), printed with its measured age.
  The trade is current only if its age is below the time the stock typically needs to move one merge distance:
  `t_max = (merge_distance / σ_1m)² minutes`. **Definition (v5):** σ_1m is the sample standard deviation, in dollars, of one-minute
  close-to-close changes (close[t] − close[t−1], consecutive minutes only) in the same clock window over the last 20 sessions (T1 bars).
  It is not a percentage return and not a high–low range. Under a driftless random walk the typical time to move d dollars scales as
  (d / σ_1m)² minutes, which is what t_max uses; a formula built on another σ definition (per-bar range, or returns) needs a different
  constant and is not used.
- **Trigger history.** The run fetches real-time IEX one-minute bars covering every minute from the SIP cutoff to now. The history is
  complete only when every minute in that gap has an IEX bar.
  - A level counts as already broken if any SIP bar since the level time crossed it.
  - It also counts as broken if any IEX bar in the gap came within the side's venue buffer of it (call side: IEX high ≥ level − b_high;
    put side: IEX low ≤ level + b_low).
  - **Two buffers (v5; floored at 0 in v6, a negative percentile from misaligned bars is logged and treated as 0).** `b_high` is the 99th percentile of (SIP one-minute high − IEX one-minute high); `b_low` is the 99th percentile of
    (IEX one-minute low − SIP one-minute low). Both use the same clock window and the last 20 sessions, and both cover excursions printed on
    other venues but not on IEX. v4 used the high-side gap for both sides, so a downside break printed off-IEX could be missed.
  - The measurement below is of `b_high` only; `b_low` is measured in build step 0 (§9) before the crons are armed.
  - Measured 2026-10-30 over 20 sessions, 06:25–06:40 PT minutes where both feeds had a bar (scratchpad `morning/iexcov.py`): SPY p50/p95/p99 = $0.03 / $0.12 / $0.175; QQQ $0.07 / $0.285 / $0.60 (b_high).
- **IEX minute coverage** (same 20 sessions): 100% of minutes 06:30–06:40 PT for both SPY and QQQ. Pre-market 05:45–06:00 PT: median 0% (SPY)
  and 20% (QQQ). So the 06:00 send almost always has incomplete history. The after-open send runs at 06:46 PT (§5), late enough that the
  SIP cutoff (about 06:31) falls after the 06:30 open and the gap is fully regular-hours minutes.
- **No current price, or incomplete history** → the CALL/PUT sections are labelled **REFERENCE ONLY — trigger history not verified** (with
  the last IEX trade time, its age, and how many gap minutes are missing). They never say a trigger is unbroken, never say "only if …
  breaks above/below", and show **no limit price and no stop** ("Limit/Stop: N/A — stale data").
- **Too close:** a level within one merge distance of the current price is not a trigger.

### 2b. Level sources
1. GEX call wall, put wall, gamma flip: existing read-only `data/gex.py` internals (`_fetch_contracts`, `_fetch_snapshots`, `_compute_gex`),
   this week's expiries, prior-close open interest, labelled "GEX (prior-close OI)". The regime word comes from the existing gex.py
   classifier, labelled as such. GEX unavailable → "GEX n/a".
2. Prior-day high, low and close (T1 daily bar).
3. Overnight high and low (SIP one-minute bars from 01:00 PT to the level-price time).
4. Expected move (EM) = (ATM call ASK + ATM put ASK) × √(2π)/2. The factor is computed in code as `math.sqrt(2 * math.pi) / 2`
   (≈ 1.2533), never typed as 1.25, so it cannot be confused with the $1.25 cap. It is the at-the-money straddle-to-expected-move
   approximation; skew is ignored; labelled "approx.". Asks are used, not mids, so EM is never narrowed by unexecutable prices.
   Requirements: both legs have a current two-sided quote (§3 rule 1), AND the uncertainty the spread adds to EM, √(2π)/2 × (call spread +
   put spread) / 2, is no larger than the merge distance. Past that point the bands cannot be placed to the ladder's own resolution, so EM is
   **unavailable** (no fallback number). This cutoff comes from measured data (live spreads and the measured merge distance), not a fixed
   percentage. When available, spot ± EM are inserted into the ladder as ordinary levels labelled "EM high / EM low".
5. 06:46 send only: VWAP over the one-minute bars from 06:30 PT. A bar is used only when it is confirmed complete by timestamp: its start +
   60 s ≤ the response's server time. The bar still forming is never used.

Merge rule: levels closer than the merge distance merge into one, labelled with all their sources ("769.24 · prior-day low + put wall").
The merge distance is the median one-minute high–low range of the same clock window over the last 20 sessions.
Cold start: if fewer sessions come back (a data gap), the run uses those it has. With none, it uses today's own pre-market SIP bars,
labelled "merge distance: today only". With no bars at all, levels are listed unmerged and both sides are REFERENCE ONLY, because t_max
and b are undefined. No fixed default distance is used. Merged levels are
labelled "confluence".

### 2c. Setup completeness (no strike work without all three parts)
Using the sorted ladder (EM levels included when available):
- CALL trigger = the first unbroken level above the current price (§2a). Target = the next level above the trigger. Invalidation = the
  nearest level below the current price.
- The PUT side mirrors this.
- **The EM band can be the target.** It is an ordinary ladder level, so when it is the next level beyond the trigger it is the target,
  labelled "EM high/low (approx.)". When EM is unavailable and no level lies beyond the trigger, the target is missing. No band is ever
  made up.
- If the trigger, target or invalidation is missing, the side renders **NO ACTIONABLE SETUP — missing <trigger|target|invalidation>**, the
  missing parts are logged, and **strike ranking is not run**. When the current price is not current, the setup is still built and shown as
  reference only (§2a).

## 3. Strike selection: always a CALL section and a PUT section, with the cap enforced on the executable ask

Strikes are ranked only for a complete setup (§2c). Candidates are 0DTE strikes from the trigger level out to the target level, plus the
first strike beyond the target.

Rule 1: a strike is eligible only when all of these hold (otherwise the rejection reason is logged):
- the quote is two-sided: bid > 0, ask > 0, ask > bid. Zero-bid, locked and crossed markets are rejected.
- **the quote is current at send time.** The run polls the latest quotes for every candidate strike throughout a send-time polling window.
  A quote is current only if its timestamp falls inside that window. Window length is plumbing (a poll timeout).
  - Once step 0 and the archive have measured quote-update intervals per strike, a quote also qualifies if its age is at most the 95th
    percentile of observed update intervals for its moneyness bucket (an estimator over the archive).
  - A quote that is merely from today's session does not qualify. Its age and venue are printed.
  - No candidate on a side has a current quote → the side reads **NO CURRENT EXECUTABLE QUOTE**.
- **ask ≤ $1.25.** Last trade and mid are never used to decide eligibility or the price shown.

Rule 2 (pick): rank by intrinsic value at the target ÷ ask (call: max(0, target − K); put: max(0, K − target)). This is a floor, not a model:
an American-style SPY/QQQ option is worth at least its intrinsic value whenever the stock is at the target. Ties go to the strike nearer the
money. If no eligible strike has intrinsic value at the target, pick the eligible strike nearest the money and print "target does not reach
this strike".

Rule 3 (output for the pick; in v5 printed by the TRIGGERED message, §3b — the 06:46 watchlist shows only strike, ask and quote time):
- strike, bid / ask, quote time and venue;
- **limit price = the ask, never above $1.25**;
- trigger, target and invalidation;
- intrinsic value at the target;
- maximum loss = the premium (× 100 per contract);
- the suggested stop (§3a).
IV is shown for context only. It comes from the quote mid by bisection, with T = seconds to 16:00 ET on expiry day ÷ (365 × 86,400), and only
when intrinsic < mid < S (call) or intrinsic < mid < K (put). Otherwise the message shows "IV n/a" and the reason is logged. 20-session
realized vol is shown beside it.

Rule 4: if no strike is eligible, the section still appears and reads **NO ACTIONABLE CONTRACT UNDER $1.25** (asks above the cap) or **NO
CURRENT EXECUTABLE QUOTE** (no quote current at send time), with the reason. An idea is never invented.

Rule 5: a TRIGGERED message (§3b) is **ACTIONABLE** only when all of these hold at the break (the 06:46 watchlist is never ACTIONABLE, at most WATCHING):
- the trigger history is complete and the current price is current (§2a);
- the setup is complete (§2c);
- an eligible strike exists (rules 1–2);
- a broker-placeable stop exists (§3a).
Otherwise it is REFERENCE ONLY, with the failing condition named, or one of the NO … states above.

### 3a. Stops on manual report trades

The bot's stop rule (a stop on every position) covers the bot's own positions, which the bot places and protects. This report places no
orders; any trade from it is the owner's manual trade. Whether manual report trades fall under the stop requirement is owner question 3.
Either way, **an idea is labelled ACTIONABLE only when it comes with a broker-placeable stop**. There is no "no stop" path.

What Alpaca supports (Alpaca Trading API docs, "Options Trading" page, read 2026-10-30):
- option order types are `market`, `limit`, `stop` and `stop_limit`; `stop` and `stop_limit` are single-leg only;
- `time_in_force` is `day` or `gtc`;
- `extended_hours` must be false;
- no bracket/OTO order class for options is documented, so the stop is a separate sell order placed right after the buy fills;
- options orders are not accepted in extended hours, so ideas sent before 06:30 PT cannot be entered at Alpaca until the open.
The Broker API overview page lists only market and limit, and says new opening orders on contracts expiring that day stop at 3:30 pm ET.
Both differences are checked in step 0 against the paper account (read-only order validation is not possible, so the check is the owner's
first manual order).

The stop printed with every ACTIONABLE idea:
- **Option sell-stop**, `type=stop`, `time_in_force=day`, qty 1.
- Stop price = `bid − |delta| × |price_at_trigger − invalidation|`, rounded down to the tick. **v5:** the distance is an absolute value,
  so the PUT stop (invalidation above the price) is below the bid like the CALL stop; v4's signed form put the PUT stop above the bid.
  `bid`, `delta` and `price_at_trigger` are read when the trigger breaks (§3b), not at report time. It is labelled "estimate (delta)"
  because it is a linear approximation that ignores gamma and decay.
- Required: delta is defined (the IV root exists, §3 rule 3), and 0 < stop price < current bid. Otherwise the idea is **REFERENCE ONLY — no
  placeable stop**, and no limit price is shown.
- A stock-level line follows: "if SPY trades back through <invalidation>, exit". It adds information; it is not the stop.
- The maximum loss (the premium) is printed as the worst case if the stop gaps. The message never says a stop has been placed.

### 3b. Watchlist and trigger-time refresh (v5)

v4 priced the limit and the stop when the report was sent. A trigger usually breaks later, when the option's bid, ask and delta are
different, so a report-time limit or stop can be wrong at the moment it is used. v5 splits the job:

- **The 06:46 message is a WATCHLIST.** Per side it shows the trigger, target, invalidation, the candidate strike range and the strike
  that ranks first at send time with its current ask (marked "at 06:46, re-checked at trigger"). It shows **no limit price and no stop**.
  Its state is WATCHING (setup complete, history verified, at least one eligible strike) or one of the §3 NO … / REFERENCE ONLY states.
- **`scripts/morning_watch.py`** runs from 06:47 PT until 12:30 PT (= 15:30 ET, when Alpaca stops new opening orders on same-day
  contracts per the Broker API overview; step 0 confirms the paper account follows the same cut-off).
- **Gap-safe, chronological processing (v6).** Per symbol the watcher persists `last_processed_bar_ts` in its state file. Each poll (and
  every startup) fetches ALL completed IEX one-minute bars from `last_processed_bar_ts` (or the 06:46 report's IEX cutoff on first run)
  through the newest completed bar, and processes them oldest first, one bar at a time. The poll interval is plumbing; correctness does not
  depend on it.
  - **Per-bar order for a side not yet triggered (v7, one rule shared with §8):** (1) the bar touches the invalidation → VOID (this wins
    even on a bar that also closes beyond the trigger — the documented conservative exception); (2) the bar CLOSES beyond the trigger →
    TRIGGERED at that close; the target is NOT evaluated on this bar; (3) otherwise, the bar touches the target → STALE (the move ran
    without a close beyond the trigger).
  - **For a triggered side** the target and the invalidation are evaluated from the next bar on (§8).
  - **Coverage check:** the fetched bars must cover every minute in that range. A missing minute → every still-open side of that symbol is
    marked **VOID — watch history incomplete** (logged with the missing minutes) and receives no further alerts that day. A later complete
    poll does not revive it, because what happened inside the hole is unknown.
- **A trigger counts as broken** when a completed one-minute IEX bar (start + 60 s ≤ server time) CLOSES beyond the trigger: call side
  close > trigger, put side close < trigger. A wick through the level that closes back inside does not count. This matches the phase-2
  ordering rule (§8).
- **At the break the watcher rebuilds the idea from scratch:** fresh quotes for every candidate strike (§3 rule 1, polled at that moment),
  the $1.25 cap on the fresh ask, rule-2 ranking, IV and delta from the fresh mid, and the §3a stop from the fresh bid and delta with
  `price_at_trigger` = the breaking bar's close. It sends one Slack message: **TRIGGERED — ACTIONABLE** with limit = fresh ask ≤ $1.25
  and the sell-stop, or **TRIGGERED — <NO … state>** when the fresh rebuild fails a rule.
- **Quote fetch at the break (v6):** a failed or empty quote fetch is retried until the break bar's age reaches t_max (retry spacing is
  plumbing); the break is only usable while it is fresh, so t_max is the derived retry budget. Still failing at t_max → **TRIGGERED — NO
  CURRENT EXECUTABLE QUOTE** (sent, never silent).
- **Re-validation before every ACTIONABLE send (v7):** after each retry wait, and again immediately before an ACTIONABLE message is
  sent, the watcher runs the same backfill pass on every bar completed after `last_processed_bar_ts` (continuous coverage required). A
  target or invalidation in those bars → **TRIGGERED — STALE, no entry**; a missing minute → VOID — watch history incomplete. The pass,
  the state write and the send decision run in one process under the singleton lock, so no other watcher process or state
  change can interleave. A bar that completes while the Slack message is in flight is not covered; the alert and the JSONL record the
  decision cutoff (`decision_cutoff_bar_ts`, the last bar processed before the send).
- **Validity checks at the break:** the break bar is fresh (its close age < t_max), and, from the chronological pass above, neither the
  target nor the invalidation printed in any processed bar since the report. Any failure → **TRIGGERED — STALE, no entry** with the reason.
- **One alert per symbol and side per day.** State is kept in `logs/morning_watch_state_<date>.json` (atomic write), so a watcher restart
  never re-alerts. A side whose invalidation prints before its trigger is marked VOID and is not watched further.
- **Failure mode:** watcher down or data stale → no TRIGGERED message, and the 06:46 watchlist footer already says "no TRIGGERED message
  = no entry". On restart the backfill above runs first; any uncovered minute VOIDs the open sides. The heartbeat records the last poll
  time and `last_processed_bar_ts` per symbol; a VOID for incomplete history is sent once as a one-line Slack warning.
- **Fill-time check (printed in every ACTIONABLE alert, `<strike>` filled from the pick):** "If the <strike> bid is at or below the stop when you are filled, do not keep the
  trade without a stop: exit, or re-run the stop formula on the live bid." The bot cannot see the owner's fill.
- **Singleton lock:** `fcntl.flock` on a file in `/tmp`; the kernel releases it when the process dies, so a stale file never blocks a run.

## 4. Slack message mock (phone first; Block Kit via `send_slack_blocks`; the fallback text carries both ideas)

Illustrative: prices are 10/29 06:36 PT bar closes standing in for live quotes, and level labels are illustrative.

```
☀️ Morning Rundown · Thu Oct 29 · 06:46 PT
SPY now 768.13 (IEX trade, 4s old) · SIP to 06:31 PT · IEX history 06:31–06:46 complete · EM ±$5.2 (asks, approx.)

━ SPY ━  GEX regime: positive (gex.py)
🔴 773.36  prior-day high
🔴 771.34  overnight high      ← call target
🔴 769.24  prior-day low       ← CALL trigger
   ── now 768.13 ──
🟢 765.89  put wall            ← PUT trigger
🟢 763.17  EM low (approx.)    ← put target

📈 CALL 👀 WATCHING — alert comes only if a 1-min bar CLOSES above 769.24
   Candidate 770C · ask 1.17 at 06:45:58 PT (re-checked at trigger) · max loss ≤ $125
   Target 771.34 · Invalidation 765.89
📉 PUT 👀 WATCHING — alert comes only if a 1-min bar CLOSES below 765.89
   Candidate 766P · ask 1.14 at 06:45:58 PT (re-checked at trigger) · max loss ≤ $125
   Target 763.17 · Invalidation 769.24

━ QQQ ━  📈 CALL — NO ACTIONABLE SETUP — missing target
         📉 PUT — NO CURRENT EXECUTABLE QUOTE (no 0DTE put quote updated 06:45–06:46)

🚫 Don't: buy from this list · pay above $1.25 · chase after the target prints · hold without the sell-stop
_No TRIGGERED message = no entry. Limit and stop come with the TRIGGERED alert. The bot places no orders for these._
```

TRIGGERED alert (illustrative, sent by the watcher when a bar closes beyond the trigger; prices re-read at that moment):

```
🔔 SPY PUT ✅ TRIGGERED — ACTIONABLE · 07:12 PT
   1-min bar 07:11 closed 765.70 < trigger 765.89 (bar age 12s)
   765P · bid 1.20 / ask 1.22 (quote 07:12:05 PT) · Limit $1.22, never above $1.25 · max loss $122
   Target 763.17 → worth ≥ $1.83 there
   Stop: sell-stop 765P at $0.13 (day, estimate from delta 0.30) — place right after the fill
   Exit line: SPY back above 769.24
   If the 765P bid is at or below $0.13 when you are filled: exit, or re-run the stop on the live bid.
```

(Stop = bid − |delta| × |price_at_trigger − invalidation| = 1.20 − 0.30 × |765.70 − 769.24| = 1.20 − 1.062 = 0.138, rounded down
to the tick = $0.13. v4's signed form would have given 1.20 + 1.062 = $2.26, above the bid. When the result is ≤ 0 — a cheap contract
far from its invalidation — the alert reads TRIGGERED — REFERENCE ONLY — no placeable stop, per §3a.)

06:00 PT send: levels and setups, header "PRE-MARKET · reference". The sections are REFERENCE ONLY when the history is incomplete, the
price is not current, or the broker cannot take pre-market option orders; with 05:45–06:00 IEX coverage measured at 0–20%, that is
nearly always. Limit and stop read "N/A — stale data". Footer: "The watchlist comes at 06:46 PT; entries come only as TRIGGERED alerts after that."

Failure text:
- GEX unavailable → "GEX n/a" rows.
- Missing setup parts → NO ACTIONABLE SETUP.
- No eligible contract → NO ACTIONABLE CONTRACT UNDER $1.25.
- Build failure → a one-line Slack warning and heartbeat `ok=false`.
- Market closed per the Alpaca calendar → no send.

## 5. Schedule (OCI crontab, `scripts/cron_tz_wrapper.py` takes ET times; 06:00 PT = 09:00 ET all year)

| Job | Cron (UTC pair covers EDT/EST) | Wrapper ET | Gate |
|---|---|---|---|
| Morning rundown, pre-market | `0 13,14 * * 1-5` | 09:00 | Alpaca calendar: today is a trading day |
| Morning rundown, after the open (watchlist) | `46 13,14 * * 1-5` | 09:46 | same; SIP cutoff after the open, so the IEX gap is all regular-hours minutes (§2a) |
| Morning watcher | `47 13,14 * * 1-5` | 09:47 | same; runs until 15:30 ET, singleton lock in `/tmp` (§3b) |
| Weekly GEX report | `0 13,14 * * 1,2` (replaces `0 13,14 * * 6`) | 09:00 | today is a trading day (Alpaca calendar) AND this ISO week's marker `logs/gex_weekly_sent_<ISO-year>-W<week>.json` is absent. The marker is written only after a successful send. v5: the Tuesday run decides by reading Monday's marker, so a Monday holiday AND a failed Monday send both lead to a Tuesday send (logged "Monday send missing"), and a successful Monday send stops Tuesday |
| Grader (phase 2 only) | `20 20,21 * * 1-5` | 16:20 (= 13:20 PT) | trading day |

- The weekly report fires only on Monday and Tuesday. A Monday holiday defers it to Tuesday. Two consecutive closed weekdays (no case in the
  normal NYSE holiday calendar; only an unscheduled closure) means no weekly report that week. That week's Wednesday morning rundown logs
  "weekly GEX report skipped: no trading Mon/Tue" and still shows that day's GEX rows.
- The UTC pair stays because the wrapper runs once in EDT and once in EST; a single `0 13` line would miss 09:00 ET in winter.
- Each run logs the clock it detected: `now_utc`, `now_et`, the ET UTC offset (−04:00 / −05:00), and the wrapper target, so a DST mismatch
  shows in the log. The shared wrapper itself is unchanged.
- The Saturday line is removed (the report moves, it is not duplicated).

Changes to `gex_weekend_report.py`:
- add the gate above;
- spot = the latest pre-market price with its time and source (on Monday at 06:00 PT, IEX `bars/latest` returns a pre-market bar; verified
  10/29), replacing "Friday close as spot";
- `_expiry_range()` already returns run day → Friday.

## 6. Logging (DST-safe)

- Every record carries `ts_utc` (ISO-8601 `Z`), `ts_local` (ISO-8601 with offset, e.g. `2026-10-29T06:46:00-07:00`), `tz`
  (`America/Los_Angeles`) and `schema_version`. Slack text shows PT.
- `logs/morning_rundown_recs.jsonl`: one row per symbol, side and send. Each row holds:
  - the level price (value, time, source) and the current price (value, IEX trade time, age, t_max, current yes/no);
  - every level with its sources;
  - the trigger/target/invalidation presence and any missing part;
  - the broken-check result per level;
  - EM availability with its spread uncertainty and merge distance;
  - every candidate strike: raw bid, ask, quote time, venue/condition, rejection reason;
  - the pick and its ranking value;
  - the suggested stops and the delta used.
  Appends take an `fcntl` exclusive lock.
- `logs/morning_rundown_chain/<date>_<HHMM>.json.gz`: the raw contract and snapshot payloads, kept for a later point-in-time replay.
- `logs/morning_rundown_<date>.md`, written atomically (tmp → replace).
- `logs/morning_rundown_heartbeat.json`, written atomically. Fields:
  - `last_run_utc`, `last_run_local`, `send` (0600/0646), `detected_et_offset`, `ok`, `slack_ok`, `md_ok`;
  - per symbol: `b_high`, `b_low`, `sigma_1m`, `t_max`, `merge_distance` with their session counts (v6);
  - per symbol/side from the watcher: `watch_state` (WATCHING / TRIGGERED / STALE / VOID), `void_reason`, `void_ts_utc` (v7);
  - per symbol/side: `state` (ACTIONABLE / REFERENCE ONLY / NO ACTIONABLE SETUP / NO CURRENT EXECUTABLE QUOTE / NO ACTIONABLE CONTRACT UNDER $1.25);
  - `violations`: counts for `ask_over_cap`, `quote_not_current`, `trigger_text_without_history`, `ranked_without_setup`,
    `actionable_without_stop`, `limit_or_stop_shown_when_stale`; counts are per send, with a `violations_today` running total. Every
    count must be 0. The renderer checks the final blocks before sending.
    Any non-zero count blocks the send of the offending section, which is replaced by its REFERENCE ONLY form, and is logged.

## 7. Numbers in phase 1

| Number | Status |
|---|---|
| $1.25 cap | owner number (question 1) |
| √(2π)/2 straddle factor | math constant, computed in code (≈1.2533), labelled approx. |
| r = 0.045, q = 0.012 (IV and the delta stop estimate) | existing gex.py constants, grandfathered; display and suggestion only |
| Quote freshness | send-time poll window (plumbing timeout); later the p95 of measured update intervals |
| Current-price age limit t_max | derived: (merge distance / σ_1m)² |
| IEX-gap buffers b_high / b_low | derived: p99 of SIP−IEX one-minute high gap / IEX−SIP low gap, same window, 20 sessions |
| Option stop price | derived at the trigger: bid − \|delta\| × \|price_at_trigger − invalidation\| (estimate, labelled) |
| σ_1m | derived: sample std of one-minute close-to-close dollar changes, same window, 20 sessions |
| Watcher end 15:30 ET | broker rule (Alpaca: no new same-day opening orders after 15:30 ET); confirmed in step 0 |
| Watcher poll interval | plumbing |
| EM availability cutoff | derived: spread uncertainty ≤ merge distance |
| Spread | no threshold; zero, locked and crossed quotes rejected structurally |
| Merge distance | derived: median one-minute range of the same clock window, 20 sessions (an estimator argument) |
| Candidate strike range | derived: trigger → target, plus one strike beyond the target |
| 20-session windows | estimator arguments |
| Send times 06:00 / 06:46 PT; weekly Mon/Tue | owner schedule; 06:46 = 06:30 open + 15-min SIP delay + one bar |

Removed in v2: 30-minute age, ±2% window, halfway trigger time, touch probability, "weak" threshold, mid-based eligibility, 0.25 spread
ratio. Removed in v3: the ±2 × daily-range fallback band (a missing target now means NO ACTIONABLE SETUP).

## 8. Phase 2 (separate approval): replay and grader

Built only on data that can support it:
- Point-in-time OI/GEX does not exist historically from Alpaca (current contracts carry today's OI only), so a GEX replay uses the
  phase-1 archive from its first day forward. A pre-archive replay may test only non-GEX levels and must be labelled as a different variant.
- Historical option quotes are not available (404), so the replay grades SIGNAL behaviour only (did the trigger break after a completed
  one-minute close beyond it; did the target or the out level print first) and claims no executable option P&L. Option P&L comes from
  archived live asks and the owner's actual fills.
- Ordering (the same per-bar rule as the watcher, §3b): entry only after a completed bar closes beyond the trigger; a bar that touches
  the out (invalidation) level never triggers, even if it closes beyond the trigger; target/out evaluated from the next bar; a later bar
  that touches both target and out counts as out (conservative).
- Any probability is a measured rate with a confidence interval; the driftless touch formula is not used as a benchmark.
- Holdout looked at once; candidate count reported; VIX bands use ^VIX (T4, permitted) labelled as such.

## 9. Build step 0 (read-only, on OCI, before arming the crons)

On a trading day at 06:00 and 06:46 PT, record:
- bid/ask presence and quote timestamps against the wall clock (feed lag);
- venue and condition codes;
- the IEX latest-trade age against t_max and the IEX gap coverage;
- quote-update intervals per strike from repeated polling (input to the p95 freshness rule);
- how many strikes have an ask ≤ $1.25 on each side;
- EM availability.

Then do a full message dry run (built, not sent) and check it renders at phone width.

Go/no-go for arming the crons (machine-checked from the step-0 log):
- the 06:46 dry run on one trading day yields, for SPY and QQQ: complete IEX gap history, a current price, at least one quote current inside
  the polling window per side, and zero heartbeat violations;
- the owner reviews that dry-run message;
- the 06:00 send is armed with it, since it is always a reference message when its checks fail.
If the 06:46 checks fail, the cause is fixed and step 0 is repeated; the crons are not armed. The owner confirms two things: whether his
broker takes Cboe GTH orders (if not, the 06:00 ideas are labelled "pre-market reference only"), and whether it supports stock-triggered
contingent orders on options.

### 9a. Machine tests (tests/test_morning_rundown.py; all HTTP mocked; a guard fails the test on any unmocked network call)
1. **Trigger state.**
   (a) The IEX trade is older than t_max → both sections render "REFERENCE ONLY — trigger state not verified", and the text contains no
   "only if … breaks".
   (b) The current price is already above the nearest resistance → that level is not the call trigger, and the next unbroken level is.
   (c) The SIP high since the level time crossed a level → that level is not a trigger.
   (d) A level within one merge distance of the current price → not a trigger.
   (e) One minute missing from the IEX bars between the SIP cutoff and now → "REFERENCE ONLY — trigger history not verified".
   (f) An IEX bar in the gap touched the level and then reversed (Codex scenario: 601 at 09:35, 599 at 09:40) → not a trigger.
   (g) In every REFERENCE ONLY section: no "only if … breaks" text, and no limit or stop number.
2. **Setup completeness.**
   (a) No level above the current price → CALL renders "NO ACTIONABLE SETUP — missing trigger".
   (b) A trigger with no level beyond it and EM unavailable → "missing target".
   (c) No level on the other side → "missing invalidation".
   (d) EM available and next beyond the trigger → EM is the target, labelled approx.
   In (a)–(c) the ranking function is a mock asserted not called, and no strike, limit or stop is rendered.
3. **Stops.**
   (a) Every ACTIONABLE idea has a sell-stop price with 0 < stop < bid, for both a CALL and a PUT fixture (v5: the PUT case, invalidation
       above the price, must give stop < bid; it failed under v4's signed formula).
   (b) Delta n/a, or stop ≤ 0, or stop ≥ bid → REFERENCE ONLY — no placeable stop, with no limit shown.
   (c) The string "accept up to the full premium" and any "no stop" wording never render.
   (d) No rendered text claims a stop was placed.
4. **Quote freshness.** A quote from today's session but timestamped before the polling window (e.g. 07:31 ET at a 09:46 ET send), with
   ask ≤ $1.25 → not eligible; the side reads NO CURRENT EXECUTABLE QUOTE.
5. **Cap.** No rendered idea has ask > $1.25, a quote older than today's option session open, or a price taken from a trade or mid. Every
   eligibility path is fed a fixture where the last trade is ≤ $1.25 and the ask is > $1.25: the expected result is
   NO ACTIONABLE CONTRACT UNDER $1.25.
6. **Schedule.** Monday trading day → weekly report sends. Monday holiday → Tuesday sends. Second run in the same week → exits without
   sending (marker present). Wrapper target times match the stated PT times across a DST change, and the logged detected offset switches at the change.
8. **Watcher (tests/test_morning_watch.py, v5).**
   (a) A 1-min bar whose high crosses the trigger but closes back inside → no alert.
   (b) A completed bar closing beyond → the rebuild re-polls quotes (mock asserts the quote fetch happens after the break, not reuse of the
       06:46 quotes), and limit and stop come from the fresh quote.
   (c) Fresh ask > $1.25 at the break → TRIGGERED — NO ACTIONABLE CONTRACT UNDER $1.25.
   (d) Target or invalidation printed before the break → STALE / VOID, no entry text.
   (e) Restart after an alert → no second alert (state file).
   (f) Put side uses b_low and IEX lows; call side uses b_high and IEX highs (fixtures where only the off-side buffer would flag).
   (g) After 15:30 ET → the watcher exits without alerting.
   (h) v6 gap tests: last processed bar closes 100; the next poll's newest bar is 3 minutes later; the backfill holds a bar with low 98.80
       (invalidation 99); a later bar closes 101.20 above trigger 101 → VOID / no ACTIONABLE (Codex round-5 input).
   (i) Same, but the backfill has one missing minute → VOID — watch history incomplete; no alert even after a later trigger close.
   (j) A trigger close and a reversal both inside a restart gap → the trigger is processed from the backfill in order, its bar age is
       > t_max → TRIGGERED — STALE, no entry.
   (l) v7: quote fetch fails at 10:00, the 10:01 bar touches the invalidation, the quote succeeds at 10:02:30 (bar age < t_max) →
       TRIGGERED — STALE, no ACTIONABLE (Codex round-6 input). Same with the target printed at 10:01 → STALE.
   (m) v7: a call bar opens below the trigger, touches the target and closes above the trigger, no invalidation touch → TRIGGERED (target
       not evaluated on the trigger bar). A bar touching the invalidation and closing above the trigger → VOID.
   (n) v7: a bar before any close beyond the trigger touches the target → STALE.
   (k) Quote fetch fails at the break, succeeds on a retry before t_max → ACTIONABLE; still failing at t_max → TRIGGERED — NO CURRENT
       EXECUTABLE QUOTE is sent.
7. **Logging and heartbeat.** Every row has `ts_utc`, `ts_local` with offset, `tz` and `schema_version`; the JSONL is valid when two writers overlap; the heartbeat has
   every `violations` key; an injected violation (a rendered ask of $1.30) makes the count 1 and replaces that section with its reference form.

## 10. Required questions (RQ-4)

Q1: Daily S/R + capped 0DTE ideas for the owner, with archived inputs that later allow edge evaluation; no bot capital used.
Q2: Bot trade count unchanged. Owner's manual trades may change; he records them himself or via phase 2.
Q3: Phase 1 is the simplest form that honours the cap on executable prices.
Q4: options_scanner.py makes 0DTE recs with a fixed ~0.35 delta, a $75 cap and Public.com greeks; gex_weekend_report.py is reused. Checked
    2026-10-30: origin/main commits since 10/20 (none on these files), open PRs #804 and #727 (docs, unrelated), and the day-tier
    family admissions file (no entry for this report; it is not a bot strategy).
Q5: Section 7.
Q6: Forward pass: phase 2 measured rates; later replace options_scanner.py's fixed delta with this ask-based selection (own gate).
Q7: Report-only; levels ↔ Day tier and selection ↔ options_scanner are later diffs, each with its own fallback.
Q8: Section 1 claims are measured; live pre-market bid/ask, feed lag and broker GTH routing are open and tested in step 0.
Q9: No profitability claim in phase 1; phase 2 replay per section 8. Wrong if, after step 0, sends show no eligible strike on most days.
Q10: No validated edge exists yet.
Q11: No bot size/frequency/exposure change.
Q12: Bot envelope untouched. The bot's stop rule covers bot positions. A report idea is ACTIONABLE only with a placeable Alpaca option
     sell-stop (§3a); otherwise it is reference only. Owner question 3 decides whether manual trades formally fall under the rule.
Q13: Stale current price or incomplete trigger history → REFERENCE ONLY, with no limit or stop; stale quotes → NO CURRENT EXECUTABLE QUOTE; missing trigger/target/invalidation → NO ACTIONABLE SETUP with no ranking; no eligible contract →
     NO ACTIONABLE CONTRACT UNDER $1.25; EM spread too wide → EM unavailable; per-symbol isolation.
Q14: Not a shadow: the report is acted on; the archive logs a live report's inputs.
Q15: Section 6 JSONL: both prices with ages, setup completeness, broken-check results, raw quotes, rejection reasons, the pick, the stops.
Q16: Heartbeat + log line `morning_rundown: Slack blocks sent`; first session audited by rebuilding the message from the archive.
Q17: Step 0 on OCI.
Q18: N/A.
Q19: No bot change; owner approves the cap reading and message labels.
Q20: No payoff, probability or "stronger" claims remain.
Q21: No broker calls; tests mock HTTP and include a test that fails on any unmocked network call.
Q22: Not a fail-closed control.
Q23: Step 0 dry run before arming, plus the §9a tests, which include the setup edge cases.
Q24: GTH start date checked against trade data this session (0 pre-open prints 10/14, 10/16; 63 on 10/19).
Q25: Figures from 10/29 bars; realized vol measured at run time.
Q26: T1 Alpaca (stock bars, contracts, snapshots, calendar); T4 ^VIX only in phase 2.
Q27: logs/ only; md and heartbeat atomic; JSONL locked appends; gz archive.
Q28: Separate cron processes; never on the run_cycle thread.
Q29: Owner request; about one session; not ranked against the backlog.
Q30: Executable ask, quote provenance and a no-trade state included; fill capture and calibration are phase 2.
Q31: Phase 2.
Q32: Phase 2 ordering rules; phase 1 uses only data before the frozen spot time.
Q33: The cap is checked on the ask, so the spread is paid inside the cap; GTH liquidity measured in step 0.
Q34: Ideas are directional on the index; no alpha claim.
Q35: Phase 2.
Q36: One contract; under-cap GTH liquidity measured in step 0.
Q37: Phase 2.
Q38: Phase 2, owner-approved definition required.
Q39: N/A.
Q40: Machine checks (§9a, §6 heartbeat): every shown idea has ask ≤ $1.25 and a quote current at send time; complete trigger history; no "only if … breaks" text without a current price; no
     ranking without a complete setup; every idea carries a stop line. The heartbeat records the violation count per send (must be 0).
Q41: Limit price, quote provenance, no-trade state, archived inputs.
Q42: Raw snapshot quotes and contract OI, archived per send.
Q43: Executable-price selection under a hard limit with a no-trade state.
Q44: Phase 2 evidence: archived live asks plus the owner's actual fills.

## 11. Owner questions

1. "$1.25 per contract": the recommended reading is $1.25 per share of premium, which is $125 per contract. A $1.25 total would allow only
   $0.01 options.
2. Two sends: 06:00 (levels and reference setups; pre-market IEX coverage is 0–20%, and Alpaca takes no extended-hours option orders) and
   06:46 (watchlist), then TRIGGERED alerts with the limit and stop when a level breaks (§3b). This is recommended over sending only at
   06:46 with fixed prices.
3. Do manual trades taken from this report fall under the "stop on every position" rule? Recommended: yes. Every actionable idea prints a
   sell-stop you place right after the fill; ideas without one are reference only. The bot cannot place or verify stops on trades it did not
   make.
4. Do you place these trades in the Alpaca paper account? The stop wording assumes Alpaca's documented option stop orders. Another broker
   needs its own check.
5. Call and put for each of SPY and QQQ (recommended), or only the better one per side?
6. Weekly GEX report on Monday at 06:00 PT, Tuesday if Monday is a holiday, Saturday send removed: recommended yes.

## Changes after review round 1

| # | Finding | v2 fix |
|---|---|---|
| C1, C2, C16 | Cap checked on a model / last trade / mid | Eligibility on the fresh executable ask only; limit = ask ≤ $1.25 (§3 rules 1, 3) |
| C3 | Modeled price shown as a quote | No modeled prices in phase 1; bid/ask, time, venue shown (§3, §4) |
| C4, G1, G4 | Same-time valuation, median trigger time | Removed with all modeled valuation |
| C5, G3 | "Always" idea vs no market; weak ideas shown | Both sections always present; "NO ACTIONABLE CONTRACT UNDER $1.25" when nothing qualifies; no weak/score labels (§3 rule 4). The "hide when payoff < 1" suggestion is moot: there is no payoff number, and the owner requires both sections |
| C6 | Mock trigger already crossed | Trigger beyond the current price by one merge distance; completed in round 2 (R1) |
| C7 | Grader cron UTC wrong | `20 20,21` with wrapper 16:20 ET (§5) — verified: old line was the 09:20 PT pair |
| C8 | Holiday deferral impossible | Weekday cron + first-trading-day-of-week calendar gate + idempotency marker (§5) |
| C9, C10, C11, C13, C14, C15 | Replay/grader cannot support P&L/probability claims | Moved to phase 2 with signal-only grading, conservative ordering, archived point-in-time data; probability formula and payoff ranking removed (§8) |
| C12 | IV root/no-arbitrage rules | Bounds, expiry cutoff and day count stated; IV display-only, "n/a" with reason (§3 rule 3) |
| C17 | GTH/feed/broker routing | Confirmed GTH start by trade data (0 pre-open prints 10/14, 10/16; 63 on 10/19, venues C/A); remaining checks in step 0 (§9); owner question 3 |
| C18 | EM from stale legs | Fresh two-sided quotes on both legs plus the spread-uncertainty cutoff (§2b) |
| C19 | "Stronger" unvalidated | Renamed "confluence" (§2) |
| C20 | PT-only timestamps | UTC + offset + zone + schema version; locked appends (§6) |
| Static list | 25% spread, 30-min age, halfway time, weak threshold, ±2%, 1.25 | Removed or derived (§7); 1.25 kept as a math constant, labelled |
| Nits | Merge distance seasonality; regime label; price-age mixing | Same-clock-window merge distance; regime labelled as the existing gex.py classifier; only quote time shown |
| G2 | IV skew can break the cap at the trigger | Resolved by C1: no modeled entry; the message says to re-check the ask and never pay above $1.25 |

## Changes after review round 2

| # | Finding | v3 fix |
|---|---|---|
| R1 | A delayed SIP price could choose a trigger that already broke | §2a: SIP builds levels only. Trigger state uses the IEX latest trade, which must be newer than the derived t_max; otherwise the sections are REFERENCE ONLY and never say "only if … breaks". The SIP high/low since the level time also disqualifies crossed levels. Tests §9a-1 |
| R2 | No defined result when the trigger, target or invalidation is missing | §2c: all three are required; otherwise "NO ACTIONABLE SETUP — missing <part>", logged, no ranking. EM levels are ordinary ladder levels and can be the target. The ±2 × daily-range fallback band was removed. Tests §9a-2 |
| R3 | Stop requirement vs manual trades | §3a: the bot's stop rule covers bot positions. Each idea prints a stock-triggered stop at the invalidation level, an option sell-stop estimate (labelled), and the maximum loss. The message never claims a stop exists. Owner question 3 decides scope. Tests §9a-3 |
| G-a | VWAP from a bar still forming | The job starts at 06:41 PT and VWAP uses completed bars through 06:39 only (§2b, §5); v4 moves the send to 06:46 |
| G-b | Weekly report waking every weekday | Cron limited to Mon and Tue, with the UTC pair kept for DST (§5) |
| G-c | EM from a wide straddle | EM unavailable when 1.25 × the straddle's half-spread sum exceeds the measured merge distance. The cutoff is derived, not a fixed percentage (§2b) |
| Q4/Q27 | Overlap search; heartbeat atomicity | Open PRs, recent main commits and the day-tier admissions file checked; heartbeat written atomically |

## Changes after review round 3

| # | Finding | v4 fix |
|---|---|---|
| C-1 | One IEX trade cannot show what happened in the SIP-delay gap | §2a: real-time IEX one-minute bars over every minute from the SIP cutoff to now, plus a measured venue buffer b (SPY $0.175, QQQ $0.60 at p99). An incomplete gap gives REFERENCE ONLY — trigger history not verified. The after-open send moves to 06:46 so the gap is all regular-hours minutes (measured IEX coverage 100% then, 0–20% pre-market). Tests §9a-1(e–g) |
| C-2 | A same-session quote can be hours old | §3 rule 1: a quote must update inside the send-time polling window, or later fall within the measured p95 update interval; otherwise NO CURRENT EXECUTABLE QUOTE. Test §9a-4 uses a same-session stale quote |
| C-3 | A path allowed a position with no stop | §3a: ACTIONABLE requires a placeable Alpaca option sell-stop (stop/stop_limit single-leg, day/gtc, no extended hours — read from Alpaca's Options Trading docs 2026-10-30); otherwise REFERENCE ONLY. "Accept up to the full premium" removed. Tests §9a-3 |
| G-1 | EM from mids | EM from the call and put asks (§2b) |
| G-2 | Limit/stop shown on stale data | REFERENCE ONLY sections show "Limit/Stop: N/A — stale data" (§2a). Test §9a-1(g) |
| G-3 | Cold-start merge distance | Uses available sessions, then today's own bars, then no merging with both sides REFERENCE ONLY; no fixed default (§2b) |
| G-4 | Wrapper offset | Each run logs now_utc, now_et and the detected ET offset (§5). Test §9a-6 |
| Nits | Go/no-go; VWAP bar completeness; heartbeat violations | Machine-checked go/no-go (§9); a bar is used only if start + 60 s ≤ server time (§2b); heartbeat `violations` schema in §6, with a test (§9a-7) |

## Changes after review round 4

| # | Finding | v5 fix |
|---|---|---|
| R4-1 (Codex) | PUT stop sign wrong: `bid − |delta| × (current − invalidation)` is above the bid when invalidation is above the price | §3a: absolute distance. Test §9a-3(a) has a PUT fixture |
| R4-2 (Codex) | Downside breaks confirmed with upside-only venue data (b from highs) | §2a: separate b_high (highs) and b_low (lows); b_low measured in step 0. Test §9a-8(f) |
| R4-3 (Codex) | Limit and stop priced at send time, not when the trigger breaks | §3b: 06:46 is a WATCHLIST with no limit/stop; `morning_watch.py` rebuilds the idea from fresh quotes on a completed 1-min close beyond the trigger and sends one TRIGGERED alert per side. Tests §9a-8 |
| R4-4 (GAI) | t_max formula needs a correction factor | Not changed. GAI's correction assumes σ is a per-bar range or a return. v5 states σ_1m explicitly as the std of one-minute close-to-close dollar changes, under which (d/σ)² minutes is the driftless scaling (§2a) |
| R4-5 | Tuesday weekly run vs Monday | Tuesday run gates on Monday's ISO-week marker: Monday holiday or failed Monday send → Tuesday sends; successful Monday → Tuesday exits (§5). Test §9a-6 |
| R4-6 | EM factor shown as 1.25 next to a $1.25 cap | Computed as √(2π)/2 in code, never typed as 1.25 (§2b, §7) |

## Changes after review round 5

| # | Finding | v6 fix |
|---|---|---|
| R5-1 (Codex, REJECT) | Watcher read only the latest bar, so an invalidation printed during an outage could be missed and an ACTIONABLE alert sent later | §3b: persisted `last_processed_bar_ts`; every poll and startup backfills all completed bars and processes them oldest first; any missing minute VOIDs the open sides for the day. Tests §9a-8(h)–(j) use Codex's input |
| R5-2 (GAI) | No retry on the quote fetch at the break | §3b: retry until the break bar's age reaches t_max (the derived freshness budget, not a fixed count); then TRIGGERED — NO CURRENT EXECUTABLE QUOTE is sent. Test §9a-8(k) |
| R5-3 (GAI) | b_low not in heartbeat | §6: b_high, b_low, σ_1m, t_max, merge distance per symbol in the heartbeat |
| Nits | Negative buffer; stop unplaceable by fill time; stale /tmp lock | b floored at 0 and logged; fill-time line in every ACTIONABLE alert; `fcntl.flock` lock released by the kernel |
| Nit (Codex) | "Typical time" not quantified | Kept as a scaling rule. t_max is a freshness bound, and step 0 reports how often the IEX trade age exceeds it |

## Changes after review round 6

| # | Finding | v7 fix |
|---|---|---|
| NEW-1 (Codex, REJECT) | Quote retries could race a later invalidation/target, then send ACTIONABLE | §3b: the backfill pass re-runs after every retry wait and immediately before any ACTIONABLE send, under the singleton lock. Test §9a-8(l) uses Codex's input |
| NEW-2 (Codex) | Watcher ordering (target first) conflicted with §8 (target from the next bar) | One per-bar rule in §3b and §8: invalidation touch wins; a close beyond the trigger triggers and the target is not evaluated on that bar; a target touch before any trigger close is STALE. Tests §9a-8(m), (n) |
| GAI D1 | No void reason in the heartbeat | §6: `watch_state`, `void_reason`, `void_ts_utc` per symbol/side |
| GAI D2 | Same-bar invalidation + trigger untested | Test §9a-8(m) |
| GAI nits | Violations scope; hard-coded strike in fill text | Per-send counts plus `violations_today`; `<strike>` template |

## Review round 7

Codex APPROVE, GAI APPROVE (2026-10-30). Codex nit applied: the lock's scope is stated precisely and the decision cutoff bar is logged.
