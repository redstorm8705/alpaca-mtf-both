# Short capability for Swing, QHM and the Day tier's overnight carry — SHORT-ALL-1029 (signed: Claude, 2026-10-29) — DRAFT v11 (after confirmation round 8); owner decision pending

Approval-ID: SHORT-ALL-1029
Covers: run_short_selftest.py (new, operator-triggered), execution/swing_breakout_manager.py, execution/symbol_direction_lock.py (new), execution/short_rules.py (new), execution/day_trade_manager.py (import + lock call), execution/entry_logic.py (lock call), execution/quarterly_hold_manager.py, execution/day_promotion.py,
execution/broker.py, execution/ownership_guard.py, execution/overnight_capacity.py, execution/tier_capital_allocator.py
(verify only), main.py, scripts/qhm_thesis.py, scripts/qhm_report.py (display), data/state/quarterly_holds_config.json
(schema: `direction`), config.py, tests/test_swing_short.py, tests/test_qhm_short.py, tests/test_day_promotion_short.py,
tests/test_ownership_guard_short.py

Owner, 2026-10-29 (verbatim): "No tier can hold a short through a report. QHM and Swing only buy, and the Day tier is
flat by the close. This absolutely must be fixed. All tiers except F6 should have the ability to short. Back testing
data is not needed here. Build it. Hard stop."

Frame: a paper account growing $2.5K → $25K. The #1 goal is data collection and edge evaluation. The 7% daily kill
switch is the only daily brake. F6 stays long-only.

## 1. What the code does today (verified by reading the source, this worktree, 2026-10-29)

**Swing (`execution/swing_breakout_manager.py`, ledger tier `TIER = "intraday"`, L78)**
- Long-only throughout.
- Signal `breakout_signal` L146-163: `last > prior_high and last > sma200`.
- Exit `exit_reason` L187-194: live < 20-session low, or max hold.
- Stop `stop_px = ask - stop_dist` L947. GTC stop is placed with side `"sell"` (L1127).
- Entry is `submit_limit_order(..., "buy", ...)` L978. The allocator is called with `"buy"` (L963).
- The entry refuses an ask below the level (L884).
- `_reprotect` protects only the broker's LONG qty (L1093-1094).
- `_find_resting_stop` matches sell-side stops only (L1185).
- `_book_exit` P&L is `(px - entry) * qty` (L1377).
- `_enter` counts `long_value += max(mv_signed, 0)` (L897), so a short lot would be invisible to the gap check.
- `p99_down` (L226) measures downside only.
- **The tier has no earnings handling at all.** A grep for "earnings" in the file returns nothing; lots already hold
  through reports.

**Track B trend-swing (`logs/design_records/trackb_trend_swing_2026-10-28.md`)**
- Design only, not built.
- It already specifies short signals when SPY is below its 200-day average.
- When built, it inherits the `direction` support added here.

**QHM (`execution/quarterly_hold_manager.py`, 7,171 lines)**
- Partly plumbed for direction already:
  - `HoldPosition.direction` L351;
  - `add_candidate(direction=...)` L1225;
  - stop side `"sell" if long else "buy"` at L1762, L1784, L1963, L2835, L4360, L4813, L6398;
  - entry side `"sell_short"` at L2864, L3151, L4522, L4529.
- The caller never passes direction: `main.py` L846 calls `qhm.add_candidate(_sym, target_equity_pct=_tgt)`, and the
  config picks carry no `direction` key (`data/state/quarterly_holds_config.json`).
- Explicit long-only refusals:
  - post-earnings stop L2458;
  - stop-safe add L2805;
  - owner trim L3518;
  - earnings trim L3714;
  - trailing lock L4329;
  - active tranche add L4577;
  - owner-buy adoption L6162;
  - owner partial-sale heal L6437.
- Sell-side-only recognition:
  - `cancel_stray_sell_orders` L734 / L825;
  - stop shape L6833;
  - registered stop identity L6866;
  - coverage L6913;
  - overnight room L7082.
- The entry gate (L2564-2597) is `current_close > prior_close × gate_pct` (long-shaped). Day-3 reconfirm (L2600-2623)
  is long-shaped.
- `_design_stop_price` (L4686-4708) is `max(avg - 2.5 × 14-week ATR, avg × (1 - 15%))`, which is long-shaped.
- The pre-earnings stop cancel (L2321) is OFF by default (`QHM_EARNINGS_STOP_CANCEL_ENABLED`, False), so the stop
  stays on through the report.

**Day tier → overnight (`execution/day_promotion.py`)**
- v1 is long-only by design (docstring L14: "no shorts in v1").
- It refuses short lots at L251-252. It requires broker `side == "long"` at L259, L442 and L558, and daily side
  `"LONG"` at L273-275.
- The stop is `entry - INTRADAY_STOP_ATR_MULT × ATR` (L286).
- On adopt it writes tracker `direction: "long"` (L569) and a GTC `"sell"` (L586).
- Reusable pieces that already exist:
  - the Swing tracker and run_cycle after-hours GTC block handle shorts (`strategy/run_cycle.py` L694-806, buy-stop
    for `_gdir == "short"`);
  - `tier_transfers` qty is SIGNED (`execution/tier_transfers.py` L15: "-n moves an n-share short").

**Day tier short pattern (reusable)**
- `day_trade_manager.py` L594 `_short_maintenance_rate`.
- Short stop/target geometry checks at L696-700.
- Short maintenance skip at L2990-2997.
- Unbounded-loss treatment of an unresolved short at L4370-4380.

**Broker (`execution/broker.py`)**
- Short-block session cache at L135-143, filled only by a SELL rejected with 40310000. A BUY / cover rejection never
  poisons it (L662-687).
- `submit_gtc_stop_order(side="buy")` exists for short stops (L1177-1180).
- The held-for-orders probe already checks for "buy to protect a short" (L331).
- `_floor_bound_stop_qty` gates only sell stops (L1066-1076).
- **There is NO borrow / easy-to-borrow check anywhere.** A grep for `easy_to_borrow|shortable` returns only a comment
  at entry_logic.py L678. `get_asset_tradable` (L2913) and `get_asset_maintenance_margin_rate` (L2930) are the
  existing asset readers.
- Account shorting gate: `run_cycle.py` L2065, `shorting_enabled and equity >= 2000`.

**Ownership**
- `ownership_guard.py` L26-27 and L846-851 REJECT every short on a ring-fenced (floor>0) name, a Rafael lock from
  2026-07-09: "ring-fenced names are LONG-ONLY … shorts → options".
- The protected floor = qhm + forever6 qty (L13-14, L99). This lock is what the owner's order now supersedes for QHM.
- Day shorts are already stored as negative ledger qty (orphan_manager.py L1192, EWY −1.0).
- `ownership_snapshot.py` L114 signs shorts negative.

**Overnight K**
- `overnight_capacity.py` L235 counts gross `abs(market_value)`, so shorts already count in the K limb.
- L247-248 builds the margin ratio from long value only ("a short … must never make K looser"). Short maintenance is
  not modelled there.

## 2. Alpaca facts

Verified:
- `get_asset("NVDA")` (Alpaca MCP, live, 2026-10-29) returns `shortable: true, easy_to_borrow: true,
  borrow_status: "easy_to_borrow", margin_requirement_short: "30"`. These fields exist and are readable.
- docs.alpaca.markets/us/docs/margin-and-short-selling (fetched 2026-10-29):
  - ETB has $0 locate and borrow fees.
  - HTB "a locate is strictly required before opening a short".
  - The HTB borrow fee is charged daily: (HTB short $ value × HTB rate) / 360, with 3 days billed over a weekend.
  - Shorting needs ≥ $2,000 equity.
  - Short maintenance: price ≥ $5 → greater of $5/share or 30%; price < $5 → greater of $2.50/share or 100%.
    `_short_maintenance_rate` already implements this.
  - In extended hours only limit DAY/GTC orders are accepted. Trailing stops do not trigger outside RTH.

Unverified:
- Forced buy-in when a held short turns HTB.
- Whether one order can flip long→short.
- Paper vs live borrow differences.

Inferred from code (not docs):
- One net position per symbol. orphan_manager L1411 derives direction from the sign of qty.
- 40310000 also covers held_for_orders and wash-trade rejections, which the broker already separates
  (`_is_wash_trade_reject` L509).

### Facts added in v2 (verified at source, 2026-10-30)
- **Margin units.** `broker.get_asset_maintenance_margin_rate` (L2930-2950) already parses Alpaca's percent string
  (`"30"`) into a decimal (`float(raw) / 100.0`). It rejects missing, non-finite, ≤ 0 or > 1 values with None.
- **Short rate input.** `day_trade_manager._short_maintenance_rate` (L594-609) takes that DECIMAL rate plus a price
  and returns a decimal; it returns None on bad input.
- **K is already sign-aware.** `overnight_capacity.compute_k_raw` (L123-175):
  - takes SIGNED weights;
  - charges each short its worst UP day (`worst[s] = ... r.max()` when `w[s] < 0`, L159);
  - uses `lev = Σ|w|` (L131);
  - L247-251 builds `m` from the account's `maintenance_margin` over gross and over long value.
