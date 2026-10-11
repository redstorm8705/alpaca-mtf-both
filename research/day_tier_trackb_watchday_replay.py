#!/usr/bin/env python3
# ruff: noqa: E501
"""
research/day_tier_trackb_watchday_replay.py — does the Day tier's watch-day rule help or hurt TRACK B shorts?

Question (logs/design_records/day_tier_track_b_review_2026-10-11.md, item 3): the watch-day rule
(run_day_tier._watch_day_ok) skips a SHORT when the symbol's Layer-A daily side is LONG and the previous session was
not "red and closed below the prior-prior low". Its evidence was measured on counter-trend shorts held to the close.
Track B shorts are a different setup (gap-down mover that broke and held below its 15-min opening-range low).

READ-ONLY research: no orders, no execution imports. T1 Alpaca bars via data.fetcher.fetch_bars_window, cached to
logs/cache/trackb_watchday/ so a rerun does not refetch.

Replay per session x Track-B universe symbol:
  * signal frame = IEX 5m RTH bars from 09:30 (split-adjusted), growing bar by bar (completed bars only, as live);
    a non-contiguous frame is skipped unless strategy.day_tier_track_b._iex_holes_ok accepts it (as live);
  * daily context = SIP split-adjusted prior close + mean of the last 20 IEX split-adjusted daily volumes (as live);
  * screen_mover(now = bar end) -> compute_momentum_trigger(gap side) — the bot's own functions; FIRST ENTER per
    symbol per day uses the day's shot (as live);
  * Layer-A side = the compute_side_bias MA stack rebuilt point-in-time from SIP split-adjusted daily closes, with the
    signal bar's close as today's close; weekly / monthly closes resampled from those dailies (proxy, see CAVEATS);
  * watch day = previous completed session close < its open AND < the prior-prior session low (SIP split-adjusted);
  * live min-stop gate proxy: stop distance >= 1.5 x median true range of the last 14 5m bars (no spread term);
  * trade: entry = signal bar close; stop = OR level x (1 +- 0.001) (the bot's _compute_stop_price RIDE geometry);
    target = 2R; else flat at the 15:50 bar close (~15:55 ET). Path on SIP 5m bars; a bar touching both stop and
    target counts as the stop; a gap through a level fills at the bar open. Cost 5 bps round trip.

CAVEATS: entry at the bar close (live enters on the next 2-min tick with a marketable limit); live side reads
fetch_bars daily with the account-default feed/adjustment and Alpaca weekly/monthly bars; the intraday rule-2 gate
(every 2m/5m check opposes) is not replayed (a Track-B short below VWAP rarely meets it); no spread term in the
min-stop gate; sizing, caps, co-hold and ETF routing are not modelled (R units only).

Run: uv run -q --python 3.11 --with pandas --with alpaca-py==0.43.3 --with requests --with python-dotenv \
       python research/day_tier_trackb_watchday_replay.py [--start 2025-10-01] [--end 2026-10-09]
Output: logs/trackb_watchday_replay_<today>.json
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import random
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv("/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.env")
except Exception:  # noqa: BLE001
    pass

import pandas as pd  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = ROOT / "logs" / "cache" / "trackb_watchday"
COST_BPS = 5.0
STOP_BUF = 0.001
TARGET_R = 2.0
MIN_STOP_ATR = 1.5


# ---------------------------------------------------------------- data (cached)
def _cached(name: str, fn):
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{name}.pkl"
    if p.exists():
        return pickle.loads(p.read_bytes())
    df = fn()
    p.write_bytes(pickle.dumps(df))
    return df


def fetch_5m(sym: str, feed: str, start: str, end: str) -> pd.DataFrame:
    import config
    from data.fetcher import fetch_bars_window
    parts = []
    d = datetime.fromisoformat(start).replace(tzinfo=ET)
    stop = datetime.fromisoformat(end).replace(tzinfo=ET) + timedelta(days=1)
    while d < stop:
        e = min(d + timedelta(days=31), stop)
        key = f"{sym}_{feed}_5m_{d:%Y%m%d}_{e:%Y%m%d}"
        df = _cached(key, lambda d=d, e=e: fetch_bars_window(sym, config.TF_5M, d, e, feed=feed, adjustment="split"))
        if df is not None and not df.empty:
            parts.append(df)
        d = e
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts)
    out = out[~out.index.duplicated()].sort_index()
    idx = out.index.tz_convert(ET) if out.index.tz is not None else out.index.tz_localize("UTC").tz_convert(ET)
    out.index = idx
    m = idx.hour * 60 + idx.minute
    return out[(m >= 570) & (m < 960)]


def fetch_daily(sym: str, feed: str, start: str, end: str) -> pd.DataFrame:
    import config
    from data.fetcher import fetch_bars_window
    s = datetime.fromisoformat(start).replace(tzinfo=ET)
    e = datetime.fromisoformat(end).replace(tzinfo=ET) + timedelta(days=1)
    df = _cached(f"{sym}_{feed}_1d_{start}_{end}",
                 lambda: fetch_bars_window(sym, config.TF_DAILY, s, e, feed=feed, adjustment="split"))
    if df is None or df.empty:
        return pd.DataFrame()
    idx = df.index.tz_convert(ET) if df.index.tz is not None else df.index.tz_localize("UTC").tz_convert(ET)
    df = df.copy()
    df.index = pd.DatetimeIndex([pd.Timestamp(t.date()) for t in idx])
    return df[~df.index.duplicated()].sort_index()


# ---------------------------------------------------------------- Layer-A side (point in time)
def side_pit(daily_closes: pd.Series, today_close: float, today: pd.Timestamp) -> tuple:
    """compute_side_bias rebuilt on settled dailies strictly before `today` + today's close = the signal price."""
    from strategy import day_tier_side as s
    hist = daily_closes[daily_closes.index < today].tail(s._DAILY_BARS - 1)
    closes = pd.concat([hist, pd.Series([today_close], index=[today])])
    daily = pd.DataFrame({"close": closes})
    weekly = pd.DataFrame({"close": closes.resample("W-FRI").last().dropna()}).tail(s._WEEKLY_BARS)
    monthly = pd.DataFrame({"close": closes.resample("ME").last().dropna()}).tail(s._MONTHLY_BARS)
    close = today_close
    ws = wu = wt = 0.0
    for period, _ in s._DAILY_EMAS:
        wt += 1
        ma = s._ema(daily, period)
        if ma is not None:
            ws += (1 if close > ma else -1 if close < ma else 0)
            wu += 1
    for period, _ in s._DAILY_SMAS:
        wt += 1
        ma = s._sma(daily, period)
        if ma is not None:
            ws += (1 if close > ma else -1 if close < ma else 0)
            wu += 1
    for df_, per in ((weekly, s._TENWEEK_PERIOD), (monthly, s._TENMONTH_PERIOD)):
        wt += 1
        ma = s._sma(df_, per)
        if ma is not None:
            ws += (1 if close > ma else -1 if close < ma else 0)
            wu += 1
    wt += s._QUARTERLY_WEIGHT
    ma = s._sma(daily, s._QUARTERLY_PERIOD)
    if ma is not None:
        ws += s._QUARTERLY_WEIGHT * (1 if close > ma else -1 if close < ma else 0)
        wu += s._QUARTERLY_WEIGHT
    cov = wu / wt if wt else 0.0
    if wu <= 0 or cov < s._MIN_WEIGHT_COVERAGE:
        return "UNKNOWN", None
    score = ws / wu
    side = "LONG" if score >= s._LONG_THRESHOLD else "SHORT" if score <= s._SHORT_THRESHOLD else "TWO_SIDED"
    return side, round(score, 3)


