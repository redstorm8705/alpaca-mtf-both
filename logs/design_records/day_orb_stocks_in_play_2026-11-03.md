# DAY-ORB-1103 — Day tier: stocks-in-play opening-range breakout (design rev 2, signed: Claude)

Approval-ID: DAY-ORB-1103
Covers: strategy/day_tier_orb.py, run_day_orb_candidates.py, run_day_tier.py, strategy/day_tier_family_admissions.json, execution/day_trade_manager.py, execution/broker.py, strategy/day_tier_logger.py, config.py, tests/test_day_tier_orb.py

Status: rev 2 — board (Harris execution, Thorp sizing, Taleb masked-loss, López de Prado validation) + GAI +
reviewer #2 design pass pending; owner approval required (new Day entries, new order type, new universe).
Evidence: `logs/research/day_pillars_r8_r10_2026-11-02.md` (Round 11 and the 11-03 checks). Spread data:
SCHWAB-QUOTES-1103 (approved; code under review) with the prior-day proxy as fallback.

## Rule (as tested; test 2022-07..2026-10)
- Candidates, known at 09:30 ET from daily bars: prior close > $5, prior 20-day average dollar volume > $20M,
  ranked by |open gap| / 14-day ATR%; top 40.
- At 09:35 rank them by opening-bar relative volume (09:30 bar volume / (20-day average shares / 78)); top 5.
- Entry: buy above / sell below the 09:30 bar, first touch by 11:30 ET; stop-limit at the level (limit = level),
  live 30 s (best on discovery; test fill rate 94%). Shorts only where Alpaca marks the stock easy-to-borrow.
- Stop: the other side of the 09:30 bar. Target: 2R. Otherwise flat at the Day tier's EOD force-flat (15:58 ET).
- Spread filter at the trigger: full-market spread <= 16 bps (chosen on discovery). Measured: exact filter
  +28.9 bps net per trade, 2,302 trades, book t 3.45, positive every year; prior-day 15:50 SIP spread <= 25 bps
  (the fallback) +25.4 bps on a 900-trade sample (exact filter on that sample +50.0).
- Test-year looks for this family: about 14 (Bonferroni over 10 looks on t 3.45 gives p ≈ 0.006). Data never
  consulted: its first live sessions.

## What the bot lacks today (research map, file:line in the review package)
1. The Day tier trades a fixed 18-name list (`config.DAY_TIER_UNIVERSE`, a deliberate thin-name guard).
2. No stop or stop-limit ENTRY order exists (marketable limits only); the Day runner runs every 2 minutes.
3. No easy-to-borrow check (shorts learn from Alpaca rejects) and no pre-trade spread gate.
4. Only the family router (Track M) admits new families; Tracks A/B are not routed.

## Proposed build (each a fork for the board; recommendation first)
1. **New routed family `orb_in_play_v1`, track letter "O"** (Track M is the template): registry entry in
   `day_tier_family_admissions.json` (`ADMITTED_PAPER` only after the independent audit), kill flag
   `DAYTRADE_TRACK_O_ENABLED`, one-shot daily state, decisions and skips logged to `day_tier_events.jsonl` with
   rank, opening relative volume, bar high/low, spread used (source schwab_nbbo or prior_day_sip), borrow status.
2. **Universe: this family only** trades the pre-open candidate list, not `DAY_TIER_UNIVERSE`. The fixed list's
   purpose (thin names) is covered by the rule's own liquidity floor (20-day dollar volume > $20M, price > $5),
   the existing thin-name notional cap and single-name cap, which stay in force. Alternative: keep the 18 names
   only — not tested and drops most of the measured edge.
3. **Candidate builder `run_day_orb_candidates.py`** (cron 09:00 ET weekdays, via `scripts/cron_tz_wrapper.py`):
   T1 Alpaca daily bars for all active US equities, filters + ranking above, Alpaca asset flags (tradable,
   shortable, easy_to_borrow) and the prior-day 15:50 ET SIP spread (Alpaca SIP older than 15 min, available on
   this plan) → `data/cache/day_orb_candidates.json` (atomic). Missing/stale file → the family does not trade
   that day (logged, Slack once).
