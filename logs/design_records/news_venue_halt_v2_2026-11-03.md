# NEWS-VENUE-HALT-1103 — false news halts and the false close-time venue alert (design v2, signed: Claude)

Approval-ID: NEWS-VENUE-HALT-1103
Covers: events/news_monitor.py, strategy/run_cycle.py, config.py, tests/test_news_matcher_v2.py

Supersedes the 10-31 draft `logs/research/news_venue_halt_1031.md`.
Status: design aligned. Board Taleb (masked-loss) and Kyle (signal) APPROVE-WITH-CHANGES, all folded in; GAI
APPROVE-WITH-CHANGES after 3 counter-prompt rounds (systemic-risk removal accepted on log evidence; crypto exclusion and
replay table folded in); Gro APPROVE. Code not written: `events/news_monitor.py` has no owner approval on record, so the tool gate needs
the owner's typed `APPROVE NEWS-VENUE-HALT-1103` before any edit.

## Problem (evidence in the 10-31 draft)
- 29 regular-hours cycles in 103 days were blocked by a news "HALT" keyword; all 29 were false (substring matches such
  as "Inter**national emergency**" in a PG&E headline, "trading halt" on single-stock halts, "systemic risk" in
  commentary). Each block stops QHM, Swing and main-bot entries for that cycle; exits are unaffected.
- A cycle that starts just before 16:00 ET and reads Alpaca's clock just after it pages a CRITICAL "venue halt"
  (2026-10-30 and 2026-08-07). No trading impact; it is a false page.

## Change 1 — keyword matching (risk-path: frequency axis; board + GAI + reviewer #2 on the diff)
1. One shared matcher, compiled once at import (Kyle R4): each keyword becomes `(?<!\w)` + words joined by `[\s\-]+`
   (Kyle R2: "rate-hike", double spaces) + optional plural `(?:s|es)?`, with the `-ies` form for words ending in "y"
   (Kyle R3: "national emergencies", "bankruptcies") + `(?!\w)` (Kyle R1: lookarounds, not `\b`).
2. HALT list: remove "trading halt" and "systemic risk" (20 of 29 false blocks, zero true). Add narrow market-wide
   phrases (Taleb R2): "market-wide circuit breaker", "marketwide circuit breaker", "circuit breakers triggered",
   "level 1/2/3 circuit breaker", and venue-wide halt phrases (GAI round 1): "halts all trading", "trading halted
   in all", "market-wide halt", "marketwide halt", "exchange-wide halt", "halts trading in all", "trading halt in all", "halt all trading". Requirement: zero
   hits in the 103-day replay.
   - Why "systemic risk" goes (GAI asked to keep it): every logged HALT on it was commentary. Server
     `logs/mtf_bot.log*`: 2026-07-23 "Catastrophic disruption cascades driven by the nonlinearity of systemic risk";
     07-24 "Adani says want to start airline…"; 08-14 "Stock prices are high. But where else do you go?"; 09-11 "FM
     calls for AI safeguards…"; 11-03 06:14 PT "Most compliance experts think AI now being used for financial fraud".
     Four of five titles do not contain the phrase (it matched the description). A real systemic event moves SPY,
     which the price-reaction engine and the circuit-breaker band already watch; the 7% kill switch is unchanged.
5. Kill flag `NEWS_MATCHER_V2_ENABLED` (Rule E per-feature flag): False restores the substring matcher.
3. HALT matches the headline TITLE only (CAUTION/MONITOR keep title + description).
   - The Fed feed's synthetic `"federal reserve " + title` prefix is not part of the title (Kyle R6, Taleb R4); add
     Fed-feed-only HALT phrases "emergency rate cut" and "emergency meeting" so a real Fed emergency still matches.
   - Truth Social posts have no title: the whole post is matched (Kyle R7). Every HALT entry is a specific multi-word
     phrase, so a long post cannot trip on a stray word; a first-sentence split breaks on "U.S." (reviewer round 3).
4. The halt_eval record logs the raw headline, the matched phrase and snippet, and which leg fired (Taleb R5; GAI).
6. A HALT title that also names a crypto venue or asset (crypto, bitcoin, ether, token, coin as whole words) does not
   count as HALT (GAI round 3: a crypto-exchange circuit breaker is not an equity-market event).