- **The ownership guard is dormant.** `config.OWNERSHIP_GUARD_ENFORCE = False` (config.py L749), so no live order passes
  through `check_never_sell_floor` today (broker.py L752).
- **The guard misreads a flip.** In `submit_limit_order` (L757-759), when the broker position is long, a `sell_short`
  entry is classified as `"sell"`, so a long→short flip would be read as a reducing sell.
- **Swing already skips busy symbols.** It skips any symbol that is broker-held by anyone, has any open order, is a
  QHM hold or configured pick, or is an F6 name (L755-789, L837).
- **QHM keeps quantities unsigned.** `HoldPosition.qty_filled` is unsigned throughout. `_qhm_total_notional`
  (L6989-7013) skips `qty_filled <= 0` and sums `price × qty` per position.
- **The Swing breakout is close-based** (L154 `prior_high = c.iloc[...].max()` on closes), so a close-based breakdown
  is the exact mirror.

## 3. Design (v4)

### 3a. Conventions (one rule for every consumer)
1. **Signed quantity** (+ long, − short) is used ONLY in the ownership ledger, tier transfers, ownership snapshots and
   the broker-vs-ledger reconcile assert.
2. **Everything else uses `qty = abs(...)` plus a `direction` field ∈ {"long", "short"}.** This covers:
   - order qty, stop qty and stop coverage;
   - lot `qty` in Swing state, QHM `qty_total` / `qty_filled`, and the tracker `qty` / `qty_remaining`;
   - sizing, risk and notional.
   A missing direction means "long" for records written before v2.
3. **Short P&L** = `(entry − exit) × abs(qty)`. Long P&L = `(exit − entry) × abs(qty)`. One helper,
   `signed_pnl(direction, entry, exit, qty)`, is used by:
   - Swing `_book_exit` (L1377) and `_book_partial`;
   - `lot_r`;
   - QHM trims and the QHM exit;
   - the promotion booking.
4. **Risk at the stop** = `abs(qty) × abs(stop − entry)`. **Notional** = `abs(qty) × price`.
5. **Margin rates are always decimals.** A new `broker.get_asset_short_margin(symbol)` parses
   `margin_requirement_short` exactly like L2930-2950 (`float(raw) / 100`; reject None, non-numeric, non-finite, ≤ 0
   or > 1). The short rate used anywhere = `_short_maintenance_rate(max(long_rate, short_rate), price)`, moved to
   `execution/short_rules.py` and imported by the Day tier unchanged. None → no short entry.
   Test fixtures: `"30"`→0.30, `"100"`→1.00, None, `"abc"`, `"-5"`, `"150"`, price $4 / $10 / $100.
6. **Reconcile assert.** After every entry fill, cover fill, stop fill and restart, check: broker signed qty == Σ
   ledger signed qty for that symbol. On mismatch, see §3d step 5.

### 3b. Eligibility (fail-closed, before any short entry)
A short entry needs all of the following, each read this cycle:
- account: not `trading_blocked` / `account_blocked`, `shorting_enabled`, equity ≥ $2,000 (Alpaca rule, docs);
- **SEC Rule 201 (short-sale price test), v4:** the restriction is in force when the stock traded at or below 90% of
  its prior close (a regulatory constant), for the rest of that day and all of the next day. It is computed from T1
  bars: today's low vs the prior close, and yesterday's low vs the close before that.
  **Bar definition (v5):** "today's low" is the minimum low of today's one-minute bars from 04:00 ET through the latest
  completed bar, read live (not the daily bar, which is incomplete intraday). Extended-hours prints are INCLUDED, which
  can over-flag a restriction that the rule (a regular-hours test) would not trigger; that errs toward skipping a short,
  never toward entering a restricted one. The prior close and the lows come from the same request with the same
  `adjustment` setting, so a split cannot mix bases. A restriction found on day D blocks D and D+1 (next trading day,
  Alpaca calendar). Restricted → no short entry
  (skip and log `ssr_active`). Pricing at the offer is not used. Alpaca's own SSR handling (rejecting, or re-pricing a
  marketable short) is unverified, so the bot never relies on it. Bars unreadable → skip (fail-closed). Existing
  covers and stops are unaffected;
- **self-test passed:** `data/state/short_selftest.json` holds a PASS from §3l with `paper=True`. Missing or FAIL →
  no short entry in any tier;
- buying power covering the order (the Swing check at L943-945 is mirrored);
- asset: `shortable` and `easy_to_borrow`;
- the symbol is not in `get_short_blocked_symbols()`;
- both margin rates are parsed (§3a-5).

Any read failure → skip, logged with the raw values and their source. HTB is excluded (D4).

### 3c. Opposite-direction lock — `execution/symbol_direction_lock.py` (new)
The ownership guard is dormant (config L749), so it cannot be the control. This lock is the control.
- **What it is:** a persisted, fcntl-locked, atomically written file `data/state/symbol_direction.json`.
- **Schema (v3), keyed by stable identity:**
  `{symbol: {direction, entries: {entry_id: {tier, kind: "pending"|"held", qty_signed, coid, lot_id, ts}}}}`.
  - `entry_id` = the order's client order id for a pending order, and the lot id for a held lot.
  - Many entries of one tier can coexist (QHM tranches 2 and 3 both pending; a Swing lot plus a recovery order).
  - `direction` is derived from the entries and cleared only when `entries` is empty.
- **Quarantine object (v4).** Each symbol may also carry
  `symbol_quarantined: {reason, evidence: {order_ids, fill_ids, position_reads}, ts, set_by, clear_rule:
  "operator" | "reconcile_2_cycles", clean_cycles}`.
  - It is a separate key from the entries, so it survives `release`, `promote` and every entry being removed.
  - The startup rebuild reloads it from the file and never recreates it from broker state; an existing object is
    kept as is.
  - Unreadable file → treated as quarantined (fail-closed).
  - The name is distinct from Alpaca's account-level `trading_blocked`.
  - **Clearing:**
    - `operator` → only `clear_quarantine(symbol, operator_note)` from the operator command path;
    - `reconcile_2_cycles` → cleared when the §3a-6 reconcile assert passes on two consecutive cycles (`clean_cycles`
      reaches 2; any failure resets it to 0), and only when no lot of that symbol is unprotected.
  - Every set and clear is logged with its evidence.
- **`reserve(symbol, tier, direction, entry_id, qty_signed)`:**
  - called BEFORE an order is submitted, and before the state record is persisted;
  - **step 1:** refuse if `symbol_quarantined` is present (checked BEFORE any direction evaluation);
  - **step 2:** succeed only if every existing entry of that symbol (any tier, held or pending) has the same
    direction;
  - refuses otherwise, and refuses (fail-closed) when the file is unreadable.
  - Exits, covers and protective stops never call `reserve` and are never blocked by a quarantine.
- **`promote(entry_id → lot_id, filled_qty_signed)`** on a fill.
- **`release(symbol, entry_id)`** removes exactly that one entry: on a terminal zero-fill order, or when a lot is
  flat. It never touches another entry of the same tier.
- **Callers:**
  - every entry path in Swing, QHM, the Day tier (`day_trade_manager` entry), the main-book tracker entry
    (`entry_logic`) and day_promotion;
  - for longs as well as shorts, so a pending short blocks a long and a pending long blocks a short.
- **Startup rebuild:** from the broker's open orders (client-id tier prefix + side) plus the ledger, BEFORE any tier
  runs. An order that cannot be attributed → the symbol is locked for all tiers and paged.
- **F6 universe names and DRAM / EWY:** short reservations are refused (the F6 ring-fence is kept).
- **Ownership guard alignment:** `ownership_guard` (L846-851) is changed to REJECT a short only when the symbol has
  F6 qty > 0, or ANY tier's signed qty > 0 — Day tier included (GAI). The protected floor = `max(0, qhm) + forever6`.
- **Broker flip guard (v3, active regardless of `OWNERSHIP_GUARD_ENFORCE`).** In `broker.submit_limit_order`
  (L744-769) and `broker.submit_market_order` (L612), a new check runs BEFORE the flag-gated guard block, at function
  level and not nested inside `if OWNERSHIP_GUARD_ENFORCE:` (test §6-19 runs with the flag False). Using the
  signed broker position:
  - a `sell_short` / `short` while the broker is long → refused;
  - a `sell` while the broker is long with qty > that long qty → refused (it would flip to short). A `sell` while the
    broker is flat is an ordinary short entry and passes (existing Day-tier behaviour);
  - a `buy` from a short tier's cover path with qty > the broker short qty → refused (it would open a long);
  - position unreadable → refused for these sides.
  This replaces the L757-759 classification that maps a long position to `"sell"`. The flag-gated floor check is
  unchanged.

