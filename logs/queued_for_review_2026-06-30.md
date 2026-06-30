
---

## execution/portfolio_tracker.py — Pagination infinite loop in _fetch_alpaca_fills_for_date() — queued 2026-06-30

REASON: Board Agent A FAIL on process grounds. Agent A flagged that Gro/GAI audit and static analysis gates were not complete at time of board vote — these steps are part of the draft workflow currently in progress, not independent failures. However, per protocol "Any FAIL → STEP 6." Agents B (PASS) and C (PASS) both cleared the patch logic.

FINDING: `_fetch_alpaca_fills_for_date()` (lines 155-228 in portfolio_tracker.py) includes `after`/`until` date-window params on EVERY page request, including subsequent pages. Alpaca's `/v2/account/activities/FILL` endpoint ignores `after_id` when `after`/`until` are present — confirmed via bounded test (page 2 byte-for-byte identical to page 1 for >100 fills/day). Current max daily fill count is 18, so not triggered yet. Latent until volume grows beyond 100 fills/day.

PROPOSED FIX (conceptual — not applied tonight):
```diff
--- a/execution/portfolio_tracker.py
+++ b/execution/portfolio_tracker.py
@@ -185 @@
     while True:
-        # Always anchor to ET day window; after_id added for pagination
-        # (does not conflict)
         params = {
             "direction": "asc",
             "page_size": "100",
-            "after":     et_start.isoformat(),
-            "until":     et_end.isoformat(),
         }
         if after_id:
+            # Subsequent pages: after_id only — Alpaca ignores after_id when
+            # after/until are also present, causing page 2 = page 1 infinite loop
             params["after_id"] = after_id
+        else:
+            # First page only: anchor to ET day window
+            params["after"] = et_start.isoformat()
+            params["until"] = et_end.isoformat()
```

ONLY CALLER: line 887 in write_eod_summary() — EOD reconciliation, NOT real-time trading.
RC AUDIT: All 8 RC classes PASS CLEAR (from full read of 2084 lines).
BOARD B (Red Teamer): PASS — client-side filter at function end is sufficient protection.
BOARD C (Quant Risk): PASS — EOD-only path, no RTH sizing/P&L impact.

ACTION: Can be re-attempted next autonomous session. Recommended: run board vote fresh (Agents A+B+C simultaneously), then proceed through full STEP 4b draft sequence.

PRIORITY: P2
RC CLASS: N/A (latent API pagination bug, not an RC class)
FILE: execution/portfolio_tracker.py

---

## execution/quarterly_hold_manager.py — resubmit_stop_if_needed() dead code — queued 2026-06-30

REASON: Item involves order routing/submission logic. Per nightly agent hard rules, items involving "order routing/submission/cancellation" are skipped regardless of whether the code is live or dead.

FINDING (from S67 handoff): `resubmit_stop_if_needed()` in quarterly_hold_manager.py is dead code — never called from main.py or run_cycle.py. No mechanism resubmits a missing QHM GTC stop. The function exists but is unreachable.

TWO OPTIONS (decision for Rafael):
Option A: REMOVE the dead function (safe — dead code removal doesn't affect execution). However, this eliminates the only implementation of stop-resubmission for QHM positions.
Option B: WIRE the function into main.py/run_cycle.py (enables actual GTC stop resubmission). This is the correct fix but touches order routing/submission — forbidden for autonomous application.

ACTION: Requires Rafael's decision: remove dead code (Option A) or wire it up (Option B). Option B requires full board vote, Gro/GAI review, and Rafael's explicit approval per standard patch sequence.

PRIORITY: P2 (from S67 handoff)
RTH-CHAIN: YES (main.py and entry_logic.py both import quarterly_hold_manager)
FILE: execution/quarterly_hold_manager.py

---

## scan_to_html.py — Additional RC-9 violations at L97 and L1035 — queued 2026-06-30

REASON: Found during full read of scan_to_html.py (2410 lines) tonight. Two additional yfinance T4 violations beyond the _fetch_yfinance_news() item already drafted:

1. L97: `_fetch_implied_range(symbol)` — uses `yf.Ticker(symbol).options` and `.option_chain(nearest)` for ATM straddle pricing. Data: options chain expirations and call/put quotes.
2. L1035: `_fetch_spy_0dte_data()` — uses `yf.Ticker("SPY").fast_info`, `.options`, `.option_chain(nearest)`, `.history(period="21d", interval="1d")` for SPY 0DTE options data.

BOTH fetch options chain data. Alpaca requires OPRA agreement for options (confirmed by handoff.md: "GEX greeks/OI — Greeks still absent from v1beta1 snapshots — OPRA agreement required"). FMP free tier also does not provide options chain data.

DECISION NEEDED: Are these valid T4 uses per the CLAUDE.md carve-out ("yfinance is permitted only for ... any instrument explicitly not available on Alpaca Data or FMP")? Options chain data IS unavailable on Alpaca/FMP without OPRA. If yes, these are legitimate T4 uses and should be documented as approved. If no, need to find an alternative source (CBOE API, polygon.io, etc.) or disable the features.

Board vote required before any change to these functions (options data affects display features in scan results).

PRIORITY: P2 — no immediate trading impact (display-only functions)
FILE: scan_to_html.py
