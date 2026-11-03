# SCHWAB-QUOTES-1103 — full-market (NBBO) quotes from the owner's Schwab account (design rev 3, signed: Claude)

Approval-ID: SCHWAB-QUOTES-1103
Covers: data/schwab_quotes.py, scripts/schwab_login.py, config.py, tests/test_schwab_quotes.py

Status: rev 3. Board design pass: McKinney (data), Harris (microstructure + masked-loss), Schneier (security) —
all APPROVE-WITH-CHANGES, folded in (rev 2). GAI and Gro (reviewer #2; Codex at its usage limit) APPROVE-WITH-CHANGES,
folded in below (rev 3). Final round: GAI APPROVE. Gro (4 rounds) APPROVE-WITH-CHANGES on two items left to the
owner: (a) a pre-merge CI stage against a live Schwab sandbox — no sandbox account exists for this owner and CI holds no
credentials, so the runtime shape check plus the read-only login run stand in; (b) drop the whole quote batch and kill
the module after 3 bad log lines a day — a log line is written after the decision, so a bad line cannot have fed a
decision; it counts as a disagreement instead, and the kill threshold is derived, not a fixed 3. Owner approval required (new data
source feeding entry gates; a real-money credential on the server).

## Why
- The account's real-time quotes are IEX only. On 300 test trades (2025+) at the breakout minute IEX had no quote on 99
  and its spread was a median 3.6x the full-market spread (`logs/research/day_pillars_r8_r10_2026-11-02.md`).
- The Day ORB edge depends on a full-market spread filter: exact filter +50.0 bps vs prior-day proxy +25.4 bps on the
  same 900-trade test sample. The BRK.B 10-27 Swing entry below its level came from a wide IEX ask (PR 764 audit).
- The owner holds a Schwab brokerage account. Third-party docs say account holders get real-time NBBO quotes from the
  Trader API at no charge [hypothesis — unverified against Schwab's own terms; checked at app approval and first login].
  Whether Schwab's personal-use terms allow automated use: [hypothesis — checked at app approval].

## What the owner does
1. developer.schwab.com → create an app with the **Market Data** product only, callback URL `https://127.0.0.1`.
2. When Schwab approves it (reported as a few business days), add `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET` to the
   server `.env`.
3. Run `ssh mtf-bot "cd mtf-bot && venv/bin/python3 scripts/schwab_login.py"` once. It prints a Schwab login link; log
   in; the browser lands on a 127.0.0.1 page that does not load; paste that page's address when the script asks.
- **Weekly:** Schwab refresh tokens last 7 days and cannot be extended (consistent across third-party docs). Slack sends
  the same one command on day 6 (the command only, never a link). A missed login only drops the bot back to today's
  behaviour. Nothing else recurs.

## Build — `data/schwab_quotes.py` (new approved quote tier T1b; quotes only; plain `requests`, no OAuth library)
Endpoint `api.schwabapi.com/marketdata/v1/quotes`.
- **Symbols:** one two-way map Alpaca ↔ Schwab (e.g. `BRK.B` ↔ `BRK/B` [hypothesis — confirmed against the live
  response on first login]); results keyed by the Alpaca symbol; an unmapped symbol is logged with its reason.
- **Token:** access token (30-minute life) refreshed on demand under a file lock around read → refresh → write, so the
  Day and Swing processes never overwrite each other. Token file `data/state/schwab_token.json` is created mode 600
  from the start (`os.open(O_CREAT|O_EXCL, 0o600)` temp file, fsync, replace); a test asserts the mode.
- **Quote validity** (each failure → that symbol falls back, reason logged):
  - quote time from Schwab epoch milliseconds converted to tz-aware UTC; age measured against the server clock, the
    clock offset logged;
  - freshness bound per symbol derived before the open from Alpaca delayed-SIP history (a quantile of the gaps between
    quote updates at that time of day); no history → no bound → fallback; PAIRED with a second signal: the quote must
    also have changed within the symbol's recent 1-minute bars' activity (a quote unchanged while bars trade is stale);
  - crossed, one-sided or zero-size books → fallback; a locked book (bid = ask) is valid with spread 0 and logged.
- **Spread statistic:** the consumer reads the same statistic the research tested. The research took SIP quotes in the
  first seconds of the breakout minute; live reads a short burst of snapshots at the trigger and uses its median, and
  the DAY-ORB diff must pin the exact research statistic from its script before go-live.
- **Calls:** per-call timeout derived from the observed latency distribution of its own calls (cold start: the
  Alpaca quote timeout in use today), symbols batched, rate kept under the limit read from Schwab's response headers
  [120/minute reported by third parties, unverified]; never called from `check_exits` or any exit path.
- **Secrets:** a redaction filter on the bot logger and the Slack path strips `code=`, `access_token`,
  `refresh_token` and `Authorization`; Slack failure pages carry only the HTTP status and error class; a unit test
  captures the logs of every failure path and asserts no secret string appears.
- **Kill flag** `SCHWAB_QUOTES_ENABLED`. Missing key, expired token, HTTP error or invalid quote → `{}`; the caller
  uses its existing source. One Slack page per day on failure (state-file dedupe).

## Build — `scripts/schwab_login.py` (runs on the server over ssh; one home for the secret and token)
- Generates a random OAuth `state`, prints the login link, reads the pasted redirect address from typed input (never a
  command-line argument), rejects it unless `state` matches, exchanges the code, writes the token as above.
- **Scope proof:** right after login it calls `/trader/v1/accounts` read-only and requires Schwab to refuse it
  (401/403). If Schwab does not refuse, it deletes the token, pages Slack and exits — the key must not be able to see
  or trade the account. The result is logged as the login's proof line; repeated at every weekly login.
- `--revoke`: deletes the token and posts to Slack. Runbook (in this record): regenerate the app secret or delete the
  app at developer.schwab.com; run `--revoke`; remove the app's access in Schwab account security; change the Schwab
  password if the server itself is suspected.

## Rev 3 — GAI and Gro changes
- **Lock:** one `fcntl.flock` lock file wraps the whole expiry-check → refresh → write cycle. Acquired with a deadline
  (derived from the observed refresh latency); on timeout the caller falls back and logs `LOCK_TIMEOUT`. A lock held by
  a crashed process is released by the kernel when that process exits, so it cannot block forever.
- **File mode on every write:** each write creates its temp file with `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)`,
  fsyncs, `os.replace`s, and checks the result's mode is 600 before releasing the lock.
- **401/403 on a quote call:** not a quote failure — the module disables itself for the day (writes the kill state),
  pages Slack at once, and every consumer falls back. 429: back off, fall back for that call, count it.
- **Shared request budget:** a token bucket shared by the Day and Swing processes (state file under the same lock),
  sized from Schwab's limit headers; each batch takes tokens first or falls back. Snapshot bursts are spaced and count
  against it.
- **Missing symbols in a batch response** → that symbol falls back with `MISSING_IN_RESPONSE`.
- **Clock:** the clock offset is measured each session from Schwab's own SPY quote timestamps against the local clock
  (plus the server's chrony status) and applied to every age check; if the offset is larger than the symbol's
  freshness bound, quotes are not used that session and Slack is paged. The offset goes in the daily agreement report.
- **Trading thread:** in the `run_cycle` thread the fetch runs in a worker with a hard deadline (connect and read
  timeouts derived from its own observed latency; cold start uses the Alpaca quote timeouts in use today); on deadline
  the cycle continues on fallback. `check_exits()` never waits on it.
- **Scope proof on every token refresh** (about every 30 minutes), not only at login. If Schwab ever answers the
  accounts call with 200, the module deletes the token, disables itself and pages Slack. It does NOT exit the trading
  process (that would stop stop-management on the paper book).
- **Mapping tests:** `BRK.B`↔`BRK/B`, `BRK.A`, `BF.B` — one-to-one, no collisions.
- **Redaction** applies to exception messages and tracebacks as well as log lines.
- **`--revoke`** also clears the dedupe file, the kill state and the request-budget state.
- **Gro round 3:** each quote-log line carries a CRC32 of its JSON; a line that fails its CRC or does not parse
  counts as a disagreement and is reported (above). Every Schwab response is checked against the expected field shape
  at runtime (bid, ask, sizes, quote time, symbol); an unexpected shape → fallback for that call, Slack page once a
  day, and the shape failure is in the agreement report — this catches an API change live, which a CI test without
  credentials cannot.
- **Not adopted, with reason:**
  - CI against a real Schwab endpoint: there is no Schwab test account. Instead (Gro round 2): a mock-based
    integration test replays recorded-shape Schwab responses — success, rate-limit headers, 401, 403, 429, missing
    symbols, truncated body — through the whole module, asserting each outcome (kill state, fallback reason, budget).
    The live check is the read-only server run at each login (scope proof + agreement report).
  - A `paper=True` check inside the quote module: the module imports no broker and places no orders; a CI test asserts
    it imports nothing from `execution/` and blocks the merge otherwise. A runtime import-time `RuntimeError` (Gro round
    3) is not added: raising inside the Day or Swing process at import would stop that process's stop management on
    the open paper positions, a worse outcome than the CI block it duplicates.
  - Checksum on the quote log: replaced by (Gro round 2) — a malformed or truncated line is never skipped silently: it
    counts as a DISAGREEMENT in the agreement metric (so it can only lower the match rate), the count appears in the
    report and its Slack digest, and malformed lines above the share seen on the SIP side trip the kill flag.

## Consumers (each a separate diff, each with its own gate and fallback)
1. DAY-ORB-1103: trigger-time spread filter. Fallback: the prior-day proxy WITH its own ≤25 bps cutoff; the decision
   record says which rule applied. (If DAY-ORB rests stop orders before the trigger, its own design must run the spread
   check at placement and cancel when it fails.)
2. Swing breakout level check: NBBO last/ask instead of IEX. Fallback: the current SIP last-trade rule.
3. Entry-price quality across tiers (reject when IEX and NBBO disagree beyond the symbol's own spread distribution).
   This can REDUCE trades → needs its own owner approval.
4. GEX spot sanity. Fallback: unchanged.
- **Hard rule, enforced by a test:** a `schwab_nbbo` value is a gate input only. It never reaches the stop/target
  calculation, `record_exit`, P&L, kill-switch equity, or the entry price of record (that stays the Alpaca fill). The
  test fails any import of `data.schwab_quotes` outside the named gate functions.

## Proof it works (before any consumer goes live, and daily after)
- Every Schwab quote is stored (symbol, quote time, bid, ask, sizes, fetch time). After 16+ minutes each is matched
  to the last Alpaca delayed-SIP quote at or before its time. Report: mid difference and spread difference (bps),
  timestamp offset, and — the deciding number — how often the spread filter makes the same keep/skip decision on Schwab
  as on SIP (IEX managed 64%). The pass bar is derived from SIP's own disagreement with itself across the measured
  offset. A failing day trips the kill flag automatically; consumers fall back.
- Before go-live: measure research spreads for stocks above $250 before and after the late-2025 round-lot change
  [hypothesis — date unverified], to check the cutoff still means the same thing.
- Decision records carry: symbol sent, request time, quote time, age, bound used, latency, bid/ask/sizes, the IEX
  quote beside it, source, fallback reason.

## Safety envelope
Read-only market data; Alpaca stays the only broker; `paper=True` untouched; no stop, kill-switch, exit or P&L path
reads it. The new real-money credential is bounded by the scope proof, mode-600 storage, redaction and the revoke
runbook. The data-source tier table gains T1b (part of this approval).

## Reversal criterion
The daily agreement report failing its derived bar on most days of a week, any scope proof where Schwab does not refuse
the accounts call, or any secret string found in logs.

## Required questions
Q1: It moves edge evaluation and the $25K goal: the Day ORB filter recovers the full-market edge (+50 vs +25 bps on the same sample) and Swing entries stop reading wide IEX quotes.
Q2: The module itself changes no trade count; DAY-ORB adds Day entries; consumer 3 could reduce trades and is flagged for its own owner approval.
Q3: It should exist; the alternative is Alpaca's paid full-market plan, while the owner's existing Schwab account gives the data free; fallbacks keep trading unchanged during an outage.
Q4: Builds on DAY-ORB-1103 and SWING-LEVEL-LAST-1103 and the round 11 research; repo and PR search for schwab found nothing else; no conflict with the T1 bar fetcher.
Q5: Freshness bound, timeout and agreement pass bar are all derived from observed data (delayed SIP gaps, own latency, SIP self-disagreement); the 7-day token life is a vendor fact.
Q6: It exposes the assumption that IEX quotes stand for the market; next build: a per-symbol quote-quality score from NBBO versus IEX divergence feeding every tier's entry check.
Q7: Wired as a gate input to Day ORB, the Swing level check, entry-price quality and GEX spot sanity, each with its own fallback and tests; a test keeps it out of stops, exits and P&L.
Q8: The IEX gap numbers come from the measured 300-trade check; Schwab cost, symbol format, rate limit, terms and the round-lot date are labelled hypotheses until checked.
Q9: The value case used 2022-26 trades with exact SIP spreads; expected effect is the full filter edge on Day ORB; the daily agreement report failing would prove it wrong.
Q10: No; the data source unblocks an edge that is otherwise limited to the weaker proxy filter.
Q11: The module alone changes no size or frequency; consumers that add or cut entries are risk-path and take the board with the masked-loss seat in their own diffs.
Q12: Stops, the kill switch, never-mask-a-loss and paper=True are untouched; it adds quote tier T1b and a real-money credential bounded by the scope proof, storage and redaction rules.
Q13: Missing key, expired token, errors, stale, crossed or one-sided quotes return nothing for that symbol and every consumer uses its current source, with one daily Slack page.
Q14: No shadow: consumers act on it once each passes its gate; the agreement report is a live check that can trip the kill flag, not a waiting period.
Q15: Each decision record carries symbol, request and quote times, age, bound, latency, bid, ask, sizes, the IEX quote, source and fallback reason.
Q16: Proof lines are the login scope-proof record, schwab_quotes fetch records, and the daily agreement report against delayed SIP; the first session is audited with that report.
Q17: After login a read-only run on the server fetches quotes for the watchlist and produces the agreement report before any consumer goes live.
Q18: The BRK.B entry and IEX gaps were traced to their cause rather than accepted, which led to this design.
Q19: It feeds entry gates, so the owner approves this design, and each consumer change is approved separately.
Q20: Review prompts carry facts and research numbers without verdict language; three board seats reviewed and their changes are folded in; GAI and reviewer two next.
Q21: Tests mock every HTTP call and never create an Alpaca or Schwab client; the scope proof shows the key cannot reach the account at all.
Q22: The fallback and scope controls run read-only on the server against the real Schwab API and Alpaca delayed SIP before any consumer goes live.
Q23: Ready only after login, the scope proof, the read-only agreement report, and unit tests for token refresh, locking, file mode, redaction, expiry and stale quotes.
Q24: The IEX gap claim was checked on 300 test trades; the Schwab free-access, symbol-format and terms claims are labelled unverified.
Q25: Spread statistics come from measured SIP and IEX quotes in the research, not memory.
Q26: New tier T1b Schwab Market Data quotes endpoint; bars stay Alpaca T1; fallback is the existing IEX quote or SIP rule, or the prior-day proxy for Day ORB.
Q27: Writes only the mode-600 token file and a daily dedupe file in data/state with atomic tmp-then-replace writes, plus quote records in the existing logs.
Q28: Called by the Day tier runner at trigger time and by the Swing entry step, outside check_exits and every exit path, with a latency-derived timeout.
Q29: High rank: it unlocks the strongest Day candidate found in eleven research rounds and fixes the Swing quote problem, at small build cost.
Q30: Professional desks gate on consolidated quotes; blind spots are quoted versus effective spread and credential compromise, covered by fill logging and the security rules.
Q31: The edge it serves was chosen on discovery years; about fourteen test-year looks, Bonferroni p near 0.006; this module adds no fitted parameters.
Q32: The research used quotes at or before the trigger second with delisted symbols included; the live statistic is pinned to the research one.
Q33: Spread cost is what it measures; NBBO at each fill will be compared with the fill price to measure effective cost.
Q34: Not a return source itself; the Day ORB edge was positive both long and short, so it is not just market exposure.
Q35: In high volatility spreads widen and the filter trades less; an outage or token lapse drops to fallback without blocking trading.
Q36: Quote calls are a few per minute for a handful of candidates, inside the vendor limit read from response headers.
Q37: The filter result had 2,302 test trades with book t 3.45; live power builds with each session.
Q38: Decay is tracked by comparing filtered and unfiltered Day ORB results by month; the owner approves the non-working definition in DAY-ORB-1103.
Q39: Sizing is untouched by this module; consumers size from their tier's Kelly on Alpaca fills.
Q40: It protects entry quality and the credential; machine checks are the daily agreement report and the scope proof refusal.
Q41: An expert would log NBBO at each fill for effective spread and alert on token refreshes from an unexpected address; both listed.
Q42: The rawest data is the vendor's top-of-book quote with its own timestamp, used directly and stored.
Q43: The professional method is gating on consolidated quotes measured at decision time, which this design adopts.
Q44: Independent evidence is agreement with Alpaca delayed SIP, Schwab's own refusal of the accounts call, and live fills compared with quoted NBBO.
