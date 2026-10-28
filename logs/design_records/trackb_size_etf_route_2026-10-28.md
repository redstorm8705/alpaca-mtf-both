# Track B size + 2-share ETF route — TRACKB-SIZE-ETF-1028 (signed: Claude, 2026-10-28) — DESIGN, not approved for build

Approval-ID: TRACKB-SIZE-ETF-1028
Covers: config.py, run_day_tier.py, strategy/day_tier_leverage.py, execution/day_trade_manager.py, tests/test_day_tier_track_b_live.py, tests/test_day_tier_affordable_route.py, tests/test_day_tier_leverage.py

Owner direction 2026-10-28 (verbatim):
- "I've stated multiple times that we need more than 2 shares at a minimum. If we can only afford one, we need to buy a
  levered etf given the stocks signal."
- "This needs to increase. I don't want it to continue cutting the share count down. How can we safely increase this?"
- "Yes, both" (take the ETF-route fix and the Track B size change through review and bring the package).

## Problem (measured, 2026-10-28 session; sources: OCI `logs/day_tier_events.jsonl` decision `size.wire_cap_reason`,
`logs/day_tier_runner_cron.log`)

1. **Track B is cut to a fixed $140 of open exposure.** Every non-10/10 Track B entry is clamped so that all open Track B
   notional stays under 15% x 35% of equity (about 5.25%, $139.85 on 2026-10-28), never below one share
   (`execution/day_trade_manager._wired_entry` and `_bounded_entry_qty`, flag `DAYTRADE_TRACK_B_CASH_ONLY`). A 10/10
   entry already skips it (CEO 2026-10-10). Of 17 live Track B entries since 2026-10-09, 12 were 10/10 and 5 were cut:

   | entry | wired before the cap | wired after | live P&L | same exits at the uncapped size |
   |---|---|---|---|---|
   | PLTD 10/16 | 275 | 18 | +$1.27 | +$19.42 |
   | AMZN 10/19 | 18 | 1 | +$3.29 | +$59.22 |
   | PLTR 10/22 | 6 | 1 | +$3.77 | +$22.62 |
   | TSLQ 10/28 | 87 | 9 | −$1.41 | −$13.63 |
   | PLTR 10/28 | 6 | 1 | −$1.60 | −$9.60 |
   | total | | | +$5.32 | +$78.03 |

   "Wired before the cap" is the bot's own size after every account limit (gross, buying power, maintenance cushion, Day
   gross ceiling, single-name 65% of equity, thin-name $1,300) and the risk sizing. Live P&L is from the bot's fill records
   (exit fills x quantity); the last column scales the same per-share result, so it assumes the same fills at the larger
   size (thin names are capped at $1,300, about 6 PLTR shares).

2. **The 2-share rule does not reach Track B.** Track A routes through `run_day_tier._place_affordable` (stock needs 2+
   shares when an ETF route exists; else the 2x bull / inverse ETF, which itself needs 2+; else 1 share of the stock).
   Track B decides the route earlier from the $140 budget (`_stock_shares` in the Track B loop) and then calls
   `place_entry` with a 1-share minimum. 2026-10-28 PLTR short: the budget bought 0 shares, so it tried PLTD (1x inverse,
   $4.57). The PLTR structural stop translated to $0.0489 on PLTD, under the 5-tick ($0.05) minimum in
   `day_tier_leverage.etf_liquidity_ok`, so the route was refused ("inverse route not taken (inverse route to PLTD: stop
   0.0489 < 5 ticks) — shorting the stock") and the bot shorted 1 PLTR share. place_entry then widened the PLTR stop to
   the measured noise room ($1.39, 0.72%) — a width the ETF route never got. Note: even with that 0.72% room, PLTD's stop
   would be $0.033, still under 5 ticks, so widening alone would not have routed PLTD.

## What the replay says about edge (so size is not mistaken for profit)
- 6-year Track B replay (`logs/research/track_b_6y_confirmation_exit_2026-10-28.md`, 7,624 signals): same-day Track B
  is −0.061R (2020-24) / −0.022R (2024-26) per trade after 5 bps cost; stops from 0.1 to 1.0 x daily ATR held to the
  close are about breakeven. 2026-10-28 additions (session scratchpad `entry6y.py`, 240 entry rule sets): entering
  earlier does not help (earliest-entry fifth −0.117R / −0.074R; latest −0.011R / +0.008R); a pullback-limit entry has
  no rule set with discovery t > 2.
- Live Track B, 16 completed trades: +$54.83 (bot fill records). Live Track A, 31 trades: −$102.14.
- Bigger size scales dollars, not the per-trade R. At about $33 of stop risk per trade (TSLQ 87 sh x $0.384), the
  6-year same-day expectation is roughly −$2 (2020-24 rate) to −$0.7 (2024-26 rate) per trade; live Track B so far has
  been positive. This change collects data at full size; it does not create an edge.