4. **Entry orders: a resting stop-limit entry** (new `broker.submit_stop_limit_entry`, `paper=True` path
   unchanged), placed at 09:35:00–09:36 for the top 5 — ONE side per stock: the side the 09:30 bar closed toward
   (Alpaca rejects a second opposite-side order on the same symbol; [hypothesis — to be measured on the replay:
   share of first touches on the closing side]). Limit = level; time-in-force day; cancelled at 11:30 ET or when
   the stock is no longer eligible. The 30-second life is replaced by the limit itself (a stop-limit at the level
   only fills at the level or better). Alternative: market-touch by the 2-minute runner — tested worse.
5. **Spread check at placement** (Harris, Schwab design review D5): Schwab NBBO median of a short burst at
   09:35 (≤ 16 bps) — fallback prior-day spread (≤ 25 bps); the record says which rule applied. A placed order
   whose stock's spread later widens is not re-checked [hypothesis — accepted; measured live via effective spread].
6. **After the fill:** the Day tier's existing OCO stop + target path with `stop_ref` = the other side of the
   09:30 bar (the TRACK_M stop_ref branch extended to "O"); the room-stop widening is NOT applied to this family
   (it would change the tested stop). EOD force-flat unchanged.
7. **Sizing: the Day tier's existing per-trade risk basis (1.5% of start-of-day equity at the stop) × router
   share**, all existing caps (thin-name $1,300 notional, single-name, gross, daily risk budget) in force. No
   reduced "trial" size (owner rule: size from the replay). Kelly from this family's own fills after 30 trades.
8. **Admission:** `independent_audit` = PASS only from the López de Prado validation seat on the replay
   evidence; `routing_score` from the replay's net bps.

## Safety envelope
Stops on every position (OCO), 7% kill switch, `paper=True`, never-mask-a-loss: unchanged. New risk path: more
Day entries (frequency) and a new order type → full board + masked-loss seat + GAI + reviewer #2 on design and diff.

## Reversal criterion
Live net per trade below zero after the first 60 trades with a 95% interval that excludes the replay's +25 bps,
or a fill-rate / slippage gap vs the replay larger than the replay's own cost assumptions.

