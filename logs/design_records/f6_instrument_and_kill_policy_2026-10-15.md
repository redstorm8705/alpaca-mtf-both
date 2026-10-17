# F6 instrument, loss-attribution, and starter policy — 2026-10-15

**Author:** OpenAI Codex
**Status:** instrument and attribution foundation implemented; starter-policy implementation pending; F6 remains disabled

## Locked owner directives

F6 is a long-only, cash-only accumulation tier. It may hold reviewed common stocks and reviewed
unleveraged long ETFs. It may never buy a leveraged or inverse exchange-traded product. F6 remains
outside the account's 7% Day/Swing trading-loss brake: its exact owned quantity is removed from that
brake's numerator, and a genuine current-session trading-loss trip may not by itself suppress an F6
starter. Control faults and ambiguous state must still block every F6 buy. F6 trims remain reachable.

The existing static starter formula (`max(2%, 0.15 × VIX)`) is retired by the aligned design but is not
removed in this foundation diff. Its replacement must use completed post-close SPY data: today's
close-to-close return must cross the empirical lower tail of prior completed returns and a distinct
completed-bar stress series must confirm against its own empirical distribution. Missing, stale,
future, duplicated, time-misaligned, nonfinite, or insufficient evidence means no starter. The exact
inputs, as-of timestamps, and prior-record hash must be appended to the durable event evidence.

## Foundation implemented in this diff

1. `execution/instrument_policy.py` is a fail-closed admission registry. It independently consults
   `config.LEVERAGED_TICKERS`, `config.LEVERAGED_3X_TICKERS`, and
   `strategy.day_tier_leverage.exposure_sign`, so a newly mapped tactical Day product is barred without
   waiting for the F6 registry to be updated.
2. `config.validate_config()` refuses any incompatible or unknown F6 universe member and refuses to arm
   F6 without enforced ownership attribution.
3. The F6 planner filters incompatible symbols. The final money boundary repeats the static check and
   performs a synchronous Alpaca `get_asset` read under the symbol-order path. Missing/mismatched symbol,
   inactive, untradable, non-US-equity, unnamed, or a name identifying leveraged/inverse exposure blocks
   before an ownership reservation or order exists. The same boundary enforces `FOREVER6_ENABLED`; a
   direct or future caller cannot submit a starter while the tier is dark.
4. The 7% numerator subtracts exact enforced QHM/F6 share quantities, not whole symbols. On a co-held
   symbol, Day/Swing shares remain visible. Same-day protected fills are removed only when their exact
   immutable order tag resolves to QHM/F6; unreadable ownership degrades toward the more sensitive
   account-equity measure while enforcement is advisory. Once ownership enforcement is active, any
   unresolved fill/position join raises a control fault so the cycle blocks all new entries in every
   session while exits continue; it never uses the blended-basis fallback that can mask a Day loss.
   Before subtracting a protected quantity, the current broker net must also equal the ledger's
   replayed net; a protected sell that fills before ledger replay therefore blocks instead of leaving
   a stale protected share in the subtraction. Every ledger tier claim must be a whole-share value and
   all four claims must add exactly to that replayed net. Each fill's order ID, symbol, side, and quantity
   must agree with the synchronous broker order before its QHM/F6 tag is trusted. The complete
   protected-tier client-order ID must also encode the same tier, symbol, side, timestamp, and nonce;
   production Alpaca side enums are normalized through their API value rather than their display text.
5. Once F6 is armed and its after-hours path runs, F6 trims execute before and independently of SPY
   starter-data retrieval. This prevents a market-data outage from suppressing a risk-reducing trim;
   the master F6 flag still keeps both buys and trims dormant today.
6. The final rebase incorporated Claude's QHM immediate add-credit path (#662). Because the exact-loss
   attribution consumes that ledger as protected truth, the integration gate also requires every QHM add
   response to match the authorized order ID, durable QH client ID, symbol, buy side, requested whole
   quantity, and bounded whole filled quantity before allocator binding, fill accounting, ledger credit,
   or owner-quantity hints. Order status and fill quantity must form a valid state (for example, rejected
   cannot carry a fill and filled must equal requested quantity). Because protected QHM ownership is
   nonnegative long shares, non-long stop-safe adds are refused before their protective stop is touched.
   Contradictory evidence remains pending and cannot create protected ownership;
   the ledger writer also refuses a blank, non-string, or whitespace-altered source order ID. The caller
   accepts only a nonblank exact string or Alpaca UUID order ID, never Boolean/numeric coercions, and
   proves the current broker position plus enforced QHM claim still describe the expected positive long
   before it touches the existing protective stop.