### 3d. Exit and stop coordination — single-buy-reservation state machine (per short lot; mirrors SWING-OVERNIGHT-1029 v8 item 6)
1. **Broker-first reconcile before every buy submission:**
   - list this lot's orders by client id with status and `filled_qty`, and read the signed broker position;
   - `held = entry filled − (cover + stop) filled`, which must match the ledger;
   - `reserved` = remaining qty of every non-terminal or UNKNOWN buy (stop or cover) of this lot;
   - `free = held − reserved`.
2. **An EXIT cover (closing the whole lot) is submitted only when `reserved == 0` and `free > 0`, for exactly `free`.**
   To cover, first cancel the lot's buy stop. Proceed only when the broker reports it `canceled` (or `filled` / `expired` / `rejected`).
   - A cancel request, timeout or missing acknowledgement is UNKNOWN. It is resolved by the exact client-id lookup.
   - Nothing else is submitted while anything is UNKNOWN.
2b. **Protection-gap cover (v5, Codex round-2 defect).** This is the only buy allowed while confirmed-live slices stay
   in place. It never cancels a confirmed-live slice.
   - Allowed only when NO buy of this lot (slice or cover) is UNKNOWN.
   - qty = `unprotected = held − Σ remaining qty of confirmed-live slices − Σ remaining qty of active cover buys`.
     A rejected slice is terminal and reserves nothing. Submitted only when `unprotected > 0`, for exactly that qty.
   - So live slices + gap cover = held: the cover can never overbuy, and the protected shares keep their stops.
   - **Gap protection ladder (v7, replaces v6's unbounded resubmit; Codex round-3 and round-4 inputs).** Every order in
     the ladder is terminal before the next step, so no unfilled order is ever counted as protection, and the cycle
     always completes:
     1. **New stop slice first.** One fresh GTC buy stop for exactly `unprotected`, client id `<lot coid>-G<n>`, at the
        lot's stop price. Confirmed live → the shares have a resting stop; done. Outside regular hours that stop
        rests until the next session in which Alpaca triggers it (extended-hours stop triggering is not assumed), so it
        is protection from the next executable session, not during the current one; the log says which. Rejected → step 2. UNKNOWN → resolved by
        client id as in §3d-6a; nothing else is submitted meanwhile.
     2. **Cover attempts.** Regular hours: a `market` buy (day) for exactly `unprotected`, waited to terminal status
        (`_await_fill` → `_settle`). Outside regular hours: an IOC limit buy at the fresh ask. After each terminal
        result, fills are booked and `unprotected` is recomputed from the new `held`; a remainder (zero fill, partial
        fill, or a rejected order) gets the next attempt. Attempts per cycle are capped by a plumbing retry count.
     3. **Still unprotected after the cap** (for example a halted symbol, or no executable extended-hours liquidity):
        no order can protect those shares at that moment, and the design says so rather than claiming a stop. The cycle
        finishes; the symbol is `symbol_quarantined`; the exact unprotected qty is logged every cycle as
        `unprotected_short`; and the ladder restarts from step 1 next cycle.
   - **Every log line and state record names the protection state (v10):** `active` (a live stop in a session where
     Alpaca triggers it), `resting` (a live GTC stop that triggers from the next session), or `none`
     (`unprotected_short`).
   - **Paging is deduplicated:** one page when a symbol first has `unprotected_short > 0`, and one each time that qty
     changes or returns to 0. Same qty on later cycles → log only.
   - A partial fill books its filled shares through `signed_pnl`; the remainder continues down the ladder.
   - **Direction is never defaulted (v7, GAI nit):** a lot's direction comes from its entry fill side (`sell_short` →
     short). A lot record with no direction is refused for booking and paged; `signed_pnl` is never called with an
     assumed "long".
2c. **Partial resize cover (v9, Codex round-6) — reduce a lot from `held` to a target T, 0 < T < held** (earnings
   resize §3e, or any other partial reduction):
   - allowed only when no buy of this lot is UNKNOWN;
   - first trim protection down to T: cancel-confirm slices newest-first (each cancel terminal before the next step)
     until Σ live slices ≤ T; if that leaves Σ live slices < T, one slice is added for exactly `T − Σ live`, so
     protection of the kept shares is restored before the resize buy (while the cancelled slice is being replaced,
     only Σ live shares have a resting stop);
   - **one buy quantity (v11, Codex round-8):** `protected_target` = T when the added slice is live (or none was
     needed), or Σ live when the added slice is rejected; `resize_buy_qty = held − protected_target`. The buy is
     submitted only after this is fixed, using the §3d-2b step-2 order types (regular hours: market, waited to
     terminal; outside: IOC at the fresh ask, capped attempts). Live slices (= protected_target) + this buy = held, so
     it cannot overbuy. A rejected added slice therefore gives a deeper reduction than T, logged `resize_overshoot`
     with `target_T` and `actual_T`, and every share still held after the buy has a live stop;
   - fills are booked through `signed_pnl` and the tier's partial-exit path, tagged with the resize reason;
   - a remainder that does not fill within the cap: `resize_incomplete` is logged and paged once, and the coverage rule
     (§3d-4) immediately re-protects the shares still held (Σ live slices < held → the §3d-2b ladder), so the
     unfilled part never sits without a stop when an order can protect it. The resize is retried next cycle.
3. **A stop that filled or partly filled before the cancel confirmed** is booked from its fill. The cover is reduced to
   the remainder, and any residual short is re-protected at once.
