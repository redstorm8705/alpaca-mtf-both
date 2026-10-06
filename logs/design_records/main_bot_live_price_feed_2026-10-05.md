# Main-bot live-price feed fix — design record (2026-10-05, Claude)

## Fork (verbatim prompt)
FRAME: PAPER trading account, $2.5K -> $25K goal; data collection and edge evaluation first; the bot trades every day; the 7% account daily kill switch and the 5% day-tier kill are the only daily brakes; stops on every position, never-mask-a-loss and paper=True unchanged.

VERIFIED FACTS (live API probes + production logs, 2026-10-05):
- data.fetcher.fetch_bars(symbol, timeframe, num_bars) builds an Alpaca StockBarsRequest with NO feed parameter. On this account's data plan the server default is consolidated SIP, and SIP is only served once it is more than ~15 minutes old (a feed=sip request for recent data returns 403 "subscription does not permit querying recent SIP data"). So fetch_bars' newest intraday bar is ~15 minutes old during market hours. Evidence: on 2026-10-05 the day-tier Track M logged "live 1m bar is 908s old" on every 2-minute tick 09:46-10:14 ET; a Track-A META long priced at the stale 5m close ($731.41) never filled while META traded $737-740.
- The plan's real-time feed is IEX (the latest-trade endpoint returns exchange "V"=IEX; fetch_bars_window(feed="iex") returns current bars). IEX carries ~2-5% of consolidated volume, so IEX volumes are much smaller than SIP volumes; prices match closely.
- Already fixed and deployed (day tier only): Track M and the Track-A trigger now read IEX real-time bars.
- Still affected (main bot), ~50 fetch_bars callers. Most read daily/weekly bars (a 15-minute lag is immaterial there). The live-price ones: the main entry gate (strategy/run_cycle.py ~1205: SPY and QQQ 5m bar-over-bar % change and SPY 5m RVOL from fetch_bars(5Min, 100) — Architecture Invariant #1 calls the SPY 5-min bar-over-bar move the SOLE entry gate); exit/stop current-price reads (execution/exit_logic.py ~459, ~1150, ~1169: fetch_bars(15Min, 2) last close); entry price checks (execution/entry_logic.py ~837 15Min; ~2001 1Min after-hours); execution/lifecycle.py ~265; quarterly_hold_manager ~1990; run_cycle ~659 (15Min). Entry fill price itself uses data.alpaca_data.get_latest_trade (real-time IEX) per Architecture Invariant #5.

THE DESIGN FORK — how to remove the 15-minute lag from live-price decisions:
A) CENTRAL STITCH: fetch_bars keeps SIP history but, for intraday timeframes, fetches the most recent window from IEX and replaces/appends the bars SIP has not yet published (last ~15-20 minutes). One change, every caller gets fresh bars. Downside: the last few bars' volumes are IEX-scale (tiny) while older bars are SIP-scale — any volume ratio that spans the seam (e.g. the SPY RVOL in the entry gate: newest bar volume / average of prior bars) becomes wrong unless the stitch rescales or callers are adjusted.
B) SWITCH fetch_bars to IEX entirely for intraday timeframes (daily/weekly stay SIP). Consistent single basis per frame; volumes everywhere become IEX-scale (ratios stay internally consistent, absolute-volume thresholds would change), and IEX bars can be missing for minutes with no IEX trade on thinner names.
C) SURGICAL: leave fetch_bars as the settled-history function; change only the ~8 live-price call sites to a real-time read (IEX bars via fetch_bars_window(feed="iex") or the latest-trade endpoint), each with a freshness check. Touches hotspot files (run_cycle.py, exit_logic.py, entry_logic.py), each through the full patch gate.
D) Do nothing for the main bot (accept the 15-minute lag) — listed for completeness.

QUESTIONS: 1) Which option (or combination), and why? 2) Is fixing the SPY entry gate's data source a change that needs a board vote under Architecture Invariant #1 (the gate's existence is unchanged; its inputs become current)? 3) What could go wrong in production with your choice (volume basis, missing IEX bars, rate limits, caching), and the concrete guard for each? 4) Order of work and what to verify after deploy. Cite published work for research claims or mark [inferred]. Under 450 words. End with: RECOMMENDATION: <one sentence>.