def watch_day(daily: pd.DataFrame, today: pd.Timestamp) -> "bool | None":
    done = daily[daily.index < today]
    if len(done) < 2:
        return None
    y, p = done.iloc[-1], done.iloc[-2]
    return bool(float(y["close"]) < float(y["open"]) and float(y["close"]) < float(p["low"]))


# ---------------------------------------------------------------- simulation
def simulate(path: pd.DataFrame, direction: str, entry: float, stop: float) -> dict:
    r = abs(entry - stop)
    tgt = entry - TARGET_R * r if direction == "short" else entry + TARGET_R * r
    exit_px, how = None, "eod"
    for _, b in path.iterrows():
        o, h, low = float(b["open"]), float(b["high"]), float(b["low"])
        if direction == "short":
            if h >= stop:
                exit_px, how = (max(o, stop) if o >= stop else stop), "stop"
                break
            if low <= tgt:
                exit_px, how = (min(o, tgt) if o <= tgt else tgt), "target"
                break
        else:
            if low <= stop:
                exit_px, how = (min(o, stop) if o <= stop else stop), "stop"
                break
            if h >= tgt:
                exit_px, how = (max(o, tgt) if o >= tgt else tgt), "target"
                break
    if exit_px is None:
        if path.empty:
            return {}
        exit_px = float(path["close"].iloc[-1])
    gross = (entry - exit_px) if direction == "short" else (exit_px - entry)
    cost = COST_BPS / 1e4 * entry
    return {"exit": round(exit_px, 4), "how": how, "R": round((gross - cost) / r, 4),
            "net_bps": round((gross / entry) * 1e4 - COST_BPS, 2), "risk_bps": round(r / entry * 1e4, 1)}


