# Trend-night overnight hold (QQQM) — SWING-OVERNIGHT-1029 (signed: Claude, 2026-10-29) — DRAFT v6, not approved

Approval-ID: SWING-OVERNIGHT-1029
Covers: execution/swing_overnight.py, run_swing_overnight.py, config.py, tests/test_swing_overnight.py, server-ops

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
3. **Size** = the minimum of four limits. No new static: each comes from an existing rule or from data.
   - **The Swing tier's existing risk-at-stop rule** (≤ 2% of equity at the stop, SWING-OWNER-1025):
     `floor(0.02 × equity / (price × q))`.
     - `q` = QQQ's 99.9th-percentile overnight loss over all prior nights (expanding window, point-in-time).
     - Measured 3.6-4.2%, so this gives about 0.5× equity.
   - **The K overnight limb room** (Architecture Invariant #11) after QHM/F6/other notional.
   - **All-tier gap check:** Σ over every overnight position (notional × that symbol's own 99th-percentile
     overnight loss), plus QQQM's, must stay within the 7% daily kill threshold. Otherwise size down to fit, or skip.
   - **Buying power.**
   - Today ≈ 4 QQQM shares (≈ 0.46× equity).
   - Board seat Thorp computed full Kelly ≈ 13-16× and supports up to 1.0×. GAI and Codex recommend ≤ 0.5×. The
     existing 2% rule lands at ~0.5×. Going above it needs a separate owner decision.
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
- About 200 entries a year at about 0.5× equity overnight.
- Risk-path on all three axes. Board (with the masked-loss seat), GAI + Codex and owner approval apply.

## Expected effect and reversal
- Expected about +1.5-1.9 bps of equity per trend night ≈ $0.40-0.50 a night at today's equity, ~4% a year. Small
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

## Required questions (short answers; evidence above)
- **Q1, goal:** ~4%/yr at ~0.5×, positive in all three periods; it uses idle overnight capacity and scales with
  equity.
- **Q2, trade count:** +~200 round trips a year; no tier's trades are reduced.
- **Q3, simpler way:** the index instead of stock picks; QQQM to avoid shared symbols.
- **Q4, overlap:** additive to Track B / Track M / F6 / QHM; no symbol overlap.
- **Q5, static numbers:** size derives from the existing 2% rule, measured `q`, K room and the 7% threshold. The
  200-day filter and the windows are the tested rule.
- **Q6, forward pass:** next, VIX-term-structure and earnings-night sizing, tested first.
- **Q7, cross-use:** the all-tier gap check is a new shared signal for the book.
- **Q8 / Q25, evidence:** replay-measured; independent stock re-derivation; QQQM spreads measured.
- **Q9 / Q31 / Q37, testing:**
  - three periods;
  - honest multiplicity statement;
  - the first 50 live nights are the confirmation;
  - auto-off rule.
- **Q11, risk path:** size, frequency and concurrency all nonzero (risk-path).
- **Q12, safety:** stop on every share (invariant), kill switch, paper assertion, fail-closed entry.
- **Q13, stale data:** stale or missing data → no entry. The exit is never blocked.
- **Q14, shadow:** none; ships live after the gate.
- **Q15 / Q16, logging:** state-transition events plus the result check line.
- **Q17 / Q22 / Q23, proof:** full-path simulation, then an OCI read-only probe; first live night audited.
- **Q19, approval:** the owner approves the exact design.
- **Q21, broker in tests:** test-process refusal in the broker plus per-function tests.
- **Q26, data:** T1 Alpaca daily bars and calendar; no fallback for entry.
- **Q27, output:** trade_events.jsonl plus atomic lot-state file.
- **Q28, threading:** a separate cron process.
- **Q33, cost:** 2 bps assumed vs 0.66-1.31 bps measured spreads; reviewed above 4 bps.
- **Q34, alpha vs beta:** beta timed into the night (stated).
- **Q35, tails:** worst night ~1.4-2.5% at 0.5×; book correlation is handled by the gap check.
- **Q36, capacity:** QQQM ~$921M a day; trivial at $25K.
- **Q38, decay:** auto-off rule.
- **Q39, sizing source:** sizing from the stop rule; Kelly from its own fills later.
- **Q40, outcome check:** the daily result check.
- **Q42 / Q44, raw data:** broker fills and orders, plus the official-print comparison.