## Reviewer recommendations (verbatim final lines)
**RECOMMENDATION:** Implement the surgical replacement (Option C) with volume‑rescaling and fallback logic, then roll it out via staged paper‑trading before full production.
RECOMMENDATION: Implement Option C with a centralized IEX price cache to prevent rate limits and ensure volume-ratio consistency by using IEX-only lookbacks for RVOL calculations.
- Board (Harris lens): Adopt Option C by adding a freshness-checked real-time helper for the ~8 live-price call sites, ship exits first and the SPY entry gate last with a board ratification and an explicit RVOL single-basis rule, and leave fetch_bars unchanged.
- Board (McKinney lens): Adopt C through a single shared fetch_live_bars helper with source and age tagging, ship exits first, and put the SPY gate and its RVOL basis to a board vote last.

## Aligned plan
1. data/ helper: real-time IEX bars + latest-trade fallback, closed-bar rule for signals, age/source tag, short TTL cache, timeouts; never fail open on a stop evaluation (fall back to SIP + WARNING + alert).
2. exit_logic live-price reads first; 3. entry_logic / lifecycle / run_cycle ~659; 4. SPY/QQQ 5m entry gate last with an IEX-only RVOL (single basis) and board ratification (Architecture Invariant #1).
5. Prod probe on OCI before calling anything live (age per site, fallback rate, 429s, price vs latest trade). No shadow period (Rafael 2026-09-26).

## Step 2 (exit_logic) — reviewer findings and forward items (2026-10-05 night, Claude)

Shipped design: exit reads use `live_price_or(..., DELAYED_FEED_AGE_S=900)`. Fresh IEX bar (<=180s) as is, else the newer
of IEX bar / IEX trade (<=1080s = 900s SIP delay + 180s fetch_bars cache, i.e. newer than the delayed fallback), else the delayed close. Live reads age each
source against the clock after its own response. Bar-only 120s pause after a >3s bar read; failures cached 15s.
Five cold-2nd rounds (rev1 PASS-with-threats, rev3 FAIL x2, rev4 FAIL, rev5 below), risk seat APPROVE rev4.

Measured (adversarial, OCI, 2026-10-05 RTH): IEX vs SIP same-minute close gap median 0.1-4.5 bps, p99 1.3-18 bps (max
MARA 102 bps); the 15-min delay it replaces: median 13-27 bps, p95 44-117 bps, max 457 bps.

FORWARD (each its own gated diff):
1. AFTER-HOURS EXITS ARE PRICE-BLIND: IEX has no bars after 16:00 and its latest trade froze at ~16:01 ET, so
   `_check_exits_extended_hours` evaluates EH stops against a ~4 PM price all evening. Largest remaining exit-path gap.
2. Hold `stop_breach_count` / `hard_out_count` (no reset) when the price came from `delayed_fallback` (risk seat; risk-path).
3. Same-bar breach dedupe still keys on the delayed 15M bar — a 3-scan stop confirm needs ~30+ min. Key it on the live minute.
4. Trail hits in check_partial_exits act on ONE read; a phase-3 trail (~0.25 ATR, 15-30 bps) is near IEX's p99 gap.
   Consider a 2-read confirm or a p99-gap band.
5. Record price source + age in the exit decision record (trade_events.jsonl), not only a DEBUG log (Rule D).
6. Replay mode: an explicit `now` can read a 1m bar whose close is after `now` (<=59s look-ahead) — require
   bar_start+60s <= now when replaying.
7. Static thresholds (180/900/3/120/15 s) — derive: 900 -> per-call "newer than the fallback bar's end"; 3s -> rolling
   p99 latency; 120s -> next cycle; cache TTL -> cycle id.
8. Test hygiene: an auto_ai_audit test posts a REAL Slack message when the full suite runs.
