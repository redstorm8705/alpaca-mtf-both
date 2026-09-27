"""Point-in-time S&P 500 / Nasdaq-100 universe for the Confluence 2.0 lab.

Research tool (never imported by the bot). Run from the lab environment
(requirements-lab.txt), not the bot's venv:

    python research/pit_universe.py [--since YYYY-MM-DD] [--no-fetch] [--archive]

1. Candidate change lists: Wikipedia "Historical components of the S&P 500" and
   "... of the Nasdaq-100" (dated add/remove rows with citations).
2. Each change is checked against its cited PRIMARY document (S&P Dow Jones Indices
   or Nasdaq). S&P announcement PDFs refuse automated downloads (HTTP 403), so the
   Internet Archive's raw copy (`id_` URL) of the SAME document is used.
   Status per change: VERIFIED_PRIMARY | PRIMARY_UNREACHABLE | PRIMARY_MISMATCH |
   SECONDARY_ONLY | NO_SOURCE. Nothing is accepted silently; counts are reported.
3. Membership is walked BACKWARD from today's constituents (S&P: Wikipedia current
   list; Nasdaq-100: Nasdaq's own list API) through each change to `--since`.
   Rows that contradict the walk (adding a current member, removing a non-member)
   are flagged as anomalies, never silently applied.

Outputs (logs/lab/universe/): <index>_changes.csv, <index>_membership.csv
(ticker, start, end), <index>_summary.json. Fetch cache: data/cache/pit/.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_OUT = _ROOT / "logs" / "lab" / "universe"
_CACHE = _ROOT / "data" / "cache" / "pit"
_PT = ZoneInfo("America/Los_Angeles")

WIKI_HIST = {
    "sp500": "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500",
    "ndx": "https://en.wikipedia.org/wiki/Historical_components_of_the_Nasdaq-100",
}
WIKI_SP500_CURRENT = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ_NDX_CURRENT = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"

PRIMARY_DOMAINS = {
    "sp500": (
        "spglobal.com",
        "spice-indices.com",
        "spindices.com",
        "standardandpoors.com",
        "mcgraw-hill.com",
    ),
    "ndx": ("nasdaq.com", "globenewswire.com", "nasdaqomx.com"),
}
_BLOCKED_DIRECT = (
    "www.spglobal.com",
    "spice-indices.com",
    "spindices.com",
    "standardandpoors.com",
)  # 403 to automated fetches -> Wayback copy
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_FETCH_DELAY_S = 1.0  # politeness between network requests


# ─── pure helpers (unit-tested) ──────────────────────────────────────────────


@dataclass
class Change:
    index: str
    effective: date
    added: str
    added_name: str
    removed: str
    removed_name: str
    reason: str
    ref_ids: list = field(default_factory=list)
    added_cur: str = ""  # today's ticker for the added security (rename-resolved)
    removed_cur: str = ""  # today's ticker for the removed security
    status: str = ""
    source_url: str = ""
    note: str = ""


def _clean(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(s: str) -> date | None:
    s = _clean(s)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def expand_rows(table) -> list[list]:
    """Rows of an lxml <table> as lists of cell elements, with rowspan/colspan
    expanded so every data row has one element per column."""
    grid: list[list] = []
    pending: dict[int, tuple] = {}  # col -> (cell, rows_left)
    for tr in table.xpath(".//tr"):
        cells = tr.xpath("./td|./th")
        row: list = []
        col = 0
        it = iter(cells)
        while True:
            if col in pending:
                cell, left = pending[col]
                row.append(cell)
                if left > 1:
                    pending[col] = (cell, left - 1)
                else:
                    del pending[col]
                col += 1
                continue
            try:
                cell = next(it)
            except StopIteration:
                break
            span_c = int(cell.get("colspan", "1") or 1)
            span_r = int(cell.get("rowspan", "1") or 1)
            for _ in range(span_c):
                row.append(cell)
                if span_r > 1:
                    pending[col] = (cell, span_r - 1)
                col += 1
        while col in pending:  # trailing spanned cells
            cell, left = pending[col]
            row.append(cell)
            if left > 1:
                pending[col] = (cell, left - 1)
            else:
                del pending[col]
            col += 1
        grid.append(row)
    return grid


def parse_changes(html: str, index: str) -> tuple[list[Change], dict]:
    """(changes, refs) from a Wikipedia historical-components page. refs maps a
    cite_note id -> list of external URLs in that footnote."""
    import lxml.html

    doc = lxml.html.fromstring(html)
    tables = doc.xpath('//table[contains(@class,"wikitable")]')
    if not tables:
        return [], {}
    table = max(tables, key=lambda t: len(t.xpath(".//tr")))
    out: list[Change] = []
    for row in expand_rows(table):
        if len(row) < 6 or row[0].tag == "th":
            continue
        eff = _parse_date(row[0].text_content())
        if eff is None:
            continue
        ref_ids = []
        for cell in row:
            for a in cell.xpath(".//sup//a[@href]"):
                href = a.get("href", "")
                if href.startswith("#cite_note") and href[1:] not in ref_ids:
                    ref_ids.append(href[1:])
        out.append(
            Change(
                index=index,
                effective=eff,
                added=_clean(row[1].text_content()).upper(),
                added_name=_clean(row[2].text_content()),
                removed=_clean(row[3].text_content()).upper(),
                removed_name=_clean(row[4].text_content()),
                reason=_clean(row[5].text_content()),
                ref_ids=ref_ids,
            )
        )
    refs: dict = {}
    for li in doc.xpath('//li[starts-with(@id,"cite_note")]'):
        urls = [
            a.get("href")
            for a in li.xpath(".//a[@href]")
            if (a.get("href") or "").startswith("http")
        ]
        refs[li.get("id")] = urls
    return out, refs


def primary_urls(urls: list, index: str) -> list:
    doms = PRIMARY_DOMAINS[index]
    return [
        u
        for u in urls
        if any(d in u.split("/")[2] for d in doms) and "wikipedia" not in u
    ]


def wayback_raw(url: str, timestamp: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{url}"


_COMMON_FIRST_WORDS = {
    "american",
    "first",
    "united",
    "general",
    "international",
    "national",
    "global",
    "new",
    "southern",
    "northern",
    "western",
    "eastern",
    "public",
}


def _name_key(name: str) -> str:
    """First significant word of a company name (two words when the first is a
    common one such as 'American'), lower-case; '' if nothing usable."""
    words = [
        w.lower()
        for w in re.findall(r"[A-Za-z0-9&']+", name or "")
        if w.lower() not in {"the", "inc", "corp", "co", "company", "plc", "ltd"}
    ]
    if not words:
        return ""
    if words[0] in _COMMON_FIRST_WORDS and len(words) > 1:
        return f"{words[0]} {words[1]}"
    return words[0]


def side_found(text: str, ticker: str, name: str) -> bool:
    """The ticker in exchange-quoted form ('(NYSE:FLS)', 'NASD: NXPI',
    '(Nasdaq: PLTR)', '(XYZ)') or the company-name key appears in the document.
    Bare-word ticker matches are not accepted: one-letter tickers such as 'A'
    or 'T' would match ordinary words."""
    if not ticker and not name:
        return True
    t = text or ""
    if ticker and re.search(rf"[:(]\s*{re.escape(ticker)}(?![A-Za-z0-9.])", t):
        return True
    key = _name_key(name)
    return len(key) >= 3 and key in re.sub(r"\s+", " ", t.lower())


_PRESS_NEAR_CHARS = 400  # PROV:one-sentence-scale-window-for-an-S&P-500-replacement


def _ticker_positions(text: str, ticker: str) -> list:
    pat = rf"[:(]\s*{re.escape(ticker)}(?![A-Za-z0-9.])"
    return [m.start() for m in re.finditer(pat, text or "")]


def verify_press_change(ch: Change, text: str) -> bool:
    """Stricter match for a press release NOT cited for this row (S&P releases often
    cover several indexes). Both tickers (exchange-quoted) must appear within
    _PRESS_NEAR_CHARS of each other, with "S&P 500" inside that passage (extended by
    the same distance), so a MidCap/SmallCap move elsewhere in the release cannot
    verify an unrelated S&P 500 row. One-sided rows: the ticker within
    _PRESS_NEAR_CHARS of an "S&P 500" mention."""
    t = text or ""
    sp = [m.start() for m in re.finditer(r"S&P\s*500", t)]
    if not sp:
        return False
    near = _PRESS_NEAR_CHARS
    if ch.added and ch.removed:
        for pa in _ticker_positions(t, ch.added):
            for pr in _ticker_positions(t, ch.removed):
                if abs(pa - pr) > near:
                    continue
                lo, hi = min(pa, pr) - near, max(pa, pr) + near
                if any(lo <= p <= hi for p in sp):
                    return True
        return False
    one = ch.added or ch.removed
    return any(abs(p - q) <= near for p in _ticker_positions(t, one) for q in sp)


def verify_change(ch: Change, text: str) -> bool:
    return side_found(text, ch.added, ch.added_name) and side_found(
        text, ch.removed, ch.removed_name
    )


_RENAME_REASON = re.compile(
    r"chang\w* (its |their )?(ticker|symbol)|ticker (symbol )?change", re.I
)
_RENAME_GRACE_DAYS = 30  # PROV:index-tables-may-cite-a-ticker-renamed-days-earlier


def resolve_current(ticker: str, on: date, renames: list) -> str:
    """The ticker a security that traded as `ticker` on date `on` uses today,
    following dated renames forward (old_symbol -> new_symbol on process_date).
    Date-aware because tickers are reused (e.g. META was a Metaverse ETF until
    2022-01-31, then Meta Platforms from 2022-06-09). Renames up to
    _RENAME_GRACE_DAYS before `on` also apply: an index change can be written
    with a ticker the company dropped a few days earlier (FBHS -> FBIN on
    2022-12-15, removal row dated 2022-12-19 still says FBHS)."""
    t, d = ticker, (on - timedelta(days=_RENAME_GRACE_DAYS)).isoformat()
    for _ in range(20):  # rename chains are short; bound against cycles
        nxt = [r for r in renames if r["old_symbol"] == t and r["process_date"] > d]
        if not nxt:
            return t
        r = min(nxt, key=lambda r: r["process_date"])
        t, d = r["new_symbol"], r["process_date"]
    return t


_PRESS_LOOKBACK_DAYS = 30  # PROV:S&P-announces-changes-days-to-weeks-before-effective


def press_candidates(releases: list, effective: date) -> list:
    """S&P press releases dated within _PRESS_LOOKBACK_DAYS before (or on) an
    effective date, nearest first. `releases` = [(date, title, url)]."""
    lo = effective - timedelta(days=_PRESS_LOOKBACK_DAYS)
    hits = [r for r in releases if lo <= r[0] <= effective]
    return [r[2] for r in sorted(hits, key=lambda r: r[0], reverse=True)]


def walk_membership(current: set, changes: list[Change], since: date):
    """Walk membership backward from `current` through `changes` (any order).
    Returns (intervals, anomalies): intervals = {ticker: [(start|None, end|None)]}
    where None start = before `since`, None end = still a member; anomalies are
    changes that contradict the walk (recorded, not applied to the contradicted
    side).

    All changes sharing an effective date are applied as ONE batch: a same-day
    chain (Z replaced by Y, then Y replaced by X) nets to Z -> X, so row order
    within a day cannot create a false contradiction or a phantom member. A
    ticker added and removed on the same day is a zero-length transient and is
    skipped. Rows whose two sides are the same security (a ticker change) are
    not membership changes."""
    members = set(current)
    end_of: dict = {t: None for t in members}  # ticker -> open interval end
    intervals: dict = {}
    anomalies = []
    by_date: dict = {}
    for ch in changes:
        if ch.effective >= since:
            by_date.setdefault(ch.effective, []).append(ch)
    for d in sorted(by_date, reverse=True):
        adds: dict = {}
        rems: dict = {}
        for ch in by_date[d]:
            a = ch.added_cur or ch.added
            r = ch.removed_cur or ch.removed
            if a and a == r:
                continue
            if a:
                adds.setdefault(a, ch)
            if r:
                rems.setdefault(r, ch)
        for t, ch in adds.items():
            if t in rems:
                continue  # same-day transient
            if t in members:
                intervals.setdefault(t, []).append((d, end_of.pop(t)))
                members.discard(t)
            else:
                anomalies.append((ch, f"added {t} not a member after the change"))
        for t, ch in rems.items():
            if t in adds:
                continue
            if t in members:
                anomalies.append((ch, f"removed {t} still a member after the change"))
            else:
                members.add(t)
                end_of[t] = d
    for t in members:
        intervals.setdefault(t, []).append((None, end_of.get(t)))
    return intervals, anomalies


# ─── network (lab only) ──────────────────────────────────────────────────────


_ARCHIVE = {
    "enabled": False
}  # --archive: Internet Archive fallback (slow; can be blocked)
_ARCHIVE_DELAY_S = 6.0  # PROV:archive.org refused connections at ~1 req/s on 2026-09-27
_REFUSED_BACKOFF_S = (60.0, 180.0)  # waits after a refused/failed connection


def _fetch(url: str, *, binary: bool = False, headers: dict | None = None):
    """GET with an on-disk cache (atomic write). Only successful responses are
    cached, so an interrupted run resumes where it stopped. Slower pacing for
    archive.org, with backoff when a connection is refused."""
    import requests

    _CACHE.mkdir(parents=True, exist_ok=True)
    key = _CACHE / hashlib.sha1(url.encode()).hexdigest()
    if key.exists():
        data = key.read_bytes()
        return data if binary else data.decode("utf-8", "replace")
    archive = "archive.org" in url.split("/")[2]
    r = None
    for attempt in range(len(_REFUSED_BACKOFF_S) + 1):
        time.sleep(_ARCHIVE_DELAY_S if archive else _FETCH_DELAY_S)
        try:
            r = requests.get(
                url, headers={"User-Agent": _UA, **(headers or {})}, timeout=45
            )
            if r.status_code != 429:
                break
        except requests.RequestException as e:
            print(f"  fetch failed {url}: {e}", file=sys.stderr)
            r = None
        if not archive:
            break  # only archive.org gets the long backoff; others fail fast
        if attempt < len(_REFUSED_BACKOFF_S):
            time.sleep(_REFUSED_BACKOFF_S[attempt])
    if r is None or r.status_code != 200 or not r.content:
        return None
    tmp = key.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(key)
    return r.content if binary else r.text


def _doc_text(url: str) -> str | None:
    host = url.split("/")[2]
    data = None
    if not any(b in host for b in _BLOCKED_DIRECT):
        data = _fetch(url, binary=True)
    if data is None and _ARCHIVE["enabled"]:
        import requests

        time.sleep(_ARCHIVE_DELAY_S)
        try:
            snap = requests.get(
                "https://archive.org/wayback/available", params={"url": url}, timeout=30
            ).json()
        except (requests.RequestException, ValueError):
            snap = {}
        closest = snap.get("archived_snapshots", {}).get("closest", {})
        if closest.get("available"):
            data = _fetch(wayback_raw(url, closest["timestamp"]), binary=True)
    if data is None:
        return None
    if data[:4] == b"%PDF":
        import pypdf

        try:
            return "".join(
                p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(data)).pages
            )
        except Exception as e:  # malformed PDF -> unreachable, logged
            print(f"  pdf parse failed {url}: {e}", file=sys.stderr)
            return None
    import lxml.html

    try:
        return lxml.html.fromstring(data).text_content()
    except Exception as e:  # malformed HTML -> unreachable, logged
        print(f"  html parse failed {url}: {e}", file=sys.stderr)
        return None


def _renames(changes: list[Change], since: date) -> list:
    """Dated ticker renames (T1 Alpaca corporate actions) for every ticker in the
    change rows, following new tickers until no new ones appear. Aborts on a failed
    lookup rather than silently walking with an incomplete rename map."""
    from data.alpaca_data import get_name_changes

    today = datetime.now(_PT).date().isoformat()
    seen: set = set()
    todo = {t for c in changes for t in (c.added, c.removed) if t}
    out: list = []
    while todo:
        batch = sorted(todo)[:50]
        todo -= set(batch)
        seen |= set(batch)
        got = get_name_changes(since.isoformat(), today, batch)
        if got is None:
            raise SystemExit(f"rename lookup failed for {batch[:5]}...; aborting")
        for r in got:
            if r not in out:
                out.append(r)
            for t in (r["old_symbol"], r["new_symbol"]):
                if t not in seen:
                    todo.add(t)
    return out


PRESS_LIST = "https://press.spglobal.com/index.php?s=2429&l=100&o={offset}"


def press_index(since: date) -> list:
    """S&P DJI press releases whose title mentions the S&P 500, from the press site's
    listing pages (fetched fresh, not cached), back to `since` minus the lookback."""
    import lxml.html
    import requests

    out: list = []
    stop = since - timedelta(days=_PRESS_LOOKBACK_DAYS)
    for page in range(60):  # ~22 pages cover 10 years; bounded
        time.sleep(_FETCH_DELAY_S)
        try:
            r = requests.get(
                PRESS_LIST.format(offset=page * 100),
                headers={"User-Agent": _UA},
                timeout=45,
            )
        except requests.RequestException as e:
            raise SystemExit(f"press listing fetch failed (page {page}): {e}") from e
        if r.status_code != 200:
            raise SystemExit(f"press listing HTTP {r.status_code} (page {page})")
        oldest = None
        for a in lxml.html.fromstring(r.text).xpath("//a[@href]"):
            m = re.search(r"/(\d{4}-\d{2}-\d{2})-", a.get("href") or "")
            if not m:
                continue
            d = date.fromisoformat(m.group(1))
            oldest = d if oldest is None or d < oldest else oldest
            title = _clean(a.text_content())
            if "S&P 500" in title and d >= stop:
                out.append((d, title, a.get("href")))
        if oldest is None or oldest < stop:
            break
    return sorted(set(out))


def current_members(index: str) -> set:
    if index == "ndx":
        raw = _fetch(NASDAQ_NDX_CURRENT, headers={"Accept": "application/json"})
        rows = json.loads(raw)["data"]["data"]["rows"] if raw else []
        return {r["symbol"].strip().upper() for r in rows}
    import lxml.html

    html = _fetch(WIKI_SP500_CURRENT)
    doc = lxml.html.fromstring(html or "")
    table = doc.xpath('//table[@id="constituents"]')
    if not table:
        return set()
    return {
        _clean(r[0].text_content()).upper()
        for r in expand_rows(table[0])[1:]
        if r and r[0].tag == "td"
    }


def build(index: str, since: date, fetch_docs: bool = True) -> dict:
    html = _fetch(WIKI_HIST[index])
    if not html:
        raise SystemExit(f"cannot fetch {WIKI_HIST[index]}")
    changes, refs = parse_changes(html, index)
    changes = [c for c in changes if c.effective >= since]
    renames = _renames(changes, since)
    for ch in changes:
        ch.added_cur = (
            resolve_current(ch.added, ch.effective, renames) if ch.added else ""
        )
        ch.removed_cur = (
            resolve_current(ch.removed, ch.effective, renames) if ch.removed else ""
        )
    press = press_index(since) if (index == "sp500" and fetch_docs) else []
    unconfirmed_renames = []
    for ch in changes:
        if ch.added_cur and ch.added_cur == ch.removed_cur:
            ch.status = "RENAME"
            continue
        if ch.added and ch.removed and _RENAME_REASON.search(ch.reason):
            # The index table's reason text mentions a ticker change but Alpaca has
            # no rename record (sparse before ~2020). Flag it and still run the
            # primary-document check: the text may refer to a third company.
            # Membership stays correct either way (the walk keys on tickers used
            # on each date).
            ch.note = "reason mentions a ticker change; no Alpaca rename record"
            unconfirmed_renames.append(f"{ch.effective} {ch.removed}->{ch.added}")
        urls = [u for rid in ch.ref_ids for u in refs.get(rid, [])]
        prim = primary_urls(urls, index)
        cited = [
            u
            for u in prim
            if _ARCHIVE["enabled"]
            or not any(b in u.split("/")[2] for b in _BLOCKED_DIRECT)
        ]
        extra = press_candidates(press, ch.effective) if press else []
        if fetch_docs:
            for u in cited + [x for x in extra if x not in cited]:
                text = _doc_text(u)
                if text is None:
                    continue
                ok = (
                    verify_change(ch, text)
                    if u in cited
                    else verify_press_change(ch, text)
                )
                if ok:
                    ch.status, ch.source_url = "VERIFIED_PRIMARY", u
                    break
                if u in cited:
                    ch.status, ch.source_url = "PRIMARY_MISMATCH", u
            if ch.status in ("VERIFIED_PRIMARY", "PRIMARY_MISMATCH"):
                continue
        ch.note = (ch.note + "; " if ch.note else "") + (
            "accepted on the Wikipedia index table (Rafael 2026-09-27)"
        )
        if prim:
            ch.status, ch.source_url = "PRIMARY_UNREACHABLE", prim[0]
            if not fetch_docs:
                ch.note = "fetch disabled"
        elif urls:
            ch.status, ch.source_url = "SECONDARY_ONLY", urls[0]
        else:
            ch.status = "NO_SOURCE"
    current = current_members(index)
    intervals, anomalies = walk_membership(current, changes, since)
    _OUT.mkdir(parents=True, exist_ok=True)
    import csv

    anomaly_ids = {id(c) for c, _ in anomalies}
    with (_OUT / f"{index}_changes.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "effective",
                "added",
                "added_now",
                "added_name",
                "removed",
                "removed_now",
                "removed_name",
                "reason",
                "status",
                "source_url",
                "anomaly",
                "note",
            ]
        )
        for c in changes:
            w.writerow(
                [
                    c.effective,
                    c.added,
                    c.added_cur,
                    c.added_name,
                    c.removed,
                    c.removed_cur,
                    c.removed_name,
                    c.reason,
                    c.status,
                    c.source_url,
                    "; ".join(m for x, m in anomalies if x is c)
                    if id(c) in anomaly_ids
                    else "",
                    c.note,
                ]
            )
    with (_OUT / f"{index}_membership.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "start", "end"])
        for t in sorted(intervals):
            for s, e in sorted(intervals[t], key=lambda x: x[0] or date.min):
                w.writerow([t, s or f"<={since}", e or ""])
    counts: dict = {}
    for c in changes:
        counts[c.status] = counts.get(c.status, 0) + 1
    summary = {
        "index": index,
        "since": str(since),
        "built_at_pt": datetime.now(_PT).strftime("%Y-%m-%d %I:%M %p PT"),
        "changes": len(changes),
        "status_counts": counts,
        "current_members": len(current),
        "anomalies": len(anomalies),
        "renames_without_alpaca_record": unconfirmed_renames,
    }
    (_OUT / f"{index}_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def ten_years_before(d: date) -> date:
    try:
        return d.replace(year=d.year - 10)
    except ValueError:  # Feb 29 -> Feb 28
        return d.replace(year=d.year - 10, day=28)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="default: 10 years before today")
    ap.add_argument("--index", choices=["sp500", "ndx", "both"], default="both")
    ap.add_argument(
        "--no-fetch", action="store_true", help="skip primary document fetches"
    )
    ap.add_argument(
        "--archive",
        action="store_true",
        help="also try the Internet Archive copy of blocked S&P PDFs (slow)",
    )
    a = ap.parse_args(argv)
    _ARCHIVE["enabled"] = a.archive
    since = (
        date.fromisoformat(a.since)
        if a.since
        else ten_years_before(datetime.now(_PT).date())
    )
    for idx in ["sp500", "ndx"] if a.index == "both" else [a.index]:
        print(json.dumps(build(idx, since, fetch_docs=not a.no_fetch), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
