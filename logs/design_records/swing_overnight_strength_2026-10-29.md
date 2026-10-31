# Trend-night overnight hold (QQQM) — SWING-OVERNIGHT-1029 (signed: Claude, 2026-10-29) — v12 amendment (re-issued as SWING-OVERNIGHT-1030), owner approval pending

Approval-ID: SWING-OVERNIGHT-1030
Covers: execution/swing_overnight.py, run_swing_overnight.py, config.py, tests/test_swing_overnight.py, execution/orphan_manager.py, main.py, execution/stop_protection.py, execution/unmanaged_positions.py, server-ops

Owner, 2026-10-29: "You need to find a winning day tier strategy ... and not stop until you do ... think dynamically on
the compositions and combinations."

## Evidence (logs/research/day_deep_search_r1..r4_2026-10-29.md; 30,241 candidates in all)
- **No same-day rule survives** adjusted significance (rounds 1-3).
- **The trend-day drift is overnight.** On days when SPY's prior settled close is above its 200-day average:
  - stocks: overnight +6.7 bps, open-to-close +1.0 bps;
  - ETFs: overnight +8.9 bps, open-to-close −0.7 bps.
  - An independent re-derivation from raw Alpaca daily bars (own code, 2016-2020) gives overnight +6.7 bps gross on
    30 large caps.
- **Stock picking adds nothing significant** once SPY's overnight move is hedged out. The edge is the index's
  overnight drift on trend days.
- **The rule has no look-ahead (verified at source).**
  - `day_search/fam_r3.py` `trend()` line 38: `lab = up.shift(1).reindex(U["days"])  # prior session's state`.
  - The night after day d uses day d−1's settled close.
  - A re-run with that signal (V2) reproduces the original numbers exactly.
- QQQ, buy at the 16:00 close, sell at the next 09:30 open; 0.5× equity, $2,555 start compounding, whole shares,
  2 bps cost; the signal is the prior settled close (V2):
  | | Discovery 2020-24 | Test 2024-26 | Confirmation 2016-20 |
  |---|---|---|---|
  | bps of equity / night | 1.53 (t 1.78) | 1.91 (t 1.63) | 1.45 (t 1.98) |
  | Sharpe / annual | 0.89 / 3.8% | 1.16 / 4.8% | 0.91 / 3.6% |
  | max drawdown / worst night | −9.2% / −2.5% | −4.7% / −1.4% | −6.2% / −1.8% |
  - Trend nights per year: 184-224.
  - Without the filter the worst night reaches −5.1% and drawdowns −18%.
- **Significance, honestly stated:**
  - Pooled test + confirmation, corrected for the last 4 instruments and 2 signal variants: p = 0.011.
  - That correction does not cover the 30,000-rule search that led here.
  - On discovery alone it fails a deflated-Sharpe test.
  - What carries it is external evidence. The overnight-return effect is documented across decades in published work
    (cited from memory, not re-fetched):
    - Cliff, Cooper & Gulen 2008;
    - Kelly & Clark 2011;
    - Lou, Polk & Skouras 2019;
    - Hendershott, Livdan & Rösch 2020.
  - Per CLAUDE.md, the first live sessions are the confirmation on data never consulted (see reversal rule).
- **QQQM real cost** (Alpaca SIP, last 20 sessions):
  - ~3.1M shares (~$921M) a day;
  - median quoted spread 0.66 bps at 15:55 and 1.31 bps at 09:30;
  - 2 bps round trip is conservative.

## Rule (Swing tier, new sub-strategy `trend_night`; additive to Track B and every other tier; Day tier unchanged)
1. **Regime:** SPY's prior settled daily close > the simple average of the 200 settled closes ending that day.
   - Source: T1 Alpaca daily bars.
   - Missing or stale data → no entry that night, logged.
   - Long only.