4. **Entry is terminal before the first stop (v4, Harris).** A short entry is a limit `sell_short` with
   `time_in_force=ioc`. The Alpaca docs list IOC, but that it works with `sell_short` on paper is checked in the §3l
   self-test. If IOC is unavailable, the entry runs the existing `_await_fill` → `_settle` cancel-confirm
   (swing_breakout_manager L657-690) BEFORE the first stop is placed. So no stop ever rests beside a still-working
   entry. Shares filled before the cancel confirms sit uncovered for at most `SWING_BREAKOUT_FILL_WAIT_S` plus the
   settle, the same window as long entries today. Each entry (a Swing lot, or one QHM tranche) then gets one stop
   slice for its filled qty.
   **Independent stop slices (v3, kept).** Each increment of filled entry qty gets its OWN GTC buy
   stop for exactly that increment, at the lot's stop price. An existing slice is never cancelled or resized to grow
   protection.
   - **Deterministic slice client id:** `<lot coid>-S<cumulative filled qty after this fill>`. A slice that was
     submitted but not recorded (crash) is found by exact client-id lookup and never duplicated.
   - **Coverage rule, checked every cycle and at restart (v8: against `held`, not the entry's filled qty):** Σ qty of
     live slices (status `new` / `accepted` / `held`) == `held` (entry filled − cover and stop fills). Any shortfall
     gets protection for exactly the shortfall through the §3d-2b ladder, at once.
   - **Excess protection is trimmed before anything else (v8, GAI round-5).** A partial cover, a stop partial fill, or
     an earnings resize (§3e) lowers `held` below Σ live slices; a buy stop larger than the short would flip it long
     if elected. Trim: cancel-confirm the newest slice (UNKNOWN → resolved by client id, nothing else submitted), then,
     if the remaining slices now protect less than `held`, add one slice for exactly the difference. Repeat until
     Σ live slices == `held`. Each cancel is terminal (confirmed, or found filled and booked) before the re-slice is
     submitted, so two overlapping stops never exceed the short. During a trim the remaining live slices still protect at least `held` minus the one
     cancelled slice, and the re-slice follows at once in the same cycle. A slice that is
     UNKNOWN counts as live until resolved by client id, so nothing is doubled.
   - **Stop moves (trailing lock):** use `broker.replace_stop_order` (L1775) one slice at a time. Its docstring says a
     failed replace leaves the old order untouched, but that is a repo claim, not verified against Alpaca. The slice
     keeps its old stop until the replacement is confirmed live, so protection never drops below the old stop.
   - **Covers and exits:** step 2 applies to ALL slices: every slice is cancel-confirmed (or found filled) before the
     cover buy, for `free` only.
   - **Why slices, not replace-to-grow:** a growing replace raises qty on one order. Alpaca's `pending_replace`
     behaviour (whether the old order stays live during the replace) is not verified in docs or in this repo. Slices
     never depend on it: protection only ever increases.
   - **Alpaca facts:** a BUY stop is not limited by held-for-orders. That rejection (40310000 "insufficient qty
     available") applies to sells against long qty (broker.py L1269-1281), so several buy-stop slices can rest
     together. Unverified for paper → probe §6-14.
5. **Post-fill invariant (v3, decided).** If the broker side is long after a cover (a flip), or more shares were bought
   than the lot held:
   - the symbol gets the §3c `symbol_quarantined` object (no tier may enter or add) and is paged at once. Clear rule:
     `reconcile_2_cycles` after a proven auto-sell, `operator` otherwise;
   - **the excess is auto-sold only when it is PROVEN**, meaning BOTH: (a) this lot's own buy fills, by client id from
     the Alpaca orders/fills API, exceed the lot's short qty by exactly that excess; and (b) two signed position
     reads, taken with the existing confirmed-read pattern (`_confirmed_gone`, L1166-1178), agree with it;
   - then the bot sells exactly that excess through the same reservation rule. Its real-fill P&L is booked to the
     lot's tier P&L AND enters that tier's short Kelly sample (v4, Thorp + Taleb). There is no separate
     execution-error bucket; the event is still tagged `flip_excess` for the audit;
   - **if the proof is incomplete** (fills unreadable, or reads disagree), nothing is sold; the symbol stays blocked
     for operator confirmation.
   - **Evidence for the proof requirement:** position reads in this codebase have shown a wrong sign before —
     orphan_manager L1340 records "RIVN's closed LONG reappeared as -17 and was adopted as a SHORT". Swing already
     needs repeated reads because of endpoint lag (L1166-1178). A read-only trigger could therefore sell into a
     phantom.
   - **Why auto-sell at all when proven:** an unowned long has no stop and no owner. Waiting for the operator overnight
     leaves real exposure unprotected. Proven own fills cannot be a reporting artefact.
   - The block is cleared only by its clear rule (§3c).
6a. **Slice rejection fallback (v4) — protection does not depend on multi-slice support.** If any stop slice is
   rejected, returns None, or stays unacknowledged:
   - the entry stops adding: its working remainder is cancelled (cancel-confirmed), and no further tranche or add
     follows;
   - the filled qty NOT covered by a confirmed-live slice is bought back at once with a marketable limit buy, through the
     step-2b protection-gap cover (not step 2, which would first cancel the live slices);
   - **UNKNOWN slice (v5):** an unacknowledged slice is NOT answered with an immediate overlapping buy (that could
     double the buy if the slice is in fact live). Instead: the symbol gets `symbol_quarantined` (no adds), a page is
     sent, and the slice is resolved by exact client-id lookup every cycle. Live → adopted as a slice. Proven absent
     (two consecutive lookups return order-not-found, the same two-read proof pattern as `_confirmed_gone`) or
     rejected → step 2b covers the proven gap. A lookup that errors keeps it UNKNOWN and re-pages each cycle;
   - the lot keeps only the qty its accepted slices protect;
   - this is paged and logged as `slice_rejected_cover`;
   - the kill flag for that tier's shorts is set off until the self-test (§3l) is re-run and passes.
6. **Direction-aware `_reprotect`** (GAI): read the SIGNED broker position. For a short lot, protect
   `min(want, -broker_qty)` only when the broker qty is < 0. Broker 0 → the existing confirmed-gone path. Broker > 0 →
   step 5. `_find_resting_stop` matches buy stops for shorts.

### 3e. Earnings (owner decision D6 — see §7)
- **Both modes:**
  - A short entry or promotion requires an FMP (T2) next-report date.
  - **Earnings date UNKNOWN → no overnight short** (no Swing short, no QHM short tranche, no Day promotion of a short).
    The Day tier's intraday shorts are unaffected.
  - The QHM pre-earnings stop cancel is never applied to a short.
- **Mode A ("hold-through", what the owner's text asks for; recommended):**
  - A short may hold through a report with its GTC buy stop resting.
  - At entry, and again on the session before the report, the lot is resized down (never up). Both measures come
    from T1 bars on FMP report dates. `report_gap` is decided by D7:
    - **D7-a (Thorp, recommended):** the stock's own largest post-report one-day rise. The pool's largest is used
      only when the stock's history is shorter than the pool's longest, copying the `compute_k_raw` condition at
      overnight_capacity L163-170.
    - **D7-b (Taleb):** max(own, pool's largest) for every name.
    - **Why D7-a:**
      - it is the rule K already applies to the same book, so the two caps agree;
      - under D7-b one extreme pool event (for example a buyout gap) becomes every name's report gap. That likely
        drives budget B to zero for every report-crossing short, which turns D6 Mode A into Mode B in practice
        [hypothesis — unverified; the build measures the pool's report-gap distribution and states how many
        current names would still fit];
      - the remaining tail is still covered by budget B and the kill threshold.
    - **Taleb's point, stated fairly:** a buyout can hit any name regardless of its own history. If the owner
      weights that above Mode A's usability, D7-b is the consistent choice.
  - **Derived report cap (v3, replaces any fixed percentage):**
    `abs(qty) × price × report_gap ≤ min(A, B)`, where:
    - A = the lot's stop-risk budget × (K / K ceiling) (the existing Swing rule);
    - B = the remaining kill budget = `MAX_DAILY_LOSS_PCT × equity` (7% on paper, config L320) − Σ over every OTHER
      position held overnight tonight of `abs(notional) × its own adverse tail` (its `report_gap` if its report
      falls before the next close, else its `p99` adverse day).
  - B ≤ 0 → no new report-crossing short.
  - **Deterministic resize order (v4):** existing report-crossing shorts are processed one at a time, ordered by
    `abs(notional) × tail` descending, ties broken by symbol. After EACH resize's cover fill, every tail product and B
    are recomputed from the live book before the next lot is considered. The pass stops as soon as B ≥ 0. An
    unfilled or UNKNOWN cover stops the pass until it is resolved; each cover follows §3d (a partial reduction uses the §3d-2c resize cover; a full close uses §3d-2).
  - What this does, stated plainly: the plausible worst report night for the whole overnight book stays inside the
    daily kill threshold. The kill switch only halts new entries; it does not cap a gap, so this is a sizing bound,
    not a loss bound.
  - If `report_gap` cannot be measured, the lot is covered before the report.
  - This is tail-risk REDUCTION, not a bound (see §3h).
- **Mode B ("flatten"):**
  - Every Swing, QHM and promoted-Day short is fully covered, through §3d, inside the last RTH exit window before a
    pre-market report or on the day of an after-close report.
  - A new short is refused when the report falls inside its maximum hold.

### 3f. Swing tier
- **Signal:** `breakdown_signal`, the exact close-based mirror of L146-163: last close < the minimum close of the prior
  55 sessions AND last close < SMA200. It creates a pending SHORT setup at that level.
  - Entry needs the session open and the live bid ≤ the level.
  - The setup is dropped when a close is above the prior 20-session high (the mirror of the L812-819 drop).
- **Exclusions:** the existing L837 exclusion list (any holder, any order, QHM, F6), the §3c lock, and §3b.
- **Entry:** limit `sell_short` at `bid × (1 − SWING_BREAKOUT_LIMIT_SLIP_PCT)`.
  - The allocator is called with `"sell_short"`.
  - Wash rule: `foreign_opposite_stop(symbol, "sell", TIER)`. In that function (L53) `open_side="sell"` →
    `opposite = "buy"`, so it finds another tier's resting BUY stop. A side-semantics test proves it.
- **Stop:** GTC buy at `fill + stop_distance` (unchanged function). Coverage follows §3d-4.
- **Exit:** trend_break when live > the prior 20-session high; time at `SWING_BREAKOUT_MAX_HOLD`; plus earnings
  (§3e); borrow (§3i); all through §3d.
- **Sizing:**
  - `size_order` unchanged, except that r for shorts comes from short lots only (D3, changed):
    - while there are fewer than `KELLY_MIN_SAMPLE_SIZE` closed shorts, r = the existing replay-prior r used for the
      first long lots (`_load_prior`, the tier's existing exploration size);
    - after that, Kelly from the short lots' own Alpaca fills.
  - The gap check uses `worst_up` = the stock's worst-ever one-day rise over its full T1 history (v4, Taleb). This is
    the same measure K charges shorts (overnight_capacity L159, `r.max()`), and replaces p99. `short_value` is passed
    alongside `long_value`. If the history is shorter than the pool's longest, the pool's worst up day is used
    (the K rule at L163-170).

### 3g. QHM
- **Trigger:** a finalized weekend pick with `"direction": "short"` (thesis routine verdict, owner-finalized).
  - `main.py` L846 passes `direction=_pick_cfg.get("direction", "long")`.
  - Any other value → skipped and paged.
- **Exclusions:** the §3c lock and §3b.
- **Gates:**
  - Day-1: `current_close < prior_close × (2 − gate_pct)`.
  - Day-3: skip if price > tranche1 × (1 + `day3_reconfirm_pct`).
- **Stop:** `min(avg + _ATR_MULT × 14-week ATR, avg × (1 + _HARD_FLOOR_PCT))`, a GTC buy. Exit through §3d.
- **Trailing lock:** mirrored (lowers the buy stop).
- **Rally-add: OFF for shorts in v1 (D2 changed).** It would add to an adverse move with no short-side fills yet.
- **Exposure: gross.**
  - `qty_filled` stays unsigned (§3a), so `_qhm_total_notional` already sums `price × qty` per position: gross.
  - A test asserts that $1,000 long A plus $1,000 short B → $2,000.
  - Pending-entry notional (non-terminal tranche orders) is added to the cap basis.
- **Earnings:** §3e (the stop is never cancelled; Mode A trim/resize or Mode B cover).
- **Order recognisers** (L734/825, L6833, L6866, L6913, L7082) take the protecting side from `direction`.
  `cancel_stray_sell_orders` never treats the lot's own `sell_short` entry, or its buy stop, as stray.
- **Owner-app adoption / heal / trim** stay long-only. Any outside qty change on a short → page + operator confirm.

### 3h. Day tier overnight carry (`day_promotion.py`)
A short lot is promoted under the mirrored P2–P5 checks:
- P2: `entry − px ≥ DAYTRADE_PROMOTION_MIN_R × (day_stop − entry)`;
- P3: daily side SHORT;
- stop: `entry + INTRADAY_STOP_ATR_MULT × ATR`; at-risk: `qty × (stop − px)`;
- |notional| in both Invariant #11 limbs;
- plus §3b eligibility, the §3e earnings rule and the §3c lock (the transfer keeps the symbol's direction).

Mechanics:
- The broker side must equal the lot side (L259 / L442 / L558).
- Adopt writes the tracker with `direction: "short"` and places a GTC buy stop. The existing run_cycle short path
  (L694-806) manages it overnight.
- The ledger transfer is negative (the signed `tier_transfers` already supports this).

**Tail honesty (all tiers).** A stop does not bound a gap. A short gapping through its stop loses `abs(qty) × (fill −
entry)`. Every exit logs `slippage_vs_stop` and `gap_loss_beyond_stop`. One-share lots:
- the existing min-1-share rule caps the one share's STOP risk at `SWING_BREAKOUT_MIN1_RISK_PCT`;
- additionally, a one-share short is allowed only when `price × worst_up` (or `price × report_gap` across a report)
  ≤ that same `SWING_BREAKOUT_MIN1_RISK_PCT × equity`;
- otherwise no lot is entered.
This bounds the measured tail at the same existing number. It is still not a hard bound.

### 3i. Borrow lost on a held short (D5 changed)
- Detected by the per-cycle eligibility re-read (raw status and source logged). The decision:
  - **During RTH:** cover immediately via §3d (cancel-confirmed stop → marketable limit buy for `free`, re-protect any
    residual), and page.
  - **Outside RTH:** the stop stays. The cover is queued for the first RTH minute. Alpaca accepts only limit orders in
    extended hours (docs), and IEX quotes outside RTH are thin.
- A broker buy-in (unverified behaviour) shows up as a buy fill not owned by the lot. It is booked from its fill by the
  §3a-6 reconcile.

### 3j. K limb and caps
- **K needs no new formula.** `compute_k_raw` already takes signed weights, charges shorts their worst UP day (L159)
  and uses gross leverage (L131). `m` comes from the account's `maintenance_margin` (Alpaca-computed for the whole
  account; that it includes shorts is not verified in paper — test on OCI) over gross and over long value (L247-251).
- **Added:** `m_short_floor = Σ(|short mv| × short_rate) / gross`, using §3a-5 decimals. Then
  `m = max(existing m, m_short_floor)`. This can only raise m and so lower K.
- **Fixture tests:**
  - long only;
  - long + short mixed;
  - adding a short never raises K;
  - `"30"` units.
- **Admission:** the K limb counts `abs(market_value)` for held lots plus the pending entries' notional (unchanged
  pattern, L888-913).

### 3l. Operator-triggered short self-test — `run_short_selftest.py` (v4)
Claude cannot place orders, so this is a bot-side script the operator starts on OCI. It is never run by Claude or
by tests (the test process refuses the broker). It runs on paper (`paper=True` is asserted) during RTH, and its
steps are:
1. Pick the most liquid ETB, shortable, non-SSR name in the Swing pool that no tier holds or reserves. Reserve it
   in the §3c lock as tier `selftest`.
2. Short 2 shares with an IOC limit at the bid. This records whether IOC is accepted and the fill.
3. Place two 1-share GTC buy-stop slices at price × (1 + the stock's worst-ever up day). That level is unlikely to
   trigger during the test. Record acceptance, status and the account `buying_power` before and after each order.
4. Slice beside a resting entry (Harris): place a 1-share `sell_short` DAY limit far above the market (it will not
   fill), then a third buy-stop slice. Record whether Alpaca accepts it, or rejects it as a wash trade / 40310000.
   Then cancel-confirm that limit.
5. Cancel-confirm all slices. Cover the 2 shares with a marketable limit buy. Verify the broker is flat and the
   ledger is zero. Release the reservation.
6. Write the result to `data/state/short_selftest.json`: PASS only if steps 2, 3 and 5 succeeded. Step 4's result is
   recorded either way. Every order id and the raw broker response are logged to `trade_events.jsonl`.

Any failure → FAIL, and everything opened is closed through §3d. Short entries in every tier stay disabled until
one PASS exists. If step 4 shows a stop beside a resting entry is rejected, that confirms the §3d-4 "entry terminal
first" rule, which the design already requires. The test costs the round-trip spread on 2 shares.

### 3k. Logging
Every transition (reserve → submit → fill → stop → cover → release) logs:
- direction, signed broker qty before/after, ledger qty before/after;
- reservation holder, order ids and statuses, and the stop id / status / qty;
- normalized long/short rates with their raw strings;
- borrow status and source;
- the earnings date with its source, the mode, and the flatten deadline;
- `worst_up` and `report_gap` with their sample counts, history start and source;
- the gross-cap components;
- `slippage_vs_stop` and `gap_loss_beyond_stop`.

## 4. Rule B / Rule C
- **Rule B delta:**
  - per-trade size: zero (the same rules; shorts get the exploration r until their own fills exist);
  - frequency: non-zero;
  - concurrency / overnight: non-zero;
  - → RISK-PATH, with full cold board + masked-loss seat + GAI + Codex.
- **Rule C:** the edge replay was waived by the owner. Mechanical correctness is proved by §6.
- **Review point:** each tier's short side is reported to the owner at 30 closed lots with its mean R and a confidence
  interval. 30 is a review checkpoint, not a power claim.

## 5. Static numbers
- No new tuned number.
- Reused existing rules: the `SWING_BREAKOUT_*` set, QHM `_ATR_MULT` / `_HARD_FLOOR_PCT`, per-pick gate percentages,
  `DAYTRADE_PROMOTION_*`, `INTRADAY_*`, `KELLY_MIN_SAMPLE_SIZE`, `SWING_BREAKOUT_MIN1_RISK_PCT`.
- Broker facts: $2,000; $5 / 30%; $2.50 / 100%.
- Measured per stock at runtime: `worst_up`, `report_gap`.
- **Flags:** the `(2 − gate_pct)` reflection is derived from an existing number. The 30-lot review point reuses
  `KELLY_MIN_SAMPLE_SIZE`.

## 6. Full-path simulation (mocked broker; the test process refuses the real broker)
1. Swing short: setup → IOC `sell_short` fills 6 of 10 (terminal) → one slice for 6. QHM: tranche 1 fills 4 → slice
   A (4); tranche 2 fills 6 → a SECOND slice B (6); slice A is untouched. Trend-break cover: both slices
   cancel-confirmed → buy 10 → P&L positive for a falling stock (entry 100, exit 90, qty 10 → +$100).
2. Stop elected during a cover: cancel UNKNOWN → no second buy; stop fill booked; remainder covered.
3. The broker read alone shows +5 after a cover, with no fill proof → `symbol_quarantined` (clear rule `operator`) +
   page; NOTHING is sold. The proven case is §6-18(a).
4. Lock race: Swing reserves a short and Day tries a long before any fill → refused. Restart rebuilds the
   reservations from open orders.
5. Eligibility: each failing field, and malformed margin strings → skip.
6. Earnings: UNKNOWN date → no overnight short. Mode A resize; unmeasurable gap → cover before the report. Mode B
   flatten in the window.
7. QHM short: tranches, buy stop, trailing lock, rally-add refused, gross cap with mixed directions, the stray sweep
   leaves its own orders alone.
8. Day promotion of a short → adopt → GTC buy → negative ledger transfer → reconcile assert.
9. Borrow lost during RTH → immediate cover. Outside RTH → queued for the open.
10. K fixtures (§3j). Kill switch tripped → no short entries; exits run.
11. 40310000 on a buy stop (held / unknown paths) and on a `sell_short` (the short-block cache).
12. Wash rule: another tier's buy stop rests → short deferred.
13. Restart mid-entry and mid-cover in every tier → client-id recovery, no double buy.
14. OCI read-only probe against the real account and ledger:
    - raw asset fields and their types;
    - margin parsing;
    - the lock rebuild;
    - the account's `maintenance_margin` behaviour.
15. The first paper short per tier is audited end to end (decision → reservation → order → fill → stop → exit → P&L,
    joined by client id). That audit also confirms that two or more buy-stop slices rest together on the paper
    account, a fact that cannot be checked read-only.
16. **Crash between steps (slices).** The process is killed at each point below, then restarted; afterwards Σ live
    slice qty == filled qty, with no duplicate slice and no uncovered share left at the end of the restart reconcile:
    - (a) after an entry partial fill, before its slice is submitted → restart submits the shortfall slice;
    - (b) after the slice submit, before the state write → found by its deterministic client id, not resubmitted;
    - (c) a slice UNKNOWN (timeout) → counted live until resolved;
    - (d) a trailing-lock replace raised mid-flight → the old slice is read back and kept, or the replacement is
      adopted;
    - (e) during a cover, after slice 1 is cancelled but before slice 2 → no cover buy until every slice is terminal.
      Slice 2 still protects its qty. Slice 1's qty is uncovered only inside the cover sequence; it is the next order
      sent.
17. **Lock with two pending same-tier entries.** QHM tranches 2 and 3 are both pending. Tranche 2 expires unfilled →
    release removes only tranche 2's entry; tranche 3 still blocks an opposite-direction Swing entry. Then tranche 3
    fills → `promote` → held.
18. **Flip handling.** (a) Own buy fills prove an excess of +3 and two reads agree → symbol blocked, page, sell 3.
    (b) A read shows long but the fills do not prove it → blocked, page, nothing sold.
19. **Broker flip guard with the ownership flag False.** Each of the following is refused: a `sell_short` while long;
    a `sell` greater than the long; a cover `buy` greater than the short. A `sell` while flat passes.
20. **Derived report cap.** Book fixture with other overnight tails summing to 6% of equity → B = 1% → the short is
    resized. Tails summing to ≥ 7% → no report-crossing short. A pool-wide gap larger than the stock's own gap → the
    pool-wide gap is used (under D7-a only when the stock's history is shorter). Resize order: two lots with equal
    products are taken by symbol; B is recomputed after the first cover fill; the pass stops once B ≥ 0.
21. **Quarantine.** (a) Quarantined with zero entries → `reserve` refuses for both directions. (b) Quarantined with a
    held entry → same-direction `reserve` refuses, and the lot's covers and stops still run. (c) Operator clear →
    `reserve` works again. (d) `reconcile_2_cycles` → one failing cycle resets the count; two clean cycles clear it.
    (e) Restart → the quarantine is reloaded and not rebuilt away. (f) Lock file unreadable → treated as
    quarantined.
22. **Slice rejected or unacknowledged** → the entry stops adding, the tier's short flag is set off, and a page is sent.
    (a) v5 mixed case: 10 short, slice A (4) accepted, slice B (6) rejected → a gap cover for exactly 6 is submitted;
    slice A is NOT cancelled; the lot ends as a 4-share short with a 4-share live stop; the 6-share cover P&L is booked
    to the tier. (a2) v7 ladder: a fresh G-slice for 6 accepted → no cover. G-slice rejected → regular-hours market buy
    fills 6 → 4-share short. Market buy rejected with 0 filled → next attempt; 2 of 6 fill → 4 recomputed and retried.
    Every attempt zero-filled up to the cap (halt fixture) → the cycle completes (no hang), `unprotected_short=6`
    logged, symbol quarantined, ONE page; the next cycle restarts at the G-slice; a third cycle with the same qty pages
    nothing new. (a4) v8: short 10 with slices A(4)+G(6); a cover fills 3 → held 7, slices 10 → G is cancel-confirmed,
    slices now 4 < 7 → one new slice for 3 → slices == 7. An earnings resize from 10 to 6 → same trim to 6. No state
    has Σ live slices > held after the cycle. (a5) v9 resize 10 → 6 with A(4)+G(6): G cancel-confirmed (Σ 4 ≤ 6), one
    slice for 2 (Σ 6), market buy 4 fills → held 6 = Σ 6. Same with the buy 0-filled to the cap → `resize_incomplete`
    paged, Σ 6 < held 10 → ladder adds protection for 4 the same cycle. (a6) v10: the 2-share re-slice is rejected →
    buy 6 (held 10 − Σ 4) → held 4 = Σ 4, `resize_overshoot` logged; no share held without a live stop. (a3) Lot record without direction → booking refused, page. (b) Slice B UNKNOWN → no buy submitted; symbol quarantined; next cycle B found live → adopted, nothing
    bought; or B found absent twice → gap cover for 6. (c) Any buy UNKNOWN → step 2b refuses.
23. **Rule 201.** Today's low ≤ 0.9 × prior close → skip. Yesterday's low triggered it → skip all day today. Bars
    unreadable → skip. A held short's cover is unaffected. v5 cases: a 09:29 ET extended-hours print below the threshold
    → skip (conservative, logged `ssr_active src=ext_hours`); a 10:15 ET regular-hours print below it → skip at 10:16
    from the one-minute bars (before any daily bar exists); a Monday trigger → skip all Tuesday; a split day → the low and
    prior close share one adjustment basis.
24. **Entry terminal first.** IOC accepted → a stop is placed only after the terminal status. IOC rejected →
    `_settle` cancel-confirm, then the stop. Remainder cancel UNKNOWN → no stop beside a working entry, then resolved
    by client id.
25. **Self-test gate.** No `short_selftest.json`, or a FAIL → no short entry in any tier. A PASS → shorts allowed.

## 7. Owner decisions (reviewer positions after round 2)
| # | Decision | Design | Codex | GAI |
|---|---|---|---|---|
| D1 | Opposite directions on one symbol | Refuse when held or pending by any tier (§3c); no forced close of another tier | Refuse; a forced close would mix tier results and recreate the flip race | Refuse; the reservation system is preferred over forced closes |
| D2 | QHM rally-add for shorts | **v12 owner: ON, mirror of the long dip-add** (see Owner directions 2026-10-30) | Off (superseded) | Off (superseded) |
| D3 | Swing short sizing | **v12 owner: REJECTED as a shadow.** Replay-derived short Kelly from day one, updated by live fills | superseded | superseded |
| D4 | Hard-to-borrow names | **v12 owner: defined** as Alpaca's live per-symbol borrow status (see below) | — | — |
| D5 | Borrow lost on a held short | Immediate cover during RTH; queued for the first RTH minute outside it, stop kept | Same | Same |
| D6 | Earnings | Mode A: hold through with the derived report cap; cover if the gap cannot be measured; no overnight short without a known date | Mode A | Mode A |
| D7 | Pool-wide report gap | **v12 owner: neither D7-a nor D7-b. Each stock's own measured gaps only; no pool** | — | — |

Board panel on D7: Thorp is for D7-a; Taleb is for D7-b. The owner decides.

## Changes after review round 1
| Finding | Fix in v2 |
|---|---|
| Codex 1, short held through a report | §3e: an UNKNOWN earnings date blocks every overnight short. Mode B flatten is designed and selectable. The default is put to the owner as D6, because the owner's text reads as a request for the ability to hold through a report. Mode A now resizes for the stock's own measured report gap or covers before the report. |
| Codex 2, margin units | §3a-5: both fields parsed to decimals with the broker.py L2930-2950 rule; validation and fixtures listed. |
| Codex 3, pending entries in the opposite-direction guard | §3c: a persisted, file-locked reservation taken before submit; pending orders on both sides count; rebuilt from open orders at startup. |
| Codex 4, a cover flipping long | §3d: single-buy-reservation, cancel-confirmed, terminal-state rule, partial-fill coverage, flip quarantine and flatten. |
| Codex 5, QHM gross vs net | §3g: unsigned `qty_filled`, per-position sum (L6989-7013), pending notional added, mixed-direction test. |
| Codex 6, K not implementable | §3j: the existing equations are cited (compute_k_raw is already sign-aware); one explicit added term, which can only lower K; fixtures. |
| Codex 7, signed-quantity conventions | §3a: signed only in ledger / transfers / snapshot / reconcile; abs + direction everywhere else; one P&L helper; worked example in §6-1. |
| Codex 8, one-share tail | §3h: tail language corrected; a one-share short must also fit its measured `p99_up` / report gap within `SWING_BREAKOUT_MIN1_RISK_PCT`; gap loss logged. |
| Codex nit, close vs high/low | The long breakout is close-based (L154); the close-based mirror is exact. No change. |
| Codex nit, `foreign_opposite_stop` side | §3f: semantics stated (L53) and a test added. |
| Codex nit, account status and buying power | §3b. |
| Codex nit, borrow status logging | §3i and §3k: raw status and source logged; no buy-in claimed. |
| Codex D2, D3 | Adopted (§3g, §3f). |
| GAI 1, `_reprotect` direction | §3d-6: the signed broker position is read. v1 §3b already specified the short branch. v2 makes the signed read and the flip handling explicit. |
| GAI 2 / D1, tier priority | Not adopted, for these reasons. A forced close: (a) books a lower tier's P&L from another tier's signal, corrupting per-tier edge data, which is the account's main goal; (b) puts a cover and an opposite entry on one net position at the same moment, which is the flip race in Codex 4; (c) breaks the tier rule "only this tier's qty, never a whole-symbol close" (swing_breakout_manager L1289-1291). The priority need is met without a close: configured QHM picks already reserve their symbols against Swing (L766-771), and §3c extends this to every tier. |
| GAI 3 / D5, lost borrow | §3i: immediate cover during RTH; queued for the open outside RTH, where only limit orders are accepted (Alpaca docs). |
| GAI 4, Day-tier shorts in the guard | §3c: the guard counts every tier's signed qty, Day included. The floor stays protected-tiers-only with a non-negative QHM term. The guard is dormant (config L749), so the §3c lock is the live control. |
| GAI D3 (pooled Kelly) | Codex's split adopted, so a new short book is never sized from long-side results. |

## Changes after review round 2
| Finding | Fix in v3 |
|---|---|
| Codex: a partial-fill stop resize left a short without a stop | §3d-4: independent buy-stop slices per fill increment, with deterministic client ids; never cancelled to grow; coverage rule at every cycle and restart. Replace-to-grow was not chosen because Alpaca's `pending_replace` behaviour is unverified; slices do not depend on it. Crash-between-steps cases in §6-16. |
| Codex: the lock could not hold several same-tier pending orders | §3c: entries keyed by client order id / lot id; release removes one entry; two-pending-tranche test in §6-17. |
| GAI: a broker flip auto-sold on a possibly false read | §3d-5: symbol set `trading_blocked` + page in every case. The excess is auto-sold only when proven by the lot's own buy fills AND two agreeing reads. Otherwise it waits for the operator. Reason: a sign-flipped read has happened in this repo (orphan_manager L1340), while a fill-proven excess has no stop and no owner. Test in §6-18. |
| GAI: fixed 5% notional cap per report | §3e: a derived cap. The report loss fits within the remaining 7% kill budget after the other overnight tails, plus a pool-wide worst report gap so one stock's history cannot understate a buyout gap. No fixed percentage added. Test in §6-20. |
| GAI: the flip classification depended on the dormant guard | §3c: a broker flip guard in `submit_limit_order` / `submit_market_order`, independent of `OWNERSHIP_GUARD_ENFORCE`. Test in §6-19. |
| Codex nit: first-RTH-minute borrow cover | Kept as D5; the stop rests until the cover. |
| D1–D6 | §7 records both reviewers' positions. |

## Changes after the confirmation round and board panel (v4)
| Finding | Fix in v4 |
|---|---|
| Codex: the flip block was neither in the lock schema nor checked by `reserve` | §3c: a persisted `symbol_quarantined` object (reason, evidence, ts, clear rule, clean_cycles). It is checked first in `reserve`, kept through release and restart, and an unreadable file counts as quarantined. Tests in §6-21. |
| Codex: multi-slice buy stops not proven on the real broker | §3l: an operator-triggered self-test on OCI paper (2-share short, two slices, buying power, slice beside a resting entry); shorts stay disabled until one PASS. §3d-6a: a fallback that does not depend on it — a rejected or unacknowledged slice → entry stops adding, the uncovered qty is bought back at once for exactly that qty, the tier's short flag goes off. Tests in §6-22 and §6-25. |
| Codex: §6-1 and §6-3 described v2 behaviour | §6-1 now shows a second fill creating a second slice. §6-3 now shows a read-only flip: quarantine + page, nothing sold. |
| Codex nits | Heading is v4. Deterministic resize order with recomputation after each cover (§3e). Q19 lists D1–D7. The name is `symbol_quarantined`, not `trading_blocked`. |
| Thorp: pool-wide report-gap condition | §3e: D7-a (the K condition at L163-170) and D7-b (Taleb: max everywhere) both recorded. D7-a recommended, with reasons; the owner decides. |
| Thorp + Taleb: where the flip excess is booked | §3d-5: it goes to the lot's tier P&L and short Kelly sample, tagged `flip_excess`. |
| Thorp: Kelly from short fills only after 30 | Unchanged from v3 (§3f, D3). |
| Harris: Rule 201 | §3b: computed from T1 bars, skip while active, Alpaca handling unverified and not relied on. Test in §6-23. |
| Harris: stop beside a still-working entry | §3d-4: IOC short entry, or the existing cancel-confirm before the first slice. Test in §6-24. Buying power and slice-vs-entry are measured in §3l. |
| Taleb: worst-ever up day | §3f and §3h: `worst_up` (as K uses at L159) replaces p99 in the gap check and the one-share rule. |
| Cross-references | Q15 → §3k; Q17 / Q22 → §6-14 plus §3l; Q19 → D1–D7. |

## Required questions (short answers)
- **Q1 goal:** two-sided exposure in Swing, QHM and the overnight Day carry. It collects short-side data toward $25K.
- **Q2 trade count:** adds short entries; no long trade is removed. Cross-tier netting can defer a few.
- **Q3 simpler way:** inverse ETFs would avoid borrow, but the owner asked for shorts; tiers trade single names.
- **Q4 overlap:** Track B (design) already plans shorts; the Day tier already shorts intraday.
- **Q5 static:** §5; no new tuned numbers; mirrored grandfathered rules flagged.
- **Q6 forward pass:** next, a direction-specific Kelly and a borrow-fee-aware cost model.
- **Q7 silo:** the borrow status feeds sizing, the Day promotion and exits. The worst_up and earnings gap feed the shared
  gap check.
- **Q8 evidence:** code line citations §1; Alpaca facts §2.
- **Q9 simulation:** owner-waived edge replay; mechanical full-path list §6.
- **Q10 profitable>perfect:** ships at the mirrored rules; refined from fills.
- **Q11 size/freq/conc:** size 0; frequency and concurrency non-zero → risk-path.
- **Q12 safety envelope:** a stop on every short (GTC buy), never cancelled for a report; the §3d single-buy state
  machine; the §3c lock; kill switch; `paper=True`; fail-closed eligibility. A stop does not bound a gap (§3h).
- **Q13 missing data:** no asset / borrow / margin / bars / earnings date → no overnight short entry. An unreadable
  lock file blocks the entry. Exits are never blocked.
- **Q14 shadow:** none.
- **Q15 logging:** §3k.
- **Q16 post-ship proof:** the first-session audit and replay of every short decision.
- **Q17 runtime proof:** the OCI read-only probe (§6-14) and the §3l self-test PASS before "live".
- **Q18 anomaly:** a held short with a lost borrow, or a broker side flip → page + rule.
- **Q19 owner approval:** this record, D1-D7 (§7).
- **Q20 self-QA:** self-audit before the cold-2nd.
- **Q21 broker in tests:** mocked; the test-process refusal stays.
- **Q22 fail-closed probe:** §6-14 (read-only) plus §3l (write, operator-triggered).
- **Q23 full-path sim:** §6.
- **Q24 absence claims:** "no borrow check exists" is grep-verified this turn; the forced buy-in is UNVERIFIED.
- **Q25 stats measured:** worst_up and the earnings gap are measured per stock from T1 bars at runtime; none quoted from
  memory.
- **Q26 data tier:** T1 Alpaca (bars, asset, quotes), T2 FMP (earnings, grades).
- **Q27 output:** the existing state files (atomic) + `trade_events.jsonl`.
- **Q28 threading:** unchanged hooks (run_cycle single-threaded; Day runner; QHM in the main bot). The eligibility read
  is cached per cycle.
- **Q29 value rank:** owner-ordered hard stop.
- **Q30 whitespace:** borrow monitoring and a short-maintenance-aware K were absent.
- **Q31 OOS:** not run (owner waiver). The first 30 shorts per tier are the confirmation.
- **Q32 look-ahead:** signals use closed daily bars only, as on the long side.
- **Q33 costs:** ETB $0 borrow (verified); HTB excluded; margin interest unchanged.
- **Q34 alpha/beta:** short exposure partly hedges the long book's beta, which is stated, not claimed as alpha.
- **Q35 tails:** a short's gap loss is not bounded. It is reduced by the stop, the worst_up / report-gap resize, the
  one-share tail rule and the K limb, and it is logged as `gap_loss_beyond_stop`.
- **Q36 capacity:** megacap ETB names; trivial at $25K.
- **Q37 power:** 30 is a review checkpoint with a reported confidence interval, not a power claim.
- **Q38 decay:** the per-tier reversal review at 30 lots.
- **Q39 Kelly from fills:** short-only fills after 30 closed shorts; exploration r before that (D3).
- **Q40 outcome check:** per-tier short R in the weekly report.
- **Q41 expert:** O'Neil / Weinstein Stage-4 breakdown (mirror of the breakout); Harris on short-sale mechanics.
- **Q42 raw data:** Alpaca fills and orders.
- **Q43 professional method:** a stop-based mirror with borrow pre-checks and a locate rule, as in prime-broker
  practice.
- **Q44 independent evidence:** the Alpaca docs and the live `get_asset` read, not reviewer claims.

## Changes after confirmation round 2 (v5)

| Finding | Fix in v5 |
|---|---|
| Codex REJECT: with slice A (4) live and slice B (6) rejected, step 2 (`reserved == 0`) forbade covering the 6 without cancelling A, leaving 6 shares without a stop | §3d-2b: a protection-gap cover for exactly `held − confirmed-live slices − active covers`, never cancelling live slices, refused while any buy is UNKNOWN. §3d-6a routes through it. Test §6-22(a)–(c) |
| Codex: UNKNOWN slice both "counts as live" and triggers an immediate buy | §3d-6a: UNKNOWN → quarantine + page + per-cycle client-id resolution; adopt if live, gap-cover only after two not-found reads or a rejection |
| Codex nit: fallback-cover P&L booking implied | §3d-2b: booked through `signed_pnl` and the tier's partial-exit path, tagged `slice_rejected_cover` |
| Codex nit: Rule 201 bar definition | §3b: live one-minute lows from 04:00 ET (extended hours included, errs toward skipping), one adjustment basis, D and D+1 by the Alpaca calendar. Test §6-23 |
| GAI | Round-2 response was truncated after its header (no findings text returned); re-asked in round 3 |

## Changes after confirmation round 3 (v6)

Codex raised one concrete input on v5; fixed:

| Finding | Fix in v6 |
|---|---|
| Codex: an unfilled marketable-limit gap cover counted as "active cover" left 6 shares with no stop indefinitely | §3d-2b: the gap cover is terminal inside the same cycle — a market buy (day) in regular hours, waited to terminal status; an IOC limit at the fresh ask outside regular hours, resubmitted for any unfilled remainder in the same cycle, with a page. Test §6-22(a2) |
| GAI nit: flip guard vs `broker.py` L752 ownership block | Both refuse the same order; neither changes state on refusal, so a double refusal leaves the order refused and the state unchanged. The flip guard runs first and logs the reason |

## Changes after confirmation round 4 (v7)

| Finding | Fix in v7 |
|---|---|
| Codex: a regular-hours market cover rejected with 0 filled had no resubmit rule; outside regular hours the IOC resubmit was unbounded, so a halted or illiquid symbol hung the cycle with shares unprotected | §3d-2b gap protection ladder: fresh stop slice first, then capped cover attempts (both sessions), then an explicit `unprotected_short` state (quarantine, logged each cycle, restart next cycle). The cycle always completes. Test §6-22(a2) |
| GAI: page every cycle | Pages deduplicated by qty change |
| GAI: `_reprotect` with a positive broker read | Already covered: §3d-6 protects "only when the broker qty is < 0"; broker > 0 routes to step 5, so `min(want, -broker_qty)` is never computed on a positive read |
| GAI: Rule 201 vs the bot's buy stops | Rule 201 restricts short SALES; covers and buy stops are buys and are not price-tested. §3b already states "Existing covers and stops are unaffected" |
| GAI nit: direction defaulting to long | §3d-2b: direction from the entry fill side; a missing direction refuses booking and pages |

## Changes after confirmation round 5 (v8)

| Finding | Fix in v8 |
|---|---|
| GAI: the coverage rule compared live slices to the entry's filled qty, so after a partial cover or an earnings resize the stops could exceed the short and flip it long if elected | §3d-4: coverage is checked against `held`; excess slices are trimmed newest-first by cancel-confirm, then re-sliced for the exact difference. Test §6-22(a4) |
| GAI nit: earnings resize leaves the stop at the old qty | Same trim rule; §3e resizes reduce `held`, and the next coverage check trims the slices |
| GAI: flip guard vs the legacy L757-759 classification | Already in §3c: the guard "runs BEFORE the flag-gated guard block" and "replaces the L757-759 classification". Test §6-19 |
| Codex nit: a GTC stop placed outside regular hours is not executable until the next session | §3d-2b step 1 wording corrected; the log records it |

## Changes after confirmation round 6 (v9)

| Finding | Fix in v9 |
|---|---|
| Codex REJECT: no transition could buy back part of a lot (resize 10 → 6): §3d-2 closes the whole lot, §3d-2b only covers unprotected shares | §3d-2c partial resize cover: trim slices to the target first (keeping the kept shares protected), buy exactly `held − T`, book the fills; an unfilled remainder is re-protected by the coverage rule the same cycle. §3e points to it. Test §6-22(a5) |
| GAI: a slice found rejected after being UNKNOWN could race a gap cover | Already ruled out: §3d-2b is "Allowed only when NO buy of this lot (slice or cover) is UNKNOWN", and §3d-6a adopts or gap-covers only after the client-id lookup resolves it |
| GAI nit: trim cancel must be terminal before the re-slice | §3d-4 states it |

## Changes after confirmation round 7 (v10)

| Finding | Fix in v10 |
|---|---|
| GAI: if the re-slice for `T − Σ live` is rejected, kept shares are unprotected | §3d-2c: the resize buy waits for Σ live == T; a rejected re-slice turns the buy into `held − Σ live` (logged `resize_overshoot`), so every remaining share has a live stop. Test §6-22(a6) |
| Codex nit: "protected throughout" was imprecise during the replacement | §3d-2c wording: protection is restored before the resize buy; during the replacement only Σ live shares have a resting stop |
| GAI nits: failed resize re-protects all of `held`; log resting vs active protection | Coverage rule compares to the full `held`; every record carries `active` / `resting` / `none` |

## Changes after confirmation round 8 (v11)

| Finding | Fix in v11 |
|---|---|
| Codex: §3d-2c still said "then buy exactly `held − T`" after the overshoot rule, so a rejected re-slice could buy 4 and leave 2 shares without a stop | §3d-2c: a single quantity, `resize_buy_qty = held − protected_target` (T if the added slice is live, Σ live if rejected); the old sentence is removed |
| GAI: the flip guard must not sit inside the ownership-flag block | §3c: stated at function level, not nested; test §6-19 runs with the flag False |
| GAI: Rule 201 extended-hours over-flag reduces data collection | Kept. The window starts 04:00 ET, and only a ≥10% drop below the prior close in extended hours over-flags, which skips one name for D and D+1. Entering a short that is actually restricted is the worse error. Measured frequency is reported after the first 30 sessions (`ssr_active src=ext_hours` count) |
| GAI nit: log target vs actual | `resize_overshoot` carries `target_T` and `actual_T` |

## Confirmation round 9 result (2026-10-30)

Codex APPROVE on v11. GAI raised two points (a resize leaving held at 10; no slippage cap on the market cover); after a
counter-prompt with §3d-1's `held` definition, test §6-22(a6), and the stop-order parity facts, GAI withdrew both and
returned APPROVE. Board panel (Thorp, Taleb, Harris seats) reviewed v4; the v5-v11 changes are confined to §3d stop
and cover sequencing, §3b Rule 201 bar definition, and §3c guard placement. Owner decisions D1-D7 (§7) remain open.

## Owner directions 2026-10-30 (v12) — supersede §7 D2, D3, D4, D7; re-review by GAI + Codex owed before build
- **D2 (rally-add for QHM shorts): ON.** QHM is averaged up or down on directional conviction (CLAUDE.md paper-account
  frame). A QHM short adds on a rally against it using the mirror of the long dip-add: the stock's OWN rally zones
  (its own upper return quantiles, no pooled floor), the same ceiling, quarterly count and spacing rules, and the same
  stop-adding line (a rally past the first-entry stop-adding line halts adds and pages).
- **D3 (sizing): the "exploration r until 30 closed shorts" rule is withdrawn.** The owner ruled it a shadow in other
  words. Swing shorts are sized from day one by the short-side Kelly fraction measured on the front-loaded short replay
  (win rate and payoff from the replay's short trades), bounded by the existing per-lot stop risk, K and gap checks.
  Each closed short (Alpaca fills) updates the estimate as it arrives: replay prior blended with live results, weighted
  by trade count, no sample-count switch. No literal trade-count threshold appears in the code.
- **D4 (hard to borrow): defined, broker-derived, not a threshold we set.** At order time the bot reads the Alpaca
  asset record (T1) for the symbol: `shortable` must be true and `borrow_status` must be `easy_to_borrow`. Anything
  else, or an unreadable record, means no new short (fail closed). The same read on a held short drives D5 (borrow
  lost → cover). Example read 2026-10-30: SMCI shortable=true, easy_to_borrow=true, borrow_status=easy_to_borrow.
- **D7 (report gap): each stock's own actual post-report gaps; the pool is never substituted.** `report_gap` = the
  stock's own largest adverse (upward) post-report one-day move, from Alpaca daily bars on its own report dates (T1
  bars, dates from EARN-SOURCE-1029). Alpaca daily bars reach back to 2016, roughly 40 reports for a listed large cap
  [count to be measured per name in the build]. A stock with NO measurable own report (new listing, or dates unknown)
  gets no report-crossing short: it is covered before the report (fail safe), never sized from other stocks' gaps.
  - Owner: "Mag7 all have dynamically different gaps. The static gate should've caught this." The static gate scans
    numeric literals; a pooled substitution carries no literal, so it passed. The same pattern is LIVE in the dip-zones
    module (lines 84-101): Rung A = the deeper of the stock's own 10th percentile and the POOLED 10th percentile. On
    2026-10-30 NVDA, GOOGL, GE, TSLA, AMZN, META and AAPL all carried the identical pooled Rung A (-5.06%).
    Follow-up design: DIP-ZONES-OWN (own-stock zones only) plus a gate check that flags pooled substitution.