def robust_atr(bars: pd.DataFrame) -> "float | None":
    b = bars.tail(15)
    if len(b) < 15:
        return None
    h, low, c = b["high"].values, b["low"].values, b["close"].values
    trs = [max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1])) for i in range(1, len(b))]
    return float(statistics.median(trs))


def replay_symbol(sym: str, start: str, end: str, dstart: str) -> list:
    from strategy import day_tier_track_b as tb
    from strategy.day_tier_momentum_trigger import compute_momentum_trigger
    iex = fetch_5m(sym, "iex", start, end)
    sip = fetch_5m(sym, "sip", start, end)
    d_sip = fetch_daily(sym, "sip", dstart, end)
    d_iex = fetch_daily(sym, "iex", dstart, end)
    if iex.empty or sip.empty or d_sip.empty or d_iex.empty:
        print(f"[{sym}] missing data — skipped", flush=True)
        return []
    out = []
    days = sorted(set(iex.index.date))
    for day in days:
        today = pd.Timestamp(day)
        prior = d_sip[d_sip.index < today]
        prior_iex = d_iex[d_iex.index < today]
        if prior.empty or len(prior_iex) < 15:
            continue
        # previous session must be the previous trading day in both series (as live)
        if prior.index[-1] != prior_iex.index[-1]:
            continue
        prior_close = float(prior["close"].iloc[-1])
        adv = float(prior_iex["volume"].dropna().tail(20).mean())
        day_bars = iex[iex.index.date == day]
        if day_bars.empty or (day_bars.index[0].hour, day_bars.index[0].minute) != (9, 30):
            continue
        for k in range(5, min(len(day_bars), 72) + 1):
            frame = day_bars.iloc[:k]
            span = (frame.index[-1] - frame.index[0]).total_seconds()
            if len(frame) < int(round(span / 300)) + 1:
                ok, _ = tb._iex_holes_ok(frame)
                if not ok:
                    continue
            now = frame.index[-1].to_pydatetime() + timedelta(minutes=5)
            if (now.hour * 60 + now.minute) > 15 * 60 + 35:
                break
            scr = tb.screen_mover(sym, frame, prior_close, adv, now)
            if not scr.get("is_mover"):
                continue
            mom = compute_momentum_trigger(sym, scr.get("gap_direction"), frame)
            if mom.get("trigger") != "ENTER":
                continue
            direction = mom["direction"]
            entry = float(mom["entry_ref"])
            level = float(mom["structural_level"])
            stop = round(level * (1 + STOP_BUF) if direction == "short" else level * (1 - STOP_BUF), 2)
            valid = (stop > entry) if direction == "short" else (stop < entry)
            side, score = side_pit(d_sip["close"], entry, today)
            wd = watch_day(d_sip, today)
            hist5 = iex[iex.index <= frame.index[-1]]
            atr = robust_atr(hist5)
            room_ok = bool(valid and atr is not None and abs(entry - stop) >= MIN_STOP_ATR * atr)
            path = sip[(sip.index.date == day) & (sip.index > frame.index[-1])
                       & ((sip.index.hour * 60 + sip.index.minute) <= 15 * 60 + 50)]
            sim = simulate(path, direction, entry, stop) if valid else {}
            if direction == "short":
                if side == "LONG":
                    gate = "watchday_confirmed" if wd else "watchday_block"
                elif side == "SHORT":
                    gate = "with_trend"
                else:
                    gate = "no_clear_side"
            else:
                gate = "with_trend" if side == "LONG" else ("counter_block" if side == "SHORT" else "no_clear_side")
            out.append({"symbol": sym, "date": str(day), "time_et": frame.index[-1].strftime("%H:%M"),
                        "direction": direction, "mode": mom.get("mode"), "gap_pct": scr.get("gap_pct"),
                        "rvol": scr.get("rvol"), "entry": entry, "level": level, "stop": stop,
                        "side": side, "side_score": score, "watch_day": wd, "gate": gate,
                        "room_ok": room_ok, "atr": atr, **sim})
            break  # the day's one shot is used at the ENTER signal (as live)
    print(f"[{sym}] {len(days)} sessions, {len(out)} signals", flush=True)
    return out


