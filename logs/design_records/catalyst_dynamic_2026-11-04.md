# CATALYST-DYNAMIC-1104 — whole-article news judgement for the bad-news entry block (signed: Claude, 2026-11-04)

Approval-ID: CATALYST-DYNAMIC-1104
Status: rev 3.1 — BGC aligned on Phase A (board 5/5, GAI, Codex); awaiting owner approval. No code written.
Covers (planned): events/catalyst_engine.py, execution/entry_logic.py, execution/forever_hold_manager.py, the Swing / QHM /
Day entry paths, tests/test_catalyst_*.py (new).

## Owner direction (2026-11-04, verbatim)
"This feels like it can fall into a static trap of headlines similar to the regime keyword trap we spent months trying to
work out of. The solution needs to be dynamic. It shouldn't be a keyword alone, it needs to be the entire article,
sentence, phrase, etc which then needs to be reviewed by the bot and/or sent to the board or however it's evaluating and
deciding whether or not the news is net good or net bad."

## 1. Defects in the live gate (code-verified 2026-11-04 at `8fcd50f`; how often they fire is unmeasured — no server logs here)
1. **Newer news cancels a real block.** `get_active_catalysts` keeps only the newest non-neutral item per symbol
   (`events/catalyst_engine.py:175-196`, `if target and target not in result`). An offering followed by an analyst
   "price target raised" stores the positive item and the block disappears. Found by 4 of 5 seats.
2. **The 30-day solvency / legal window never applies.** The fetch looks back 72h (`:64`, `:135`) and every run
   overwrites the state (`:269-282`), so the 720h window at `:76` never sees older items.
3. **Recall is capped silently.** One request for all symbols, `limit=25` default, max 50, no paging (`:127`, `:137`,
   `:172`). Busy megacaps can push a small name's offering out of the result.
4. **The watch set misses the names the bot trades.** Without arguments `_default_watch()` is 10 fixed megacaps plus open
   `trade_log.json` positions (`:285-296`). RIVN (the founding case), PLTR, COIN, SOFI, MU, SNDK are in `config.WATCHLIST`
   (config.py:18-36) but not watched; MU and SNDK are in `FOREVER6_UNIVERSE` (config.py:421-422). A name with no cache
   entry is never blocked (`:232-234`). [hypothesis — unverified: the OCI cron passes no symbols; crontab not readable here.]
5. **Siloed.** Only core MTF (`execution/entry_logic.py:496-542`) and F6 (`execution/forever_hold_manager.py:559-574`, F6
   OFF) consult it. Swing breakout, QHM, Day tier and mean-reversion entries are unscreened. The Day tier already has
   empty `catalyst_type`/`catalyst_ts` fields (`strategy/day_tier_mechanism_context.py:227-235`).
6. **No decision record.** A block logs only `CATALYST BLOCK [type]: <80-char headline>` (`entry_logic.py:537-540`); no
   article id, URL, age or evidence, and nothing in `trade_events.jsonl`.
7. **Stale text.** The docstring still says "DARK" / `CATALYST_GATE_ENABLED = False` (`:16-19`, `:305`, `:315`); the flag
   is True (`:61`). `handoff.md:4282` names `logs/catalyst_state.json`; the code writes `data/state/catalyst_state.json`.

## 2. Board positions (cold, parallel, independent)
| Seat | Vote | Core argument |
|---|---|---|
| Simons + Shaw (signal) | APPROVE-WITH-CHANGES | Treat the text verdict as a measured feature; pair it with the price reaction; novelty field fixes the July "past cut" fight |
| López de Prado + Tulchinsky (validation) | APPROVE-WITH-CHANGES | Replay only on articles after the model's training cutoff; triple-barrier abnormal-return labels; pre-registered rule, deflated by trials |
| Kyle + Harris (microstructure) | APPROVE-WITH-CHANGES | Text adds value before the market reacts and to tell information drops from liquidity drops; market reaction resolves mixed news |
| Peterffy + Schneier + Katsuyama (infra/security) | APPROVE-WITH-CHANGES | Fix defects 1-4 first; model runs in the cron only, cached by article id; prompt injection can at worst cause a false block |
| Taleb + Thorp (risk / masked loss) | APPROVE-WITH-CHANGES | The verdict may only remove exposure, never add it; "any block-worthy item in its window blocks", not newest-wins |

