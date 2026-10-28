# One shared Day-tier stock list for Track A and Track B — DAY-UNIVERSE-1027 (signed: Claude, 2026-10-27)

Approval-ID: DAY-UNIVERSE-1027
Covers: config.py, data/gex.py, tests/test_day_shared_universe.py

Owner, 2026-10-27: "Shouldn't all trades for track a and track b be the same?" then "I'd prefer one shared stock list
generally but understand they can vary by day for track b since that's measuring a different set of data."

## Problem (measured at source, 2026-10-27)
- Two hand-kept lists. config.py `DAYTRADE_UNIVERSE` (Track A, 11 names) and `DAYTRADE_TRACK_B_UNIVERSE` (Track B, 16).
  Track A only: DRAM, EWY. Track B only: AMD, AVGO, NFLX, COIN, PLTR, SMCI, UBER.
- A third copy, data/gex.py `_DAYTRADE_UNDERLYINGS`, must match the Track A list by hand ("MUST stay in sync", config.py
  comment) or the new name reads options regime UNKNOWN and never trades. Nothing checks it.
- SNDK was missing from Track B until today for exactly this reason (SNDK-TRACK-B-1027).

## Change (revised after board + Codex + GAI review)
- config.py: one canonical tuple `DAY_TIER_UNIVERSE` = the 18-name union in the existing Track B scan order, then the Track A-only names: NVDA TSLA META AMD AMZN AAPL MSFT
  GOOGL AVGO NFLX MU COIN PLTR SMCI UBER SNDK DRAM EWY. Keeping Track B's order means no Track B name moves later in
  its scan (Harris seat: a name late in the scan is the first deferred when the per-tick API budget runs out; budget
  hit 9 times in 4,270 runner ticks).
  `DAYTRADE_UNIVERSE = DAY_TIER_UNIVERSE` and `DAYTRADE_TRACK_B_UNIVERSE = DAY_TIER_UNIVERSE` (same object, not copies).
  Kill switches unchanged. The stale "shared 3-concurrency cap" comment is corrected (that cap was replaced by the
  dollar budget on 2026-10-04, config.py "REPLACED 2026-10-04").
- Track B still varies by day: its existing gap/volume screen picks which of the 18 are movers each session (unchanged).
- data/gex.py: the options refresh reads `config.DAY_TIER_UNIVERSE` directly (no second literal list); if config cannot
  be read the refresh covers only SPY/QQQ and held positions, so Track A reads UNKNOWN and stands down (fail safe).
- tests/test_day_shared_universe.py (CI): both tracks and the options refresh resolve the same 18 names; every added
  Track A name resolves to its intended route (stock, or the mapped ETF when 0-1 shares fit).
- No rule, stop, cap or budget change. Size effect (it is LOWER, not higher): the 7 added names are not on the deep-
  liquidity list (config.py `DAYTRADE_DEEP_LIQUIDITY_SYMBOLS`, Mag-7), so on Track A they enter under the thin-name rules
  (0.60 gross ceiling, $1,300 per-entry cap) exactly like DRAM/EWY today.
- Routes (strategy/day_tier_leverage.py, checked 2026-10-27): bull 2x ETF for all 7 added names (AMD AMDL, AVGO AVL,
  NFLX NFXL, COIN CONL, PLTR PLTU, SMCI SMCL, UBER UBRL); inverse for AMD, AVGO, NFLX, COIN, PLTR; none for SMCI and UBER
  (their shorts trade the stock, minimum 1 share). DRAM and EWY have no ETF and trade as stock.

## Reviewer changes taken / not taken
- Taken: single canonical tuple (Codex); routing test and pre-deploy full-path dry run with orders refused (Codex);
  per-symbol post-live coverage check of all 18 names from real logs (Codex); corrected caps wording, thin-name size
  disclosure, PLTR/SMCI/UBER routes (Thorp seat).
  Scan order kept from Track B, per-name reversal check, post-deploy check that Track B budget deferrals did not rise
  (Harris seat).
- Not taken, with reason: automatic removal of a name from a rolling fill rule (GAI) and a CRITICAL page when a name's
  options data is UNKNOWN (GAI). Both are new automated processes the owner did not ask for; thin names read UNKNOWN
  routinely (DRAM after hours on 2026-10-27), so a page would be noise. The writer already logs a WARNING per failed name.
- The removal threshold below is an owner-approved provisional number, not derived from data (Codex, Thorp).

## Measurements
- Options data, read-only probe on OCI 2026-10-27 (data.gex fetch + compute, no orders): AMD 686 contracts POSITIVE,
  AVGO 303 POSITIVE, NFLX 156 NEGATIVE, COIN 262 POSITIVE, PLTR 285 POSITIVE, SMCI 225 POSITIVE, UBER 116 NEGATIVE;
  1.1-3.0 s each. Today's 13-symbol refresh takes ~27 s every 15 min (live_writer.log 19:40:21 -> 19:40:48); +7 names
  is about +13 s. No Alpaca data 429s in live_writer.log 10/20-10/27 (the only 429s are the SPY pivot fetch). $0 cost.