# ---------------------------------------------------------------- stats
def stats(rows: list) -> dict:
    rs = [r["R"] for r in rows if "R" in r]
    if not rs:
        return {"n": 0}
    bps = [r["net_bps"] for r in rows if "R" in r]
    worst = min((r for r in rows if "R" in r), key=lambda r: r["R"])
    sd = statistics.stdev(rs) if len(rs) > 1 else float("nan")
    return {"n": len(rs), "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 3),
            "mean_R": round(statistics.mean(rs), 3), "median_R": round(statistics.median(rs), 3),
            "mean_net_bps": round(statistics.mean(bps), 1), "total_R": round(sum(rs), 2),
            "t_stat": round(statistics.mean(rs) / (sd / math.sqrt(len(rs))), 2) if len(rs) > 1 and sd > 0 else None,
            "worst": f"{worst['symbol']} {worst['date']} {worst['R']:+.2f}R"}


def bootstrap(rows: list, n: int = 10000, seed: int = 7) -> dict:
    rs = [r["R"] for r in rows if "R" in r]
    if len(rs) < 2:
        return {}
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(rs, k=len(rs))) for _ in range(n))
    return {"ci90": [round(means[int(0.05 * n)], 3), round(means[int(0.95 * n)], 3)],
            "p_mean_le_0": round(sum(1 for m in means if m <= 0) / n, 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-01")
    ap.add_argument("--end", default="2026-10-09")
    ap.add_argument("--dstart", default="2024-03-01")
    a = ap.parse_args()
    from strategy.day_tier_track_b import track_b_universe
    out_path = ROOT / "logs" / f"trackb_watchday_replay_{datetime.now(ET):%Y-%m-%d}.json"
    sigs: list = []
    for sym in track_b_universe():
        sigs += replay_symbol(sym, a.start, a.end, a.dstart)
        out_path.write_text(json.dumps({"partial": True, "signals": sigs}, indent=1, default=str))
    traded = [s for s in sigs if "R" in s]
    live = [s for s in traded if s["room_ok"]]
    res: dict = {"window": [a.start, a.end], "n_signals": len(sigs), "n_simulated": len(traded),
                 "n_room_ok": len(live), "groups": {}}
    for label, pool in (("all_signals", traded), ("room_ok", live)):
        g: dict = {}
        sh = [s for s in pool if s["direction"] == "short"]
        lg = [s for s in pool if s["direction"] == "long"]
        g["short_all"] = stats(sh)
        g["short_b_watchday_blocks"] = {**stats([s for s in sh if s["gate"] == "watchday_block"]),
                                        **bootstrap([s for s in sh if s["gate"] == "watchday_block"])}
        g["short_c_allowed"] = stats([s for s in sh if s["gate"] in ("with_trend", "watchday_confirmed")])
        g["short_c1_side_short"] = stats([s for s in sh if s["gate"] == "with_trend"])
        g["short_c2_watchday_confirmed"] = stats([s for s in sh if s["gate"] == "watchday_confirmed"])
        g["short_no_clear_side"] = stats([s for s in sh if s["gate"] == "no_clear_side"])
        g["long_all"] = stats(lg)
        g["long_with_trend"] = stats([s for s in lg if s["gate"] == "with_trend"])
        g["long_counter_block"] = stats([s for s in lg if s["gate"] == "counter_block"])
        g["long_no_clear_side"] = stats([s for s in lg if s["gate"] == "no_clear_side"])
        res["groups"][label] = g
    res["sessions"] = len({s["date"] for s in sigs})
    res["live_window_signals"] = [s for s in sigs if s["date"] >= "2026-09-24"]
    res["signals"] = sigs
    out_path.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k not in ("signals",)}, indent=1, default=str))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