## Current-universe classification evidence

Direct paper-Alpaca `/v2/assets/{symbol}` reads on 2026-10-15 returned HTTP 200, `active`, `tradable`,
and `us_equity` for every configured name. The exact observed names and reviewed classes were:

| Symbol | Reviewed class | Alpaca name |
|---|---|---|
| TSLA | common stock | Tesla, Inc. Common Stock |
| GOOGL | common stock | Alphabet Inc. Class A Common Stock |
| AMZN | common stock | Amazon.com, Inc. Common Stock |
| CRWD | common stock | CrowdStrike Holdings, Inc. Class A Common Stock |
| META | common stock | Meta Platforms, Inc. Class A Common Stock |
| NVDA | common stock | NVIDIA Corporation Common Stock |
| MU | common stock | Micron Technology, Inc. Common Stock |
| SNDK | common stock | Sandisk Corporation Common Stock |
| DRAM | unleveraged ETF | Roundhill Memory ETF |
| EWY | unleveraged ETF | iShares MSCI South Korea ETF |

Issuer evidence independently identifies DRAM as the ordinary Roundhill Memory ETF and identifies RAM
as Roundhill's separate 2X product. iShares identifies EWY as the fund tracking the MSCI Korea 25/50
Index. Runtime does not trust issuer prose alone; it requires reviewed admission plus the live broker
check. ETF issuer wind-down detection remains a separate go-live prerequisite.

## BGGN design alignment

The revised Thorp/Taleb board initially rejected the old boolean gate and static trigger. It requires a
versioned, atomic, ET-session-dated kill verdict. F6 may bypass only a schema-valid current-session
`GENUINE_TRADING_LOSS`; `CONTROL_FAULT`, missing/corrupt/stale/unknown cause, invalid SOD equity,
account/kill-evaluation failure, halt uncertainty, ownership drift, cash/allocator/reservation/order
ambiguity, or bad starter evidence blocks. Day and Swing remain latched after the genuine trip.

Google AI Studio approved that corrected design. Groq first rejected because Alpaca asset data has no
source timestamp. The counter-prompt established that the implementation uses the direct synchronous
broker response under the symbol path, records local request/response time, blocks slow/unreadable or
contradictory responses, and retains reservations on ambiguous submits; Alpaca exposes no stronger
source timestamp. Groq then approved. No NVIDIA vote was needed.

## Next implementation contract

1. **SHIPPED:** instrument/attribution foundation merged in PR #667 and deployed on OCI at `6d7947d`.
2. Add the typed durable kill verdict and make F6 bypass only a verified genuine trading-loss cause.
3. Replace the static starter formula with the paired empirical post-close trigger and append-only
   evidence; run a front-loaded historical replay.
4. Complete bounded entry, trim resync, allocator/emergency-flatten integration, ETF wind-down
   detection, then run the full-path pre-market simulation.
5. Do not set `FOREVER6_ENABLED=True`. Present a one-page, stock-and-dollar approval package only after
   every prerequisite and simulation passes.

Signed: **OpenAI Codex**

## Ship and deployment record — 2026-10-16

PR #667 merged at `6d7947d40a7440bce43bc1c2dfd03f708a89e32f`. GitHub CI passed. The exact final
local suite passed 186 tests plus 25 parameterized subtests, Python 3.10 compilation, Ruff E/W/F/B,
mypy `--warn-unreachable`, the repository preship gate, and diff checks. The independent board/cold
review passed after two concrete partial-fill under-protection paths were corrected. Google AI Studio
approved the exact files; Rafael waived Groq for the final diff.

OCI is running the merge SHA with `mtf-bot`, `mtf-writer`, and `mtf-http` active. Python 3.10
compile/import passed, each current universe member returned `ok` from the deployed admission policy,
and `FOREVER6_ENABLED=False` was read back. The server's local report-sync commit `d68b9ee` was
preserved on `oci/report-sync-drift-20261016-2345` before the production branch fast-forward; no report
content was discarded.

The next change is the separately gated typed kill verdict. This shipment does not implement a kill
exception, the empirical starter, or F6 enablement.

Signed: **OpenAI Codex**
