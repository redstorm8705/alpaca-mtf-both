#!/usr/bin/env python3
# ruff: noqa: E501, E701  (long report/prompt strings; intentional one-line dispatch in _shorten_reason)
"""
weekly_postmortem.py
Weekly Trade Post-Mortem (WTP) — reads trade_events.jsonl, fetches
weekly/daily Alpaca T1 bars, checks earnings cache, computes Weinstein
stage, sends to Gemini for adversarial analysis, posts summary to Slack.

Saturday 6 AM PT (cron: 0 13,14 * * 6, cron_tz_wrapper 06:00)
Sunday  6 AM PT (cron: 0 13,14 * * 0, cron_tz_wrapper 06:00 — retry if Sat failed)

Script skips automatically if logs/wtp_{friday}.md already exists.

Read-only — no execution imports, no order calls, no shared state.
"""

import json
import logging
import os
import ssl
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

PT  = ZoneInfo("America/Los_Angeles")
ET  = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("weekly_postmortem")

# ── Config ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "").strip()
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL", "").strip()
ALPACA_KEY      = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET   = os.getenv("ALPACA_SECRET_KEY", "").strip()
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
GEMINI_MODEL    = "gemini-2.5-flash"

BASE_DIR        = Path(__file__).resolve().parent
LOGS_DIR        = BASE_DIR / "logs"
TRADE_EVENTS    = LOGS_DIR / "trade_events.jsonl"
POSTMORTEM_DIR  = BASE_DIR / "data" / "state" / "postmortem"
EARNINGS_CACHE  = BASE_DIR / "data" / "cache" / "earnings_week.json"

try:
    import certifi as _certifi
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# ── Week range ─────────────────────────────────────────────────────────────────

def _get_week_range() -> tuple[date, date]:
    """Return (monday, friday) for the most recently completed Mon-Fri week."""
    today = datetime.now(PT).date()
    # Weekday: Mon=0 … Fri=4 … Sat=5 … Sun=6
    # Find the most recent Friday
    days_back = (today.weekday() - 4) % 7   # 0 if today IS Friday
    if days_back == 0 and today.weekday() != 4:
        days_back = 7  # today is the exact weekday but not Friday — shouldn't happen
    if today.weekday() == 5:   # Saturday → yesterday was Friday
        days_back = 1
    elif today.weekday() == 6: # Sunday → two days ago was Friday
        days_back = 2
    friday = today - timedelta(days=days_back)
    monday = friday - timedelta(days=4)
    return monday, friday

# ── Trade loading ──────────────────────────────────────────────────────────────

def _load_trades_for_week(monday: date, friday: date) -> list[dict]:
    """
    Read trade_events.jsonl (authoritative), match entry→exit pairs
    for all exits in [monday, friday]. Returns list of enriched dicts.
    """
    if not TRADE_EVENTS.exists():
        logger.error(f"trade_events.jsonl not found: {TRADE_EVENTS}")
        return []

    entries: dict[str, dict] = {}   # symbol → most recent entry event
    trades: list[dict]       = []

    with open(TRADE_EVENTS, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event  = ev.get("event", "")
            sym    = ev.get("symbol", "")
            ts_str = ev.get("ts", "")
            if not sym:
                continue

            try:
                ts      = datetime.fromisoformat(ts_str)
                ev_date = ts.astimezone(PT).date()
            except Exception:
                continue

            if event == "entry":
                entries[sym] = ev
            elif event == "exit" and monday <= ev_date <= friday:
                entry_ev = entries.get(sym, {})
                trades.append({
                    "symbol":      sym,
                    "direction":   ev.get("direction") or entry_ev.get("direction", ""),
                    "entry_price": entry_ev.get("price"),
                    "exit_price":  ev.get("price"),
                    "entry_time":  entry_ev.get("ts", ""),
                    "exit_time":   ev.get("ts", ""),
                    "pnl":         ev.get("pnl", 0.0) or 0.0,
                    "exit_reason": ev.get("reason", ""),
                    "score":       entry_ev.get("score") or ev.get("score", "?"),
                    "mri_level":   entry_ev.get("mri_level") or ev.get("mri_level", "?"),
                    "size":        ev.get("size", 1),
                    "exit_date":   ev_date,
                })
    return trades

# ── Postmortem ─────────────────────────────────────────────────────────────────

def _load_postmortem(symbol: str, exit_date: date) -> dict:
    """Load postmortem JSON for symbol near exit_date. Returns {} if not found."""
    for delta in [0, -1, 1, -2, 2]:
        path = POSTMORTEM_DIR / f"{(exit_date + timedelta(days=delta)).isoformat()}_{symbol}.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
    return {}

# ── Alpaca bars ────────────────────────────────────────────────────────────────

def _alpaca_get(params: str) -> dict:
    url = f"{ALPACA_BARS_URL}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        },
    )
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.warning(f"Alpaca API error: {e}")
        return {}


