## scan_to_html.py + data/fmp_client.py — T4 violation fix — queued 2026-06-22 S63 autonomous

REASON: board FAIL [Agent B] — 2/3 PASS (A=PASS, B=FAIL, C=PASS) — one FAIL → STEP 6 per protocol

FINDING: _fetch_yfinance_news() at scan_to_html.py L1226-1303 uses yfinance for US equity/ETF news
(T4 violation — yfinance approved only for ^VIX, ^VIX3M, JPY=X per CLAUDE.md §1).
Proposed fix: replace with FMP T2 /stable/news + add get_news() to data/fmp_client.py.
Board vote: A=PASS | B=FAIL | C=PASS

BOARD:
  A=PASS — legitimate T4→T2 data source upgrade, no forbidden categories, atomic write preserved,
    all exception paths handled.

  B=FAIL — 4 substantive findings (primary blockers + secondary):

    BLOCKER 1 (RC-6 HIGH): FMP /stable/news field names unverified.
      - `n.get("publisher")` does not exist in FMP news responses — correct field is `site`.
        The fallback chain `n.get("publisher") or n.get("site")` produces correct result via `site`
        but `publisher` is dead code masking the real field — RC-6 pattern.
      - `n.get("headline")` also does not exist in FMP /stable/news — `title` is the correct field.
        `(n.get("title") or n.get("headline") or "")` silently drops items if `title` is empty
        (returns "" not None), so `headline` fallback never fires. Dead code.
      FIX REQUIRED: Change to `(n.get("title") or "").strip()` and
        `n.get("site") or syms_str` (no publisher fallback).

    BLOCKER 2 (THREAT 7 MEDIUM): FMP quota exhaustion logged at DEBUG only.
      When FMP returns HTTP non-200 (including 429 rate limit), code logs at DEBUG level only.
      Per CLAUDE.md guardrails §5, T2 failures affecting RTH display should log WARNING.
      FIX REQUIRED: Change `logger.debug("FMP news: HTTP %d"...)` to
        `logger.warning("FMP news: HTTP %d — news unavailable"...)` for non-200 responses.

    SECONDARY (THREAT 4 LOW): Cache not keyed on parameters.
      Cache keyed only on file path, not on (symbols, max_items, max_age_hours). If a second
      caller ever uses different params, cached result from first call is returned without
      re-filtering. Currently only ONE caller exists (scan_to_html.py L1678), so not a bug today.
      ACCEPTABLE as-is given single caller. Note for future: add param-keyed cache if second
      caller is ever added.

    SECONDARY (THREAT 2 RC-5 LOW): Cache write uses write_text() (non-atomic). Consistent with
      existing fmp_client.py pattern. Acceptable (30-min TTL, display-only cache).
      No fix required — matches existing pattern.

  C=PASS — zero trading-decision impact, background thread only, return value discarded,
    no forbidden files touched, pure data-source compliance fix.

PROPOSED DIFF (3 sites when re-submitted):
  Site 1 (scan_to_html.py L1226-1303): Replace _fetch_yfinance_news() body with FMP T2 call.
    [SEE AUDIT in tb_audit_log.md S63 entry for full proposed body]
  Site 2 (data/fmp_client.py end): Add get_news() with CORRECTED field names:
    - Change: `(n.get("title") or n.get("headline") or "").strip()` →
              `(n.get("title") or "").strip()`
    - Change: `n.get("publisher") or n.get("site") or syms_str` →
              `n.get("site") or syms_str`
    - Change: `logger.debug("FMP news: HTTP %d", resp.status_code)` →
              `logger.warning("FMP news: HTTP %d — news unavailable", resp.status_code)`

STATIC ANALYSIS (pre-patch, current files):
  py_compile: PASS | mypy: PASS (0 errors) | ruff: PASS (0 violations)

INTEGRITY ANCHORS:
  sha256 scan_to_html.py: b7b67d6f6354538501d269108f08ef9fabbdf3297c01101f33b66f45dc7a8460
  sha256 fmp_client.py:   7bd9c8f47f4fda4ee3dd1ce64e5e884c5b22e917c3cdbde686d1a62d97c20dfb
  BASE_COMMIT:            bfb6c82a945835cfb7e619e592188a9dca1ed34a

DECISIONS NEEDED BEFORE PATCH CAN PROCEED:
  1. Confirm FMP /stable/news response fields: `title`, `publishedDate`, `site` are correct —
     verify against live FMP API response or FMP stable API docs before re-drafting.
     (RC-6 requires field verification against confirmed live response, not assumed from docs.)
  2. Apply the 3 field-name and log-level corrections listed above.
  3. Re-run full board vote (3/3 PASS required) with corrected draft.

ACTION: Next interactive session — apply 3 corrections, re-run board vote, submit for DS/GAI.
  Full patch sequence resets to Step 1 per RULE C-2 (session boundary).