## Design
**A. Track B size (no fixed $140 limit).** Set `DAYTRADE_TRACK_B_CASH_ONLY = False` (the existing kill flag; both
clamp sites already honour it). Track B entries are then sized exactly like Track A and like Track B's own 10/10
entries already are: the conviction-band / per-trade risk sizing, then every account limit (all-tier gross 2.5x,
buying power after the $1,200 main-bot reserve, $650 maintenance cushion, Day gross ceiling, single-name 65% of equity,
thin-name $1,300), then the Day tier's daily dollar budget (realized losses + every open lot's stop risk x the 1.2
gap-through allowance <= 5% of start-of-day equity), a protective stop on every lot, the end-of-day flat and the 7%
account kill. Unchanged: the Track A reservation for an active Track B (`DAYTRADE_TRACK_B_RESERVE`), the 10/10 path.
- Halt/gap containment (the cap's stated reason). 10% halt-gap loss on one lot at equity $2,667 (7% kill = $187),
  corrected by the board sizing seat 2026-10-28:

  | lot | notional cap | 10% gap loss | % equity |
  |---|---|---|---|
  | deep stock (Mag-7) | 65% of equity ($1,733) | $173 | 6.5% |
  | thin stock / 1x fund | $1,300 | $130 | 4.9% |
  | 2x fund today | $1,300 (= $2,600 of the stock) | $260 | 9.75% |
  | 2x fund under A.1 below | $650 | $130 | 4.9% |

  A halt gap is not nested inside the 5% Day budget or the 7% kill (the budget charges stop risk x 1.2), today or after
  this change. The deep-stock and 2x-fund cases already exist for Track A and 10/10 Track B on the same 18 names
  (DAY-UNIVERSE-1027); removing the $140 limit makes them reachable by non-10/10 Track B entries too. Measured: the
  largest 5-minute open-vs-prior-close jump on the 18 names over 6 years is 1.79% (SNDK; 1,851,696 bars, `gaprisk.py`);
  halted sessions with missing bars are not in that panel, so the 10% design case is the bound used.
- **A.1 Leverage-scaled caps for Track B fund lots.** For a Track B lot on a leveraged or inverse fund, the thin-name and
  single-name rooms are divided by |k|, the fund's daily leverage from the fund map ($650 for a 2x fund), so its
  underlying exposure matches a stock lot. Applied at both room sites (`_bounded_entry_qty` and `_conv_compute`). A
  fund with no readable k is not routed (the stock path applies). Track A fund lots are unchanged here (owner rule: Track A is the larger tier);
  the same scaling for Track A is a separate decision for the owner.
- No per-track Track B brake exists (`DAYTRADE_TRACK_B_KILL_PCT` is not wired); after the change only the shared 5% Day
  budget/kill and the 7% account kill bound Track B's day.

**B. 2-share minimum with the levered-ETF route for Track B.**
1. Route on the WIRED size, inside Track B's own loop (revised after the execution seat showed `_place_affordable`
   would add a one-lot-per-underlying skip that cuts Track B trades, drop 10/10 max size on the ETF, drop Track B's
   delayed-feed ETF price and re-fetch prior closes). Track B keeps its co-held pre-route, its 10/10 stamping, its
   `_direction_conflict` rule and its counter-trend tag. Only the BUDGET branch changes (`_stock_shares <= 1` computed
   from the $140 budget): the stock is placed with `min_qty=2` when an ETF route exists; when `place_entry` skips it
   as `below_min_qty` (wire-time sizing fits 0-1 shares), Track B runs its existing pivot code (2x bull ETF for a long
   signal, inverse ETF for a short) and places the ETF (2-share minimum, 10/10 max size kept); if no ETF route passes
   its live checks and exactly 1 stock share fits, that 1 share (owner fallback 2026-10-07; trade count unchanged).
   Each `place_entry` attempt is charged to the tick's API budget, and a second attempt is made only when the tick
   has the time reserve for it. A counter-trend entry's durable tag is recorded once before the first attempt and the
   same `decision_id` is passed to the fund attempt (Track A's pattern).
2. ETF stop floor, one shared helper in `strategy/day_tier_leverage.py`: a fund lot's stop is at least
   max(5 ticks, quoted spread / 0.25) from its entry — both existing constants (`_INV_MIN_STOP_TICKS`,
   `_INV_MAX_SPREAD_OF_STOP`), so the floor follows the live spread instead of refusing the route — rounded away from
   the entry to the cent. Applied (a) to the translated `wall_ref` in both pivots BEFORE sizing and the liquidity
   check, and (b) in `place_entry` for fund symbols to `stop_px` BEFORE `_wired_entry` (both sizing paths), and as
   `conv_size`'s `min_room` so the conviction-band sizing cannot tighten it again (it can today:
   `d = min(terms[binding], s)`; board input PLTD $4.57, 5-min ATR $0.02, spread $0.01 -> 3 ticks). Never applied
   after the share count is set, so shares are always sized to the floored stop and dollars at risk do not rise
   (a test pins dollar risk with and without the floor). PLTR 10/28 in those
   terms: a 0.72% stock-equivalent stop on PLTD becomes 1.09% ($0.05 on $4.57). Tests pin both inputs.
   Watch-day-exempt shorts (scoped 2026-10-12 to shorts whose structural stop already has the room): if the floor
   would widen the fund's stop, the fund route is not used and the 1-share-stock / stock path applies instead.
