#!/usr/bin/env python3
# ruff: noqa: E501
"""research/bv5_block_replay.py — point-in-time replay of the main MTF scan (signed: Claude, 2026-10-26).

Why: BV-5 (strategy/run_cycle.py) skips the main scan entirely while the Macro Risk Index is HIGH/CRITICAL, so on a
blocked cycle no signal was ever computed or logged. This tool regenerates those signals with the LIVE scan code
(strategy.signal_generator.run_scan, unmodified) fed point-in-time bars, so the owner can see what the block stopped.
Design record: logs/design_records/bv5_mri_entry_block_2026-10-25.md.

How it stays faithful:
  * Scores come from the real run_scan / confluence code; only its inputs are swapped (module attributes patched in
    this process only): build_scan_universe -> the universe given; fetch_multi_timeframe / fetch_bars -> bars as the
    live scan would have seen them at time T; get_earnings_surprise -> None (16-pt log only; no FMP calls); the
    score-comparison log writes -> a scratch directory under logs/research/.
  * Bars at time T: settled history before the session, plus the session's 1-minute bars that had CLOSED by T,
    aggregated into the same 15Min / 1Hour / 1Day / 1Week buckets (extended hours kept on intraday frames, regular
    session only on the daily bar, matching Alpaca's bar conventions). The still-forming bar is included, as the live
    fetch_bars returns it. Tail lengths follow config.BARS_TO_FETCH (num_bars when the caller passes one).
  * Feed: IEX (the account's real-time feed, which the live fetch_bars uses).

Fidelity check (`validate`): on a normal day, replay the scans the bot actually logged in
logs/score_comparison_events.jsonl and compare every ticker's 12-point long/short score. Limits: the live weekly-bias
cache can hold a split-bar bias the replay cannot see; bar revisions after the fact.

Outcome (`replay`): for each regenerated signal, the move from the first price after T to +30 min, +60 min, the close
and the next session's close, signed by direction, with max favourable / adverse excursion to the close. No stop /
target / later entry gates are modelled (ORB confirmation, raised MIN_SCORE, confirm buffer, sizing), so the signal
count is an UPPER bound on entries.

Read-only: market data only (T1 Alpaca via data.fetcher.fetch_bars_window, throttled), no broker import, no order.
Output: logs/research/bv5_replay_<mode>_<date>.json (atomic). Run on the server after the close (shared data quota).

    venv/bin/python3 research/bv5_block_replay.py validate --date 2026-10-21 --scans 6
    venv/bin/python3 research/bv5_block_replay.py replay --date 2026-10-22 --start 10:01 --end 15:53 --every 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("MTF_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

import config  # noqa: E402

ET = ZoneInfo("America/New_York")
LOGS = ROOT / "logs"
OUT_DIR = LOGS / "research"
EVENTS = LOGS / "score_comparison_events.jsonl"
_PAUSE_SECS = 2.0          # throttle between data requests (shares the account's data quota with the live bot)


# ── logged scans ──────────────────────────────────────────────────────────────────────────────────────────
def logged_scans(day: str) -> list:
    """[(scan_time, {symbol: ticker_row})] for every scan the live bot logged on `day` (ET date)."""
    out: list = []
    if not EVENTS.exists():
        return out
    with open(EVENTS, encoding="utf-8") as f:
        for line in f:
            if f'"scan_time":"{day}' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ts = datetime.fromisoformat(ev["scan_time"]).astimezone(ET)
            out.append((ts, {t["symbol"]: t for t in ev.get("tickers", []) if t.get("symbol")}))
    return sorted(out, key=lambda x: x[0])


def nearest_universe(day: str) -> tuple:
    """Universe for a day with no logged scan: the last scan of the prior logged day plus the first scan of the next
    one (the live universe is rebuilt daily from premarket movers + the watchlist). Returns (symbols, source)."""
    d = date.fromisoformat(day)
    prev = nxt = None
    for k in range(1, 8):
        if prev is None:
            s = logged_scans((d - timedelta(days=k)).isoformat())
            prev = (s[-1], (d - timedelta(days=k)).isoformat()) if s else None
        if nxt is None:
            s = logged_scans((d + timedelta(days=k)).isoformat())
            nxt = (s[0], (d + timedelta(days=k)).isoformat()) if s else None
    syms: set = set()
    src = []
    for item in (prev, nxt):
        if item:
            syms |= set(item[0][1])
            src.append(f"{item[1]} {item[0][0].strftime('%H:%M')}")
    return sorted(syms), "union of logged scans " + " + ".join(src)


# ── point-in-time bars ────────────────────────────────────────────────────────────────────────────────────
class BarStore:
    """Per-symbol bar history for one session, served as it stood at a given time."""

    def __init__(self, day: str):
        from data.fetcher import fetch_bars_window  # T1 Alpaca, research windowed fetch (no broker client)
        self._fetch = fetch_bars_window
        self.day = date.fromisoformat(day)
        self.open_et = datetime(self.day.year, self.day.month, self.day.day, 9, 30, tzinfo=ET)
        self.close_et = self.open_et.replace(hour=16, minute=0)
        self.minutes: dict = {}
        self.hist: dict = {}
        self.next_close: dict = {}

    def _get(self, sym, tf, start, end):
        time.sleep(_PAUSE_SECS)
        df = self._fetch(sym, tf, start, end, feed=os.environ.get("BV5_FEED", "iex"))
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        df.index = pd.DatetimeIndex(df.index).tz_convert(ET)
        return df

    def load(self, sym: str) -> bool:
        if sym in self.minutes:
            return not self.minutes[sym].empty
        d0 = datetime(self.day.year, self.day.month, self.day.day, tzinfo=ET)
        self.minutes[sym] = self._get(sym, config.TF_1M, d0.replace(hour=4), d0.replace(hour=20))
        self.hist[sym] = {
            config.TF_15M: self._get(sym, config.TF_15M, d0 - timedelta(days=45), d0),
            config.TF_1H: self._get(sym, config.TF_1H, d0 - timedelta(days=45), d0),
            config.TF_DAILY: self._get(sym, config.TF_DAILY, d0 - timedelta(days=560), d0),
            config.TF_WEEKLY: self._get(sym, config.TF_WEEKLY, d0 - timedelta(days=900), d0),
        }
        nxt = self._get(sym, config.TF_DAILY, d0 + timedelta(days=1), d0 + timedelta(days=6))
        self.next_close[sym] = float(nxt["close"].iloc[0]) if not nxt.empty else None
        return not self.minutes[sym].empty

    @staticmethod
    def _agg(m: pd.DataFrame) -> dict:
        return {"open": float(m["open"].iloc[0]), "high": float(m["high"].max()), "low": float(m["low"].min()),
                "close": float(m["close"].iloc[-1]), "volume": float(m["volume"].sum())}

    def bars(self, sym: str, tf: str, at: datetime, num_bars: "int | None" = None) -> pd.DataFrame:
        """Bars for `sym`/`tf` as a live fetch_bars call at time `at` would have returned them."""
        n = num_bars or config.BARS_TO_FETCH.get(tf, 300)
        if not self.load(sym):
            return pd.DataFrame()
        m = self.minutes[sym]
        m = m[m.index + timedelta(minutes=1) <= at - timedelta(minutes=int(os.environ.get("BV5_DELAY_MIN", "0")))]  # closed by `at` (minus any feed delay)
        h = self.hist[sym].get(tf, pd.DataFrame())
        rows = []
        if tf in (config.TF_15M, config.TF_1H):
            step = 15 if tf == config.TF_15M else 60
            if not m.empty:
                key = m.index.floor(f"{step}min")
                for k, grp in m.groupby(key):
                    rows.append((k, self._agg(grp)))
        elif tf == config.TF_DAILY:
            rth = m[(m.index >= self.open_et) & (m.index < self.close_et)]
            if not rth.empty:
                rows.append((pd.Timestamp(datetime(self.day.year, self.day.month, self.day.day, tzinfo=ET)),
                             self._agg(rth)))
        elif tf == config.TF_WEEKLY:
            daily = self.hist[sym][config.TF_DAILY]
            monday = self.day - timedelta(days=self.day.weekday())
            mon_ts = pd.Timestamp(datetime(monday.year, monday.month, monday.day, tzinfo=ET))
            week = daily[daily.index >= mon_ts] if not daily.empty else daily
            h = h[h.index < mon_ts] if not h.empty else h
            rth = m[(m.index >= self.open_et) & (m.index < self.close_et)]
            parts = [self._agg(week)] if not week.empty else []
            if not rth.empty:
                parts.append(self._agg(rth))
            if parts:
                rows.append((mon_ts, {"open": parts[0]["open"], "high": max(p["high"] for p in parts),
                                      "low": min(p["low"] for p in parts), "close": parts[-1]["close"],
                                      "volume": sum(p["volume"] for p in parts)}))
        else:
            return pd.DataFrame()
        cur = pd.DataFrame([r for _, r in rows], index=pd.DatetimeIndex([k for k, _ in rows]))
        out = pd.concat([h, cur]) if not cur.empty else h
        out = out[~out.index.duplicated(keep="last")].sort_index()
        return out[["open", "high", "low", "close", "volume"]].tail(n)

    def price_after(self, sym: str, at: datetime) -> "float | None":
        m = self.minutes.get(sym)
        if m is None or m.empty:
            return None
        after = m[m.index >= at]
        return float(after["open"].iloc[0]) if not after.empty else None

    def path(self, sym: str, at: datetime) -> pd.DataFrame:
        m = self.minutes.get(sym, pd.DataFrame())
        return m[(m.index >= at) & (m.index < self.close_et)] if not m.empty else m


# ── run the live scan at time T ───────────────────────────────────────────────────────────────────────────
def scan_at(store: BarStore, universe: list, at: datetime, scratch: Path) -> tuple:
    """(signals, per-ticker score rows) from the live run_scan at time `at`, with only its inputs swapped."""
    import strategy.signal_generator as sg
    captured: list = []

    def _mtf(sym, mode="intraday"):
        return {tf: df for tf in (config.TF_15M, config.TF_1H, config.TF_DAILY)
                if not (df := store.bars(sym, tf, at)).empty}

    def _capture(ev):
        captured.append(ev)
        return True
    patches = {"build_scan_universe": lambda run_atr_filter=True: list(universe),
               "fetch_multi_timeframe": _mtf,
               "fetch_bars": lambda sym, tf, num_bars=None: store.bars(sym, tf, at, num_bars),
               "get_earnings_surprise": lambda sym: None,
               "_LOGS_DIR": scratch,
               "_SCORE_COMPARISON_EVENTS": scratch / "events.jsonl",
               "_append_score_comparison_event": _capture}
    for name, value in patches.items():
        setattr(sg, name, value)                             # this process only; the live bot is untouched
    sg._intraday_override_cache.clear()
    if at.minute < 5:                                        # live weekly-bias cache refreshes every 60 minutes
        sg._weekly_bias_cache.clear()
    sigs = sg.run_scan(trade_mode=config.TradeMode.INTRADAY, max_workers=1)
    rows = captured[-1]["tickers"] if captured else []
    return sigs, rows


def _out(mode: str, day: str, payload: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"bv5_replay_{mode}_{day}.json"
    fd, tmp = tempfile.mkstemp(dir=OUT_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=1, default=str)
    os.replace(tmp, path)
    return path


def validate(day: str, n_scans: int) -> dict:
    scans = logged_scans(day)
    if not scans:
        raise SystemExit(f"no logged scans on {day}")
    pick = [scans[int(i * (len(scans) - 1) / max(1, n_scans - 1))] for i in range(min(n_scans, len(scans)))]
    store = BarStore(day)
    total = exact = within1 = 0
    diffs = []
    with tempfile.TemporaryDirectory() as td:
        for ts, logged in pick:
            _, rows = scan_at(store, sorted(logged), ts, Path(td))
            for r in rows:
                lg = logged.get(r["symbol"])
                if not lg:
                    continue
                for side in ("long_12pt", "short_12pt"):
                    total += 1
                    d = int(r[side]) - int(lg[side])
                    exact += d == 0
                    within1 += abs(d) <= 1
                    if d:
                        comp = "long_16_components" if side.startswith("long") else "short_16_components"
                        rc, lc = r.get(comp) or {}, lg.get(comp) or {}
                        diffs.append({"time": ts.strftime("%H:%M"), "symbol": r["symbol"], "side": side,
                                      "replay": r[side], "logged": lg[side],
                                      "conditions_differ": {k: [rc.get(k), lc.get(k)] for k in sorted(set(rc) | set(lc))
                                                            if rc.get(k) != lc.get(k)}})
    res = {"mode": "validate", "date": day, "scans": [p[0].isoformat() for p in pick], "comparisons": total,
           "exact": exact, "within_1": within1, "exact_pct": round(100 * exact / total, 1) if total else None,
           "mismatches": diffs}
    res["output"] = str(_out("validate", day, res))
    return res


def replay(day: str, start: str, end: str, every: int, universe: "list | None") -> dict:
    src = "given"
    if not universe:
        scans = logged_scans(day)
        if scans:
            universe, src = sorted(set().union(*[set(s[1]) for s in scans])), f"logged scans on {day}"
        else:
            universe, src = nearest_universe(day)
    store = BarStore(day)
    h0, m0 = map(int, start.split(":"))
    h1, m1 = map(int, end.split(":"))
    t = store.open_et.replace(hour=h0, minute=m0)
    t_end = store.open_et.replace(hour=h1, minute=m1)
    first: dict = {}
    cycles = 0
    with tempfile.TemporaryDirectory() as td:
        while t <= t_end:
            sigs, _ = scan_at(store, universe, t, Path(td))
            cycles += 1
            for s in sigs:
                key = (s["symbol"], s["direction"])
                if key not in first:
                    first[key] = {"time": t, "score": s["score"], "max_score": s["max_score"]}
            t += timedelta(minutes=every)
    out = []
    for (sym, direction), info in sorted(first.items(), key=lambda kv: kv[1]["time"]):
        at = info["time"]
        px = store.price_after(sym, at)
        p = store.path(sym, at)
        sign = 1 if direction == "long" else -1

        def ret(x, px=px, sign=sign):
            return None if (x is None or not px) else round(sign * (x - px) / px * 100, 3)

        def at_plus(mins, p=p, at=at):
            q = p[p.index >= at + timedelta(minutes=mins)]
            return float(q["open"].iloc[0]) if not q.empty else None
        close_px = float(p["close"].iloc[-1]) if not p.empty else None
        mfe = mae = None
        if px and not p.empty:
            hi, lo = float(p["high"].max()), float(p["low"].min())
            fav, adv = (hi, lo) if direction == "long" else (lo, hi)
            mfe, mae = ret(fav), ret(adv)
        out.append({"symbol": sym, "direction": direction, "first_signal": at.strftime("%H:%M"),
                    "score": info["score"], "max_score": info["max_score"], "entry_px": px,
                    "ret_30m_pct": ret(at_plus(30)), "ret_60m_pct": ret(at_plus(60)),
                    "ret_close_pct": ret(close_px), "ret_next_close_pct": ret(store.next_close.get(sym)),
                    "mfe_to_close_pct": mfe, "mae_to_close_pct": mae})
    res = {"mode": "replay", "date": day, "window": f"{start}-{end} ET every {every} min", "cycles": cycles,
           "universe_size": len(universe), "universe_source": src, "signals": out,
           "limits": "signals are an upper bound on entries: ORB confirmation, raised MIN_SCORE, confirm buffer, "
                     "sizing and stops are not modelled"}
    res["output"] = str(_out("replay", day, res))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--date", required=True)
    v.add_argument("--scans", type=int, default=6)
    r = sub.add_parser("replay")
    r.add_argument("--date", required=True)
    r.add_argument("--start", default="10:00")
    r.add_argument("--end", default="15:55")
    r.add_argument("--every", type=int, default=5)
    r.add_argument("--symbols", default="")
    a = ap.parse_args()
    if a.mode == "validate":
        res = validate(a.date, a.scans)
        print(json.dumps({k: res[k] for k in ("date", "comparisons", "exact", "within_1", "exact_pct", "output")}))
    else:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] or None
        res = replay(a.date, a.start, a.end, a.every, syms)
        print(json.dumps({k: res[k] for k in ("date", "cycles", "universe_size", "universe_source", "output")}))
        for s in res["signals"]:
            print(json.dumps(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
