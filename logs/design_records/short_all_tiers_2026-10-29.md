# Short capability for Swing, QHM and the Day tier's overnight carry — SHORT-ALL-1029 (signed: Claude, 2026-10-29) — DRAFT v1, not approved

Approval-ID: SHORT-ALL-1029
Covers: execution/swing_breakout_manager.py, execution/quarterly_hold_manager.py, execution/day_promotion.py,
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

## 3. Design

### 3a. Shared pieces
1. **`broker.get_short_eligibility(symbol)`** (new, read-only).
   - Returns `{shortable, easy_to_borrow, borrow_status, margin_requirement_short}` from `get_asset`, or None on any
     error.
   - It never raises and is cached per cycle.
   - **Fail-closed:** a short ENTRY requires `shortable and easy_to_borrow`, the account `shorting_enabled`, equity ≥
     the Alpaca $2,000 rule (a broker fact, not a tuned threshold), and the symbol not in `get_short_blocked_symbols()`.
     None → skip, logged.
   - HTB is excluded in v1: a locate is required and no locate API is wired.
2. **Short maintenance:** sizing and the gap check use `max(asset margin_requirement_short, _short_maintenance_rate)`.
   Reuse the Day-tier helper; move it to a shared module or import it.
3. **Daily borrow re-check of held shorts** (every cycle, read-only).
   - If a held short's asset turns non-ETB / non-shortable → page. The lot keeps its stop.
   - The tier's managed exit closes it at the next RTH exit window (reason `borrow_lost`).
   - Rationale: HTB fees are daily and a buy-in is possible. This is an unverified Alpaca behaviour, so we exit first.
4. **Cross-tier netting: FORBID opposite directions on one symbol across tiers.**
   - A short entry is refused while any tier's ledger qty on the symbol is > 0, or any tier has a pending buy. A long
     entry is refused while any tier holds it short.
   - Reason: one net Alpaca position per symbol.
     - Example: a Swing short of 5 AAPL against a QHM long of 10 would net to a long of 5.
     - The short's buy-stop would then "cover" by buying more of QHM's long.
     - The QHM sell-stop would sell through the Swing short.
     - Both ledgers would be wrong, and the never-sell floor would read the wrong qty.
   - F6 names stay hard-blocked for shorts (existing ring-fence, kept).
   - Enforced in the `ownership_guard` short branch, the single choke point, plus a pre-check in each tier.
5. **`ownership_guard.py`**
   - Replace the blanket short REJECT (L846-851) with: REJECT a short if (a) the symbol has F6 qty > 0, or (b) any
     tier's ledger qty on it is > 0, or (c) the ledger is unreadable → fail closed for shorts.
   - The protected floor uses `max(0, qhm_qty) + forever6_qty`, so a negative QHM qty never lowers or negates the
     floor.
   - A buy-to-cover is never floor-gated (`_floor_bound_stop_qty` already passes buy stops).
6. **K limb / Invariant #11**
   - Shorts count at |notional| in K (already true, L235).
   - `overnight_capacity` adds short maintenance (the §3a-2 rate) to the margin-derived K input, so shorts can only
     tighten K.
   - Limb 1 (swing/day ≤ 100% of equity) uses |notional| for promoted Day shorts.
   - QHM shorts are excluded from the swing limits (as QHM longs are) but count in K.
7. **Kill switch:** unchanged. It is fills-based and direction-agnostic. It halts NEW entries, shorts included.
8. **Gap check (`gap_resize`) mirrored**
   - `p99_up` = the stock's own 99th-percentile one-day RISE, measured like `p99_down`.
   - If the next FMP (T2) report falls inside the lot's expected hold, use the larger of `p99_up` and the stock's own
     worst post-report one-day rise over its history (Alpaca T1 daily bars around FMP dates).
   - Unknown → that measure is treated as unavailable, and the existing rule applies: no p99 → resized to the floor
     share.
   - `short_value` is passed alongside `long_value`.

### 3b. Swing tier
- **Signal (mirror of the 55-day breakout, owner-stated):**
  - `breakdown_signal`: last close < lowest close of the prior 55 sessions AND last close < SMA200 → a pending SHORT
    setup at that prior 55-day low.
  - Entry needs the session open and the live **bid** at or below the level. The setup is dropped by the same
    invalidation rule as the long side, mirrored.
  - Same universe ranking (`rank_universe`), same `SWING_BREAKOUT_TOP_N`.
