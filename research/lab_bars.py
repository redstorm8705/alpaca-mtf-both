"""Daily bar history for the Confluence 2.0 lab (research tool, never imported by
the bot). Run from the lab environment (requirements-lab.txt):

    python research/lab_bars.py [--rpm 20] [--refresh] [--only AAPL,MSFT]

Inputs: the point-in-time membership lists written by research/pit_universe.py
(logs/lab/universe/{sp500,ndx}_membership.csv) plus a fixed set of market-context
ETFs. For every ticker it fetches split-adjusted daily SIP bars (T1 Alpaca via
data.fetcher.fetch_bars_window) over the ticker's membership window plus a warm-up,
with Alpaca's point-in-time symbol mapping (`asof` = the last date the ticker was a
member) so renamed / reused tickers resolve to the right security.

Outputs: data/cache/lab/bars/1Day/<KEY>.csv.gz (atomic write) — KEY is TICKER for a
current member or TICKER@YYYY-MM-DD for a closed membership interval (each interval is
fetched with its own point-in-time mapping) — and logs/lab/bars_manifest.json (rows,
first/last bar, asof, status per key).
Pacing: --rpm (default 20 requests/minute). The live bot gates itself at 175/min on
the same account (Alpaca data limit 200/min), so 175 + 20 stays under the limit; the
lab's gate is its own (per process), and fetch_bars_window backs off on any 429.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_UNIVERSE = _ROOT / "logs" / "lab" / "universe"
_BARS = _ROOT / "data" / "cache" / "lab" / "bars" / "1Day"
_MANIFEST = _ROOT / "logs" / "lab" / "bars_manifest.json"
_PT = ZoneInfo("America/Los_Angeles")

WARMUP_DAYS = 400  # PROV:~1y+ of history before the window for 252-day features
CONTEXT_ETFS = (
    "SPY",
    "QQQ",
    "IWM",
    "RSP",
    "DIA",
    "XLB",
    "XLC",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLRE",
    "XLU",
    "XLV",
    "XLY",
    "TLT",
    "IEF",
    "SHY",
    "UUP",
    "USO",
    "GLD",
    "HYG",
    "LQD",
)


@dataclass
class Job:
    ticker: str
    start: date
    end: date
    asof: str | None  # None = current member (today's mapping)

    @property
    def key(self) -> str:
        """File/manifest key: TICKER for a current member, TICKER@YYYY-MM-DD for a
        closed interval (the date is the asof used)."""
        return self.ticker if self.asof is None else f"{self.ticker}@{self.asof}"


def _parse_start(s: str) -> date:
    return date.fromisoformat(s[2:] if s.startswith("<=") else s)


def jobs_from_membership(rows: list, today: date) -> dict:
    """{key: Job} with ONE job per membership interval, each fetched with its own
    point-in-time mapping (asof = the interval's end; None if still a member).
    Intervals are never merged: the same ticker string can belong to a different
    company in a later interval (tickers are reused), so a single asof across
    intervals could attach one company's prices to another. The same company's
    closed and reopened intervals simply produce two files (overlap is harmless)."""
    jobs: dict = {}
    for r in rows:
        t = (r.get("ticker") or "").strip().upper()
        if not t:
            continue
        s = _parse_start(r["start"]) - timedelta(days=WARMUP_DAYS)
        e = date.fromisoformat(r["end"]) if r.get("end") else None
        job = Job(t, s, e or today, None if e is None else e.isoformat())
        prev = jobs.get(job.key)
        if prev is None or job.start < prev.start:  # identical key: keep widest
            jobs[job.key] = job
    return jobs


def add_context(jobs: dict, since: date, today: date) -> dict:
    for t in CONTEXT_ETFS:
        if t not in jobs:
            jobs[t] = Job(t, since - timedelta(days=WARMUP_DAYS), today, None)
    return jobs


def fetch_window(job: Job, today: date) -> tuple:
    """(start_utc, end_utc) for the request. SIP excludes the latest ~15 minutes,
    so the window ends at 00:00 UTC of the day after `end`, capped at today."""
    start = datetime(
        job.start.year, job.start.month, job.start.day, tzinfo=timezone.utc
    )
    last = min(job.end + timedelta(days=1), today)
    end = datetime(last.year, last.month, last.day, tzinfo=timezone.utc)
    return start, end


def _write_atomic_csv_gz(path: Path, df) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, compression="gzip")
    tmp.replace(path)


def _read_membership() -> tuple:
    rows: list = []
    since = None
    for idx in ("sp500", "ndx"):
        p = _UNIVERSE / f"{idx}_membership.csv"
        if not p.exists():
            raise SystemExit(f"missing {p}; run research/pit_universe.py first")
        with p.open() as f:
            for r in csv.DictReader(f):
                rows.append(r)
                s = _parse_start(r["start"])
                since = s if since is None or s < since else since
    return rows, since


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpm", type=float, default=20.0)
    ap.add_argument("--refresh", action="store_true", help="refetch existing files")
    ap.add_argument("--only", default="", help="comma-separated tickers")
    a = ap.parse_args(argv)
    import config
    from data.fetcher import fetch_bars_window

    today = datetime.now(_PT).date()
    rows, since = _read_membership()
    jobs = add_context(jobs_from_membership(rows, today), since, today)
    if a.only:
        keep = {t.strip().upper() for t in a.only.split(",")}
        jobs = {k: j for k, j in jobs.items() if j.ticker in keep}
    manifest: dict = {}
    if _MANIFEST.exists():
        manifest = json.loads(_MANIFEST.read_text())
    gap = 60.0 / max(a.rpm, 1.0)
    for i, (key, job) in enumerate(sorted(jobs.items()), 1):
        t = job.ticker
        out = _BARS / f"{key}.csv.gz"
        if out.exists() and not a.refresh:
            continue
        start, end = fetch_window(job, today)
        time.sleep(gap)
        df = fetch_bars_window(
            t,
            config.TF_DAILY,
            start,
            end,
            feed="sip",
            adjustment="split",
            asof=job.asof,
        )
        rec = {"asof": job.asof, "start": str(job.start), "end": str(job.end)}
        if df is None or df.empty:
            rec.update(status="empty", rows=0)
        else:
            _write_atomic_csv_gz(out, df)
            rec.update(
                status="ok",
                rows=len(df),
                first=str(df.index[0].date()),
                last=str(df.index[-1].date()),
            )
        manifest[key] = rec
        if i % 25 == 0:
            print(f"  {i}/{len(jobs)} {key} {rec['status']} {rec['rows']}", flush=True)
            tmp = _MANIFEST.with_suffix(".tmp")
            tmp.write_text(json.dumps(manifest, indent=1))
            tmp.replace(_MANIFEST)
    _MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = _MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.replace(_MANIFEST)
    ok = sum(1 for r in manifest.values() if r.get("status") == "ok")
    empty = sorted(t for t, r in manifest.items() if r.get("status") == "empty")
    print(json.dumps({"tickers": len(manifest), "ok": ok, "empty": empty}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