## Change 2 — backstop visibility (not risk-path: it can only block more)
When SPY's prior close or last 5-minute close is missing or stale, the market-wide-circuit-breaker band cannot be
evaluated: set `_venue_uncertain` (block entries that cycle, WARNING, no CRITICAL) — the same treatment an unknown
`tradable` already gets (Taleb R1). Today such a failure is silent. `_venue_uncertain` is a per-cycle local in
`strategy/run_cycle.py` (~L1313), recomputed every cycle, so it clears on the first cycle with fresh data; it cannot
latch. The first uncertain cycle of a day sends one Slack page, deduplicated per day through a small state file in
`data/state/` (atomic write) so a restart does not page again (GAI round 2).

The matcher is linear: fixed-width lookarounds, alternations of literal words and one `[\s\-]+` class between words;
no nested quantifiers, so no catastrophic backtracking (GAI round 1 concern). Headlines are short strings.

## Change 3 — close-time venue alert (alert-only, not risk-path)
When `is_open` is False and the time is at or after TODAY's scheduled close from the Alpaca calendar (handles early
closes; never `next_close`, which already points to tomorrow — both seats found the draft's rule could never fire),
suppress only the CRITICAL alert and the "real halt" label. Entries stay blocked (Kyle R8).

## Known gap and forward builds (separate diffs)
- A venue-wide halt that is not a market-wide circuit breaker (e.g. the 2013-08-22 Nasdaq SIP outage) is caught by
  neither the old nor the new keyword list. Forward build: a per-symbol freshness check at entry (stale last trade
  or quote → no entry), the same idea Swing now uses (SWING-LEVEL-LAST-1103).
- HALT keyword + price cross-check: keep a block past one cycle only when SPY's 5-minute move is unusual for its own
  session distribution (Kyle §5).
- `get_active_event_type` still uses substring matching (e.g. "nato" in "senator" → GEO_CONFLICT); move it to the
  shared matcher (Kyle R9; Rule B screen — it changes how long the hybrid engine holds an event state).