def _fetch_weekly_closes(symbols: list[str], weeks: int = 22) -> dict[str, list[float]]:
    """Fetch last {weeks} weekly closes for stage computation."""
    if not ALPACA_KEY or not symbols:
        return {}
    start = (datetime.now(PT).date() - timedelta(weeks=weeks + 2)).isoformat()
    data  = _alpaca_get(
        f"symbols={','.join(symbols)}&timeframe=1Week"
        f"&start={start}T00:00:00Z&limit=1000&sort=asc"
    )
    return {sym: [b["c"] for b in bars] for sym, bars in (data.get("bars") or {}).items()}


def _fetch_friday_close(symbols: list[str], friday: date) -> dict[str, float]:
    """Fetch the Friday (last daily bar of the week) close for each symbol."""
    if not ALPACA_KEY or not symbols:
        return {}
    monday = friday - timedelta(days=4)
    data   = _alpaca_get(
        f"symbols={','.join(symbols)}&timeframe=1Day"
        f"&start={monday.isoformat()}T00:00:00Z"
        f"&end={friday.isoformat()}T23:59:59Z&limit=500&sort=desc"
    )
    result = {}
    for sym, bars in (data.get("bars") or {}).items():
        if bars:
            result[sym] = bars[0]["c"]   # desc sort → first = most recent = Friday
    return result

# ── Weinstein stage ────────────────────────────────────────────────────────────

def _compute_weinstein_stage(weekly_closes: list[float]) -> str:
    """
    Simplified Stage Analysis using last 20 weekly closes and 20-wk SMA.
    Stage 2: above SMA + rising week-over-week
    Stage 3: above SMA + declining (topping)
    Stage 4: below SMA + declining
    Stage 1: below SMA + rising (basing)
    """
    if len(weekly_closes) < 4:
        return "N/A"
    window = weekly_closes[-20:]
    sma    = sum(window) / len(window)
    last   = window[-1]
    prev   = window[-2]
    above  = last > sma
    rising = last > prev
    if above and rising:
        return "Stage 2"
    if above and not rising:
        return "Stage 3"
    if not above and not rising:
        return "Stage 4"
    return "Stage 1"

# ── Earnings ───────────────────────────────────────────────────────────────────