## 3. Aligned design (5/5 unless marked)
**Phase A — correctness fixes, no model (separate diff, ships first; 5/5).** Fix defects 1-4 and 6-7: keep every
material item per symbol inside its type window; make the lookback cover the longest window; page the fetch per symbol;
watch set = scanner candidates + WATCHLIST + DAY_TIER_UNIVERSE + FOREVER6_UNIVERSE + QHM holds + every broker position;
a `catalyst_decision` event in `trade_events.jsonl`. It changes which entries are blocked → owner approval before code.

**Phase B — whole-article reading.**
- Fetch the full text: Alpaca `/v1beta1/news` with `include_content` (seen working in
  `logs/research/earnings_sources_2026-10-29.md:107`). Group syndicated copies into one story, earliest timestamp.
  SEC EDGAR 8-K / S-3 / 424B as a second source for offerings (planned at `catalyst_engine.py:27`; new data tier →
  owner approval).
- Two readers (GAI + reviewer #2, the EARN-GAP-REVIEW-1104 pattern), each returning fixed JSON: subject ticker / is the
  company the subject, event type (closed list + other), new event vs reference to a past one, material, net
  (good / bad / mixed / not material), the guidance / EPS components, and verbatim quotes. Quotes must be found in the
  fetched text (`data/earnings_documents.py` quote check). EPS and revenue facts come from FMP / Finnhub, not the model.
- Keywords are not a filter in the decision path (5/5); at most a reading-priority order.
- Runs only in the cron worker, cached by article id + `updated_at`; never on the run_cycle thread (a model call can take
  minutes). Article text is passed as data; output is an enum.
- **Block** when both readers agree: subject, material, new, net = bad, quotes verified.

**What the verdict may do.**
- Block new entries in every entry path (core MTF, Swing, QHM, Day, mean-reversion, F6). Not risk-path (removes trades
  only); the net trade-count change is measured in the replay and goes to the owner.
- Never: unblock an entry on good news, size a position, delay a stop, hold through bad news, or force an exit (5/5).
- Separate risk-path proposals (board + masked-loss seat): QHM/F6 dip-buy permission on a drop with no confirmed bad
  news; Day-tier stocks-in-play feed.

## 4. Points still split (to resolve with GAI + Codex and the replay, not by assertion)
1. **Model outage.** Keyword fallback for the 4 blocking types, labelled `source=keyword_fallback` (LdP, Kyle, Peterffy)
   vs "unconfirmed = no block" (Simons, Taleb). Majority: keyword fallback, because it is today's approved behaviour.
2. **Price confirmation.** Block only when the market reaction confirms (Simons, Kyle) vs a verified new guidance cut or
   offering blocks regardless (Taleb, Peterffy) vs let the labels decide (LdP). Proposal: pre-register both rules as the
   only two replay variants and count both in the multiple-testing adjustment.

## 5. Front-loaded replay (required before live)
- Articles dated after the model's training cutoff only, with ticker / company / dates masked, plus a memorisation probe.
  Point-in-time text only (`created_at` ≤ decision time); start archiving raw text with a sha256 now.
- Population: the bot's own logged entry signals in watched names, 2026-07-14 to now, each blocked or allowed by old vs
  new rule. Outcome: forward return vs SPY and sector over each tier's measured holding period (triple barrier with the
  bot's own stop/target multiples), counterfactual through the real exit rules, costs from live fills.
- Pass: blocked signals' expected value is below admitted signals', bootstrap CI (clustered by date) excluding zero, after
  deflating for the trials run. Report false blocks vs today's gate on the same days.
- Reversal criterion (live): the same blocked-vs-admitted gap turns non-negative on a rolling window → zero blocks and
  back to Phase A behaviour. The owner defines the window.
- Age windows (48h / 720h) and the 1h stale limit are replaced by the measured decay of abnormal return per event type.
- Regression fixtures: RIVN 2026-07-06 offering, the July 19 phrases and their 20 headlines, and the 29 macro false
  halts (`logs/design_records/news_venue_halt_v2_2026-11-03.md`).

## 6. The July 2026-07-14 guidance-cut patch
5/5: do not ship. It adds substring matches, GAI rejected it three times, its validation was 11 author-written cases,
and the owner direction rules out static keyword lists. Its phrases become regression fixtures (and fallback-catalog
entries if the outage fallback is kept). `logs/pending_patch_2026-07-14_catalyst_guidance.patch` and its approval doc
move to `logs/archive/pending_2026/` once the owner closes it.

## 7. Next steps
1. GAI + Codex design review of this record (same prompt, Mac session; Gro only if Codex is unavailable), including the two split points in §4.
2. Read the OCI crontab line for `catalyst_engine.py` and `logs/catalyst_engine.log` to confirm defect 4 and measure the
   live block rate and false blocks.
3. Owner one-page approval: Phase A first, then Phase B after the replay.

## Rev 2 (2026-11-04 evening, Mac session, signed: Claude) — server facts + GAI / reviewer #2 round 1 folded

### Server facts (read-only, OCI; `logs/mtf_bot.log` starts 2026-07-19)
- **Defect 4 confirmed.** Crontab line 88 runs `events/catalyst_engine.py` with no arguments every 10 minutes, 13-21 UTC
  on weekdays. `logs/catalyst_engine.log` prints `watched: 10 names` on every run checked: `trade_log.json` `open` is empty
  (the tiers keep their own state files), so the watch set is only the 10 fixed names. RIVN, MU, SNDK, PLTR, COIN, SOFI
  are never scanned.
- **Live block record: 4 blocks since 2026-07-19, all false.**
  - MSFT ×3 (2026-08-24): "What's Going on With Nebius Shares on Monday?" classified `dilution_offering`. The subject was
    Nebius, not MSFT.
  - SPY ×1 (2026-09-30): "White House Aide Gorka Tells Jerusalem Post That Iran Deal Remains Possible…" classified
    `dilution_offering`.
  - So in 3.5 months the gate blocked 0 real bad-news entries, blocked 4 good ones, and could not have seen the names
    the bot trades outside the 10 fixed ones. It also shows the attribution rule (primary symbol) is not enough.
- State file `data/state/catalyst_state.json`: `gate_enabled: true`, `active: {}` at 2026-11-04 13:52 PT.

### What changed after GAI + reviewer #2 (round 1)
GAI: correct on all defects; Phase A approve-with-changes (owner signs the entry-count effect). Reviewer #2 (Codex):
REJECT overall because its sandbox showed no repository (the code is now pasted in round 2), Phase A approve-with-changes,
Phase B blocking rule rejected as written. Folded:

**Phase A (first diff; changes which entries are blocked → owner approval):**
1. Keep every material item per symbol inside its own type window (no newest-wins); each item expires on its own.
2. Coverage: incremental ingestion with a persisted cursor plus a one-time backfill to the longest window, instead of
   re-fetching 720h every run; complete paging; batched requests allowed.
3. Watch set: scanner candidates + `config.WATCHLIST` + Day universe + F6 universe + QHM holds + every broker position +
   pending orders + any symbol sent to entry admission after the scan.
4. **Attribution fix (from the live false blocks):** an item counts for a symbol only when that company is named in the
   headline or first sentence; a co-tag alone never blocks. Regression fixtures: the Nebius/MSFT and Gorka/SPY items.
5. `catalyst_decision` event in `trade_events.jsonl` for allows, blocks and degraded decisions: article id, URL,
   created_at, age, type, rule version, source.
6. Stale text fixed (docstring "DARK", handoff state path); a startup log line with the real flag and state path; a test
   on the configured path.
7. Measured effect to report to the owner: on the 4 live blocks, all 4 would be lifted by item 4; the watch-set change
   adds names, so new blocks become possible on RIVN-style offerings.

**Phase B (after the replay; not in the first diff):**
- Cache key = source id + revision time + content hash + reader version + prompt/schema version; raw text archived with
  sha256.
- SEC filings parsed for purpose, effective status, amount, issuer — a form type alone is not an offering.
- Blocking rule tightened (reviewer #2): point-in-time availability, verified company identity, evidence spans that
  support each field, current revision, source tier, freshness, and an owner-approved event list. Active only after the
  replay shows blocked signals do worse than admitted ones after costs.

### Split point 1 (model outage) — resolved
Reviewer #2: "unconfirmed = no new model-derived block; no keyword fallback" (it is the static trap the owner ruled out).
Board rev 1 majority was keyword fallback "because it is today's behaviour" — but the server record shows today's
keyword behaviour produced 4 of 4 false blocks. Recommendation: no keyword fallback; Phase A structured rules
(offering filings, verified items) still apply during an outage; the outage is logged and alerted.

### Split point 2 (price confirmation) — unchanged
Pre-register both variants in the replay; both counted in the multiple-testing adjustment.

### July guidance-cut patch
Unchanged: not shipped; its phrases become regression fixtures.

## Rev 3 (2026-11-04 night, signed: Claude) — round 2 folded

**No code exists yet.** This is a design review; the source excerpts in the review prompts show the CURRENT gate that
Phase A replaces.

1. **Attribution is by role, not mention (reviewer #2 failing input: "Microsoft partner Nebius announces $500 million
   offering", symbols [MSFT, NBIS]).** Phase A has no reader that can establish the issuer, so Phase A blocks a symbol
   only when the story is about one company: the Alpaca item lists exactly one symbol, and that symbol is the watched
   name. A multi-symbol story never blocks in Phase A; it is logged `catalyst_decision: ambiguous_attribution` with
   all symbols, and Phase B's reader (issuer / affected-party role per event type: offering → issuer only; guidance
   cut → reporting company; solvency → the debtor; legal probe → the named defendant) decides it later. The cost is
   lost true blocks on co-tagged stories (e.g. an offering co-tagged with a peer); the replay counts them.
   Regression tests: the MSFT/Nebius failing input above, the live Nebius/MSFT and Gorka/SPY items, a single-symbol
   offering (blocks), a two-symbol offering (does not block, logged).
2. **State schema:** `data/state/catalyst_state.json` holds, per symbol, a list of items (id, revision time, type,
   created_at, expires_at, symbols, rule version) plus a top-level cursor (last created_at + ids seen at that
   timestamp), coverage start/end and a completeness flag. Revised articles replace their earlier revision by id. A
   crash before the atomic replace re-reads from the old cursor (idempotent by id). A failed or truncated page marks
   coverage incomplete → degraded allow + one alert, never a silent "fresh".
3. **One admission function** `catalyst_admission(symbol, path, signal_id)` used by core MTF, Swing, QHM, Day,
   mean-reversion and F6 entries, after the signal and before the order; never on the exit path. Full-path tests per
   entry path.
4. **Decision record** per call: signal id, path, symbol, decision (allow / block / degraded / ambiguous), reason code,
   every candidate item id considered, the blocking item's id / URL / type / age / window, attribution result, cache
   age, cursor and coverage status, rule version, timestamp.
5. **Before commit:** end-to-end replay ingestion → state → each admission path → decision event on the 07-19 → 11-04
   server window, reporting blocks lifted, new blocks and ambiguous items; owner approves the exact rules with that
   result.
6. Age windows 48h / 720h stay in Phase A, labelled inherited; every decision logs the item age so Phase B can replace
   them with measured decay.

## Rev 3.1 — round 3 verdicts and final changes (2026-11-04 night, signed: Claude)

| Voice | Round 1 | Round 2 | Round 3 |
|---|---|---|---|
| Board (5 cold seats) | 5/5 approve-with-changes | — | — |
| GAI | approve-with-changes (Phase A) | reject (read the current code as if it were the fix; corrected with the fact that no code exists) | approve-with-changes |
| Codex (reviewer #2) | reject (no repo in sandbox; Phase A approve-with-changes) | reject (MSFT/Nebius attribution failing input) | approve-with-changes |

Folded from round 3:
1. **Revised older articles (Codex failing input: an article created 11-01, revised into an offering on 11-05, missed
   by a created_at cursor).** Each run re-reads with an overlap window equal to the longest type window (720h), with
   complete paging, and replaces any item whose revision time changed (idempotent by id). Regression test: exactly
   that input must end in `block` for a single-symbol item.
2. **Durable evidence in state (Codex):** each stored item also keeps URL, source / source id, data tier, attribution
   result (single-symbol / ambiguous), classification type and rule version, and the window used for expiry, so a
   decision can be rebuilt after a restart.
3. **Cache fault (GAI):** `catalyst_admission` checks the state's `ok` / freshness first; on a fault it returns a
   degraded allow with reason `cache_fault` and the caller fires the alert (today's never-mask behaviour, kept).
4. **Watch-set metric (Codex):** each run logs the watched count by source; a post-scan symbol with no coverage gets a
   logged degraded allow, never a silent allow.
5. **Tests:** `tests/test_catalyst_attribution.py` covers MSFT/Nebius (ambiguous, no block), the two live false blocks
   (Nebius/MSFT, Gorka/SPY), a single-symbol offering (block), a newer positive item not erasing an older block, and the
   revised-article case. No ship until they pass and the end-to-end replay is reported to the owner.
6. Ambiguous (multi-symbol) items are counted daily in the post-session report so a missed co-tagged offering is seen.

**Alignment reached:** board, GAI and Codex on Phase A (Rev 3 / 3.1). Phase B stays behind its replay. Owner approval
of the exact Phase A design comes next; then the build and the full code gate.
