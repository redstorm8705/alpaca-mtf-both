# ruff: noqa: E501
"""data/earnings_documents.py — the company's own earnings documents for the earnings gap-down review.

Design record: logs/design_records/earn_gap_review_2026-11-04.md (Approval-ID EARN-GAP-REVIEW-1104, owner-approved
2026-11-04). Read-only: no orders, no broker, no state writes outside data/cache/.

Sources (new document tiers approved with the design):
  * SEC EDGAR (free, no key): the Item 2.02 8-K filed after the report and its Exhibit 99.1 press release.
      ticker -> CIK      https://www.sec.gov/files/company_tickers.json   (cached in data/cache/, refreshed daily)
      filings            https://data.sec.gov/submissions/CIK##########.json
      exhibit            https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/<accession>.txt
                         (the full submission; the EX-99.1 <DOCUMENT> is taken from it by its <TYPE> tag)
    SEC asks automated clients to send a descriptive User-Agent; it is read from SEC_USER_AGENT in .env (the owner may
    add a contact address there), with a generic project name as the default.
  * Alpha Vantage EARNINGS_CALL_TRANSCRIPT (free key in ALPHAVANTAGE_API_KEY): the call transcript for one quarter.

The two fetchers return a dict with "ok" (bool) and "reason" (str) and never raise; the helpers never raise either. Fetched text is UNTRUSTED
third-party data: callers pass it to a model as data, never as instructions, and check quotes against it verbatim.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _ROOT / "data" / "cache"
_TICKERS_FILE = _CACHE / "sec_company_tickers.json"
_TICKERS_TTL_S = 86400          # PROV: refresh the SEC ticker map once a day (plumbing cache age)
_SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_SEC_FILING_TXT = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
_AV_URL = "https://www.alphavantage.co/query"
_DEFAULT_UA = "alpaca-mtf-bot earnings research"
_REQUEST_TIMEOUT_S = 20         # PROV: per-request timeout (plumbing)
_SEC_MIN_GAP_S = 0.12           # PROV: SEC fair-access limit is 10 requests/second; stay under it (plumbing)
_last_sec_request = 0.0
_sec_lock = threading.Lock()
# urllib3 logs the full request line (Alpha Vantage's apikey= query) at DEBUG; never let it reach a log.
# Set the logger's OWN level (not left NOTSET) so a later root DEBUG config cannot lower it; never below the level it
# has now, so it adds no INFO lines under a WARNING root.
_u3 = logging.getLogger("urllib3.connectionpool")
_u3.setLevel(max(logging.INFO, _u3.getEffectiveLevel()))


def _ua() -> str:
    return (os.environ.get("SEC_USER_AGENT") or "").strip() or _DEFAULT_UA


def _get(url: str, *, sec: bool, params: dict | None = None, deadline: float | None = None) -> requests.Response:
    """GET with a per-request timeout bounded by the caller's absolute deadline (time.monotonic()). Raises on error."""
    global _last_sec_request
    timeout = float(_REQUEST_TIMEOUT_S)
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline passed before request")
        timeout = min(timeout, left)
    if sec:
        with _sec_lock:   # SEC fair access: one request per spacing interval across all threads
            wait = _SEC_MIN_GAP_S - (time.monotonic() - _last_sec_request)
            if wait > 0:
                time.sleep(wait)
            _last_sec_request = time.monotonic()
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("deadline passed before request")
    headers = {"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"} if sec else None
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def html_to_text(raw: str) -> str:
    """Plain text from an HTML filing: scripts/styles dropped, tags removed, entities decoded, whitespace collapsed."""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw if isinstance(raw, str) else "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def cik_for(symbol: str, deadline: float | None = None) -> "int | None":
    """SEC CIK for a ticker (cached map; refreshed daily). None if unknown or unreachable."""
    sym = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not sym:
        return None
    data = None
    try:
        if _TICKERS_FILE.exists() and time.time() - _TICKERS_FILE.stat().st_mtime < _TICKERS_TTL_S:
            data = json.loads(_TICKERS_FILE.read_text())
    except Exception as exc:
        logger.warning("[edgar] ticker cache unreadable (%s) — refetching", exc)
        data = None
    if data is None:
        try:
            data = _get(_SEC_TICKERS, sec=True, deadline=deadline).json()
        except Exception as exc:
            logger.warning("[edgar] ticker map fetch failed: %s", exc)
            return None
        try:   # cache save is best-effort: a failure never discards the map just fetched
            _CACHE.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(_CACHE), prefix=".sec_tickers.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(json.dumps(data))
                os.replace(tmp, _TICKERS_FILE)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except Exception as exc:
            logger.warning("[edgar] ticker map cache save failed (using the fetched map): %s", exc)
    try:
        for row in (data.values() if isinstance(data, dict) else data):
            if str(row.get("ticker", "")).upper() in (sym, sym.replace(".", "-")):
                return int(row["cik_str"])
    except Exception as exc:
        logger.warning("[edgar] ticker map malformed: %s", exc)
    return None