- **Entry:** a marketable limit `sell_short` at `bid × (1 − SWING_BREAKOUT_LIMIT_SLIP_PCT)`. The allocator `live_admit`
  is called with side `"sell_short"` (the allocator already accepts it, tier_capital_allocator.py L649/L751).
  `foreign_opposite_stop(symbol, "sell", TIER)` for the wash rule.
- **Stop:** GTC **buy** stop at `fill + stop_distance` (same `stop_distance` function, ATR floor unchanged).
- **Exit (mirror):** trend_break when live > the prior 20-session HIGH; time exit at `SWING_BREAKOUT_MAX_HOLD`. The
  `_near_close` window is unchanged.
- **Sizing:** identical `size_order` (r from Kelly, ≤ `SWING_BREAKOUT_MAX_RISK_PCT` at the stop, K-scaled, min-1-share
  rule), then the mirrored gap check.
- **Kelly:** shorts pool with longs in the tier Kelly (R is direction-free) until 30 closed shorts
  (`KELLY_MIN_SAMPLE_SIZE`), then a direction-specific r. Open decision D3.
- **State:** each lot record gets `direction`, with default `"long"` for existing lots.
  - `_reprotect` protects `min(want, |broker short qty|)` only when the broker side is short.
  - `_find_resting_stop` matches buy-side stops for shorts.
  - `_book_exit` P&L = `(entry − px) × qty`.
  - `_book_gone` and `_close_lot` are mirrored (close = buy-to-cover, never a whole-symbol close).
- **Slots:** shorts share `SWING_BREAKOUT_MAX_SLOTS`.
- **Kill flag:** `SWING_SHORT_ENABLED` (new). An explicit False stops new shorts; held shorts keep their stops and
  exits.

### 3c. QHM
- **Trigger:** the weekend QHM thesis routine (`scripts/qhm_thesis.py`, a DRAFT that the owner and Claude finalize
  in-session) gains a per-candidate `direction` verdict.
  - Grounds: an FMP grades-consensus downgrade, price below its 50-day average and off its 20-day high (already
    computed), and a bear thesis from the 4-lens review.
  - A short QHM position exists only when the finalized pick in `quarterly_holds_config.json` carries
    `"direction": "short"`. That is the owner's weekend decision, same as long picks.
  - `main.py` L846 passes `direction=_pick_cfg.get("direction", "long")`.
  - An unknown value → the pick is skipped and paged (fail closed).
- **Entry gates, mirrored:**
  - Day-1: `current_close < prior_close × (2 − gate_pct)`, the symmetric reflection of the existing per-pick
    `entry_day_gate_pct`.
  - Day-3: skip if price > tranche1 × (1 + `day3_reconfirm_pct`).
  - Same tranches, same `target_equity_pct`, |notional| in the 20%-per-name / 40%-aggregate caps
    (`_qhm_total_notional` → abs).
- **Stop:** `min(avg + 2.5 × 14-week ATR, avg × (1 + _HARD_FLOOR_PCT))`. This mirrors `_design_stop_price` and uses
  the same existing constants; no new number. It is a GTC buy stop.
- **Trailing lock:** mirrored (lowers the buy stop).
- **Dip-add:** mirrored as a rally-add (adds on a rise, same caps / spacing / quarter count). Open decision D2.
- **Earnings:**
  - The pre-earnings stop cancel is FORCED OFF for shorts, regardless of the flag: a short never goes through a report
    unstopped.
  - The earnings trim is mirrored: a partial buy-to-cover at the existing computed target.
  - The post-earnings stop path (L2458) is mirrored.
- **All sell-only recognisers** (stray-order cancel L734, stop shape L6833, identity L6866, coverage L6913, room L7082)
  take the protecting side from `direction`. Without this, `cancel_stray_sell_orders` would treat a short's own
  `sell_short` entry as a stray, and the short's buy stop would never be recognised as covering.
- **Owner-add / owner-sale heal / owner trim:** stay long-only in v1 (they detect owner app trades). A short QHM
  position whose qty changes outside the bot → page + operator confirm.
- **Ledger:** QHM short qty is stored negative and excluded from the floor (§3a-5). Kill flag `QHM_SHORT_ENABLED`.

