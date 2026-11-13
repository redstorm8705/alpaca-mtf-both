# HOLD-NEWS-REVIEW-1113 — the bot reviews every held long-term name when material news breaks, and decides

Approval-ID: HOLD-NEWS-REVIEW-1113
Covers: events/hold_news_review.py (new), execution/quarterly_hold_manager.py, run_hold_news_review.py (new), tests/test_hold_news_review_1113.py (new)
Not changed: execution/broker.py, config.py, strategy/run_cycle.py
Status: rev 2 (signed: Claude, 2026-11-13): reviewer changes folded in; for confirmation.

## Owner direction (2026-11-13, verbatim)
"Jensen resigned as nvda ceo today. Did the bot catch that? Did it review nvda's stock and make a really tough decision on
whether to buy more, hold, or sell? If not, the bot was completely blind to an enormous piece of market and nvidia news.
... This is a critical failure and arguably changes the thesis behind owning the name."

## What happened (measured, OCI logs/mtf_bot.log, UTC)
- 2026-11-11 22:38 news_monitor: "Nvidia CEO Huang may have had relationship with company exec: report" filed CAUTION
  under keyword ['powell'] (wrong keyword; the headline was not read).
- 2026-11-13 01:22 news_monitor: "Nvidia's Forward P/E Hits 14-Year Low as Huang's Exit Sends Shares Tumbling" filed
  MONITOR under keyword ['earnings'].
- 2026-11-13 04:03: "China's access to Nvidia chips in doubt ... amid spy scandal" CAUTION ['congress'].
- No QHM log line reviewed NVDA. QHM (5 sh, avg $173.51, stop $147.48) has no news-driven review; the news path only
  blocks NEW entries (CATALYST-DYNAMIC-1104, pending re-approval, entry side only).
- NVDA 11-12: open 150.99, low 148.80 ($1.32 above the stop), close 166.07 on 24M IEX shares.
- Verified by web search (board seat, 11-13): Huang resigned as CEO and left the board 11-12 (Nvidia 8-K); CFO Colette
  Kress interim CEO; reason not stated by Nvidia, press reports an ouster. Earnings date unconfirmed (11-17 or later).

## Change
1. Trigger: every news item the bot already fetches (Alpaca News T1 via events.catalyst_engine.fetch_news_since; the
   news_monitor feed) whose symbols include a held QHM name (F6 and Swing next; see Q41) starts a review for that name,
   at most once per new item, during and outside market hours.