## Matcher probe (executed, round 3)
`scratchpad matcher_probe.py` (to be committed as a unit test with the code): "Nasdaq announces trading halt in all
symbols" → HALT; "NYSE announces halt all trading" → HALT; "Market-wide circuit breaker triggered" → HALT; "U.S.
market-wide circuit breaker triggered at 10:00 A.M." → HALT; Fed feed "Federal Reserve emergency meeting" and
"federal reserve: emergency rate cut" → HALT; "International Emergency Management" → none; "Senator says NATO" →
none; "national emergencies" → HALT; "Scorpio Gold announces trading halt" → none. The crypto case is closed by
item 6. Deliberate miss: "circuit-breaker-triggered selloff hits NYSE" (no market-wide wording, so it reads as a
single-stock halt; GAI's own suggested pattern also requires "market-wide"); a real market-wide halt moves SPY past
the circuit-breaker band, which blocks entries independently.

## Replay required before ship
Re-run the 103-day keyword replay with the v2 matcher and commit the table of the 29 previously blocked cycles with
their new status (GAI); list every headline whose tier changes, confirm zero true
emergencies lost and zero hits on the new circuit-breaker phrases.

## Reversal criterion
A real market emergency that the old matcher would have caught and v2 misses; any regular-hours venue halt the close
guard silences; or the uncertain-band block firing on more than a few cycles a week (a data problem to fix).

## Required questions
Q1: Yes for data collection: 29 false-blocked regular-hours cycles in 103 days stopped QHM, Swing and main-bot entries; removing false blocks restores trading days on this paper account.
Q2: It raises trade count: the 29 false-blocked cycles become tradeable; the only added block is the uncertain circuit-breaker band, a missing-data case that today fails silently.
Q3: It should exist; the simpler whole-word fix alone cleared only 9 of 29 false blocks, so the phrase list and title-only rule are needed; real halts stay caught by venue state and the SPY band.
Q4: Supersedes the 10-31 draft news_venue_halt_1031.md and post-live issue #806 finding; no open PR touches the keyword matcher; Kyle R9 get_active_event_type rework is listed as a separate forward build.
Q5: The keyword list is static by nature (named market-wide events); the real dynamic check is the SPY circuit-breaker band and venue state, and the forward build adds a SPY move cross-check from its own session distribution.
Q6: It exposes the assumption that a keyword alone proves an emergency; forward build: keep a HALT block past one cycle only when the SPY five-minute move is unusual for its own rolling distribution.
Q7: The shared matcher also feeds CAUTION and MONITOR display tiers and later get_active_event_type; each use keeps the substring fallback behind the kill flag and has its own unit tests.
Q8: Every claim cites the 103-day replay over 19,304 logged headlines on the server and dated log titles listed above; the 8 description-only matches are flagged as not fully replayable.
Q9: Replay 2026-07-19 to 10-31 covering the 07-24, 08-13, 08-14, 09-02, 09-11, 10-30 episodes; expected 29 of 29 false blocks removed and zero true emergencies lost; a missed real emergency reverses it.
Q10: No edge is held back; this removes a false gate that blocks entries, so shipping it promptly is the Profitable over Perfect choice.
Q11: Yes on the frequency axis (fewer false blocks means more entry cycles), so it is risk-path: board Taleb masked-loss seat and Kyle reviewed, GAI and Gro reviewed, owner approval required.
Q12: Stops, the 7% kill switch, paper=True and data tiers are untouched; exits never depended on the news gate; the circuit-breaker band and venue check remain as independent emergency stops.
Q13: Missing or stale SPY data for the band now sets venue_uncertain and blocks entries that cycle with one Slack page per day; matcher errors fall back to the old substring path through the kill flag.
Q14: No shadow part; the v2 matcher ships live after the replay table and the gate, with the kill flag only as a rollback switch.
Q15: Each halt_eval record logs the raw headline, matched phrase, snippet, which leg fired and the venue verdict, so every block can be re-derived from the logs.
Q16: Proof line is the halt_eval record carrying matcher=v2 and the matched snippet; the post-live audit replays the first full session's headlines through both matchers and compares blocks.
Q17: A read-only run on the server classifies the live news cache through the new matcher before restart, and the first session's halt_eval lines confirm it runs inside the trading process.
Q18: No: the 10-30 PG&E block and the 4 PM venue page were each traced to their exact cause instead of being called expected behaviour, which produced this design.
Q19: Yes, it changes entry gating, so the owner approves this exact design before any commit; approval ID NEWS-VENUE-HALT-1103.
Q20: Reviewer prompts carried the raw replay and code facts without verdict language; GAI needed three counter-prompt rounds with log evidence; board, GAI and Gro all reviewed the design.
Q21: Tests only call the pure matcher and a mocked clock and calendar; no broker client is created; run_cycle venue tests patch the broker calls.
Q22: The uncertain-band control will be run read-only on the server against live SPY bars and the Alpaca clock before restart to confirm it does not fire on healthy data.
Q23: Full-path check before ready: replay table over 103 days plus unit tests for each probe headline plus the server read-only run; deployed alone is not counted as ready.
Q24: The claim that no logged HALT was a real emergency was checked against all 15 logged HALT headlines in the server logs during the replay.
Q25: No market statistics are quoted; the only numbers are counts from the logged headlines and blocked cycles measured on the server.
Q26: News feeds are the existing RSS, Fed and Truth Social sources unchanged; SPY bars stay T1 Alpaca via data/fetcher; no new data source and no fallback change.
Q27: The halt_eval records go to the existing logs; the daily uncertain-page dedupe file goes to data/state with a tmp then replace atomic write.
Q28: The matcher runs inside news_monitor classification on the news thread; the venue and band checks run in run_cycle before entries, after exits, so check_exits is never delayed.
Q29: High rank: it is small, already aligned, and unblocks entries across three tiers on false-alarm days, while other items wait on data or owner input.
Q30: Professional systems gate on venue status messages and price circuit breakers rather than headline text; the blind spot is a non-circuit-breaker venue outage, covered by the per-symbol freshness forward build.
Q31: No statistical model is fitted; the change is a rule judged by a full replay of every logged headline, so variant counts and deflated Sharpe do not apply to it.
Q32: The replay uses headlines in the order logged with their timestamps, so no look-ahead; description text that the logs did not keep is reported as a gap.
Q33: No trading cost is involved in the matcher; restored entries pay the same costs as the tiers' existing entries.
Q34: Not an alpha signal; it only removes false blocks, so its effect is the restored tiers' own alpha and beta mix.
Q35: In a real crash SPY moves trip the circuit-breaker band and venue state regardless of headlines; worst case is a real venue outage without market-wide wording, listed as a known gap.
Q36: No capacity effect; the matcher cost is a regex over short headlines, linear with no backtracking risk.
Q37: Power comes from the full population: 29 of 29 blocks were false in 103 days with zero true positives, so the removed phrases had no measured value.
Q38: Decay is tracked by the halt_eval log; any real emergency missed by v2 or a false block on the new phrases triggers the reversal criterion and an owner-approved fix.
Q39: Sizing is untouched; restored entries are sized by each tier's existing Kelly path from Alpaca fills.
Q40: It protects entry availability on false-alarm days; the machine check is the post-live replay comparing v1 and v2 blocks on the session's real headlines.
Q41: An expert would add the price cross-check and per-symbol quote freshness, both listed as forward builds with their own gates.
Q42: The rawest data is the logged headline stream and the halt_eval records on the server, which the replay used directly.
Q43: The professional method is phrase-level matching with word boundaries backed by price-based circuit breakers, which this design adopts.
Q44: Independent evidence is the replay table of the 29 cycles, the reviewers' probe headlines run as unit tests, and the first session's logged blocks compared by the post-live audit.

## Replay result (2026-11-03, executed on the server; `logs/research/news_halt_v2_replay_2026-11-03.txt`)
- 20,172 unique logged headlines, 2026-07-19 .. 2026-11-03 (logged: 20 HALT, 10,754 CAUTION, 9,398 MONITOR).
- Regular-hours cycles blocked by a keyword HALT (`halt_eval keyword_hit`): **35 in 7 episodes** (07-24, 08-13,
  08-14, 09-02, 09-11, 10-30, 11-02). Under v2: **0 of 35 still block.** Causes: "systemic risk" in descriptions
  (14), "trading halt" on single-stock halts (9), "national emergency" inside "International Emergency" (6),
  "nuclear strike" matched in a description (6, 11-02 — Tucker Carlson commentary).
- Of the 20 logged HALT headlines, v2 keeps 2 as HALT: "World War III: Could Americans be called up…" (11-01) and
  "Trump's nuclear strike on Iran's Pickaxe Mountain to trigger World War III?…" (11-02). Both were outside regular
  hours. They are commentary that contains a HALT phrase in its title; the forward build (keep a keyword block only
  when SPY's 5-minute move is unusual) is what removes this class.
- No logged headline in the period described a real market-wide emergency; the new circuit-breaker and venue-wide
  phrases had zero hits. CAUTION→None 5,109 and MONITOR→None 3,518 are display-only substring artifacts ("ppi" in
  "stepping", "nato" in "anatomy", "iran" in "Miranda").
- Implementation note: the kill flag `NEWS_MATCHER_V2_ENABLED` lives in `events/news_monitor.py` beside the matcher
  (config.py untouched).

## Diff review (2026-11-03): Taleb and Kyle APPROVE-WITH-CHANGES, folded in
- Kyle C1: en/em dashes and the minus sign (U+2010–2015, U+2212) count as word separators.
- Kyle C2: the crypto exclusion matches prefix forms and venues (cryptocurrency, ethereum, stablecoin, Coinbase,
  Binance, Kraken); each is a unit test.
- Taleb C2 (wording): change 2's "stale" check is date-only — the last SPY 5-minute bar must be from today. A feed
  frozen mid-session still reads fresh; that is no worse than before, and the forward build (close-based bar age,
  Invariant #4) closes it.
- Taleb C1 (read-only server run, Q22), executed 2026-11-03 10:53 ET against live data: last SPY 5-minute bar
  10:35 ET today → fresh; today's first bar 04:00 ET (pre-market bars are included); prior close 752.10; today's
  scheduled close 16:00 ET from the Alpaca calendar. The venue check runs only after the 09:30–10:00 opening window:
  the earliest halt_eval each day from 10-21 to 11-03 was 10:01–10:07 ET, so a "no bar from today yet" morning page
  cannot occur.