3. Fund volume check on the wired quantity: a fund lot's wired shares are capped (shrink only) at today's IEX volume /
   100 (the existing `_INV_MIN_VOL_X_QTY`), since the uncapped sizing can wire more shares than the pivot checked. An
   unreadable volume skips the fund entry (fail closed; the stock fallback rules then apply).
4. Bull-fund route parity (stated change): the Track B bull route keeps its delayed-feed ETF price; the tracking and
   liquidity checks the inverse route already runs are not added to the bull route in this change.

**What 2026-10-28 would have been** (from the logged wire-time numbers): PLTR 6 shares of the stock (no ETF needed);
TSLQ route: the TSLA short would be sized on TSLA first (not computed here — label: not replayed); META 1 share fits
(global gross room $764 after UBER) -> under B it goes to the META inverse fund if that fund passes its live checks
(not replayed: METD price and volume at 10:40 ET not pulled), else 1 META share as today.

## Not changed
Track A (its own sizing and stops, TRACK-A-STOP-1026); the 10/10 rule; Track B entry signals, stops, 2R target and
end-of-day flat; trade count (no entry is skipped that is taken today; the 1-share fallback keeps every trade).

## Expected replay differences (golden master tests/golden/day_entry_grid.json, REPLAY-1019)
Regenerated 2026-10-28 with `python -m tests.golden_day_entry --write` after the build. 82 of 720 cases differ, all
Track B with max_size off (`B|max=0|...`); Tracks A and M and every 10/10 case are identical. In 54 of the 82 the
submitted share count rises (no case falls; e.g. AAPL long, ample buying power: 2 -> 11 shares) because the $140
Track B clamp is off; the other 28 differ only in the logged wire reason (the "track-B budget cap" text is gone).
With DAYTRADE_TRACK_B_CASH_ONLY patched True the grid matches the old pinned results exactly.

## Required questions (RQ-4)
Q1: Data collection: Track B trades at the same count with up to ~10x the shares on non-10/10 entries, so each live Track
   B result carries usable dollar weight; it does not claim edge (6y same-day ≈ breakeven/slightly negative).
Q2: Trade count: unchanged; the 1-share stock fallback remains when no ETF passes its checks.
Q3: Simpler way: A is a one-flag change on an existing kill flag; B changes only the budget branch of Track B's routing
   (reusing `_place_affordable` was rejected by the execution seat: it changes co-hold, 10/10 and one-lot behaviour).
Q4: Existing work: CEO 2026-10-06/07/10 sizing and route orders, CONV-1018 (conv sizing), DAY-UNIVERSE-1027 (shared list),
   TRACK-A-STOP-1026, TRACKB-TREND-SWING-1028 (parked by owner). No open PR touches these lines (checked origin/main ff475dd).
Q5: Static: no new number; the floor is max(existing 5-tick minimum, live quoted spread / existing 0.25 ratio), so it
   follows the observed spread; sizing is risk-derived (existing).
Q6: Forward pass: the per-trade dollar risk for Track B should follow its own measured win rate and payoff once it has
   30 closed lots (Kelly from its own fills, as SWING-OWNER-1025 does for the Swing tier) — next build after this.
Q7: Silo: none added; this removes a Track-B-only limit so both tracks share one sizing path.
Q8: Evidence: every number above cites the decision log, runner log or replay file.
Q9: Simulation: live counterfactual on the 5 capped entries (+$5.32 -> +$78.03); 6-year R per trade unchanged by size.
   Reversal: Track B realized P&L (Alpaca fills) below −2% of equity over its next 20 closed lots, or any single Track B
   loss above the daily dollar budget's per-lot share, is reviewed.
Q10: Not a perfectionism hold; the edge is unproven and stated as such.
Q11: Risk-path: YES (size). Board + masked-loss seat required.
Q12: Envelope: unchanged (stops, 7% kill, daily Day budget 5%, paper=True, data tiers). B.2 widens an ETF stop to 5
   ticks when tighter; dollars at risk stay at the risk-sized amount.