2. Materiality, whole article not keywords: GAI (gai_client ladder) reads the headline and full article text and
   returns JSON {material: bool, thesis_impact: -2..+2, category, one-line reason, quote}. A rolling per-name baseline
   (the share of items judged material over the last 90 days of that name's news) is logged next to it.
3. Decision for a material item: one call returns ADD / HOLD / TRIM / EXIT-PROTECT with the reasons, built from:
   the GAI article judgement, the price reaction (move since the item vs the name's own 1-year daily move distribution
   from Alpaca bars), the next earnings date (existing _earnings_hours), the position (shares, average, stop, P&L),
   the QHM caps.
4. Action, all inside existing QHM paths and caps:
   - HOLD: page only.
   - ADD: through the existing dip-add path (caps, allocator, stop recalculation QHM-RESTOP-1112).
   - TRIM: through the existing trim path (earnings-trim sizing), stop resized.
   - EXIT-PROTECT: the stop moves up to the live price minus the QHM-RESTOP-1112 gap margin (a sale only if the price
     keeps falling: that raised stop CAN sell, see Rev 2); no market order is sent on a headline.
   - An active owner stop override (QHM-OWNERSTOP-1113) wins over every action: page only.
5. Every review sends one BLUF Slack card (SLK16): bottom line, why (the article quote), the action taken, "Your action:".
   Every review writes a trade_events row (event hold_news_review) with the full decision stack.
6. Fail safe: GAI unavailable or unparseable -> no action, ACTION page "news on <SYM> not reviewed: <headline>".

## Rev 2 — changes from the board seats (Thorp, Harris), GAI and Gro (2026-11-13). Supersedes "Change" where they differ.
- Ingestion (GAI): the review does not read the keyword-filtered news_monitor feed. Every cron pass (and each QHM cycle
  in market hours) asks Alpaca News for each held symbol directly (`events.catalyst_engine.fetch_news_since`, by
  symbol, since the last seen item), so no keyword list can hide an item. Heartbeat: an item on a held name not
  reviewed by the next pass pages ACTION.
- Threading (Harris): GAI is called only by the cron, never on the run_cycle thread. The cron saves its decision to
  data/state/hold_news_review.json; QHM's cycle only reads that file and acts.
- Fresh price at action time (Harris, Thorp, Gro): every action re-reads a live two-sided market-hours quote in the
  cycle that acts. No action outside market hours: the decision queues for the open. The action is dropped (page
  instead) if the quote is older than one cycle, or if the price has moved since the item by more than the name's own
  1-year daily-move 95th percentile (the news is already priced; the owner decides).
- Stale items (Thorp, GAI): an item older than the name's own typical gap between news items (median over 90 days),
  at the time the cron first sees it, is paged, not acted on. No fixed age.
- EXIT-PROTECT stated plainly (Harris): it raises the stop to live minus the QHM-RESTOP-1112 gap margin, and that stop
  CAN sell if the price keeps falling. That is the intent. It is the default for impact -2 or -1.
- TRIM (Thorp): allowed only for impact -2, up to half the shares, when a second independent item or a company filing
  confirms it and the price reaction is outside the name's own move distribution. Otherwise EXIT-PROTECT.
- ADD (Thorp): only for impact >= +1 and only when the last two 5-minute closes are not falling. Never on bad news.
- Priority (GAI, Harris): a stop fill always wins; one news action per name per cycle (never TRIM and a stop move
  together); an active owner stop override wins over every news action (page only).
- Feeds QHM-OVERWEIGHT-1113: a name with a negative review this quarter is not eligible to overweight.
- The rolling materiality baseline is not logged (nothing would act on it).

## Required questions
Q1: Protects the edge of the long-term tiers from known, public, thesis-changing events; growth-aligned (ADD is an option).
Q2: Can add or trim trades on held names; no change to Day/Swing entries.
Q3: Should exist (owner). Simplest form: reuse the fetched news + existing QHM add/trim/stop paths; only the review is new.
Q4: CATALYST-DYNAMIC-1104 (entry block, whole-article; pending) — same GAI judgement shape, different consumer; this
reuses its article read when it ships. QHM re-entry news check (qhm_reentry_news_2026-11-09) covers re-entry only.
Q5: Dynamic: materiality from the article; reaction size vs the name's own move distribution; no keyword list.
Q6: Static assumption exposed: "a hold's thesis only changes at earnings". Forward: re-score conviction on material news.
Q7: Feeds QHM add/trim/stop, conviction, and (next) F6 and Swing holds; fails safe to page-only.
Q8: Log lines above (OCI mtf_bot.log 11-11 22:38, 11-13 01:22, 04:03 UTC); no QHM review line exists.
Q9: Replay on 11-11..11-13 NVDA news + bars: expected one material review by 11-12 pre-market; wrong if a material
CEO-exit item yields no review. Also replay a quiet week (expect zero actions).
Q10: Not applicable because no signal is benched.
Q11: ADD and TRIM change size inside existing caps -> risk-path: board + masked-loss seat.
Q12: Stops stay on every share; EXIT-PROTECT raises the stop below the market by the gap margin, and that raised stop
can sell if the price keeps falling; kill switch untouched (it does not count QHM).
Q13: GAI down -> page, no action; news feed down -> existing alerts; bars missing -> HOLD + page.
Q14: No shadow: acts live.
Q15: trade_events hold_news_review row with the article quote, GAI JSON, reaction z-score, decision, action.
Q16: Log "[change=hold-news-review-1113] <SYM> <category> impact <n> -> <decision>"; audited on the first material item.
Q17: Replay of the 11-12 NVDA items through the real module on the server (read-only, no orders) before live.
Q18: The 11-12 miss is the anomaly; the design re-examines why the news path ignored a held name.
Q19: Changes QHM exits/adds: owner approves this exact design.
Q20: Prompts neutral; logs read; board + GAI next.
Q21: Tests mock GAI, news and broker.
Q22: Read-only server run on the 11-12 NVDA items first.
Q23: Full-path sim on the replay before ready.
Q24: Not applicable because no availability claim.
Q25: Reaction measured against the name's own year of Alpaca daily bars.
Q26: T1 Alpaca News + bars; GAI for article judgement; fallback page-only.
Q27: data/state/hold_news_review.json (seen item ids, atomic tmp+replace); trade_events.jsonl.
Q28: Runs in QHM's cycle (market hours) and a 15-minute cron outside hours (run_hold_news_review.py), actions only in
market hours through QHM; GAI is called only by the cron, never on the run_cycle thread (Rev 2). A decision queued
outside market hours is re-checked at the open (fresh quote, 95th-percentile move check) and dropped with a page if it
fails; tests cover this.
Q29: P0: a thesis-changing event on a held name went unreviewed; NVDA came within $1.32 of its stop.
Q30: Blind spot: news only gated new entries; held positions had no event risk review.
Q31: Not applicable because nothing is fitted (no threshold tuned); replay is a correctness check.
Q32: Replay uses the timestamp each item was published; bars after the item only.
Q33: Actions go through existing order paths; no new cost.
Q34: Not applicable because no new return stream.
Q35: Matters most in event regimes; quiet weeks produce HOLD/no review.
Q36: Not applicable because no new sizing.
Q37: Not applicable because no statistical edge claimed.
Q38: Reversal: a material item with no review, or an action on a non-material item.
Q39: Size from existing QHM paths.
Q40: Outcome: every material item on a held name gets a logged decision within one cycle; machine check = item ids
seen vs reviewed, paged on a gap.
Q41: Extend to F6 and Swing holds; add an options-implied move check before earnings.
Q42: Full article text (Alpaca News content field), not the headline.
Q43: Event-driven risk review of held positions, as discretionary/systematic PMs run on news.
Q44: The 11-12 replay output and the first live material item, reviewed independently.