- Day runner per-tick symbol loop (day_tier_runner_cron.log 10/27, 185 ticks): median 13 s, max 23 s for 11 names;
  18 names is about 21 s median against a 120 s tick.
- Track B frozen replay (research/day_tier_trackb_watchday_replay.replay_symbol, OCI, 2025-11-01..2026-10-26, 5 bps):
  current 16-name list pooled n=989 mean -0.003R (90% CI -0.069..+0.063); per-name range GOOGL -0.295R .. AVGO +0.261R.
  Added: DRAM n=51 +0.009R (from 2026-04-24, its listing); EWY n=71 -0.165R (CI -0.391..+0.064). Both inside the
  existing per-name range. About +0.4 Track B signals per session.
- Track A on the 7 added names cannot be replayed: logs/gex_history.jsonl holds no options history for them. Honest
  limit; the post-live check measures their first live Track A trades.

## Required questions
Q1: More Day trades across 18 names on both tracks (data collection); no edge claimed for the added names.
Q2: Increases trades (frequency); never fewer. Size and exposure caps unchanged.
Q3: Simplest form: one list read by both tracks plus a test that keeps the options list in step.
Q4: Extends SNDK-TRACK-B-1027 and the Track A/B design records; no conflict.
Q5: One new number: the provisional removal rule in Q9 (owner-approved, not derived from data). No trading threshold changes.
Q6: Static assumption: three hand-synced lists. Dynamic part kept: Track B's daily mover screen chooses within the list.
Q7: Options regime now covers every Day name (anti-silo); no new signal.
Q8: Measured above (probe, logs, replay).
Q9: Expect about break-even per trade on the added names. Reversal (provisional, owner-approved): an added name's Day
    mean R from fills below -0.3R after 20 trades -> I bring its removal to the owner with pooled and per-name results.
Q10: Not applicable because no validated edge is being held back.
Q11: Risk-path by frequency (more names screened). Bounded by the dollar concurrency budget (realized loss + every open
     lot's loss-if-stopped x1.2 + the new risk <= the Day tier kill), one lot per underlying, the Track B exposure cap,
     the thin-name cap for the added names and the 7% kill. Size per trade unchanged or lower.
Q12: No safety-envelope change; every lot keeps its stop and the EOD flatten.
Q13: A name with bad options data reads UNKNOWN and Track A stands down; a name with missing bars is not a mover.
Q14: Not a shadow; live.
Q15: Existing decision-stack logs cover each screen, decision and route.
Q16: Proof: for each of the 18 names, a "day-tier DECISION" line and a Track B screen line in day_tier_runner_cron.log
     and a "GEX [SYM]" line in live_writer.log on the first session after deploy (checked by script, all 18).
Q17: Those lines come from the real runner and writer processes.
Q18: The SNDK miss was traced to the split lists; this removes the cause.
Q19: Changes which stocks are entered; owner asked for one shared list.
Q20: Neutral prompts; replay numbers are the evidence.
Q21: Tests touch config and gex constants only; no broker call.
Q22: Not applicable because no new fail-closed control is added.
Q23: Before deploy: scripts/day_tier_preflight.py full-path dry run on OCI over all 18 names with orders refused, plus
     the routing test. After deploy: per-symbol check that all 18 appear in the runner and writer logs.
Q24: Routes checked in strategy/day_tier_leverage.py this turn (see Change); SMCI/UBER have no inverse; DRAM/EWY none.
Q25: Replay and probe use Alpaca data, not quoted statistics.
Q26: T1 Alpaca bars and option snapshots via the existing modules.
Q27: Writes nothing new; the existing runner and writer logs and the GEX snapshot (already atomic) carry the added names.
Q28: run_day_tier.py Track A and Track B loops (2-minute cron); data/gex.py refresh in mtf-writer (15 min).
Q29: Owner-requested; small.
Q30: Blind spot closed: names on one track and not the other.
Q31: List union only; no rule variants tried.
Q32: Replay uses prior-session data and the frozen trigger.
Q33: 5 bps per trade; ETF spread not modelled.
Q34: Not applicable because no factor bet is added.
Q35: Replay covers 11/2025-10/2026 including the 10/22 high-risk day; worst trade COIN 2026-06-02 -2.42R. Added names
     are tech/semis like the book, so correlation is high; bounded by the dollar concurrency budget.
Q36: Added names are high-volume Nasdaq/NYSE listings; ETF volumes in strategy/day_tier_leverage.py.
Q37: n=51 and n=71 on the added Track B names; Track A unreplayable (stated above).
Q38: Non-working = Q9 reversal; take the name off the list.
Q39: Existing sizing; P&L from fills.
Q40: Protects equal coverage of both tracks; the CI test enforces it.
Q41: Forward pass: lists could later be chosen each week from liquidity and options-data quality.
Q42: Raw logs, probe output and replay JSON.
Q43: One universe, two entry rules.
Q44: Independent evidence: post-live check of the first trades on added names against fills.
