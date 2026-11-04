# CATALYST-DYNAMIC-1104 — whole-article news judgement for the bad-news entry block (signed: Claude, 2026-11-04)

Approval-ID: CATALYST-DYNAMIC-1104
Status: **board review done (5 cold seats, all APPROVE-WITH-CHANGES). GAI + Codex review owed** (BGC: board, GAI, Codex; Gro only as
fallback when Codex is unavailable; this cloud session has no reviewer keys). No code written. Owner approval of the exact
design comes after GAI + Codex.
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