Q13: Missing data: sizing fails closed today (unreadable rooms -> no entry); ETF route fails to the stock fallback.
Q14: Shadow: none; every part acts on live orders the day it ships.
Q15: Logging: wire_cap_reason already records each limit; the Track B loop logs each route step ("track-B stock fits
   N sh -> ETF X", the floored fund stop, or "no ETF route (reason)").
Q16: Proof it ran: `day-tier wire-time sizing` lines without "track-B exposure cap"; "track-B stock fits N sh -> ETF".
Q17: Runtime proof: first Track B entry after deploy, checked in the post-live audit.
Q18: Anomaly: the PLTR 1-share entry against the owner's 2-share rule is treated as a defect, not "by design".
Q19: Owner approves the exact design before any commit: yes (this record).
Q20: Prompts neutral; claims limited to measured values.
Q21: Tests mock the broker; no new broker call.
Q22: No new fail-closed control.
Q23: Full-path simulation of the routing on the 10/28 decisions before calling it ready.
Q24: ETF existence: maps in `strategy/day_tier_leverage.py` (Alpaca-checked 2026-10-06/09); live checks at order time.
Q25: Market stats measured (gap panel above).
Q26: T1 Alpaca only (existing quote/volume calls).
Q27: Output: no new file; decisions go to the existing day_tier_events.jsonl and runner log lines.
Q28: Day runner process (cron every ~2 min), not the main bot's run_cycle thread.
Q29: Rank: the owner asked for it today; one flag, one routing branch, one shared floor helper, a fund-room scale.
Q30: Gap: no per-track Kelly yet (Q6).
Q31: Out of sample: sizing does not change R; the 6-year split stands as above.
Q32: Look-ahead: counterfactual uses logged pre-cap sizes and actual exits only.
Q33: Costs: larger orders in thin names are capped at $1,300; slippage at size not measured live yet.
Q34: Beta: not changed by this design.
Q35: Tails: one-lot 10% halt gap 4.9% (thin stock, 1x fund, 2x fund after A.1) to 6.5% (deep stock) of equity; not
   nested in the 5% Day budget; inside the 7% kill for a single lot. Two simultaneous halt gaps are not bounded.
Q36: Capacity: thin-name $1,300 cap; the ETF route keeps the 100x-order volume check.
Q37: Power: 16 live Track B trades; not enough to separate edge from noise.
Q38: Decay: reversal criterion in Q9.
Q39: Sizing follows edge: partly (conviction-band sizing from Alpaca fills, CONV-1018); per-track Kelly is Q6.
Q40: Outcome check: post-live audit compares wired vs pre-cap sizes and confirms no Track B lot under 2 shares when an
   ETF passed its checks.
Q41: Best-practice beyond the ask: per-track Kelly from Track B's own fills (Q6) and measured fill slippage at the larger size.
Q42: Rawest data used: logged wire-time decision stacks, broker entry/exit fills, 1-minute SIP bars and 6-year 5-minute bars.
Q43: Professional method: equal-risk sizing bounded by per-name gap caps, leverage-adjusted fund caps and a daily loss budget.
Q44: Independent evidence: two cold board seats, GAI and Groq reviews, and the post-live audit on broker fills after the first session.
## Review (2026-10-28, design stage)
| voice | round 1 | round 2 (revised record) |
|---|---|---|
| Board sizing/tail + masked-loss (Thorp/Taleb lens) | APPROVE-WITH-CHANGES: halt-gap table wrong (6.5% deep, 9.75% 2x fund); floor undone by conviction sizing | APPROVE (build notes: divide by abs(k); A.1 at both room sites; Q15/Q29 wording — applied) |
| Board execution (Harris/Kyle lens) | APPROVE-WITH-CHANGES: `_place_affordable` would add a one-lot skip (fewer trades), drop 10/10 on funds, change bull-route checks; 9 changes | APPROVE-WITH-CHANGES: floor must be applied before the share count (failing input: PLTD conv d $0.03 -> $0.05 after sizing = 1.67x risk); reuse counter-trend decision_id; unreadable volume fails closed — all applied; round 3 APPROVE (build note: the place_entry floor runs after `_room_stop`, so the watch-day-exempt stop check compares the unfloored stop) |
| GAI | (503 overload, retried) | APPROVE-WITH-CHANGES: floor before sizing; A.1 in sizing; flag False — all in the record |
| Reviewer #2 | Codex at usage limit until 11:05 PT; Gro fallback returned APPROVE but cited files and lines that do not exist (`strategy/day_tier.py`), so it is not counted | Codex review queued for 11:07 PT. Owner 2026-10-28: use Groq as the backup. Groq on an excerpts-only prompt: REJECT twice on false premises (1: "code does not implement the design" at design stage; 2: a k=10 fund, while the map has only k=1/2). Counter-prompted with the map excerpt: Groq withdrew it ("no concrete failing input under the current map"); its remaining suggestions cite files that do not exist in the repo and are not counted |
