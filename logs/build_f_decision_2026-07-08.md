# Build F — HALT & Mass-Liquidation Redesign — DECISION LOCKED (2026-07-08)

**Design session:** MODE-2 architecture board (Taleb/fragility, Harris/microstructure, Simons/signal,
Kim+Peterffy/reliability) + Gro + GAI on 4 forks. **Rafael decided 2026-07-08.**

## Load-bearing finding (reframed the whole redesign)
Blast-radius grep (Simons + Kim, independently): the **7% kill-switch does NOT call `safe_close_all`** —
it only blocks new entries; positions ride their stops. After the F-INTERIM (commit 9d03be1), the **ONLY
live caller of `safe_close_all` is user-shutdown (`main.py:1070`)**. So there is currently NO automated
mass-liquidation reflex anywhere. Build F is deciding whether to ADD one back — not restore one.

## LOCKED ARCHITECTURE (Rafael: "No reflex + halt observability")
1. **Fork 1 — News NEVER liquidates (permanent).** A news-keyword HALT blocks NEW ENTRIES for that cycle
   only (self-clearing; never latches `_halt_entries_for_session`), never liquidates. (F-INTERIM already
   does this — make it permanent.) Board+Gro+GAI 6/6.
2. **Fork 2 — NO automated close-all reflex.** Liquidation authority stays with the 3 existing distributed,
   bounded, model-light controls: per-position GTC/DAY stops + SPY 5-min EXTREME (entry-block) + 7%
   kill-switch (entry-block). `safe_close_all` stays a **user-shutdown-only** function — no automated
   market-signal wired into it. Board 4/4 (Gro/GAI wanted rebuild, but lacked the blast-radius finding;
   Harris decisive: you can't market-liquidate into a halt — no fill until reopen auction, then worst gap).
3. **Fork 2 additive — Halt OBSERVABILITY (never blind again).** ADD Alpaca venue-state detection
   (`get_clock().is_open`, per-asset `tradable` status; corroborated by session-cumulative SPY vs prior
   close at real MWCB −7/−13/−20 levels) used ONLY to: (a) hard-block entries, (b) fire a CRITICAL Slack
   alert, (c) emit a `halt_eval` discriminator event to `trade_events.jsonl` every cycle
   (`{keyword_hit, spy_5m_pct, qqq_5m_pct, venue_status, verdict:"real|unconfirmed"}`). NEVER liquidates.
4. **Fork 3 — Keywords = context/display ONLY.** Delete `get_news_size_multiplier`'s `0.0`-on-HALT branch
   (return 1.0 / retire); `_classify` HALT collapses to a display tier; retire the dead
   `PRICE_CONFIRM_THRESHOLD` + "Unused" `price_change_pct` from the news module. Keywords keep ONE job:
   `get_active_event_type()` labels an already-price-confirmed BROAD move for persistence tuning. Catch real
   shocks in PRICE (SPY/energy 5-min), not text. **Board REJECTED GAI's energy-ETF anomaly detector** as
   unnecessary (SPY engine already catches it). Optional cosmetic: purge ambiguous KEYWORDS_HALT tokens.
5. **Fork 4 — Cross-strategy.** QHM unconditionally exempt (and **runtime-verify** the guard fires against a
   live non-empty registry — Audit-Efficacy-Not-Presence). Kill-switch + user-shutdown callers of
   `safe_close_all` unchanged. Movers never a caller (retired; must stay inert). **DEFERRED to a fresh board
   vote:** whether Bucket A should ride-its-stops vs. force-close on a real circuit-breaker (Simons/Kim say
   ride stops — market-dumping a 3x into a halt reopen is the worst fill — reversing the Apr-8 ruling;
   Gro/GAI/Harris say close). MOOT unless a CB-liquidation is ever added (it isn't, per Fork 2). Accuracy:
   `config.BUCKET_A_TICKERS` = {TSLL, NVDL, TQQQ, SQQQ}.

## IMPLEMENTATION SCOPE (next phase — full-read gate + exact diff owed before ship)
- `events/news_monitor.py` (1828L — FULL READ owed): remove `get_news_size_multiplier` 0.0-on-HALT branch;
  demote `_classify` HALT to display tier; retire `PRICE_CONFIRM_THRESHOLD`/`price_change_pct`.
- `strategy/run_cycle.py`: reconcile the F-INTERIM entry-block (it currently triggers on `news_size_mult==0.0`,
  which will no longer fire) → drive the per-cycle news entry-block off the display-tier HALT flag instead;
  add the venue-state detection → entry-block + CRITICAL alert + `halt_eval` event.
- NEW venue-detection helper (Alpaca clock/asset status) — likely a small function; wire into run_cycle.
- `trade_logger`: the `halt_eval` structured event.
- Guardrails (Kim, if any close-path ever added — N/A now since no reflex, but keep for the observability
  path): fail-to-block on ambiguity; two-tier alert (unconfirmed keyword = info, confirmed venue = CRITICAL).
- Cross-strategy: no change to safe_close_all's QHM/Bucket-A logic (no new caller); runtime-verify QHM guard.

## Gate before ship (Rafael directive): full read → 10-pt + RC → cold board (incl. masked-loss seat, since
this touches the entry-gate/halt path) → Gro + GAI on the DIFF → static → cold-2nd-agent → impact → ship.

## ADDENDUM — FOREVER-HOLD ACCUMULATION PROTOCOL (Rafael 2026-07-08, integrated into Build F)
Rafael added a NEW risk-path requirement that INVERTS the intraday posture for a conviction tier: a
crash/halt is a BUY signal, not a retreat.

**Universe — FOREVER-6 (Rafael decided): TSLA, GOOGL, AMZN, CRWD, META, NVDA.** Never sold — no stops,
no exits, no force-close (extends QHM never-sell to all 6). Merges with / becomes the QHM long-term tier
(NVDA already QHM; add TSLA/AMZN/CRWD/META as forever-holds).

**Buy triggers (accumulate on extreme weakness):** (1) post-halt RESUMPTION (Alpaca name un-halts:
tradable False→True), (2) market-wide CIRCUIT-BREAKER (S&P −7/−13/−20 MWCB, session-cum SPY vs prior close),
(3) CRASH (name or SPY down ≥ threshold — board to set), (4) FLASH-CRASH (rapid intra-bar plunge — board to
define detection). "et al." = the extreme-down/halt family.

**Override + GUARDRAIL (Rafael decided — "bounded override"):** these buys are EXEMPT from the 7%
kill-switch AND Build F's halt entry-block — they fire *regardless*. BOUNDED by: total FOREVER-6 notional ≤
**CAP% of equity** (board to set, ~40-50%) AND a buying-power floor (never trigger a margin call; skip if BP
< slice). Buying STOPS at the cap. This is what keeps "regardless" from self-destructing a ~$2.8K margin account.

**Integration with Build F (the elegant part):** Build F's halt/venue-state detection — which blocks
*intraday* entries + alerts — is the SAME signal that TRIGGERS forever-6 accumulation. ONE detection, TWO
opposite actions: intraday retreats, forever-6 leans in.

**Sizing:** reuse the QHM fixed-$ slice `max(1, floor(0.03·equity ÷ price))`; board to confirm whether to
scale up on deeper crashes. **Debounce:** ONE buy per name per trigger event (one-shot latch — do not re-buy
the same crash every cycle).

**Execution:** at reopen use a MARKETABLE LIMIT (not a naked market order into the reopen auction — Harris:
avoid the worst gapped print), or scale-in. Board to confirm.

**OPEN for the board validation pass (before API):** exact CAP%, crash/flash-crash thresholds, sizing
scale-up, execution (limit vs scale-in), debounce window. **This is RISK-PATH (buy-into-crashes, kill-switch
override) → the board + Gro + GAI validate the fully-mapped combined proposal as the LAST step before API
(Rafael directive); cold masked-loss seat MANDATORY (a bounded-override crash-buy must not create a NEW
disaster — over-leverage into a deepening crash).**

### FOREVER-HOLD — UPDATED SPEC (Rafael 2026-07-08, second pass — MAJOR)
- **COMPLETELY SEPARATE TIER — own bucket, own logic tree, own buy/sell rules.** Does NOT abide by QHM,
  intraday, intraweek, or any normal bot rules. It is an **EXTENSION ABOVE QHM** (a higher conviction tier).
- **Untouchable by other strategies:** intraday / intraweek / QHM holds each have their OWN specific share
  counts; the bot may "trade around" longer positions but must **NEVER dip into / sell forever-hold shares**
  to do so. Forever-hold share lots are ring-fenced.
- **SELL RULE (the only trim): T1 = +1000% (10x) → sell 25% of the forever-hold position.** Otherwise never
  sold. (No T2/T3 defined yet — open question for Rafael: what happens after the first 25% trim?)
- **BUY: "back up the truck HARDER"** on crashes — MAX aggression, bounded ONLY by the fixed-reserve CAP
  guardrail (below). Separate bucket, separate buy logic from QHM's dip-add.
- **Universe: curated, GROWS over time.** Parameters for "what makes a forever hold" VARY and are **NOT
  primarily valuation-based**; the bar is a **3–10 YEAR secular-growth horizon** ("think NVDA / META / TSLA
  from IPO"). Current list: **TSLA, GOOGL, AMZN, CRWD, META, NVDA** (Rafael adds names manually over time).
- **EXEMPT** from the 7% kill-switch AND the halt entry-block (confirmed — fires regardless).
- **Rafael's expanded board ask (NEXT board pass):** design concrete BUY rules for the full "sell-first /
  no-buyers" scenario set — **flash crashes, halts, circuit breakers, intraday crashes, weekly crashes, bear
  markets** — enumerate as many illiquidity/panic situations as possible and give each a rule.
- **GUARDRAILS from Gro + GAI validation (both independent):**
  1. **CAP measured on a FIXED reserve (initial equity / dedicated cash), NOT current equity** — else a crash
     shrinks the cap while the bot buys the dip → "death spiral" / margin call. THE critical fix.
  2. **One-shot latch per event that resets ONLY on recovery (condition clears / next day), NEVER on a deeper
     crash** — else it buys every leg down a −50% cascade.
  3. **Hard, atomic pre-trade CAP check** (reject if post-buy notional > cap).
  4. **Marketable limit at reopen (+~0.5% buffer)**, never naked market into the reopen auction (Harris).
  - Gro: scale slice up on deeper crashes (3/4/5% at −7/−13/−16). GAI: keep flat (scale-up accelerates ruin).
    **Rafael's "back up the truck harder" leans toward scale-up** — Sosnoff seat settling the exact schedule.
  - GAI floated an emergency-liquidate-below-10%-equity backstop → **REJECTED per Rafael's never-sell mandate**
    (noted only).
  - CAP % number + the scale-up schedule: PENDING Thorp/Taleb + Sosnoff seats (running) + Rafael's risk appetite.
### FOREVER-HOLD — BOARD VALIDATION RESULT (4 voices: Gro, GAI, Sosnoff, Thorp/Taleb) — SHIP-WITH-GUARDRAILS
All 4 = SHIP-WITH-GUARDRAILS (Thorp/Taleb: DO-NOT-SHIP if margin-funded). Load-bearing findings:
- **THE 3% SLICE IS A FICTION (Thorp/Taleb):** on $2,800, 3% = $84, but every forever-6 name trades > $84,
  so `max(1, floor(0.03·eq ÷ price))` ALWAYS floors to **1 share**. One META share ($720) = **25.7% of equity**
  in a single buy; 6 names once ≈ **68% of equity in one wave.** The % math is decorative — the min-1 floor is
  the real (unscaled) sizing engine.
- **MANDATORY GUARDRAILS (unanimous / near-unanimous — NONE optional):**
  1. **CASH-FUNDED ONLY** — a forever-6 buy requires `settled_cash ≥ share_cost`; NEVER borrow margin. Check
     *settled cash*, NOT `buying_power` (buying_power = the 2× margin trap). **This ALONE makes a margin call
     mathematically impossible at any crash depth** (Thorp/Taleb modeled: margin-funded breaches at a −42%
     cascade → forced liquidation of the never-sell book; cash-only never breaches). THE load-bearing guard.
  2. **CAP on a FIXED reserve** — snapshot `budget = CAP% × equity_at_session_open`, freeze it, decrement by
     cost basis; OR check against `min(current_equity, prior_day_equity)`. NEVER live current equity (which
     shrinks as the crash deepens = pro-cyclical, loosens exactly when you're most exposed).
  3. **Per-name cap = min(1 share, ~8% of equity)** — stops META's single $720 share = 26% concentration.
  4. **Debounce = one-shot latch per `(symbol, calendar_date)` + ≤3 forever-6 buys/day ceiling** — NOT per
     trigger event (−7/−13/−20 are 3 events in ONE day; a bear leg re-arms nightly → per-event buys the whole
     cascade). Per-day latch is the strongest form.
  5. **Execution = marketable limit `last × 1.01–1.02`, never market;** no fill = no buy (acceptable). One
     tranche per latch.
  6. **Data-quality gate BEFORE the flash detector** — reject `close < 0.5× prior` / zero-volume ticks so a
     fat-finger bad print can't manufacture a fake −10% and trigger a real, permanent buy.
  7. **Exemption is from the kill-switch + halt-ENTRY-block ONLY — never from the cash floor or the CAP.**
     "Regardless" ≠ "regardless of available cash."
- **SIZING FORK — RESOLVED (Rafael 2026-07-08): SCALE UP into deeper crashes (Gro/Sosnoff convex ladder).**
  Bigger slice at −13 / −20 than at −7 (e.g. Sosnoff 0.5× / 1.0× / 2.0×·S, or Gro 3% / 4% / 5%). **CRITICAL
  reconciliation that makes scale-up SAFE:** Thorp/Taleb's disaster model (scale-up → margin call) assumed
  MARGIN funding. With **CASH-FUNDED-ONLY + the FIXED-RESERVE CAP** enforced, the ladder allocates *within*
  a hard, pre-snapshotted ammunition budget — deeper crash = a larger share of the ALREADY-CAPPED reserve,
  weighted toward the deepest level (preserves dry powder for −20, the max-edge point). You literally cannot
  spend past the reserve → a −50% cascade cannot margin-call the account no matter how aggressive the ladder.
  So: **scale-up is APPROVED, and its safety DEPENDS on cash-only + fixed-reserve cap being non-negotiable
  co-requirements.** Implementer: the ladder multiplies the slice; the CAP + cash floor truncate any tranche
  that would exceed remaining reserve/cash (truncate, then close the (name,day) latch).
- **CAP NUMBER:** Gro/GAI 20% · Sosnoff 25–30% · Thorp/Taleb 40% of min-equity. Range 20–40% (Rafael said
  40–50%). With CASH-ONLY enforced, the CAP is a secondary belt (margin-call already impossible) — recommend
  ~30–40% of the fixed reserve.
- **Trigger thresholds (board):** CRASH = session-cum name −10% (or −7/−13/−20 MWCB bands) / SPY −5%; FLASH =
  name −7% to −10% within one-to-three 5-min bars, confirmed by a 2nd down bar (never one bar). Reuse Build F's
  `halt_eval` venue-state event; post-halt = Alpaca tradable False→True.
- **CROSS-STRATEGY (Thorp/Taleb):** register ALL 6 in the QHM never-sell registry — `get_quarterly_hold_symbols()`,
  the HOLE-2 stray-sell guard, and `_get_quarterly_notional_excl` (so intraday sizing reserves their notional).
  Verify the registry populates for all 6 at runtime (Audit-Efficacy-Not-Presence).
- **STILL OWED before API:** (a) Rafael's sizing-fork decision (scale-up vs flat/more-levels) + CAP number;
  (b) the EXPANDED crash-scenario board pass Rafael asked for (flash/halt/CB/intraday-crash/weekly-crash/bear-
  market rules for the full sell-first/no-buyers set); (c) full-read gate on quarterly_hold_manager.py (1954L)
  since forever-6 extends it; (d) final board+Gro+GAI on the fully-mapped combined proposal → API build.

## Note on duplicate work
An autonomous scheduled session also produced Build-F design output (handoff.md refs
`logs/pending_claude_session_2026-07-08.md`, "5 yes/no questions"). This interactive board reached the SAME
architecture; Rafael's decision here is authoritative. Reconcile/retire the autonomous pending file next session.
