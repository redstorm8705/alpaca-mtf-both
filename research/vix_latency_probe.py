#!/usr/bin/env python3
# ruff: noqa: E501
"""research/vix_latency_probe.py — READ-ONLY VIX source latency probe (REGIME-SIZE-1104 design item 5, Katsuyama Q22).

Every 2 minutes during regular hours it reads each VIX source once and logs, per source, the value, its own as-of time,
the fetch time and fetch − as-of. One JSON line per source per pass to logs/research/vix_latency_<date>.jsonl.
Sources: Schwab $VIX (data/schwab_quotes.index_last_with_reasons, read-only quotes API) and the Yahoo chart endpoint the
bot uses today (config.VIX_API_URL; the last non-null 1-minute bar and its timestamp).

No orders, no state writes outside logs/research/, never imported by the bot.
    python3 research/vix_latency_probe.py [--once]
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
OUT_DIR = ROOT / "logs" / "research"


def _schwab() -> dict:
    from data import schwab_quotes as sq
    t0 = datetime.now(timezone.utc)
    vals, reasons = sq.index_last_with_reasons(["$VIX"], {"$VIX": 86400.0}, deadline_s=15.0)
    q = vals.get("$VIX")
    if q is None:
        return {"source": "schwab", "fetch_utc": t0.isoformat(), "error": reasons.get("$VIX", "no value")}
    return {"source": "schwab", "value": q.last, "asof_utc": q.trade_time.isoformat(),
            "fetch_utc": q.fetch_time.isoformat(), "lag_s": round(q.age_s, 1), "latency_s": round(q.latency_s, 2)}


def _yahoo() -> dict:
    import requests

    import config
    t0 = datetime.now(timezone.utc)
    try:
        r = requests.get(config.VIX_API_URL, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        t1 = datetime.now(timezone.utc)
        res = r.json()["chart"]["result"][0]
        pairs = [(ts, c) for ts, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0]["close"], strict=False)
                 if c is not None]
        if not pairs:
            return {"source": "yahoo", "fetch_utc": t1.isoformat(), "error": "no non-null bar"}
        ts, c = pairs[-1]
        asof = datetime.fromtimestamp(ts, timezone.utc)
        return {"source": "yahoo", "value": round(float(c), 2), "asof_utc": asof.isoformat(),
                "fetch_utc": t1.isoformat(), "lag_s": round((t1 - asof).total_seconds(), 1),
                "latency_s": round((t1 - t0).total_seconds(), 2), "http": r.status_code}
    except Exception as e:  # noqa: BLE001
        return {"source": "yahoo", "fetch_utc": t0.isoformat(), "error": repr(e)[:200]}


def one_pass() -> list:
    rows = []
    for fn in (_schwab, _yahoo):
        try:
            rows.append(fn())
        except Exception as e:  # noqa: BLE001
            rows.append({"source": fn.__name__.strip("_"), "error": repr(e)[:200]})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / f"vix_latency_{datetime.now(ET):%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({"ts_et": datetime.now(ET).isoformat(), **r}) + "\n")
    return rows


def main(argv: list) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    if "--once" in argv:
        print(json.dumps(one_pass(), indent=1))
        return 0
    while True:
        now = datetime.now(ET)
        mins = now.hour * 60 + now.minute
        if now.weekday() >= 5 or mins >= 16 * 60 + 5:
            return 0
        if mins >= 9 * 60 + 25:
            one_pass()
        time.sleep(120)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