### 3d. Day tier overnight carry (`day_promotion.py`)
Mirror v1 for short lots. Leveraged/inverse ETFs stay excluded.
- P2 profit: `entry − px ≥ DAYTRADE_PROMOTION_MIN_R × (day_stop − entry)`, with `day_stop > entry`.
- P3: daily side `"SHORT"`.
- Swing stop: `entry + INTRADAY_STOP_ATR_MULT × ATR`. Target: `entry − INTRADAY_TARGET_ATR_MULT × ATR`.
- P4 at-risk: `qty × (swing_stop − px)`.
- P5: |notional| in both #11 limbs.
- Borrow eligibility (§3a-1) is checked at the 3:56 decision; non-ETB → normal Day exit.
- Broker side checks (L259/L442/L558) become `== tgt side`.
- Adopt writes the tracker with `direction: "short"` and a GTC `"buy"` stop. The tracker and run_cycle short paths
  already exist (run_cycle L694-806).
- The ledger transfer is recorded with negative qty (`tier_transfers` is already signed).
- Kill flag: `DAYTRADE_PROMOTION_SHORTS_ENABLED`.

### 3e. Logging (Rule D)
- Every entry, skip, exit and stop event carries `direction`, `borrow_status`, `easy_to_borrow`,
  `margin_requirement_short`, `short_maint_rate`, `p99_up`, `next_earnings` (date, source) and the gap-check result.
- Skips name the exact reason (`borrow_ineligible`, `opposite_tier_holding`, `shorting_disabled`, `short_blocked`).
- Exit events carry `slippage_vs_stop` as signed for the short.

## 4. Rule B / Rule C
- **Size / frequency / concurrency delta:**
  - per-trade size: ZERO (same rules mirrored);
  - FREQUENCY: NON-ZERO (new short entries in three tiers);
  - CONCURRENCY / overnight exposure: NON-ZERO (overnight shorts).
  - → **RISK-PATH**: full cold board + masked-loss seat + GAI + Codex.
- **Rule C front-loaded replay:** the owner waived the backtest ("Back testing data is not needed here"). This is
  flagged: the edge of the mirrored signals is unmeasured.
  - The full-path mechanical simulation (§6) is still required; it is a correctness proof, not an edge test.
  - Reversal criterion per tier: the short side's mean R ≤ 0 over its first 30 closed lots → bring to the owner
    (no auto-off).

## 5. Static numbers
- No new tuned number.
- Each threshold is an existing rule mirrored: `SWING_BREAKOUT_*`, QHM `_ATR_MULT` / `_HARD_FLOOR_PCT`, per-pick
  `entry_day_gate_pct` / `day3_reconfirm_pct`, and the `DAYTRADE_PROMOTION_*` / `INTRADAY_*` multipliers. All of these
  are grandfathered statics; mirroring them puts their lines in the diff.
- The no-static scan will therefore flag them: they must be reviewed as diff-touched existing rules, or the mirrored
  calls must reuse the same constant references without new literals.
- Broker facts (not thresholds): $2,000 equity; $5 / 30%, $2.50 / 100% maintenance.
- The 55 / 20 / 200 lookbacks pass to estimators and are existing signal definitions.
- **Flag:** the `(2 − gate_pct)` reflection is a derived mirror, not data-derived.

## 6. Full-path simulation list (mocked broker, no real orders; tests refuse the live broker)
1. Swing short end-to-end: breakdown setup → bid at/below level → `sell_short` fill → buy-stop placed → trend_break
   cover → P&L sign correct.
2. Swing short stopped out by a buy-stop fill, including a gap through the stop.
3. Swing short partial fill; `_reprotect` against broker short qty; broker shows long (flip) → page, no stop.
4. Borrow ineligible / `get_asset` error / account shorting disabled / equity < $2K → skip, logged.
5. Opposite-tier conflict: QHM long + Swing short signal → refused. Swing short + QHM long pick → QHM deferred.
   F6 name → refused.
6. Wash rule: another tier's buy stop rests → short entry deferred.
7. 40310000 on a buy-stop (held_for_orders) → PROTECTION_ALREADY_HELD / UNKNOWN paths. 40310000 on `sell_short` →
   short-block cache.
8. QHM short: config direction short → Day-1 mirrored gate → tranches → buy stop → trailing lock lowers it → rally-add
   → earnings: stop kept, trim covers part.