def _exhibit_991(full_txt: str) -> "str | None":
    """The EX-99.1 document body from a full EDGAR submission .txt, by its <TYPE> tag."""
    for doc in re.findall(r"(?is)<DOCUMENT>(.*?)</DOCUMENT>", full_txt or ""):
        m = re.search(r"(?im)^\s*<TYPE>\s*([^\s<]+)", doc)
        if m and m.group(1).upper() in ("EX-99.1", "EX-99"):
            body = re.search(r"(?is)<TEXT>(.*)</TEXT>", doc)
            return body.group(1) if body else doc
    return None


def edgar_earnings_release(symbol: str, since_utc: datetime, deadline_s: float = 60.0) -> dict:
    """The newest Item 2.02 8-K accepted at or after `since_utc` and its Exhibit 99.1 text.

    Returns {"ok", "reason", "symbol", "cik", "accession", "accepted_at" (ISO UTC), "items", "url", "text", "sha256"}.
    ok=False reasons: bad_input, no_cik, submissions_error, no_filing_since, fetch_error, no_exhibit, empty_text."""
    out: dict = {"ok": False, "reason": "", "symbol": symbol.strip().upper() if isinstance(symbol, str) else "", "source": "sec_edgar"}
    try:
        if since_utc is None or since_utc.tzinfo is None or not (deadline_s > 0):
            out["reason"] = "bad_input"
            return out
        deadline = time.monotonic() + float(deadline_s)
        cik = cik_for(symbol, deadline)
        if cik is None:
            out["reason"] = "no_cik"
            return out
        out["cik"] = cik
        try:
            sub = _get(_SEC_SUBMISSIONS.format(cik=cik), sec=True, deadline=deadline).json()
            rec = sub["filings"]["recent"]
        except Exception as exc:
            out["reason"] = f"submissions_error: {exc}"
            return out
        cands = []
        for i, form in enumerate(rec.get("form", [])):
            try:
                if form not in ("8-K", "8-K/A") or "2.02" not in str(rec["items"][i]):
                    continue
                acc_time = datetime.fromisoformat(str(rec["acceptanceDateTime"][i]).replace("Z", "+00:00"))
                if acc_time.tzinfo is None:
                    acc_time = acc_time.replace(tzinfo=timezone.utc)
            except Exception as exc:   # one malformed row is skipped, not fatal
                logger.warning("[edgar] %s filing row %d unreadable: %s", symbol, i, exc)
                continue
            if acc_time >= since_utc:
                cands.append((acc_time, i))
        if not cands:
            out["reason"] = "no_filing_since"
            return out
        last_reason = "no_exhibit"
        for acc_time, i in sorted(cands, reverse=True):   # newest first; fall back to older qualifying filings
            try:
                acc = str(rec["accessionNumber"][i])
                url = _SEC_FILING_TXT.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc)
                full = _get(url, sec=True, deadline=deadline).text
            except Exception as exc:
                last_reason = f"fetch_error: {exc}"
                continue
            body = _exhibit_991(full)
            text = html_to_text(body) if body is not None else ""
            if not text:
                last_reason = "no_exhibit" if body is None else "empty_text"
                continue
            out.update(ok=True, reason="ok", accession=acc, accepted_at=acc_time.astimezone(timezone.utc).isoformat(),
                       items=str(rec["items"][i]), url=url, text=text, sha256=_sha(text))
            return out
        out["reason"] = last_reason
        return out
    except Exception as exc:  # never raise to the caller (RC-3: logged)
        logger.warning("[edgar] %s release fetch failed: %s", symbol, exc)
        out["reason"] = f"error: {exc}"
        return out