## Required questions
Q1: It is the Day tier's first strategy with a positive out-of-sample net edge after costs, about +25 to +29 bps per trade, so it directly feeds the $25K goal and data collection.
Q2: It adds trades: up to five candidates a day with about two fills per day measured in the test years; nothing is removed from other tracks.
Q3: It should exist; the simpler variants (fixed universe, market-touch entries, no spread filter) were tested and lose most of the edge; the build adds the order type, borrow and spread checks that make it safe.
Q4: Builds on rounds 1-11 of the Day pillar research, the Track M family router, and SCHWAB-QUOTES-1103; the rejected Track B ORB (orb_break_hold_v1) is a different, fixed-universe rule and stays rejected.
Q5: The rule's numbers (price, dollar volume, top 40, top 5, 16 and 25 bps, 2R, 11:30) were chosen on discovery years only and tested once; sizing and caps reuse the Day tier's existing derived values; Kelly takes over from its own fills.
Q6: It exposes the assumption that the Day universe must be fixed; next build: a daily liquidity-derived universe for every Day family, and spread-aware sizing.
Q7: It feeds the Day router, the entry gate, sizing through the existing caps, and the exit OCO; Schwab quotes and the prior-day spread feed its filter with a logged fallback.
Q8: Every number cites the research file; the one-side-per-stock touch share and the stop-limit fill rate are labelled hypotheses to measure in the replay before build.
Q9: Tested on 2016-22 discovery and 2022-07..2026-10 test, positive every test year; expected about +25 bps per trade; live net below zero after 60 trades with an interval excluding the replay result reverses it.
Q10: No; the remaining calibration (exact spread statistic) is refined live.
Q11: Yes, it raises Day entry frequency, so it is risk-path: board with the masked-loss seat, GAI and reviewer two on design and diff.
Q12: Stops, the 7% kill switch, paper=True and the data tiers are unchanged; every position gets the OCO stop; a new broker order function is the one envelope-adjacent change and is gated.
Q13: Missing candidate file means no trades that day; missing Schwab quote falls back to the prior-day spread; missing borrow flag blocks shorts; every fallback is logged.
Q14: No shadow: it trades live after the gate at the existing risk basis.
Q15: Each decision and skip writes rank, relative volume, levels, spread and source, borrow status, sizing multipliers and router share to day_tier_events.jsonl.
Q16: Proof line is a day_tier_events decision with family orb_in_play_v1; the post-live audit replays the first session's candidates, orders and fills against the rule.
Q17: A read-only run of the candidate builder on the server before go-live, then the first session's decision records and fills.
Q18: No anomaly is being explained away; the earlier Track B ORB rejection was examined and this rule differs in universe and filter.
Q19: Yes, new entries, order type and universe, so the owner approves this exact design.
Q20: Review prompts will carry facts and research numbers without verdict language; board, GAI and reviewer two review before code.
Q21: Tests mock the broker and every data call; the new broker function is exercised only through mocks.
Q22: The candidate builder runs read-only on the server against real Alpaca data before go-live.
Q23: Ready only after a full-path dry run on a past session's data: candidates, ranking, order intents, fills by the stop-limit rule.
Q24: The claims that the Day tier lacks stop entries and borrow checks were checked against the code this turn.
Q25: All statistics come from the round 11 measurements on Alpaca SIP data, not memory.
Q26: T1 Alpaca daily and 5-minute bars and asset flags; T1b Schwab quotes for the spread with prior-day Alpaca SIP fallback.
Q27: Candidate file in data/cache written atomically; decisions in day_tier_events.jsonl; Day state as today.
Q28: Pre-open cron at 09:00 ET builds candidates; the Day runner places orders at 09:35 and manages them each tick; exits use the existing Day OCO and force-flat paths.
Q29: Top: it is the only Day strategy with a measured positive net edge in eleven research rounds.
Q30: Professional ORB desks gate on spread and borrow at entry; our blind spot is fill quality on resting stops in fast opens, measured live by effective spread.
Q31: Chosen on discovery among 1,728 variants (Bonferroni-adjusted p about 0.006 on discovery), test looks about 14; test result p about 0.006 over 10 looks.
Q32: Daily candidates use only data known at 09:30; delisted symbols are included; the spread filter uses quotes at or before the trigger.
Q33: Measured SIP spreads, prints-based fills and a stop-limit fill rate of 94% are in the replay; live fills will be compared.
Q34: Longs +27.9 and shorts +30.0 bps, so it is not market beta; trades last minutes to hours.
Q35: Positive every test year 2022-26 including 2022's bear market; worst day and correlation with the book are measured in the pre-build replay.
Q36: Liquidity floor $20M daily dollar volume; at $25K equity a 1.5% risk trade is far below 1% of volume.
Q37: 2,302 test trades, book t 3.45; live power builds at about two trades a day.
Q38: Decay is measured by rolling 60-trade net bps; the reversal criterion above moves it to zero trades and triggers replacement.
Q39: Size uses the existing risk basis until 30 own fills, then Kelly from its Alpaca fills.
Q40: It protects Day tier profitability; the machine check is the rolling net bps of its own fills against the replay interval.
Q41: An expert would measure effective spread at every fill and adapt the spread cutoff to the live cost distribution.
Q42: Raw SIP bars, quotes and prints were used in research; live uses broker fills.
Q43: The professional method is a liquidity- and cost-filtered in-play ORB with resting stop orders, which this adopts.
Q44: Independent evidence: the untouched first live sessions and the López de Prado validation seat's audit.