9. QHM stray-order sweep with a short's own `sell_short` entry resting → not cancelled.
10. Ledger: QHM −10 + F6 0 → floor 0; replay sums; never-sell floor unaffected; restart reconcile adopts nothing.
11. Day promotion of a short lot → hand-off → tracker adopt (short) → GTC buy stop → signed ledger transfer.
12. K limb with shorts: gross counts; maintenance only tightens K; a breach blocks new entries.
13. Held short turns HTB → page + `borrow_lost` exit at the next window.
14. Kill switch trips → no new shorts; held shorts keep their stops.
15. A restart mid-entry / mid-cover in each tier → client-id recovery, no double cover.
16. OCI read-only probe: run the eligibility and gate path against the real broker and ledger (no orders) before
    calling it live.

## 7. Open decisions (owner)
- **D1** Netting: forbid opposite directions across tiers on one symbol (recommended), or allow with priority rules.
- **D2** QHM rally-add (adding to a losing short) mirrored as-is (recommended for parity, same caps), or disabled for
  shorts.
- **D3** Swing Kelly: pooled long+short until 30 closed shorts, then split (recommended).
- **D4** HTB: excluded in v1 (recommended; no locate API wired).
- **D5** A held short that loses ETB: exit at the next window (recommended) vs hold with a page.

## Required questions (short answers)
- **Q1 goal:** two-sided exposure in Swing, QHM and the overnight Day carry. It collects short-side data toward $25K.
- **Q2 trade count:** adds short entries; no long trade is removed. Cross-tier netting can defer a few.
- **Q3 simpler way:** inverse ETFs would avoid borrow, but the owner asked for shorts; tiers trade single names.
- **Q4 overlap:** Track B (design) already plans shorts; the Day tier already shorts intraday.
- **Q5 static:** §5; no new tuned numbers; mirrored grandfathered rules flagged.
- **Q6 forward pass:** next, a direction-specific Kelly and a borrow-fee-aware cost model.
- **Q7 silo:** the borrow status feeds sizing, the Day promotion and exits. The p99_up and earnings gap feed the shared
  gap check.
- **Q8 evidence:** code line citations §1; Alpaca facts §2.
- **Q9 simulation:** owner-waived edge replay; mechanical full-path list §6.
- **Q10 profitable>perfect:** ships at the mirrored rules; refined from fills.
- **Q11 size/freq/conc:** size 0; frequency and concurrency non-zero → risk-path.
- **Q12 safety envelope:** a stop on every short (GTC buy), stops never cancelled for reports, kill switch, `paper=True`,
  fail-closed eligibility.
- **Q13 missing data:** no asset / borrow / bars → no short entry; exits never blocked.
- **Q14 shadow:** none.
- **Q15 logging:** §3e.
- **Q16 post-ship proof:** the first-session audit and replay of every short decision.
- **Q17 runtime proof:** the OCI probe (§6-16) before "live".
- **Q18 anomaly:** a held short with a lost borrow, or a broker side flip → page + rule.
- **Q19 owner approval:** this record, D1-D5.
- **Q20 self-QA:** self-audit before the cold-2nd.
- **Q21 broker in tests:** mocked; the test-process refusal stays.
- **Q22 fail-closed probe:** §6-16.
- **Q23 full-path sim:** §6.
- **Q24 absence claims:** "no borrow check exists" is grep-verified this turn; the forced buy-in is UNVERIFIED.
- **Q25 stats measured:** p99_up and the earnings gap are measured per stock from T1 bars at runtime; none quoted from
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
- **Q35 tails:** a short's loss is unbounded through a gap. It is bounded by the stop + the p99_up / earnings gap
  resize + the K limb.
- **Q36 capacity:** megacap ETB names; trivial at $25K.
- **Q37 power:** 30 closed lots per tier before any edge claim.
- **Q38 decay:** the per-tier reversal review at 30 lots.
- **Q39 Kelly from fills:** D3.
- **Q40 outcome check:** per-tier short R in the weekly report.
- **Q41 expert:** O'Neil / Weinstein Stage-4 breakdown (mirror of the breakout); Harris on short-sale mechanics.
- **Q42 raw data:** Alpaca fills and orders.
- **Q43 professional method:** a stop-based mirror with borrow pre-checks and a locate rule, as in prime-broker
  practice.
- **Q44 independent evidence:** the Alpaca docs and the live `get_asset` read, not reviewer claims.