def _load_earnings_cache() -> dict:
    try:
        with open(EARNINGS_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def _had_earnings_during_hold(symbol: str, entry_time: str, exit_time: str, cache: dict) -> bool:
    """True if any FMP earnings date falls within the hold window."""
    try:
        t0 = datetime.fromisoformat(entry_time).date()
        t1 = datetime.fromisoformat(exit_time).date()
    except Exception:
        return False
    for d_str in (cache.get("data") or {}).get(symbol.upper(), []):
        try:
            if t0 <= date.fromisoformat(str(d_str)[:10]) <= t1:
                return True
        except Exception:
            pass
    # Supplement: check earnings_surprise_{symbol}.json cache files
    surprise = BASE_DIR / "data" / "cache" / f"earnings_surprise_{symbol}.json"
    if surprise.exists():
        try:
            with open(surprise) as f:
                items = json.load(f)
            for item in (items if isinstance(items, list) else []):
                d_str = item.get("date", "")
                if d_str:
                    try:
                        if t0 <= date.fromisoformat(str(d_str)[:10]) <= t1:
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
    return False

# ── Helpers ────────────────────────────────────────────────────────────────────

def _shorten_reason(reason: str) -> str:
    r = reason.lower()
    if "overnight_atr_buffer_exit" in r: return "ATR Buffer"
    if "hard_stop"                  in r: return "Hard Stop"
    if "breakeven"                  in r: return "Breakeven"
    if "target"                     in r: return "Target"
    if "fill_unverified"            in r: return "Fill Unverf."
    if "external_close_detected_ah" in r: return "AH External"
    if "external_close"             in r: return "Ext. Close"
    if "trail_stop"                 in r: return "Trail Stop"
    if "opposite_signal"            in r: return "Signal"
    if "signal"                     in r: return "Signal"
    if "pm_exit"                    in r: return "PM Exit"
    if "thesis_invalidation"        in r: return "Thesis Inv."
    return reason[:18]


def _hold_hours(entry_time: str, exit_time: str) -> str:
    try:
        h = (datetime.fromisoformat(exit_time) - datetime.fromisoformat(entry_time)).total_seconds() / 3600
        return f"{round(h, 1)}h"
    except Exception:
        return "?"


def _r_multiple(entry: float | None, pnl: float, size: int, pm: dict) -> str:
    try:
        stop = pm.get("stop") or pm.get("original_stop")
        if not stop or not entry:
            return "?"
        risk = abs(float(entry) - float(stop))
        if risk < 0.01:
            return "?"
        return f"{(pnl / max(size, 1)) / risk:.2f}R"
    except Exception:
        return "?"

# ── Table builder ──────────────────────────────────────────────────────────────

def _build_wtp_table(
    trades: list[dict],
    friday_closes: dict[str, float],
    weekly_closes: dict[str, list[float]],
    earnings_cache: dict,
) -> tuple[str, str, dict]:
    """
    Returns (full_table_md, slack_table_md, stats_dict).
    Full table: all 15 columns.
    Slack table: 8 key columns for Slack readability.
    """
    full_header = (
        "| Symbol | Dir | Entry$ | Exit$ | P&L | Exit Reason | Score | MRI | "
        "Earnings | TQI | Hold | R-Mult | Stage | Wkly Close | Δ Exit→Wkly |\n"
        "|--------|-----|--------|-------|-----|-------------|-------|-----|"
        "----------|-----|------|--------|-------|------------|-------------|\n"
    )
    slack_header = (
        "| Symbol | Dir | Entry$ | Exit$ | P&L | Exit Reason | Stage | Wkly Close | Δ Exit→Wkly |\n"
        "|--------|-----|--------|-------|-----|-------------|-------|------------|-------------|\n"
    )
    full_rows  = []
    slack_rows = []
    total_pnl  = 0.0
    agg_missed = 0.0

    for t in trades:
        sym    = t["symbol"]
        dir_   = t["direction"]
        entry  = t.get("entry_price")
        exit_  = t.get("exit_price")
        pnl    = t.get("pnl", 0.0) or 0.0
        score  = t.get("score", "?")
        mri    = t.get("mri_level", "?")
        size   = t.get("size", 1)

        pm     = _load_postmortem(sym, t["exit_date"])
        tqi    = pm.get("tqi_score", "?")
        earn   = _had_earnings_during_hold(sym, t["entry_time"], t["exit_time"], earnings_cache)

        wkly   = friday_closes.get(sym, None)
        wkly_s = f"${wkly:.2f}" if wkly is not None else "?"

        delta_s = "?"
        if isinstance(exit_, (int, float)) and wkly is not None:
            delta = (wkly - exit_) if dir_ == "long" else (exit_ - wkly)
            delta_s = (f"+${delta:.2f}" if delta >= 0 else f"-${abs(delta):.2f}")
            if delta > 0:
                agg_missed += delta * size

        stage  = _compute_weinstein_stage(weekly_closes.get(sym, []))
        entry_s = f"${entry:.2f}"  if isinstance(entry, (int, float)) else "?"
        exit_s  = f"${exit_:.2f}" if isinstance(exit_, (int, float)) else "?"
        pnl_s   = f"+${pnl:.2f}"  if pnl >= 0 else f"-${abs(pnl):.2f}"
        dir_s   = "▲" if dir_ == "long" else "▼"
        earn_s  = "⚠️" if earn else "—"
        hold_s  = _hold_hours(t["entry_time"], t["exit_time"])
        r_s     = _r_multiple(entry, pnl, size, pm)
        rsn_s   = _shorten_reason(t.get("exit_reason", ""))

        total_pnl += pnl
        t.update({
            "_stage": stage, "_wkly": wkly, "_delta_s": delta_s,
            "_earn": earn, "_tqi": tqi, "_hold": hold_s, "_r": r_s,
            "_pm": pm,
        })

        full_rows.append(
            f"| {sym} | {dir_s} | {entry_s} | {exit_s} | {pnl_s} | {rsn_s} | "
            f"{score} | {mri} | {earn_s} | {tqi} | {hold_s} | {r_s} | "
            f"{stage} | {wkly_s} | {delta_s} |"
        )
        slack_rows.append(
            f"| {sym} | {dir_s} | {entry_s} | {exit_s} | {pnl_s} | "
            f"{rsn_s} | {stage} | {wkly_s} | {delta_s} |"
        )

    stats = {
        "total_pnl":    round(total_pnl, 2),
        "count":        len(trades),
        "winners":      sum(1 for t in trades if (t.get("pnl") or 0) > 0),
        "losers":       sum(1 for t in trades if (t.get("pnl") or 0) < 0),
        "earnings_cnt": sum(1 for t in trades if t.get("_earn")),
        "agg_missed":   round(agg_missed, 2),
    }
    full_table  = full_header  + "\n".join(full_rows)
    slack_table = slack_header + "\n".join(slack_rows)
    return full_table, slack_table, stats

# ── Gemini ─────────────────────────────────────────────────────────────────────

def _build_gemini_prompt(week_str: str, full_table: str, trades: list[dict], stats: dict) -> str:
    details = "\n".join(
        f"  {t['symbol']} | {t['direction']} | entry={t.get('entry_price')} "
        f"exit={t.get('exit_price')} pnl={t.get('pnl')} size={t.get('size')} | "
        f"reason={t.get('exit_reason','')} | stage={t.get('_stage','')} | "
        f"earnings={t.get('_earn',False)} | mri={t.get('mri_level','')} | "
        f"score={t.get('score','')} | tqi={t.get('_tqi','')} | "
        f"hold={t.get('_hold','')} | wkly_close={t.get('_wkly','')} | Δ={t.get('_delta_s','')}"
        for t in trades
    )
    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    return f"""You are an adversarial performance auditor for a PDT-constrained paper-trading
algorithmic bot running on Alpaca Markets. The bot uses a 12-point confluence scoring
system with a 5-minute run cycle.

Week reviewed: {week_str}
Total P&L: {pnl_sign}${stats['total_pnl']} | Trades: {stats['count']} |
Winners: {stats['winners']} | Losers: {stats['losers']} |
Earnings-adjacent entries: {stats['earnings_cnt']} |
Aggregate missed directional move (Δ Exit→Wkly): ${stats['agg_missed']}

═══════════════════════════════════════════
WEEKLY TRADE POST-MORTEM (WTP) TABLE
═══════════════════════════════════════════
{full_table}

Column definitions:
  Entry$ / Exit$   — actual fill prices from Alpaca fills API (authoritative)
  P&L              — dollars for full position (all lots)
  Exit Reason      — bot's internal exit classification (shortened)
  Score            — 12-pt confluence score at entry (9+ = valid)
  MRI              — Macro Risk Index at entry (NORMAL/ELEVATED/STRESSED/HIGH/CRITICAL)
  Earnings ⚠️      — FMP earnings event occurred during hold period
  TQI              — Trade Quality Index 0-100 (target=35pts, trail=28, ATR buffer=10, hard_stop=0)
  Hold             — Duration from entry to exit
  R-Mult           — P&L per share / initial stop risk (positive = profit)
  Stage            — Weinstein Stage from 20-week SMA (2=advance, 3=top, 4=decline)
  Wkly Close       — Friday closing price (where stock ended the week)
  Δ Exit→Wkly      — Additional directional move available after exit
                     Positive = bot left money on the table; negative = exit saved from loss

═══════════════════════════════════════════
TRADE DETAIL (raw)
═══════════════════════════════════════════
{details}

═══════════════════════════════════════════
YOUR TASK — adversarial performance audit
═══════════════════════════════════════════
1. PATTERN ANALYSIS (3–5 bullets)
   Which exit mechanisms caused the most aggregate Δ this week?
   Are earnings-adjacent entries concentrated in a specific direction or stage?
   Any MRI/Score combinations that consistently underperformed?

2. SYSTEMIC FINDINGS (ranked by dollar impact, minimum 3)
   For each: [mechanism] → [observable failure] → [dollar cost this week]

3. EXECUTION QUALITY SCORE (1–10)
   Grade execution separately from signal quality. Two-sentence justification.

4. DS/GAI AUDIT PREP (paste-ready)
   Top 3 code changes with:
   [PRIORITY] File: function_name() — Change: description — Impact: $X est.

5. ONE-SENTENCE VERDICT
   Signal problem, execution problem, or both?

Be specific. Cite exact exit reasons and dollar amounts. Do not fabricate.
If a field shows '?' note it and continue."""


def _call_gemini(prompt: str) -> str:
    # gemini-2.5-flash needs max_output_tokens + thinking_budget=0, or "thinking" eats the
    # output budget and response.text comes back empty/truncated (project-documented Gemini
    # gotcha) — the silent cause of a blank/half "adversarial analysis" section here. The prior
    # fallback gemini-2.0-flash-lite is now 404/retired; gemini-3.1-flash-lite is the working
    # replacement (both verified live 2026-07-24 in weekly_review.py, the sibling fix #6/PR #8).
    try:
        from google import genai
        from google.genai import types as _gtypes
        client   = genai.Client(api_key=GEMINI_API_KEY)
        cfg      = _gtypes.GenerateContentConfig(
            max_output_tokens=8192,
            thinking_config=_gtypes.ThinkingConfig(thinking_budget=0),
        )
        last_err = None
        for model in [GEMINI_MODEL, "gemini-3.1-flash-lite"]:
            try:
                logger.info(f"  Trying model: {model}")
                r   = client.models.generate_content(model=model, contents=prompt, config=cfg)
                txt = (r.text or "").strip()
                if not txt:
                    # Empty text (thinking consumed the budget, or a truncated/blocked response)
                    # → treat as a failure so we fall through to the next model instead of
                    # writing an empty/"None" analysis section into the report.
                    raise ValueError("empty response.text (no content returned)")
                logger.info(f"  Success: {model}")
                return txt
            except Exception as e:
                last_err = e
                logger.warning(f"  {model} failed: {e}")
        return f"_(Gemini adversarial analysis unavailable this run — all models failed. Last error: {last_err})_"
    except ImportError:
        return "_(google-genai not installed — adversarial analysis skipped)_"
    except Exception as e:
        # Broad catch (mirrors weekly_review.py): a Client()/config-construction error (e.g. an SDK
        # build lacking ThinkingConfig, or an auth error) must degrade to a visible notice — NEVER
        # propagate out of _call_gemini and abort the whole report + Slack post in main().
        return f"_(Gemini adversarial analysis unavailable this run — client/setup error: {e})_"

# ── Slack ──────────────────────────────────────────────────────────────────────

def _post_slack(slack_table: str, stats: dict, report_path: Path, week_str: str) -> None:
    if not SLACK_WEBHOOK:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack.")
        return
    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    msg = (
        f":bar_chart: *Weekly Trade Post-Mortem — {week_str}*\n"
        f"P&L: `{pnl_sign}${stats['total_pnl']}` | "
        f"W/L: `{stats['winners']}/{stats['losers']}` | "
        f"Earnings-adjacent: `{stats['earnings_cnt']}` | "
        f"Agg. missed move: `${stats['agg_missed']}`\n"
        f"```\n{slack_table}\n```\n"
        f"_Full report (15-col table + Gemini analysis): `logs/{report_path.name}`_"
    )
    data = json.dumps({"text": msg}).encode()
    req  = urllib.request.Request(
        SLACK_WEBHOOK, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as r:
            logger.info(f"Slack: HTTP {r.status}")
    except Exception as e:
        logger.warning(f"Slack post failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    monday, friday = _get_week_range()
    week_str       = f"{monday.isoformat()} → {friday.isoformat()}"
    report_path    = LOGS_DIR / f"wtp_{friday.isoformat()}.md"

    # Saturday succeeded → Sunday skips
    if report_path.exists():
        logger.info(f"WTP already exists for {friday.isoformat()} — nothing to do.")
        return

    logger.info(f"=== Weekly Trade Post-Mortem: {week_str} ===")

    trades = _load_trades_for_week(monday, friday)
    if not trades:
        logger.warning(f"No closed trades found for {week_str}.")
        report_path.write_text(f"# WTP {week_str}\n\nNo trades closed this week.\n")
        return

    symbols = sorted({t["symbol"] for t in trades})
    logger.info(f"Symbols: {symbols}")

    logger.info("Fetching weekly bars (Alpaca T1)...")
    weekly_closes  = _fetch_weekly_closes(symbols)

    logger.info("Fetching Friday closes (Alpaca T1)...")
    friday_closes  = _fetch_friday_close(symbols, friday)

    logger.info("Loading earnings cache...")
    earnings_cache = _load_earnings_cache()

    logger.info("Building WTP table...")
    full_table, slack_table, stats = _build_wtp_table(
        trades, friday_closes, weekly_closes, earnings_cache
    )

    if GEMINI_API_KEY:
        logger.info("Calling Gemini for adversarial analysis...")
        prompt        = _build_gemini_prompt(week_str, full_table, trades, stats)
        gemini_report = _call_gemini(prompt)
    else:
        logger.warning("GEMINI_API_KEY not set — no Gemini analysis.")
        gemini_report = "(GEMINI_API_KEY not configured — skipped)"

    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    ts_pt    = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")

    report = "\n".join([
        f"# Weekly Trade Post-Mortem — {week_str}",
        f"_Generated {ts_pt} | {GEMINI_MODEL}_",
        "",
        "## Summary",
        f"- **Week P&L:** `{pnl_sign}${stats['total_pnl']}`",
        f"- **Trades:** {stats['count']} ({stats['winners']}W / {stats['losers']}L)",
        f"- **Earnings-adjacent entries:** {stats['earnings_cnt']}",
        f"- **Aggregate missed directional move (Δ Exit→Wkly):** `${stats['agg_missed']}`",
        "",
        "## Trade Overview (full)",
        full_table,
        "",
        "---",
        "## Gemini Adversarial Analysis",
        gemini_report,
        "",
        "---",
        "_Sources: trade\\_events.jsonl · Alpaca T1 bars · FMP earnings cache_",
    ])

    report_path.write_text(report, encoding="utf-8")
    logger.info(f"WTP written → {report_path}")

    _post_slack(slack_table, stats, report_path, week_str)
    logger.info("Done.")


if __name__ == "__main__":
    if not ALPACA_KEY:
        logger.error("ALPACA_API_KEY not set — aborting.")
        sys.exit(1)
    main()
