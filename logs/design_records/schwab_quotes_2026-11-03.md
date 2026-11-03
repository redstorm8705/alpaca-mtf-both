# SCHWAB-QUOTES-1103 — full-market (NBBO) quotes from the owner's Schwab account (design draft, signed: Claude)

Approval-ID: SCHWAB-QUOTES-1103
Covers: data/schwab_quotes.py, scripts/schwab_login.py, config.py, tests/test_schwab_quotes.py

Status: draft. Board (data + microstructure + security seats), GAI and reviewer #2 design pass not yet run. Owner
approval required (new data source; feeds entry filters).

## Why
- The account's real-time quotes are IEX only. On 300 test trades (2025+) at the breakout minute IEX had no quote on 99
  and its spread was a median 3.6x the full-market spread (`logs/research/day_pillars_r8_r10_2026-11-02.md`).
- The Day ORB edge depends on a full-market spread filter: exact filter +50.0 bps vs prior-day proxy +25.4 bps on the
  same 900-trade test sample. The BRK.B 10-27 Swing entry below its level came from a wide IEX ask (PR 764 audit).
- The owner already holds a Schwab brokerage account. Third-party docs say Schwab's Trader API gives account holders
  real-time NBBO quotes at no charge [hypothesis — unverified against Schwab's own terms; confirmed on first login by
  checking the quote's exchange/NBBO fields].

## What the owner does (one time)
1. developer.schwab.com → create an app with the **Market Data** product only (no Accounts and Trading product, so the
   key cannot place orders), callback URL `https://127.0.0.1`.
2. When Schwab approves it (reported as a few business days), put `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET` in the
   server `.env`.
3. Run `python3 scripts/schwab_login.py` once: it prints a Schwab login link; after login the browser lands on a
   127.0.0.1 page that will not load — paste that address back into the script. It saves the token on the server.
- **Weekly:** Schwab refresh tokens expire after 7 days and cannot be extended (third-party docs, consistent across
  sources). A Slack reminder goes out on day 6 with the one command; a missed login only drops the bot back to today's
  behaviour (below). No other recurring step.

## Build
- `data/schwab_quotes.py` (new approved T1b module, quotes only, `requests` to `api.schwabapi.com/marketdata/v1/quotes`):
  - access token refreshed on demand (30-minute life) with the stored refresh token; token file
    `data/state/schwab_token.json`, tmp→replace atomic write, mode 600, never logged.
  - `nbbo(symbols) -> {sym: Quote(bid, ask, last, quote_time, source="schwab_nbbo")}`; a symbol whose quote is older
    than the freshness bound derived from its own recent quote intervals, crossed, or zero-sized is dropped.
  - hard per-call timeout, batched symbols, rate limit kept under Schwab's reported 120 requests/minute
    [hypothesis — confirm from response headers on first use]; never called on the `check_exits` path.
  - kill flag `SCHWAB_QUOTES_ENABLED`; missing key, expired token, HTTP error, or stale data → returns `{}` and the
    caller uses its existing source. Slack page once per day on failure (state-file dedupe).
- `scripts/schwab_login.py`: the OAuth code exchange above; runs on the server over ssh; prints nothing secret.
- Consumers (each a separate diff with its own gate):
  1. DAY-ORB-1103: exact trigger-time spread filter (fallback: prior-day SIP proxy).
  2. Swing breakout level check: NBBO last/ask in place of IEX (fallback: current SIP last-trade rule).
  3. Entry-price quality for every tier: reject an entry whose IEX quote disagrees with NBBO beyond the symbol's own
     spread distribution (fallback: unchanged).
  4. GEX spot sanity (fallback: unchanged).

## Fail-safe and safety envelope
- Read-only market data; Alpaca stays the only broker; `paper=True` untouched; no stop, kill-switch or exit path
  depends on it. Every consumer falls back to today's behaviour, so an outage can never block trading or move a stop.

## Reversal criterion
NBBO spreads that do not match Alpaca's delayed SIP for the same timestamps (checked daily on the first week), or
repeated token/rate failures that leave consumers on fallback most sessions.

## Required questions
Q1: It moves edge evaluation and the $25K goal: the Day ORB filter recovers the full-market edge (+50 vs +25 bps on the same sample) and Swing entries stop reading wide IEX quotes.
Q2: The module itself changes no trade count; consumers like DAY-ORB add Day entries; any consumer that would skip trades goes through its own gate.
Q3: It should exist; the simpler path, Alpaca's paid full-market plan, costs money, while the owner's existing Schwab account gives the same data free; fallbacks make an outage harmless.
Q4: Builds on DAY-ORB-1103 and SWING-LEVEL-LAST-1103 and the round 11 research; no open PR or design adds a second quote source; it does not conflict with the T1 bar fetcher.
Q5: Freshness and divergence bounds are derived from each symbol's own observed quote intervals and spread distribution; the rate cap is a vendor limit read from response headers.
Q6: It exposes the assumption that IEX quotes stand for the market; next build: a per-symbol quote-quality score from NBBO vs IEX divergence feeding every tier's entry check.
Q7: Wired as a fail-safe input to the Day ORB filter, the Swing level check, entry-price quality and GEX spot sanity, each with its own fallback and tests.
Q8: The IEX gap numbers come from the measured 300-trade check in the research file; the Schwab cost and rate-limit claims are labelled hypotheses until the first login.
Q9: The value case was tested on 2022-26 trades with exact SIP spreads; expected effect is the full filter edge on Day ORB; NBBO spreads disagreeing with delayed SIP would prove it wrong.
Q10: No; the data source unblocks an edge that is otherwise limited to the weaker proxy filter.
Q11: The module alone changes no size or frequency; consumers that add entries are risk-path and take the board with the masked-loss seat in their own diffs.
Q12: It does not touch stops, the kill switch, never-mask-a-loss or paper=True; it adds a quote tier that the data-source table must record, which is part of this approval.
Q13: Missing key, expired token, errors or stale quotes return nothing and every consumer uses its current source, with one daily Slack page.
Q14: No shadow: consumers act on it once each passes its gate; the module is plumbing used live.
Q15: Each consumer logs the quote source, bid, ask, quote time and fallback reason in its decision stack in trade_events.
Q16: Proof line is a schwab_quotes log record with the symbol count and latency, plus consumer events tagged source schwab_nbbo; audited against delayed SIP for the first session.
Q17: After login, a read-only run on the server fetches quotes for the watchlist and compares them with Alpaca delayed SIP before any consumer goes live.
Q18: The BRK.B entry and IEX gaps were traced to their cause rather than accepted, which led to this design.
Q19: It feeds entry filters, so the owner approves this design, and each consumer change is approved separately.
Q20: Review prompts will carry the facts and research numbers without verdict language; board, GAI and reviewer two review this design before code.
Q21: Tests mock every HTTP call and never create an Alpaca or Schwab client; the app has no trading product, so no order path exists.
Q22: The fallback control is run read-only on the server against the real Schwab API and Alpaca delayed SIP before any consumer goes live.
Q23: Ready only after the login, the read-only server comparison, and unit tests for token refresh, expiry and stale quotes pass.
Q24: The claim that IEX misses quotes was checked on 300 test trades; the claim that Schwab is free is labelled unverified.
Q25: The spread statistics come from measured SIP and IEX quotes in the research, not memory.
Q26: New tier T1b Schwab Market Data quotes via its quotes endpoint; bars stay Alpaca T1; fallback is the existing IEX quote or SIP rule.
Q27: Writes only the token file in data/state with tmp then replace atomic write and a daily dedupe file; logs go to the existing bot log.
Q28: Called by the Day tier runner at trigger time and by the Swing entry step, outside check_exits, with a short timeout so the trading thread never waits long.
Q29: High rank: it unlocks the strongest Day candidate found in eleven research rounds and fixes the Swing quote problem, at little build cost.
Q30: Professional desks gate on NBBO; the blind spot is a vendor outage or token lapse, handled by fallback and the day-six reminder.
Q31: The edge it serves was chosen on discovery years and tested once per family as recorded; this module adds no fitted parameters.
Q32: The research used quotes at or before the trigger second, symbols including delisted names, and discovery-only choices.
Q33: Spread cost is the very thing it measures; live fills will be compared with the quoted NBBO at entry.
Q34: Not an alpha source itself; the Day ORB edge was measured long and short with both sides positive.
Q35: In high volatility spreads widen and the filter trades less; an outage drops to fallback without blocking trading.
Q36: Quote calls are a handful per minute for a few candidates, well inside the reported limit at any account size.
Q37: The filter result had 2,302 test trades with book t 3.45; power for the live result builds with each session.
Q38: Decay is tracked by comparing filtered and unfiltered Day ORB results by month; the owner approves the non-working definition in DAY-ORB-1103.
Q39: Sizing is untouched by this module; consumers size from their tier's Kelly on Alpaca fills.
Q40: It protects entry quality; the machine check is the daily NBBO versus delayed-SIP agreement report.
Q41: An expert would also log NBBO at each fill to measure real execution cost per tier, listed as the next consumer.
Q42: The rawest data is the vendor's top-of-book quote with its timestamp, used directly.
Q43: The professional method is filtering and pricing on consolidated NBBO, which this design adopts.
Q44: Independent evidence is the agreement with Alpaca delayed SIP and the live fill prices compared with quoted NBBO.