def quarter_label(report_date) -> str:
    """Alpha Vantage quarter label (YYYYQn) for the fiscal quarter most companies report on `report_date`: the
    calendar quarter that ended before the report. Callers with a known fiscal calendar pass `quarter` directly."""
    try:
        y, m = int(report_date.year), int(report_date.month)
    except Exception:
        return ""
    q = (m - 1) // 3          # quarter index of the report month, 0..3; the reported quarter is the one before
    if q == 0:
        return f"{y - 1}Q4"
    return f"{y}Q{q}"


def alpha_vantage_transcript(symbol: str, quarter: str, deadline_s: float = 60.0) -> dict:
    """Call transcript for one quarter ('2026Q3'). Returns {"ok", "reason", "symbol", "quarter", "text", "turns",
    "sha256"}. ok=False reasons: no_key, bad_input, fetch_error, api_note (rate limit / plan message), empty."""
    out: dict = {"ok": False, "reason": "", "symbol": symbol.strip().upper() if isinstance(symbol, str) else "", "quarter": quarter,
                 "source": "alpha_vantage"}
    try:
        key = (os.environ.get("ALPHAVANTAGE_API_KEY") or "").strip()
        if not key:
            out["reason"] = "no_key"
            return out
        if not re.fullmatch(r"\d{4}Q[1-4]", quarter or "") or not (deadline_s > 0):
            out["reason"] = "bad_input"
            return out
        deadline = time.monotonic() + float(deadline_s)
        try:
            data = _get(_AV_URL, sec=False, deadline=deadline,
                        params={"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": out["symbol"],
                                "quarter": quarter, "apikey": key}).json()
        except Exception as exc:
            out["reason"] = f"fetch_error: {type(exc).__name__}"   # never echo the URL (it carries the key)
            return out
        if not isinstance(data, dict):
            out["reason"] = "api_note: non-object response"
            return out
        for k in ("Note", "Information", "Error Message"):
            if k in data:
                out["reason"] = f"api_note: {str(data[k])[:200]}"
                return out
        turns = [t for t in (data.get("transcript") or []) if isinstance(t, dict) and t.get("content")]
        if not turns:
            out["reason"] = "empty"
            return out
        text = "\n".join(f"{t.get('speaker', '')} ({t.get('title', '')}): {t['content']}" for t in turns)
        out.update(ok=True, reason="ok", turns=len(turns), text=text, sha256=_sha(text))
        return out
    except Exception as exc:
        logger.warning("[alpha_vantage] %s %s transcript failed: %s", symbol, quarter, type(exc).__name__)
        out["reason"] = f"error: {type(exc).__name__}"
        return out


def normalize_for_quote(text: str) -> str:
    """Normalization used before a verbatim quote check: curly quotes and dashes to ASCII, whitespace collapsed,
    case kept. Words are never fuzzy-matched (design §8b item 12)."""
    s = text if isinstance(text, str) else ""
    s = s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[‐-―−]", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(quote: str, document: str) -> bool:
    """True when the normalized quote appears verbatim in the normalized document (empty quote -> False)."""
    q = normalize_for_quote(quote)
    return bool(q) and q in normalize_for_quote(document)