2. **Instrument: QQQM**, the Nasdaq-100 fund at ~$294 vs QQQ ~$707 (IEX last trade 10-29).
   - No other tier trades it (not in DAY_TIER_UNIVERSE, FOREVER6_UNIVERSE or the Track M rule), so there is no
     shared-symbol netting.
   - The finer share price lets size track the rule.
   - Research used QQQ as the proxy for QQQM (same index; QQQM's own early bars are sparse).
3. **Size** = the minimum of four limits, priced dynamically every night (owner 10-29: "Make it dynamically
   priced. I want at least 5%").
   - **Worst-night budget `b`:** `floor(b × equity / (price × q))`.
     - `b` = max(5% of equity, ¼ Kelly from the strategy's own fills once it has ≥ 50 closed nights). Kelly is
       computed from fill-derived nightly mean and variance and re-computed every night.
     - `q` = QQQ's 99.9th-percentile overnight loss over all prior nights (expanding window, point-in-time),
       re-measured every night. Measured 3.6-4.2%.
     - With q ≈ 4%, b = 5% gives about 1.2× equity: today ≈ 10 QQQM shares (≈ $2,900 at $294 on ~$2,555 equity).
     - This replaces the Swing tier's 2%-at-stop rule for this sub-strategy only (owner override 10-29).
     - Consequence:
       - a worst-case night costs ~5% of equity, about 70% of the 7% daily kill budget, which can halt the other
         tiers' new entries that day (Taleb's kill-switch spillover);
       - expected return scales to ~9-10% a year at this size.
   - **The K overnight limb room** (Architecture Invariant #11) after QHM/F6/other notional.
   - **All-tier gap check:** Σ over every overnight position (notional × that symbol's own 99th-percentile
     overnight loss), plus QQQM's, must stay within the 7% daily kill threshold. Otherwise size down to fit, or skip.
   - **Buying power.**
   - Board seat Thorp computed full Kelly ≈ 13-16× and supported up to 1.0×. GAI and Codex recommended ≤ 0.5×.
     The owner chose a ≥ 5% worst-night budget (~1.2×) on 10-29. The K limb, the all-tier gap check (≤ 7%) and
     buying power still bound it every night.
4. **Entry:** market-on-close (`time_in_force=cls`) for the computed whole shares.
   - Submitted on the 15:40 ET run. Alpaca rejects close orders 15:50-19:00 ET. Half-days: the close time comes from
     the Alpaca calendar and the run is close − 20 minutes.
   - If the close order is rejected (paper support is unverified until the first live attempt), the fallback is a
     market order at the 15:55 ET run.
   - The fill path (`cls` / `fallback`) is logged with each night's P&L.
   - **One entry per session, idempotent:** the ids are `TN-<date>-entry-cls` and `TN-<date>-entry-mkt`.
     - The fallback is submitted only after the cls order is broker-terminal `rejected`/`expired`.
     - While the cls order is UNKNOWN the fallback is blocked, and client-id resolution takes precedence over the
       fallback timer (GAI r5 #1).
   - Stop prices are computed in `decimal.Decimal` and rounded down to the $0.01 tick before any submit or replace
     (GAI r5 #2).
   - **Entry UNKNOWN:** the same terminal-state rule as for sells applies. A timed-out or unacknowledged entry
     submission is resolved by exact client-id lookup plus a fully paged TN order and fill listing. No second entry
     is submitted while any entry order is non-terminal or UNKNOWN (Codex r4 #1).
5. **Overnight protection, with a coverage invariant.**
   - **Every run** (every minute, and immediately after any entry acknowledgement, fill event or replace
     acknowledgement) reconciles. If `held` > 0 and the resting stop does not cover `held` at the right price, it
     fixes it at once:
     - stop quantity = `held`;
     - stop price = (broker-confirmed cumulative average entry fill price) × (1 − q).
   - When the average or `held` changes, quantity and price are replaced together (`replace_order_by_id`).
   - After each replace acknowledgement the run reconciles again immediately. It repeats until stop `remaining_qty`
     == `held`, with no wait between passes.
   - The simulation asserts this invariant after every broker event (Codex r4 #2, #3).
   - Alpaca stops do not trigger outside regular hours, so this is the backstop if the exit fails, not gap
     protection. Gap risk is controlled by size (item 3).
6. **Exit — a single-sell-reservation state machine.**
   - **Reconcile (broker-first, before every sell submission):** list every TN order at the broker (by client-id
     prefix) with its status and `filled_qty`, and read the broker's QQQM position. Then:
     - `held` = cumulative entry `filled_qty` − cumulative exit/stop `filled_qty`. This must equal the broker QQQM
       position, since no other tier holds QQQM.
     - `reserved` = remaining qty of every non-terminal or UNKNOWN TN sell. This includes an opg in `new`,
       `accepted` or `pending_new`, so no 09:30 market sell can fire while an opg is live (GAI r4 #1).
     - `free` = `held` − `reserved`.
     - A new sell may be submitted only for `free` > 0 and only when `reserved` = 0.
     - Any mismatch or unreadable broker state → submit nothing, page, retry next minute.
   - **Terminal-state rule:** an order is gone only when the broker reports `canceled`, `rejected`, `expired` or
     `filled`.
     - A cancel request, a timeout or a missing acknowledgement is UNKNOWN. UNKNOWN is resolved by looking up the
       exact client order id and re-listing TN orders.
     - While any TN sell is non-terminal or UNKNOWN, no second sell is submitted. This rules out shorts and
       duplicate exits.
   - **Order ids:** persistent and idempotent, `TN-<date>-<step>-<n>`.
   1. **≥ 09:20 ET (before the 09:28 opg cutoff): swap the stop for the opg sell.**
      - Cancel the stop and reconcile until it is terminal. If it filled, `held` drops.
      - Then run one more full reconcile immediately before submitting, so the `opg` sell is for the post-cancel
        `free` (GAI r4 #2).
      - Submit the `opg` sell for that `free`.
      - Why this window has no protection gap: Alpaca stops cannot trigger outside regular hours. Between 09:20 and
        09:30 the stop protects nothing, and the opg sell is the only order able to act at 09:30.
      - Why the opg cannot go in first: Alpaca rejects it while the stop holds the shares (`40310000`).
   2. **opg outcome by 09:27:**
      - **rejected** (terminal) → re-place the GTC stop for `free`; mark the exit pending.
      - **UNKNOWN** → resolve by client id. If it is still unresolved at the 09:28 cutoff, submit nothing more, page,
        and resolve at 09:30. If the opg exists, it acts at the open.
   3. **≥ 09:30 ET, every minute until `held` = 0 (the exit is never blocked):**
      - Reconcile. If the opg filled → done.
      - If shares remain, cancel every non-terminal TN sell and reconcile until all are terminal.
      - Submit a market sell for `free`.
      - If the market sell is still open 60 s later: cancel it and reconcile until terminal. Then re-place the stop on
        the new `free` and retry the market sell next minute.
      - If the market sell is rejected: re-place the stop on `free` and retry next minute.
      - Page after the third failed attempt, and keep retrying through the session.
   4. **Partial fills:**
      - Entry partials: the stop quantity follows `held` through the broker's replace-order call
        (`replace_order_by_id`, already used in `execution/broker.py` L1867), which changes the resting stop's qty
        in one request.
        - If the replace is rejected: cancel, reconcile until terminal, then submit a new stop for `free`.
        - Fills that arrive during the transition are caught by the next reconcile.
      - Exit partials: every retry targets only the reconciled `free`.
   5. **Restart recovery:** on start, the process rebuilds state from the broker's TN orders and fills plus the
      ledger, using the same reconcile, before acting.
   6. **Calendar unavailable:**
      - Entry is skipped and logged; no close time is assumed.
      - The exit steps still run, using the broker clock (`is_open`) to detect 09:30.
      - If the broker clock is also unreachable for more than 60 s, the system clock in ET is used for the exit
        windows only.
7. **Safety:**
   - Entry is blocked when the 7% daily kill switch has tripped, the kill flag `SWING_TREND_NIGHT_ENABLED` is off, the
     data is stale, or another run holds the lock.
   - The exit is never blocked.
   - The startup reconcile and orphan manager must recognise the `swing:trend_night` lot in the ownership ledger and
     never adopt or close it. That is verified in the full-path simulation.
8. **Runtime:** a separate cron process `run_swing_overnight.py`.
   - Runs every minute on weekdays.
   - Inside each order window it stays alive and polls the broker every 5 s until every TN order is terminal and
     coverage holds. This is the production mechanism behind "immediately after a fill" (Codex r5 #3). The windows:
     - close − 20 min to close + 10 min;
     - open − 10 min to open + 10 min;
     - each exit retry.
   - Clock-source changes (broker clock → system clock) are logged.
   - Each run reads today's Alpaca calendar (open/close) and acts only when the clock falls in a step window
     relative to that calendar:
     - entry window: close − 20 min to close − 11 min (the cls cutoff is 15:50 on a normal day, and the calendar-
       relative equivalent on half-days);
     - fallback window: close − 5 min;
     - exit windows: open − 10 min to open − 2 min, then open onward.
   - Never on the `run_cycle` thread.
   - **Paper assertion:** at start and before every submit, the process uses `execution/broker.py` (paper=True
     hardcoded) and verifies through `get_account()` that the account is the paper account (the account number
     recorded in config/.env). A mismatch blocks every submit and pages.
   - **q for QQQM:** max(QQQ's q, QQQM's own q once it has 250 nights) — the more conservative of the two.
   - P&L is booked to the Swing tier, strategy `trend_night`.
9. **Logging:** append-only state-transition events to trade_events.jsonl.
   - Fields: signal (SPY close, SMA, bar dates), size inputs (each of the four limits, which one bound), kill-switch
     state, order ids, submissions, acknowledgements, fills (price, qty, time, path), stop placements/cancels, exit
     result.
   - Plus one end-of-night summary event.

## Machine-checked results and full-path simulation (before ship)
- **Result check, `trend_night` audit line, at 09:35 and again at the close:**
  - Target: `held == 0`, with no TN order open.
  - Strategy P&L recomputed from Alpaca fills (FIFO) must equal the booked P&L.
  - At 09:35 a miss pages, while the exit retries continue. At the close a miss is a failure and pages.
- **Tests cannot reach the broker.** `execution/broker.py` `_get_trading_client()` (L176-180) refuses to build a
  real client in any test process. The trigger is `MTF_TEST_MODE=1`, pytest loaded, or a test runner, so every
  account, order, position, cancel, replace and fill call fails unless mocked. `tests/test_swing_overnight.py` also
  asserts that refusal for each broker function the new module calls.
- **Full-path simulation before calling it ready, with a mocked broker** covering (in addition to the list below):
  - a market sell left open after 60 s;
  - an opg submit that times out but was accepted;
  - fills arriving during a stop replace;
  - a restart during each UNKNOWN state;
  - an unavailable calendar;
  - a cls entry accepted but timed out;
  - adverse-price partial entry fills (stop price follows the cumulative average);
  - a partial stop fill during the 09:20 cancel window.
  - The stop-coverage invariant (stop `remaining_qty` == `held` at the right price) is asserted after every broker
    event.
- Base cases:
  - cls accepted / rejected;
  - opg accepted / rejected / unacknowledged;
  - held_for_orders;
  - partial entry and exit fills;
  - crash and restart mid-sequence;
  - stale data;
  - kill switch tripped;
  - a half-day calendar;
  - a stop that fills at the open.
  Then a read-only probe on OCI: regime, size limits and K room against the real account.

## Size / frequency / concurrency (Rule B)
- About 200 entries a year at about 1.2× equity overnight (5% worst-night budget, dynamic).
- Risk-path on all three axes. Board (with the masked-loss seat), GAI + Codex and owner approval apply.

## Expected effect and reversal
- Expected about +3.5-4.5 bps of equity per trend night ≈ $0.90-1.15 a night at today's equity, ~9-10% a year. Small
  in dollars; its value is a measured, positive-expectancy use of overnight capacity, and it scales with equity.
- Live confirmation and auto-off:
  - The first 50 live trend nights are the never-consulted confirmation.
  - Auto-off (kill flag set, owner told) when, after ≥ 50 nights, cumulative net P&L < 0 AND the live mean is more
    than 2 standard errors below the replay mean (1.5 bps).
  - Any single night losing more than 2% of equity → size review.
  - Measured fill cost above 4 bps round trip over 20 nights → review.

## Board (cold panel, round 1): Thorp, Harris, Taleb — APPROVE-WITH-CHANGES 3-0. Changes, all folded in above:
- **Thorp:** fractional-Kelly/derived size and the 2% stop rule (item 3).
- **Harris:**
  - exit sequence (item 6);
  - fills checked against the official prints (logging);
  - calendar session times;
  - Track M sequencing (moot: QQQM is not traded by Track M).
- **Taleb:**
  - 09:35 re-protect check;
  - all-tier gap check;
  - kill flag + auto-off;
  - Swing-only P&L booking;
  - fail-closed entry.

## Review record and the one open difference
**Review rounds:**
- Board, cold panel (Thorp, Harris, Taleb): approve-with-changes 3-0; changes folded in.
- GAI: rejected v2 and v3; approve-with-changes on v5 and v6; v7 folds in its last two items.
- Codex: rejected v2-v6, each round on narrower execution points, all folded in. The exception is the one below.

**Open difference (Codex):** at the exit, the protective stop is cancelled and confirmed before the sell is
submitted, because Alpaca rejects a second sell while a stop holds the shares (`40310000`). Codex wants a
broker-atomic handoff so a live stop never disappears even for those seconds.
- **The checked atomic option does not work.** The broker-native OCO in `execution/broker.py`
  (`submit_oco_exit`, L956) requires each leg on the correct side of the live price, so a "sell at the open"
  limit cannot sit below the market inside an OCO.
- **This is the bot's existing exit pattern.** Every tier exits by cancelling its own stop and then selling:
  - `broker.close_position`;
  - `partial_close_position`, which detects `40310000`, cancels the blocking orders and retries.
- **How v7 bounds the window:**
  - the stop is re-placed on any sell rejection, any 60 s non-fill or any UNKNOWN that resolves to no sell;
  - the 09:20 swap happens while a stop cannot trigger anyway;
  - the process polls every 5 s through the handoff.
- **Recommendation:** accept the existing pattern. Separately, test on paper whether Alpaca lets the OCO's
  take-profit leg be replaced to a marketable price. If it does, that becomes a follow-up improvement to make the
  handoff atomic for every tier, not only this one.

## Required questions (one line per question, RQ-4)
- Q1: Adds about four percent a year at half size using idle overnight capacity, positive in all three tested periods; it scales with equity toward the goal.
- Q2: Adds roughly two hundred round trips a year; no other tier loses any trades.
- Q3: Simplest form: hold the index (QQQM) overnight on trend nights instead of picking stocks; QQQM avoids sharing symbols with other tiers.
- Q4: Additive to Track B, Track M, F6 and QHM with no symbol overlap; no open PR or Codex session covers overnight index holds.
- Q5: Size derives from the existing two percent stop rule, measured gap quantile, K room and the seven percent kill threshold; the trend filter and windows are the tested rule.
- Q6: Exposes a fixed-size overnight assumption; next build is VIX term structure and earnings-night sizing, tested first.
- Q7: The all-tier gap check becomes a shared overnight-risk signal that other tiers can read for their own sizing.
- Q8: Replay-measured returns with independent re-derivation from raw bars; QQQM spreads measured from live quotes.
- Q9: Tested across three historical periods including selloffs; expected small positive return per night; proven wrong if live nights lose beyond the auto-off rule.
- Q10: No; the edge is shipping live at its measured size rather than being held back.
- Q11: Risk-path: size, frequency and concurrency all increase, so board with the masked-loss seat reviewed it.
- Q12: Every share carries a stop, the kill switch stays armed, paper flag asserted, and entry fails closed.
- Q13: Stale or missing data means no entry; the exit is never blocked by missing data.
- Q14: No shadow; ships live after the gate.
- Q15: Every state transition logs its decision stack to trade_events.jsonl with the result check line.
- Q16: Entry and exit events on the first night prove it ran; the first live night is audited and replayed against fills.
- Q17: A full-path simulation then a read-only OCI probe prove the real process works before live.
- Q18: No anomaly is being explained away by this design.
- Q19: It changes entry, sizing and routing, so the owner approves this exact design before commit.
- Q20: Prompts passed the bias gate; board, GAI and Codex reviewed neutrally across rounds.
- Q21: The broker refuses in a test process and every function test mocks broker calls.
- Q22: Yes, a read-only OCI run against the real broker and ledger precedes go-live.
- Q23: A full-path simulation from signal to exit passes before it is called ready.
- Q24: QQQM tradability and spreads were checked live against Alpaca data.
- Q25: Overnight gap statistics were measured from Alpaca daily bars, not quoted from memory.
- Q26: Tier one Alpaca daily bars and market calendar; no fallback source for entry decisions.
- Q27: Writes trade_events.jsonl plus an atomic lot-state file using temporary file and replace.
- Q28: Runs as a separate cron process, never on the run_cycle thread, so check_exits is unaffected.
- Q29: Ranks high: measured positive edge using capacity that currently sits idle overnight.
- Q30: Frameworks require a book-wide overnight gap budget; our audits underweight cross-tier gap risk, which the gap check covers.
- Q31: Three separate periods with an honest multiplicity statement; the first live nights serve as confirmation on unseen data.
- Q32: Replay uses only closed bars at decision time and the current index product, so look-ahead is excluded.
- Q33: Replay assumes two basis points cost versus measured spreads near one basis point; reviewed if costs exceed four.
- Q34: Mostly beta timed into the night, stated openly; the residual is the trend-night timing.
- Q35: Worst night about two percent at half size; correlation with the book is handled by the all-tier gap check.
- Q36: QQQM trades about nine hundred million dollars daily, so capacity is trivial at twenty-five thousand.
- Q37: Each night is a trial; the confidence interval narrows with live nights, which inform size but never delay launch.
- Q38: Decay measured by the auto-off rule on live results; non-working moves it to zero trades and a replacement design.
- Q39: Size comes from the stop-based risk rule from day one and updates from its own Alpaca fills as they arrive.
- Q40: Protects overnight index gains; the daily result check compares realized fills with replay expectation.
- Q41: An expert would also size by implied overnight volatility, which the forward pass adds.
- Q42: Rawest data is broker fills, orders and daily bars, which the design uses directly.
- Q43: Professional method is overnight-drift capture with a volatility-scaled size and a hard stop, which this follows.
- Q44: Independent evidence is broker fills compared with official closing prints.


## Amendment v8 (2026-10-30, after the build and review round 7) — owner re-approval needed
Review of v7 (Codex found blocking defects and GAI requested changes) agreed on two gaps; v8 replaces v7.

1. **QQQM is reserved for this sub-strategy everywhere, unconditionally.**
   - `execution.swing_overnight.trend_night_symbols()` returns `{config.SWING_TREND_NIGHT_SYMBOL}` always: no state
     file read, so a lost or corrupt state file can never un-protect a held lot (Codex RQ-13).
   - Every path that can adopt, re-stop, close or mis-attribute a symbol excludes it (Codex Q-A, file:line cited):
     - `main.py` startup position count / dedicated-manager inputs (L869-881);
     - `execution/orphan_manager.py` `reconcile_positions()` orphan set (L1331-1334) and the closed-market emergency
       stop sweep (L800-803);
     - `execution/stop_protection.py` sweep exclusions next to QHM/F6 (L593-610);
     - `execution/unmanaged_positions.py` manager-source list gains `("swing_trend_night", …)` (L173-174), so a held
       QQQM is credited to this sub-strategy, never flagged or credited to `swing_tracker`.
   - The Day tier, QHM, F6 and Swing-breakout never trade QQQM (not in any of their universes); the entry paths also
     refuse it explicitly via the same set.
   - Full-path test: a simulated main restart with QQQM held and the state file deleted → no tracker adoption, no
     second stop, no non-TN sell; the trend_night result check asserts this every session.
2. **Small-sample statistics use the replay as the prior (fixes the n = 1 undefined / n ≥ 2 zero-variance cases).**
   - The prior is recomputed every night, point-in-time, from T1 Alpaca daily bars: QQQ's overnight returns on every
     prior trend night (the regime rule applied to SPY with the prior settled close), net of the 2 bps assumed cost.
     It is the replay itself, not a stored constant.
   - **Kelly (a):** pooled sufficient statistics of prior + live nights (count, sum, sum of squares; the pooled
     variance includes the between-means term). b = max(owner 5% floor, ¼ × pooled mean / pooled variance × q).
     The final size is still the minimum of b, K room, the all-tier gap check and buying power. No count switch.
   - **QQQM's own q (b):** max(QQQ q, QQQM q over whatever QQQM nights exist); the max only tightens (both reviewers).
   - **Auto-off (c):** evaluated every night: live mean < prior mean − 2 × SE, where SE = prior standard deviation /
     √(live nights). With one night SE equals the prior SD (≈ 1 % of notional), so a single normal night cannot fire it;
     identical nights cannot shrink SE to zero. The v6 `cum < 0` extra condition is dropped (not in any approved text).
     Every night logs live n, live mean, prior mean, prior SD, SE and the threshold.
   - **Night-loss review (d):** a night losing more than that night's own persisted `b × equity` (from the entry-night
     sizing snapshot, never recomputed) pages a size review.
   - **Fill-cost review (e):** live mean round-trip fill cost > 2 bps + 2 × SE, SE = SD of the measured quoted
     spreads (SIP, last 20 sessions, refreshed nightly) / √(live nights). A page only; never blocks entry.
   - GAI's suggested 20% hard cap on Kelly is not adopted: K room, the gap check (≤ 7% modeled worst night) and buying
     power already bound size every night (Codex Q-B); a fixed cap would be a new static threshold.
3. **Numbers listed for approval** (owner/rule or broker mechanics): the 200-day regime average (the tested rule), the
   5% worst-night floor (owner 10-29), ¼ Kelly (project standard), the 99.9th / 99th percentile levels (the q and
   gap definitions), the 2-standard-error band, the 2 bps replay cost, and the clock timings that follow Alpaca's
   order rules (cls refused from close−10; opg cutoff open−2). Execution-SLA timings (5 s poll, 60 s market-sell
   cancel, page after 3 failed exits, 30-min page repeat) are listed as owner numbers now; deriving them from
   measured broker latency is the forward pass once live acknowledgements exist.
4. **Server setup:** `ALPACA_PAPER_ACCOUNT_NUMBER` added to the OCI `.env` from the paper account's own
   `account_number` (read-only) before go-live. `SWING_TREND_NIGHT_ENABLED` ships False and flips True only after
   items 1 and 4 are deployed and the read-only OCI probe passes.

Owner numbers: config.py SWING_TREND_NIGHT_SMA_SESSIONS=200; config.py SWING_TREND_NIGHT_MIN_BUDGET_PCT=0.05; config.py SWING_TREND_NIGHT_KELLY_FRACTION=0.25; config.py SWING_TREND_NIGHT_Q_LEVEL=0.999; config.py SWING_TREND_NIGHT_GAP_LEVEL=0.99; config.py SWING_TREND_NIGHT_AUTO_OFF_ALPHA=0.05; config.py SWING_TREND_NIGHT_REPLAY_COST_FRAC=0.0002; config.py SWING_TREND_NIGHT_ENTRY_START_MIN=20; config.py SWING_TREND_NIGHT_ENTRY_END_MIN=11; config.py SWING_TREND_NIGHT_FALLBACK_MIN=5; config.py SWING_TREND_NIGHT_CLOSE_POLL_MIN=10; config.py SWING_TREND_NIGHT_SWAP_MIN=10; config.py SWING_TREND_NIGHT_OPG_OUTCOME_MIN=3; config.py SWING_TREND_NIGHT_OPG_CUTOFF_MIN=2; config.py SWING_TREND_NIGHT_OPEN_POLL_MIN=10; config.py SWING_TREND_NIGHT_RESULT_CHECK_MIN=5; config.py SWING_TREND_NIGHT_MKT_SELL_SECONDS=60; config.py SWING_TREND_NIGHT_EXIT_PAGE_FAILURES=3; config.py SWING_TREND_NIGHT_CLOCK_FALLBACK_SECONDS=60; config.py SWING_TREND_NIGHT_FALLBACK_OPEN_MIN_ET=570; config.py SWING_TREND_NIGHT_POLL_SECONDS=5; config.py SWING_TREND_NIGHT_PAGE_REPEAT_SECONDS=1800; config.py SWING_TREND_NIGHT_SPREAD_SESSIONS=20

## Changes after review round 8 (v9) — supersede the matching v8 text
1. **Order ids use the Swing tier's recognised encoding** (Codex r8 #1: `ownership_guard.tier_of_coid()` would not
   recognise a `TN-` prefix, so ledger sync would misattribute fills). Every trend_night order id is
   `IN-QQQM-<b|s>-<night_close_epoch_ms>-tn<step><n>` (built with `ownership_guard.make_coid("intraday", …)`), so the
   ledger credits the Swing tier exactly as it does for Swing-breakout orders. It stays deterministic per night, step
   and attempt (idempotent), and `IN-QQQM-` identifies trend_night because QQQM is reserved to it (v8 item 1). No
   change to `ownership_guard.py`. Tests cover entry, stop, opg, market exit and stop-fill attribution through
   `tier_of_coid()` and a ledger-sync replay.
2. **Auto-off uses an anytime-valid boundary** (Codex r8 #2: a fixed 2-SE test checked every night inflates the false
   shut-off rate). The live mean is compared with the replay prior mean through a normal-mixture confidence sequence
   (Robbins 1970; Howard, Ramdas, McAuliffe & Sekhon 2021):
   auto-off when `live mean < prior mean − σ · sqrt( 2(n+ρ)/n² · ln( sqrt((n+ρ)/ρ) / α ) )`, where σ = the replay
   prior's standard deviation (recomputed nightly from T1 bars), n = live nights, and ρ is set so the boundary is
   tightest at one year of trend nights (counted from the replay). This holds its false shut-off probability at α
   across ALL nights checked, from night one; no minimum count. α is an owner number (proposed 5% over the
   strategy's life). Every night logs n, live mean, prior mean, σ, ρ, α and the boundary.
3. **Fill-cost review uses realized costs** (Codex r8 Q-B(e): quoted-spread variance is not realized fill-cost
   variance). Its variance is pooled from the live realized costs plus one pseudo-observation at the measured
   quoted-spread variance (so night one is defined), with the same anytime-valid boundary against the 2 bps
   assumption. It pages only; it never blocks entry.
4. **Kelly detail** (Codex r8 Q-B(a)): a non-positive pooled mean gives the 5% floor; pooled variance is clamped at
   zero against round-off; both the uncapped b and the binding final limit are logged.
5. **Restart test additions** (Codex r8 Q-A): with QQQM held and the state file deleted, a simulated main restart
   leaves the ledger tier as Swing, does not inflate `risk.open_positions`, and `check_exits` / shutdown / pre-close
   paths submit zero QQQM orders; the closed-market emergency stop loop also skips QQQM explicitly.

The owner-numbers line above replaces the 2-standard-error band with the false shut-off rate α (AUTO_OFF_ALPHA).

## Changes after confirmation round 9 (v10) — supersede the matching v9 text
1. **trend_night's own order parser follows the new ids** (Codex r9 #1). `parse_coid()` accepts only
   `IN-QQQM-<b|s>-<night_close_epoch_ms>-tn<step><n>`: it validates the symbol QQQM, the side, a numeric epoch, a known
   step and a numeric attempt, and returns night (ET date of the epoch), step and attempt. Any other id is not a
   trend_night order. The broker-first reconcile lists QQQM orders and keeps only ids that parse. Tests feed
   new-format broker orders (entry, stop, opg, market exit, stop fill) through `reconcile()`, restart recovery and
   ledger sync, not only through `tier_of_coid()`.
2. **The edge check pages the owner; it does not switch the strategy off by itself** (Codex r9 #2: no boundary
   built from replay estimates can promise an exact lifetime false shut-off rate when overnight returns are
   heavy-tailed; paper-account frame: no new automatic halt without the owner). Each night the check computes the
   normal-mixture boundary below and, if the live mean crosses it, pages the owner with the evidence (n, live mean,
   prior mean, σ, ρ, α, boundary, the nightly returns). Turning the strategy off stays the owner's decision
   (`SWING_TREND_NIGHT_ENABLED`). The boundary is stated as an approximation designed for about α under a normal
   model, not a guarantee:
   - `boundary_n = prior mean − sqrt( σ² · 2(n+ρ)/n² · ln( sqrt((n+ρ)/ρ) / α ) + σ²/N_prior )`, which adds the
     replay mean's own sampling variance (σ²/N_prior; Codex r9 #2);
   - σ = the replay prior's standard deviation, and N_prior = its night count, both recomputed nightly from T1 bars;
   - ρ = n* / (2·ln(1/α) + ln(1 + 2·ln(1/α))) with n* = the median number of trend nights per calendar year in the
     replay (Howard et al. 2021, the ρ that makes the boundary tightest near n*). n*, ρ and the boundary are logged
     every night (Codex r9 #3).
   - The fill-cost review uses the same boundary form on realized costs (v9 item 3) and also only pages.
3. v9 items 3-5 stand. Owner numbers are unchanged: α (AUTO_OFF_ALPHA) is now the edge-check page rate.

## Changes after confirmation round 10 (v11) — supersede the matching v9/v10 text
1. **Fill-cost review, defined in cost units** (Codex r10 #1). The prior is a cost sample, not a variance: for each of
   the last `SWING_TREND_NIGHT_SPREAD_SESSIONS` (20) sessions, the quoted round-trip cost proxy = (half the QQQM
   quoted spread at the close bar + half the quoted spread at the next open bar) / mid, from SIP quotes. From that
   sample: prior_mean, σ (its sample standard deviation) and N_prior (its count), recomputed nightly. Each live night
   contributes its realized round-trip cost = (entry fill − official close + official open − exit fill) / notional,
   from Alpaca fills and official prints. The page fires when the live mean cost exceeds
   `prior_mean + sqrt( σ² · 2(n+ρ)/n² · ln( sqrt((n+ρ)/ρ) / α ) + σ²/N_prior )` (same ρ and α as the edge check).
   If the prior sample has fewer than two sessions, the review logs "prior unavailable" and does not page. It pages
   only; it never blocks entry. The fixed 2 bps stays only as the replay's cost assumption inside the edge prior.
2. **Entries are admitted by the shared capital allocator** (Codex r10 #2). Before every entry submit (cls and the
   fallback), the run calls `execution.tier_capital_allocator.live_admit("swing", "intraday", "QQQM", "buy", qty,
   price_bound, stop_price=…, overnight=True, order_type=…, time_in_force=…, client_order_id=<the IN-QQQM id>)`, as
   the Swing-breakout path does (swing_breakout_manager L958), binds the lease to the order with `live_bind`, and
   releases it with `live_release` / `live_release_proven_no_order` on a terminal non-fill. A refused admission skips
   the entry for the night and is logged. This is the same cross-process reservation every other entry path uses,
   so two tiers cannot spend the same room at once.
3. **Parser detail** (Codex r10 notes, GAI r10 #2): `parse_coid()` uses a fully anchored pattern, rejects an epoch
   outside the strategy's live dates (before 2026-10-29 or more than one day ahead), and rejects unknown steps, a side
   that does not match its step, a wrong symbol, malformed attempts and ids over Alpaca's length limit; tests cover
   each.
4. **Prior data check** (GAI r10 #1): if the nightly T1 fetch yields too few prior trend nights to compute σ (fewer
   than two), the edge check logs "prior unavailable" and does not page, and sizing falls back to the owner floor b
   with q from the last good measurement; entry still requires a fresh regime reading. No hard-coded replay
   constants are used as a fallback.

## Changes after confirmation round 11 (v12) — supersede the matching v11 text
1. **Realized cost formula, in fraction-of-price units** (Codex r11 #3): realized round-trip cost per night =
   ((entry fill price − official close) + (official open − exit fill price)) / official close, using quantity-weighted
   average fill prices per side (same units as the quoted-spread prior, which is a fraction of mid).
2. **Capital admission, stated as it actually behaves** (Codex r11 #1/#2):
   - `TIER_CAPITAL_ALLOCATOR_ENABLED` is False for every tier today (`tier_capital_allocator.live_admit` returns
     "allocator disabled" with no lease); its re-enablement is a separate redesign (comment at live_admit). Every
     existing entry path (Swing-breakout, Day, QHM) runs without a cross-process reservation today. trend_night calls
     `live_admit` exactly like Swing-breakout so it joins the allocator automatically when that redesign ships; that
     redesign must size the Swing cap for this sub-strategy (today's normal-regime cap ≈ $639 would refuse the ~$2,900
     position) and is out of this design's scope.
   - Until then, the concurrency exposure is bounded as follows, and this design adds nothing new to it: trend_night
     re-reads K room, the all-tier gap book and buying power from the broker immediately (same second) before each
     submit; Alpaca rejects an order the account cannot afford (insufficient buying power), which the run logs and
     treats as a terminal non-fill (no entry that night). The K limb (Invariant #11) is block-only, so a same-second
     race can at most let the book exceed K by the smaller of the two simultaneous orders until the next entry check;
     it never forces a sale and never masks a loss. The entry window (close−20 to close−11) does not overlap the
     Swing-breakout entry run (07:12 PT).
