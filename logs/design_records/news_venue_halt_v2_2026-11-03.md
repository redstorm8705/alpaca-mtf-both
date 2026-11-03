# NEWS-VENUE-HALT-1103 — false news halts and the false close-time venue alert (design v2, signed: Claude)

Approval-ID: NEWS-VENUE-HALT-1103 (supersedes the 10-31 draft `logs/research/news_venue_halt_1031.md`)
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
