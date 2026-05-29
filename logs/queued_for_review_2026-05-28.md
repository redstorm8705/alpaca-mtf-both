## scan_to_html.py — RC-9 (T4 yfinance news violation) — queued 2026-05-28 07:05 PM PT

REASON: RTH-chain — strategy/run_cycle.py imports scan_to_html directly. DS/GAI gate applies per RULE C-5. Cannot be autonomously applied.

FINDING: `_fetch_yfinance_news(symbol)` uses `yf.Ticker(symbol).news` to fetch news data. yfinance is approved only for `^VIX`, `^VIX3M`, and `JPY=X` (T4 explicit list). News is not in any approved tier (T1=Alpaca, T2=FMP, T3=TraderMonty CSV). This is a T4 violation documented in handoff.md P2.

RTH CLASSIFICATION: RTH-chain confirmed
- `strategy/run_cycle.py` → imports `scan_to_html` (direct)
- run_cycle.py is an RTH entry point → scan_to_html.py is RTH-chain

DRAFT PATCH (conceptual — not code-level):
```
Before:
    # _fetch_yfinance_news(symbol) — uses yf.Ticker(symbol).news
    # Returns list of {"title": str, "link": str, "providerPublishTime": int}

After (proposed — FMP T2 migration):
    # Replace with FMP T2 /v3/stock_news?tickers={symbol}&limit=5&apikey={FMP_API_KEY}
    # from data.fmp_client import get_fmp_news  (new helper or inline)
    # Returns list of {"title": str, "url": str, "publishedDate": str, "source": str}
    # On API failure or missing FMP_API_KEY: return [] (fail-closed, log WARNING + T4 tag)
    # Data source tag: "fmp_news_t2" in output metadata
```

Full implementation requires:
1. Read scan_to_html.py in full (2,580L — Explore subagent, last read S39)
2. Locate exact function signature and callers of `_fetch_yfinance_news`
3. Check if data/fmp_client.py has a news endpoint already (likely /v3/stock_news)
4. DS/GAI diff-level review (RTH-chain per RULE C-5)
5. Full 9-step mandatory patch sequence

BOARD: A=[not run — RTH gate pre-empts] | B=[not run] | C=[not run]
ACTION: DS/GAI review required before patch can be proposed. Requires full board vote for migration plan (FMP T2 news source — new data tier usage). Present to Rafael for scheduling.

PRIORITY: P2 (from handoff.md)
SOURCE: handoff.md open items (Gist unavailable tonight)
