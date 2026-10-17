#!/usr/bin/env python3
# ruff: noqa: E501
"""run_day_setup_scorecard.py — nightly builder for the Day-tier setup scorecard (Claude, 2026-10-17).

Design record: logs/design_records/day_setup_scorecard_2026-10-17.md. Cron, after the close; never trades.

1. Reads logs/day_tier_events.jsonl decisions; keeps the FIRST signal per (symbol, session day, direction) — repeats of
   the same level on the same day are one signal, not many.
2. For each signal from a COMPLETED session, computes two market-adjusted outcomes from Alpaca 1-minute SIP bars (T1,
   data.fetcher.fetch_bars_window): after the Day tier's MEDIAN HOLDING TIME (measured from its own entry/exit fills in
   the same log) and at the session close, each = the symbol's return
   minus SPY's over the same minutes, signed by the signal's direction. Outcomes are cached in
   data/state/day_setup_outcomes.json so each signal is fetched once.
3. Builds the scorecard (strategy.day_setup_scorecard.build_scorecard) and writes it atomically.
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from strategy import day_setup_scorecard as sc  # noqa: E402

ET = ZoneInfo("America/New_York")
EVENTS = _ROOT / "logs" / "day_tier_events.jsonl"
OUTCOMES = _ROOT / "data" / "state" / "day_setup_outcomes.json"
LOG = _ROOT / "logs" / "day_setup_scorecard.log"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
logger = logging.getLogger("day_setup_scorecard")


def _median_hold_minutes() -> "float | None":
    """Median minutes between a Day-tier trade's entry fill and its last exit fill, from the event log."""
    entries: dict = {}
    exits: dict = {}
    for line in EVENTS.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        tid = e.get("trade_id")
        if not tid:
            continue
        if e.get("event") == "entry_fill":
            entries[tid] = datetime.fromisoformat(e["ts"])
        elif e.get("event") == "exit_fill":
            ts = datetime.fromisoformat(e["ts"])
            exits[tid] = max(ts, exits.get(tid, ts))
    holds = [(exits[t] - entries[t]).total_seconds() / 60 for t in entries if t in exits and exits[t] > entries[t]]
    return statistics.median(holds) if holds else None


def _signals() -> list:
    seen: set = set()
    out: list = []
    for line in EVENTS.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") != "decision":
            continue
        t = e.get("trigger") or {}
        if t.get("trigger") != "ENTER" or t.get("direction") not in ("long", "short"):
            continue
        ts = datetime.fromisoformat(e["ts"]).astimezone(ET)
        sig_id = f"{e['symbol']}|{ts.date().isoformat()}|{t['direction']}"
        if sig_id in seen:
            continue
        seen.add(sig_id)
        out.append({"id": sig_id, "symbol": e["symbol"], "day": ts.date().isoformat(), "ts": ts,
                    "direction": t["direction"], "mode": t.get("mode")})
    return out


def _session_close(day: str) -> "datetime | None":
    try:
        from execution.broker import _get_trading_client  # read-only calendar lookup
        from alpaca.trading.requests import GetCalendarRequest
        cal = _get_trading_client().get_calendar(GetCalendarRequest(start=day, end=day))
        if cal:
            c = cal[0].close
            return c.replace(tzinfo=ET) if c.tzinfo is None else c
    except Exception as exc:  # RC-3
        logger.warning("calendar read failed for %s: %s", day, exc)
    return None


def _returns(symbol: str, start: datetime, close: datetime, hold_min: float) -> "tuple[float, float] | None":
    from data.fetcher import fetch_bars_window
    df = fetch_bars_window(symbol, "1Min", start.astimezone(timezone.utc), close.astimezone(timezone.utc))
    if df is None or df.empty:
        return None
    ref = float(df["open"].iloc[0])
    after = df[df.index >= (start + timedelta(minutes=hold_min)).astimezone(timezone.utc)]
    if ref <= 0:
        return None
    # A signal less than one holding time before the close is held to the close (the Day tier flattens then).
    held_px = float(after["close"].iloc[0]) if not after.empty else float(df["close"].iloc[-1])
    return held_px / ref - 1.0, float(df["close"].iloc[-1]) / ref - 1.0


def _outcome(sig: dict, close: datetime, hold_min: float) -> "dict | None":
    mine = _returns(sig["symbol"], sig["ts"], close, hold_min)
    spy = _returns("SPY", sig["ts"], close, hold_min)
    if mine is None or spy is None:
        return None
    sign = 1.0 if sig["direction"] == "long" else -1.0
    return {"key": sc.setup_key(sig["direction"], sig["mode"]), "day": sig["day"], "symbol": sig["symbol"],
            "rhold": sign * (mine[0] - spy[0]), "rclose": sign * (mine[1] - spy[1]), "hold_min": hold_min}


def main() -> int:
    now = datetime.now(ET)
    try:
        cache = json.loads(OUTCOMES.read_text()) if OUTCOMES.exists() else {}
    except ValueError:
        logger.warning("outcome cache unreadable — rebuilding")
        cache = {}
    hold_min = _median_hold_minutes()
    if hold_min is None:
        logger.warning("no completed Day-tier trades to measure the holding time — scorecard not rebuilt")
        return 0
    cache = {k: v for k, v in cache.items() if v.get("hold_min") == hold_min}   # a new median re-scores every signal
    closes: dict = {}
    added = 0
    for sig in _signals():
        if sig["id"] in cache:
            continue
        if sig["day"] not in closes:
            closes[sig["day"]] = _session_close(sig["day"])
        close = closes[sig["day"]]
        if close is None or close > now:
            continue                      # session not finished (or calendar unknown): try again next run
        out = _outcome(sig, close, hold_min)
        if out is None:
            logger.warning("no outcome for %s (bars unavailable)", sig["id"])
            continue
        cache[sig["id"]] = out
        added += 1
    tmp = OUTCOMES.with_suffix(".json.tmp")
    OUTCOMES.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(cache, indent=1))
    tmp.replace(OUTCOMES)
    rows = list(cache.values())
    latest = ""
    first_day = min((r["day"] for r in rows), default=now.date().isoformat())
    span = (now.date() - datetime.fromisoformat(first_day).date()).days
    for back in range(span + 1):                  # the latest COMPLETED session (a quiet day still counts)
        d = (now.date() - timedelta(days=back)).isoformat()
        c = closes[d] if d in closes else _session_close(d)
        if c is not None and c <= now:
            latest = d
            break
    card = sc.build_scorecard(rows, latest, now.isoformat())
    sc.write_scorecard(card)
    blocked = [k for k, v in card["setups"].items() if v["blocked"]]
    logger.info("day setup scorecard [day-setup-scorecard]: %d signals (%d new), %d setups, blocked=%s, session=%s, "
                "hold=%.0f min", len(rows), added, len(card["setups"]), blocked, card["built_for_session"], hold_min)
    return 0


if __name__ == "__main__":
    sys.exit(main())
