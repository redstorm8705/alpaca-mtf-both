# Day tier Track A vs Track B — review and decision (2026-10-11, Claude)

**CEO (2026-10-11):** "I'm not exactly clear on the material differences between track A and track B. Let's revisit this."

## What each track is (verified at source)
- **Track A ("GEX-core"):** 11 names. Trades the options-driven price walls on 5-minute bars all session: in a calm
  options market it fades a failed poke through a wall; in a volatile one it rides a close through it. Budget
  min(buying power x 20%, equity x 100%) x conviction. Stop at the wall. (`strategy/day_tier_entry_trigger.py`)
- **Track B ("movers"):** 15 names, screened for a gap of 1%+, 1.5x relative volume, price $5+. Enters a mover that
  broke and held its first-15-minute range on volume, on the right side of VWAP, not over-extended. Budget equity x
  15% x 35% (about $130) x conviction 0.5-0.6; one shot per stock per day; stop at the opening-range level, 2R
  target, bracket order; 2x bull ETF on longs. (`strategy/day_tier_momentum_trigger.py`, `strategy/day_tier_track_b.py`)
- **Shared:** order code, end-of-day flat, Day kill switch, the watch-day rule, the Day gross cap (equity x 60%).

## Production record (OCI logs; fills 9/15-10/09, Track B funnel 9/24-10/09, 12 sessions)
- 27 Day fills 9/15-10/09: Track A 26 (14 tagged + 12 untagged on 9/15-9/23, before the tag existed), Track B 1
  (PLTU 10/09, take-profit, +$37.89).
- Track B funnel: 911 screen passes -> 7 trigger signals -> 1 fill. Misses: watch-day rule 3 (META 10/07, MU 10/08,
  SMCI 10/09), Day gross cap already used by Track A 2 (MU/MUU, COIN 10/07, while B's own
  budget had room), one-share minimum (TSLA 10/02, before the 10/06 floor) 1.
- Track A realized -$55.86 over 26 fills since 9/15 (tier's own realized_pnl, not Alpaca fills — indicative only).
- The trigger's WAIT reasons were not logged.

## BGG consensus (board Simons+Brandt 2-0, GAI; Gro unavailable — daily cap, 2026-07-07 rule)
1. **KEEP Track B as a separate track.** Not merge (loses per-track measurement); not retire (n=1 judges the gates,
   not the edge).
2. **Ship now (not risk-path):** log why each screened mover did not trigger (`wait_code` on the trigger; one
   `[track-b-wait]` line per runner tick).
3. **Replay first, then the risk-path gate:** exempting Track B's breakdown shorts from the watch-day rule (its
   evidence was measured on counter-trend shorts held to close, a different setup), and reserving Track B's 35%
   slice inside the Day gross cap so Track A borrows it only while B is idle. Both raise entry frequency (Rule E).
4. **Not now:** a bigger Track B budget (size; GAI against, Simons for; no majority).
5. **Separate review owed:** Track A's own record.

## Draft — the two risk-path fixes (for the board gate, 2026-10-12)
**F1 — Track B shorts and the watch-day rule.** Exempt a Track B breakdown short (gap-down, broke and held below the
opening-range low, below VWAP, on volume) from the watch-day rule, IF the replay
(`research/day_tier_trackb_watchday_replay.py`) shows those shorts are not negative. Track A's counter-trend shorts
keep the rule. Reversal: the live exempted shorts' mean R below zero after N trades (N set from the replay variance).

**F2 — Track B's budget inside the Day gross cap.** Verified at source (`execution/day_trade_manager.py`
`_bounded_entry_qty`): every Day entry is clamped by `rooms["day_gross"] = equity × ceiling (60%) − open Day gross −
pending Day entries`, shared by both tracks. Track B's own cap is `equity × 15% × 35%` (about $130 today) minus open
Track B notional. On 10/07 Track A's open lots had used the shared room (`day_gross:$-5.32`), so B's MU and COIN
signals wired 0 shares while B's own budget had room.
- **Option R1 (reserve from A):** for a Track A entry, `day_gross` room also subtracts B's unused budget
  (`max(0, B budget − open B notional)`). The total Day cap is unchanged; A gives up at most about 5% of equity of
  headroom, only when A is near the cap.
- **Option R2 (B may exceed the shared cap by its own room):** total Day exposure can rise by up to B's budget
  (concurrency increase).
- Recommendation for the board: R1 (no increase in total exposure; the config already says "B may NEVER borrow A's",
  and A borrowing B's room is what happened on 10/07).

## BGG consensus on F1/F2 (2026-10-12; board Thorp + López de Prado 2-0, GAI; Gro unavailable — daily cap)
Replay: research/day_tier_trackb_watchday_replay.py, logs/trackb_watchday_replay_2026-10-11.json (2025-10-01..2026-10-09,
~258 trading days, 245 with a signal; the
115 watch-day-blocked Track B shorts: mean -0.044R, t -0.37, 90% CI -0.24..+0.15; reproduces all 7 live signals).
- **F1 APPROVED with changes:** Track B breakdown shorts are exempt from the watch-day rule. Justification: no edge
  either way, and the paper account buys data on a budget-capped trade (~0.45 trades/day). Risk-path (frequency);
  this board is its gate.
  - Each exempt short is tagged `watch_day_exempt_b` and counted SEPARATELY from the watch-day rule's own ~100-short
    test (run_day_tier.py `_watch_day_ok`).
  - **Reversal (fixed before the first trade, LdP — the drafted thresholds tripped ~50% at a true mean of zero):**
    revert the exemption if, from Alpaca fills after costs, mean R < -0.60R after 20 exempt shorts, or < -0.25R at
    the 40-trade check; report against the same-period allowed Track B shorts.
- **F2 APPROVED as R1 with a release (R2 rejected — it widens total exposure):** a Track A entry's Day gross room also
  subtracts Track B's unused budget, max(0, B budget - open B notional), ONLY while a Track B mover is live today (a
  symbol passed the mover screen today and its daily shot is unused). On a no-mover day A keeps borrowing B's idle
  budget (config comment unchanged). Total Day cap unchanged. Risk-path (frequency); this board is its gate.
  Review after 30 days: Track A entries clamped by the reservation vs Track B fills it enabled.
- **By-product findings (no-clear-side shorts +0.16R; with-trend longs -0.18R):** out-of-sample replay first
  (pre-2025-10 sessions or held-out names, session-blocked, realistic next-tick entry, deflated Sharpe with ~20
  trials); act only if out-of-sample t >= 2 with the same sign. Neither survives a ~20-test correction today.

## Build notes (2026-10-12, after cold-2nd + adversarial)
- The Track A fade reversal rule (strategy/day_tier_alignment.counter_trend_fades_ok) now ignores Track B decisions:
  a Track B win was diluting Track A's fade losses (cold-2nd T1).
- F2 "live" means the mover passed the screen within the last 6 minutes (~3 runner ticks), not "at any time today"
  (adversarial: on 9 of 9 mover days a never-triggered mover would have held Track A back from ~10:00 to the close).
- Known limits: F2 frees dollar room, not a slot under the shared 3-position limit; the replay fires ~4-5x more
  signals than live did in 9/24-10/09 (it uses today's settings throughout; 23 extras live never screened), so the
  115-short sample may differ from what live trades; the F1 reversal is a manual review (no code computes it) —
  count exempt shorts by the `watch_day_exempt_b` tag, not by "counter-trend short".
